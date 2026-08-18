from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.bridge.protocol import AgentName, utc_now

RequestStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
EventType = Literal[
    "SESSION_CREATED",
    "MESSAGE_RECEIVED",
    "MESSAGE_ROUTED",
    "AGENT_STARTED",
    "AGENT_PROGRESS",
    "AGENT_FINISHED",
    "AGENT_FAILED",
    "REQUEST_CANCELLED",
    "SESSION_CLOSED",
]


class RequestRecord(BaseModel):
    id: str
    message_id: str
    session_id: str
    agent: AgentName
    status: RequestStatus = "queued"
    queued_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    response_message_id: str | None = None
    error_code: str | None = None
    error: str | None = None


class EventRecord(BaseModel):
    id: str
    session_id: str
    request_id: str | None = None
    agent: AgentName | None = None
    type: EventType
    message: str
    created_at: datetime = Field(default_factory=utc_now)
