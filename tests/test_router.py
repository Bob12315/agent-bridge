from __future__ import annotations

import pytest

from app.adapters.mock import MockAdapter
from app.bridge.protocol import MessageContent, MessageEnvelope, utc_now
from app.bridge.router import Router, RoutingError
from app.bridge.session import SessionContext
from app.storage.database import Database


def message(session_id: str, receiver: str = "deepseek") -> MessageEnvelope:
    return MessageEnvelope.model_validate(
        {
            "id": f"msg_{receiver}",
            "session_id": session_id,
            "sender": "chatgpt",
            "receiver": receiver,
            "type": "task" if receiver == "deepseek" else "review_request",
            "task_id": "task_1",
            "stage": 1,
            "round": 1,
            "content": MessageContent(text="Execute exactly one turn"),
            "created_at": utc_now(),
        }
    )


async def test_router_calls_only_selected_adapter_once(
    database: Database, session: SessionContext
) -> None:
    deepseek = MockAdapter("deepseek")
    codex = MockAdapter("codex")
    router = Router({"deepseek": deepseek, "codex": codex}, database)

    response = await router.route(message(session.id), session)

    assert response.content.text == "DEEPSEEK_MOCK_OK"
    assert response.reply_to == "msg_deepseek"
    assert deepseek.turn_count == 1
    assert codex.turn_count == 0
    assert await database.get_message(response.id) == response
    events = await database.list_events(session.id)
    assert events[-1].type == "MESSAGE_ROUTED"


async def test_router_can_route_explicit_codex_turn(
    database: Database, session: SessionContext
) -> None:
    codex = MockAdapter("codex")
    router = Router({"codex": codex}, database)
    response = await router.route(message(session.id, "codex"), session)
    assert response.content.verdict == "PASS"
    assert codex.turn_count == 1


async def test_router_rejects_unroutable_or_missing_adapters(
    database: Database, session: SessionContext
) -> None:
    router = Router({}, database)
    with pytest.raises(RoutingError, match="no adapter"):
        await router.route(message(session.id), session)
    with pytest.raises(RoutingError, match="not locally routable"):
        await router.route(message(session.id, "system"), session)


async def test_router_rejects_session_mismatch(
    database: Database, session: SessionContext
) -> None:
    router = Router({"deepseek": MockAdapter("deepseek")}, database)
    with pytest.raises(RoutingError, match="do not match"):
        await router.route(message("ses_other"), session)
