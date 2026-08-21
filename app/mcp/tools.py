from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from app.bridge.inspection import InspectionError, InspectionResult, InspectOperation, WorkspaceInspector
from app.bridge.policy import PolicyError
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
from app.bridge.projects import ProjectError, ProjectManager
from app.bridge.git_lifecycle import GitLifecycleError, GitLifecycleManager, GitPreflight
from app.bridge.recovery import RecoveryError, RecoveryManager
from app.bridge.router import RoutingError
from app.bridge.session import SessionContext
from app.bridge.session_manager import SessionError, SessionManager
from app.runtime.workspace import WorkspaceError
from app.storage.database import Database
from app.storage.models import ProjectRecord, TaskRecord

logger = logging.getLogger(__name__)
Receiver = Literal["deepseek", "codex"]
ExecutionMode = Literal["develop", "review"]


class CreateSessionToolResult(BaseModel):
    status: Literal["completed", "failed"]
    session_id: str | None = None
    project: str | None = None
    workspace: str | None = None
    access_mode: Literal["inspect", "develop", "review"] | None = None
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


class InspectToolResult(BaseModel):
    status: Literal["completed", "failed"]
    result: InspectionResult | None = None
    error: BridgeError | None = None


class ProjectToolResult(BaseModel):
    status: Literal["completed", "failed"]
    project: ProjectRecord | None = None
    projects: list[ProjectRecord] = Field(default_factory=list)
    error: BridgeError | None = None


class TaskToolResult(BaseModel):
    status: Literal["completed", "failed"]
    task: TaskRecord | None = None
    tasks: list[TaskRecord] = Field(default_factory=list)
    session: SessionContext | None = None
    error: BridgeError | None = None


class GitToolResult(BaseModel):
    status: Literal["completed", "failed"]
    preflight: dict[str, object] | None = None
    error: BridgeError | None = None


