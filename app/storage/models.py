from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.bridge.protocol import AgentName, utc_now

TaskStatus = Literal["draft", "active", "completed", "archived", "error", "recovery"]
AgentSessionStatus = Literal[
    "unbound", "creating", "ready", "resuming", "busy", "degraded", "replaced", "error", "closed"
]
RequestStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "timeout"
]
EventType = Literal[
    "SESSION_CREATED",
    "MESSAGE_RECEIVED",
    "MESSAGE_ROUTED",
    "AGENT_STARTED",
    "AGENT_PROGRESS",
    "AGENT_FINISHED",
    "AGENT_FAILED",
    "POLICY_VIOLATION",
    "REQUEST_CANCELLED",
    "SESSION_CLOSED",
    "PROJECT_CREATED",
    "TASK_CREATED",
    "TASK_STATE_CHANGED",
    "AGENT_SESSION_RESUMED",
    "SESSION_RESUME_FAILED",
    "GIT_PREFLIGHT",
    "GIT_APPLIED",
    "GIT_DISCARDED",
]


class ProjectRecord(BaseModel):
    id: str
    name: str = Field(min_length=1)
    repo_path: str
    default_branch: str = "main"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TaskRecord(BaseModel):
    id: str
    project_id: str
    task_name: str = Field(min_length=1)
    bridge_session_id: str
    status: TaskStatus = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    event_seq: int = 0


class EventRecord(BaseModel):
    id: str
    session_id: str
    request_id: str | None = None
    agent: AgentName | None = None
    type: EventType
    message: str
    created_at: datetime = Field(default_factory=utc_now)
    event_seq: int | None = None
