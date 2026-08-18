from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentName: TypeAlias = Literal["chatgpt", "deepseek", "codex", "system"]
MessageType: TypeAlias = Literal[
    "task",
    "result",
    "question",
    "answer",
    "review_request",
    "review_result",
    "error",
]
Verdict: TypeAlias = Literal["PASS", "CHANGES_REQUIRED"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class TestSummary(BaseModel):
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    command: str | None = None
    details: str | None = None


class ReviewIssue(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    problem: str
    required_change: str


class MessageContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    commit: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    tests: TestSummary | None = None
    issues: list[ReviewIssue] = Field(default_factory=list)
    blocking: bool | None = None
    verdict: Verdict | None = None


class MessageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    sender: AgentName
    receiver: AgentName
    type: MessageType
    task_id: str | None = None
    stage: int | None = Field(default=None, ge=1)
    round: int | None = Field(default=None, ge=1)
    reply_to: str | None = None
    content: MessageContent
    created_at: datetime

    @model_validator(mode="after")
    def validate_direction(self) -> "MessageEnvelope":
        if self.sender == self.receiver:
            raise ValueError("sender and receiver must differ")
        return self
