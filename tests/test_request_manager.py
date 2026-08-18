from __future__ import annotations

import pytest

from app.adapters.mock import MockAdapter, MockBehavior
from app.bridge.protocol import MessageContent, MessageEnvelope, new_id, utc_now
from app.bridge.request_manager import RequestManager, RequestManagerError
from app.bridge.router import Router
from app.bridge.session import SessionContext
from app.storage.database import Database


def task_message(session_id: str, suffix: str = "1") -> MessageEnvelope:
    return MessageEnvelope(
        id=f"msg_{suffix}_{new_id('part')}",
        session_id=session_id,
        sender="chatgpt",
        receiver="deepseek",
        type="task",
        task_id=f"task_{suffix}",
        stage=3,
        round=1,
        content=MessageContent(text=f"task {suffix}"),
        created_at=utc_now(),
    )


def manager(database: Database, adapter: MockAdapter) -> RequestManager:
    return RequestManager(database, Router({"deepseek": adapter}, database))


async def test_fast_path_completes_and_persists_response(
    database: Database, session: SessionContext
) -> None:
    requests = manager(database, MockAdapter("deepseek"))
    result = await requests.send(task_message(session.id), session, 1)

    assert result.status == "completed"
    assert result.response is not None
    assert result.response.content.text == "DEEPSEEK_MOCK_OK"
    stored = await database.get_request(result.request_id)
    assert stored is not None
    assert stored.response_message_id == result.response.id
    assert stored.started_at is not None
    assert stored.finished_at is not None
    agent_session = await database.get_agent_session(session.id, "deepseek")
    assert agent_session is not None and agent_session.status == "idle"
    assert [event.type for event in await database.list_events(session.id)] == [
        "MESSAGE_RECEIVED",
        "AGENT_STARTED",
        "MESSAGE_ROUTED",
        "AGENT_FINISHED",
    ]


async def test_synchronous_timeout_returns_running_then_wait_completes(
    database: Database, session: SessionContext
) -> None:
    requests = manager(
        database, MockAdapter("deepseek", MockBehavior(delay_seconds=0.15))
    )
    initial = await requests.send(task_message(session.id), session, 0.01)
    assert initial.status == "running"
    assert initial.response is None

    still_running = await requests.wait(initial.request_id, 0.01)
    assert still_running.status == "running"
    completed = await requests.wait(initial.request_id, 1)
    assert completed.status == "completed"

    restored = RequestManager(
        database, Router({"deepseek": MockAdapter("deepseek")}, database)
    )
    assert (await restored.status(initial.request_id)).response == completed.response


async def test_failure_is_structured_and_updates_agent(
    database: Database, session: SessionContext
) -> None:
    requests = manager(
        database, MockAdapter("deepseek", MockBehavior(mode="error"))
    )
    result = await requests.send(task_message(session.id), session, 1)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "AGENT_ERROR"
    assert "simulated deepseek error" in result.error.message
    agent_session = await database.get_agent_session(session.id, "deepseek")
    assert agent_session is not None and agent_session.status == "error"
    assert (await database.list_events(session.id))[-1].type == "AGENT_FAILED"


async def test_agent_execution_timeout_is_failed_not_running(
    database: Database, session: SessionContext
) -> None:
    adapter = MockAdapter("deepseek", MockBehavior(delay_seconds=30))
    requests = RequestManager(
        database,
        Router({"deepseek": adapter}, database),
        agent_timeout_seconds=0.03,
    )
    result = await requests.send(task_message(session.id), session, 1)
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "AGENT_TIMEOUT"
    assert "did not respond" in result.error.message


async def test_cancel_running_request(
    database: Database, session: SessionContext
) -> None:
    adapter = MockAdapter("deepseek", MockBehavior(delay_seconds=30))
    requests = manager(database, adapter)
    initial = await requests.send(task_message(session.id), session, 0.02)
    assert initial.status == "running"

    cancelled = await requests.cancel(initial.request_id)
    assert cancelled.status == "cancelled"
    assert cancelled.error is not None
    assert cancelled.error.code == "REQUEST_CANCELLED"
    agent_session = await database.get_agent_session(session.id, "deepseek")
    assert agent_session is not None and agent_session.status == "idle"
    assert (await database.list_events(session.id))[-1].type == "REQUEST_CANCELLED"


async def test_cancel_queued_request_does_not_cancel_active_turn(
    database: Database, session: SessionContext
) -> None:
    adapter = MockAdapter("deepseek", MockBehavior(delay_seconds=0.15))
    requests = manager(database, adapter)
    first = await requests.send(task_message(session.id, "first"), session, 0.01)
    second = await requests.send(task_message(session.id, "second"), session, 0)

    assert first.status == "running"
    assert second.status == "running"
    assert (await requests.cancel(second.request_id)).status == "cancelled"
    assert (await requests.wait(first.request_id, 1)).status == "completed"
    assert adapter.turn_count == 1


async def test_validation_and_terminal_cancel_are_idempotent(
    database: Database, session: SessionContext
) -> None:
    requests = manager(database, MockAdapter("deepseek"))
    with pytest.raises(RequestManagerError, match="not found"):
        await requests.status("req_missing")
    with pytest.raises(RequestManagerError, match="negative"):
        await requests.send(task_message(session.id), session, -1)
    with pytest.raises(RequestManagerError, match="negative"):
        await requests.wait("req_missing", -1)

    completed = await requests.send(task_message(session.id), session, 1)
    assert (await requests.cancel(completed.request_id)).status == "completed"

    closed = session.model_copy(update={"status": "closed"})
    with pytest.raises(RequestManagerError, match="not active"):
        await requests.send(task_message(session.id), closed, 0)
    with pytest.raises(ValueError, match="greater than zero"):
        RequestManager(database, Router({}, database), agent_timeout_seconds=0)