class BridgeToolService:
    """MCP operations with no workflow or automatic second-hop behavior."""

    def __init__(
        self,
        database: Database,
        sessions: SessionManager,
        requests: RequestManager,
        projects: ProjectManager | None = None,
        synchronous_wait_seconds: float = 30,
    ) -> None:
        self.database = database
        self.sessions = sessions
        self.requests = requests
        self.projects = projects or ProjectManager(database, sessions)
        self.git = GitLifecycleManager(database)
        self.recovery = RecoveryManager(database, self.projects)
        self.synchronous_wait_seconds = synchronous_wait_seconds
        self.inspector = WorkspaceInspector()

    async def bridge_create_session(
        self,
        project_name: str,
        repo_path: str,
        base_branch: str = "main",
        access_mode: Literal["inspect", "develop", "review"] = "inspect",
    ) -> CreateSessionToolResult:
        try:
            session = await self.sessions.create_session(
                project_name, Path(repo_path), base_branch, access_mode
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
                access_mode=session.access_mode,
                agents=agents,
            )
        except Exception as exc:
            return CreateSessionToolResult(status="failed", error=self._error(exc))

    async def bridge_add_project(
        self, name: str, repo_path: str, default_branch: str = "main"
    ) -> ProjectToolResult:
        try:
            return ProjectToolResult(
                status="completed",
                project=await self.projects.add_project(name, Path(repo_path), default_branch),
            )
        except Exception as exc:
            return ProjectToolResult(status="failed", error=self._error(exc))

    async def bridge_list_projects(self) -> ProjectToolResult:
        try:
            return ProjectToolResult(
                status="completed", projects=await self.database.list_projects()
            )
        except Exception as exc:
            return ProjectToolResult(status="failed", error=self._error(exc))

    async def bridge_get_project(self, project_id: str) -> ProjectToolResult:
        try:
            project = await self.database.get_project(project_id)
            if project is None:
                raise ProjectError(f"project not found: {project_id}")
            return ProjectToolResult(status="completed", project=project)
        except Exception as exc:
            return ProjectToolResult(status="failed", error=self._error(exc))

    async def bridge_create_task(
        self,
        project_id: str,
        task_name: str,
        access_mode: Literal["inspect", "develop", "review"] = "develop",
    ) -> TaskToolResult:
        try:
            task, session = await self.projects.create_task(project_id, task_name, access_mode)
            return TaskToolResult(status="completed", task=task, session=session)
        except Exception as exc:
            return TaskToolResult(status="failed", error=self._error(exc))

    async def bridge_list_tasks(self, project_id: str) -> TaskToolResult:
        try:
            return TaskToolResult(
                status="completed", tasks=await self.database.list_tasks(project_id)
            )
        except Exception as exc:
            return TaskToolResult(status="failed", error=self._error(exc))

    async def bridge_get_task(self, task_id: str) -> TaskToolResult:
        try:
            task = await self.database.get_task(task_id)
            if task is None:
                raise ProjectError(f"task not found: {task_id}")
            session = await self.database.get_session(task.bridge_session_id)
            return TaskToolResult(status="completed", task=task, session=session)
        except Exception as exc:
            return TaskToolResult(status="failed", error=self._error(exc))

    async def bridge_recover_task(self, task_id: str) -> TaskToolResult:
        try:
            task = await self.recovery.recover_task(task_id)
            session = await self.database.get_session(task.bridge_session_id)
            return TaskToolResult(status="completed", task=task, session=session)
        except Exception as exc:
            return TaskToolResult(status="failed", error=self._error(exc))

    async def bridge_transition_task(
        self,
        task_id: str,
        status: Literal["active", "completed", "archived", "error", "recovery"],
    ) -> TaskToolResult:
        try:
            task = await self.projects.transition_task(task_id, status)
            session = await self.database.get_session(task.bridge_session_id)
            return TaskToolResult(status="completed", task=task, session=session)
        except Exception as exc:
            return TaskToolResult(status="failed", error=self._error(exc))

    async def bridge_git_preflight(
        self, session_id: str, operation: Literal["apply", "discard"]
    ) -> GitToolResult:
        try:
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            preflight = await self.git.preflight(session, operation)
            return GitToolResult(status="completed", preflight=asdict(preflight))
        except Exception as exc:
            return GitToolResult(status="failed", error=self._error(exc))

    async def bridge_git_apply(
        self, session_id: str, confirmation_id: str, expected_base_commit: str
    ) -> GitToolResult:
        try:
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            await self.git.apply(session, confirmation_id, expected_base_commit)
            return GitToolResult(status="completed")
        except Exception as exc:
            return GitToolResult(status="failed", error=self._error(exc))

    async def bridge_git_discard(
        self, session_id: str, confirmation_id: str, expected_base_commit: str
    ) -> GitToolResult:
        try:
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            await self.git.discard(session, confirmation_id, expected_base_commit)
            return GitToolResult(status="completed")
        except Exception as exc:
            return GitToolResult(status="failed", error=self._error(exc))

    async def bridge_send(
        self,
        session_id: str,
        receiver: Receiver,
        type: MessageType,
        content: MessageContent,
        execution_mode: ExecutionMode,
        task_id: str | None = None,
        stage: Annotated[int | None, Field(ge=1)] = None,
        round: Annotated[int | None, Field(ge=1)] = None,
        reply_to: str | None = None,
        request_id: str | None = None,
    ) -> RequestToolResult:
        try:
            if receiver not in {"deepseek", "codex"}:
                raise ValueError(f"unsupported receiver: {receiver}")
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            if session.status != "active":
                raise PolicyError(f"session is not active: {session_id}")
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
            self._validate_execution_mode(message, execution_mode)
            result = await self.requests.send(
                message,
                session,
                synchronous_wait_seconds=self.synchronous_wait_seconds,
                request_id=request_id,
            )
            return self._request_result(result)
        except Exception as exc:
            return RequestToolResult(status="failed", error=self._error(exc))

    async def bridge_inspect(
        self,
        session_id: str,
        operation: InspectOperation,
        path: str | None = None,
        query: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> InspectToolResult:
        """Read-only inspection inside one session workspace; no arbitrary shell or network access."""
        try:
            session = await self.database.get_session(session_id)
            if session is None:
                raise SessionError(f"session not found: {session_id}")
            if session.status != "active":
                raise PolicyError(f"session is not active: {session_id}")
            result = await self.inspector.inspect(
                session,
                operation,
                path=path,
                query=query,
                limit=limit,
            )
            return InspectToolResult(status="completed", result=result)
        except Exception as exc:
            return InspectToolResult(status="failed", error=self._error(exc))

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
    def _validate_execution_mode(
        message: MessageEnvelope,
        execution_mode: ExecutionMode,
    ) -> None:
        expected = "develop" if message.receiver == "deepseek" else "review"
        if execution_mode != expected:
            raise PolicyError(
                f"{message.receiver} requires execution_mode='{expected}'"
            )

    @staticmethod
    def _error(exc: Exception) -> BridgeError:
        if isinstance(exc, ValidationError):
            code = "INVALID_ARGUMENT"
        elif isinstance(exc, PolicyError):
            code = "POLICY_DENIED"
        elif isinstance(exc, InspectionError):
            code = "INSPECTION_ERROR"
        elif isinstance(exc, WorkspaceError):
            code = "WORKSPACE_ERROR"
        elif isinstance(exc, SessionError):
            code = "SESSION_NOT_FOUND" if "not found" in str(exc) else "SESSION_ERROR"
        elif isinstance(exc, RequestManagerError):
            code = "REQUEST_NOT_FOUND" if "not found" in str(exc) else "REQUEST_ERROR"
        elif isinstance(exc, ProjectError):
            code = "PROJECT_NOT_FOUND" if "not found" in str(exc) else "PROJECT_ERROR"
        elif isinstance(exc, RecoveryError):
            code = "RECOVERY_ERROR"
        elif isinstance(exc, RoutingError):
            code = "ROUTING_ERROR"
        elif isinstance(exc, GitLifecycleError):
            code = exc.code
        elif isinstance(exc, ValueError):
            code = "INVALID_ARGUMENT"
        else:
            logger.exception("Unexpected MCP tool failure")
            return BridgeError(
                code="INTERNAL_ERROR",
                message="The bridge could not complete the operation.",
            )
        return BridgeError(code=code, message=str(exc))
