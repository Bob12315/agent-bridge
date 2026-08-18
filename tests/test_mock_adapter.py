from __future__ import annotations

import asyncio

import pytest

from app.adapters.mock import MockAdapter, MockBehavior
from app.bridge.protocol import MessageContent, MessageEnvelope, utc_now
from app.bridge.session import SessionContext


def request(context: SessionContext, receiver: str = "deepseek") -> MessageEnvelope:
    return MessageEnvelope.model_validate(
        {
            "id": "msg_request",
            "session_id": context.id,
            "sender": "chatgpt",
            "receiver": receiver,
            "type": "review_request" if receiver == "codex" else "task",
            "content": MessageContent(text="test"),
            "created_at": utc_now(),
        }
    )


async def test_deepseek_success(session: SessionContext) -> None:
    adapter = MockAdapter("deepseek")
    result = await adapter.send(request(session), session)
    assert result.response.content.text == "DEEPSEEK_MOCK_OK"
    assert result.response.type == "result"


@pytest.mark.parametrize("mode,expected", [("success", "PASS"), ("changes_required", "CHANGES_REQUIRED")])
async def test_codex_verdicts(session: SessionContext, mode: str, expected: str) -> None:
    adapter = MockAdapter("codex", MockBehavior(mode=mode))
    result = await adapter.send(request(session, "codex"), session)
    assert result.response.content.verdict == expected


async def test_mock_question_and_error(session: SessionContext) -> None:
    question = MockAdapter("deepseek", MockBehavior(mode="question"))
    result = await question.send(request(session), session)
    assert result.response.type == "question"
    assert result.response.content.blocking is True

    failing = MockAdapter("deepseek", MockBehavior(mode="error"))
    with pytest.raises(RuntimeError, match="simulated"):
        await failing.send(request(session), session)


async def test_mock_delay_and_cancel(session: SessionContext) -> None:
    adapter = MockAdapter("deepseek", MockBehavior(delay_seconds=30))
    turn = asyncio.create_task(adapter.send(request(session), session))
    await asyncio.sleep(0)
    await adapter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    await adapter.close()
    assert (await adapter.health()).status == "healthy"
    assert await adapter.start(session) == f"mock-deepseek-{session.id}"
