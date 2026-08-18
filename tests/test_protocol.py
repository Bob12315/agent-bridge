from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.bridge.protocol import (
    MessageContent,
    MessageEnvelope,
    ReviewIssue,
    TestSummary as ProtocolTestSummary,
    utc_now,
)


def make_message(**overrides: object) -> MessageEnvelope:
    values = {
        "id": "msg_1",
        "session_id": "ses_1",
        "sender": "chatgpt",
        "receiver": "deepseek",
        "type": "task",
        "content": MessageContent(text="Implement the task"),
        "created_at": utc_now(),
    }
    values.update(overrides)
    return MessageEnvelope.model_validate(values)


def test_valid_message_envelope() -> None:
    message = make_message(stage=1, round=2)
    assert message.schema_version == "1.0"
    assert message.content.constraints == []


@pytest.mark.parametrize("field,value", [("receiver", "unknown"), ("type", "complete"), ("stage", 0)])
def test_rejects_invalid_closed_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_message(**{field: value})


def test_rejects_same_sender_and_receiver() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        make_message(receiver="chatgpt")


def test_content_is_strict_and_structured() -> None:
    content = MessageContent(
        text="Review failed",
        verdict="CHANGES_REQUIRED",
        tests=ProtocolTestSummary(passed=4, failed=1),
        issues=[
            ReviewIssue(
                severity="high",
                file="app/main.py",
                line=4,
                problem="Incorrect result",
                required_change="Return the correct value",
            )
        ],
    )
    assert content.issues[0].severity == "high"
    with pytest.raises(ValidationError):
        MessageContent(text="x", arbitrary={"unsafe": True})
