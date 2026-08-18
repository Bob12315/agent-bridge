from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.bridge.protocol import AgentName, utc_now

AccessMode = Literal["inspect", "develop", "review"]


class SessionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_name: str = Field(min_length=1)
    workspace: Path
    base_branch: str
    current_branch: str
    base_commit: str | None = None
    access_mode: AccessMode = "inspect"
    status: Literal["active", "closed", "error"] = "active"
    current_task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    bridge_session_id: str
    agent: AgentName
    external_session_id: str | None = None
    status: Literal["idle", "busy", "error", "closed"] = "idle"
