from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from app.bridge.protocol import (
    MessageContent,
    MessageEnvelope,
    MessageType,
    new_id,
    utc_now,
)
from app.bridge.request_manager import (
    BridgeError,
    BridgeRequestResult,
    PublicRequestStatus,
    RequestManager,
    RequestManagerError,
)
from app.bridge.router import RoutingError
from app.bridge.session_manager import SessionError, SessionManager
from app.runtime.workspace import WorkspaceError
from app.storage.database import Database

logger = logging.getLogger(__name__)
Receiver = Literal["deepseek", "codex"]


class CreateSessionToolResult(BaseModel):
    status: Literal["completed", "failed"]
    session_id: str | None = None
    project: str | None = None
    workspace: str | None = None
    agents: dict[str, str] | None = None
    error: BridgeError | None = None


class RequestToolResult(BaseModel):
    status: PublicRequestStatus
    request_id: str | None = None
    response: MessageEnvelope | None = None
    error: BridgeError | None = None


class CloseSessionToolResult(BaseModel):
    status: Literal["completed", "failed"]
    session_id: str | None = None
    workspace_removed: bool | None = None
    error: BridgeError | None = None


class BridgeToolService:
    """MCP operations with no workflow or automatic second-hop behavior."""

    def __init__(
        self,
        database: Database,
        sessions: SessionManager,
        requests: RequestManager,
        synchronous_wait_seconds: float = 30,
    ) -> None:
        self.database = database
        self.sessions = sessions
        self.requests = requests
        self.synchronous_wait_seconds = synchronous_wait_seconds

    async def bridge_create_session(
        self,
        project_name: str,
        repo_path: str,
        base_branch: str = "main",
    ) -> CreateSessionToolResult:
        try:
            session = await self.sessions.create_session(
                project_name, Path(repo_path), base_branch
            )
            agent_sessions = await self.database.list_agent_sessions(session.id)
            agents = {
                item.agent: "ready" if item.status == "idle" else item.status
                for item in agent_sessions
            }
            return CreateSessionToolResult(
                status="completed",
                session_id=session.id,
                project=session.project_name,
                workspace=str(session.workspace),
                agents=agents,
            )
        except Exception as exc:
            return CreateSessionToolResult(status="failed", error=self._error(exc))

    async def bridge_send(
        self,
        session_id: str,
        receiver: Receiver,
        type: MessageType,
        content: MessageContent,
        task_id: str | None = None,
        stage: Annotated[int | None, Field(ge=1)] = None,
        round: Annotated[int | None, Field(ge=1)] = None,
        reply_to: str | None = None,
    ) -> RequestToolResult:
        try:
            if receiver not in {"deepseek", "codex"}:
                raise ValueError(f"unsupported receiver: {receiver}")
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            message = MessageEnvelope(
                id=new_id("msg"),
                session_id=session_id,
                sender="chatgpt",
                receiver=receiver,
                type=type,
                task_id=task_id,
                stage=stage,
                round=round,
                reply_to=reply_to,
                content=content,
                created_at=utc_now(),
            )
            result = await self.requests.send(
                message,
                session,
                synchronous_wait_seconds=self.synchronous_wait_seconds,
            )
            return self._request_result(result)
        except Exception as exc:
            return RequestToolResult(status="failed", error=self._error(exc))

    async def bridge_wait(
        self,
        request_id: str,
        timeout: Annotated[float, Field(ge=0)] = 30,
    ) -> RequestToolResult:
        try:
            return self._request_result(await self.requests.wait(request_id, timeout))
        except Exception as exc:
            return RequestToolResult(status="failed", error=self._error(exc))

    async def bridge_status(self, request_id: str) -> RequestToolResult:
        try:
            return self._request_result(await self.requests.status(request_id))
        except Exception as exc:
            return RequestToolResult(status="failed", error=self._error(exc))

    async def bridge_cancel(self, request_id: str) -> RequestToolResult:
        try:
            return self._request_result(await self.requests.cancel(request_id))
        except Exception as exc:
            return RequestToolResult(status="failed", error=self._error(exc))

    async def bridge_close_session(self, session_id: str) -> CloseSessionToolResult:
        try:
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            active = [
                request
                for request in await self.database.list_requests(session_id)
                if request.status in {"queued", "running"}
            ]
            if active:
                raise SessionError(
                    "session has active requests; cancel or wait for them before closing"
                )
            was_open = session.status != "closed"
            closed = await self.sessions.close_session(session_id)
            return CloseSessionToolResult(
                status="completed",
                session_id=closed.id,
                workspace_removed=was_open,
            )
        except Exception as exc:
            return CloseSessionToolResult(status="failed", error=self._error(exc))

    @staticmethod
    def _request_result(result: BridgeRequestResult) -> RequestToolResult:
        return RequestToolResult.model_validate(result.model_dump())

    @staticmethod
    def _error(exc: Exception) -> BridgeError:
        if isinstance(exc, ValidationError):
            code = "INVALID_ARGUMENT"
        elif isinstance(exc, WorkspaceError):
            code = "WORKSPACE_ERROR"
        elif isinstance(exc, SessionError):
            code = "SESSION_NOT_FOUND" if "not found" in str(exc) else "SESSION_ERROR"
        elif isinstance(exc, RequestManagerError):
            code = "REQUEST_NOT_FOUND" if "not found" in str(exc) else "REQUEST_ERROR"
        elif isinstance(exc, RoutingError):
            code = "ROUTING_ERROR"
        elif isinstance(exc, ValueError):
            code = "INVALID_ARGUMENT"
        else:
            logger.exception("Unexpected MCP tool failure")
            return BridgeError(
                code="INTERNAL_ERROR",
                message="The bridge could not complete the operation.",
            )
        return BridgeError(code=code, message=str(exc))
