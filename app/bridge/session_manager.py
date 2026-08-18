from __future__ import annotations

from pathlib import Path

from app.bridge.protocol import new_id, utc_now
from app.bridge.session import AccessMode, AgentSession, SessionContext
from app.runtime.workspace import WorkspaceManager
from app.storage.database import Database
from app.storage.models import EventRecord


class SessionError(RuntimeError):
    pass


class SessionManager:
    def __init__(self, database: Database, workspaces: WorkspaceManager) -> None:
        self._database = database
        self._workspaces = workspaces

    async def create_session(
        self,
        project_name: str,
        repo_path: Path,
        base_branch: str = "main",
        access_mode: AccessMode = "inspect",
    ) -> SessionContext:
        if not project_name.strip():
            raise SessionError("project name must not be empty")
        session_id = new_id("ses")
        branch = f"agent-bridge/{session_id}"
        workspace = await self._workspaces.create(
            session_id=session_id,
            repo_path=repo_path,
            base_branch=base_branch,
            branch=branch,
        )
        try:
            context = SessionContext(
                id=session_id,
                project_name=project_name,
                workspace=workspace.path,
                base_branch=base_branch,
                current_branch=branch,
                base_commit=workspace.repository.base_commit,
                access_mode=access_mode,
            )
            agent_sessions = [
                AgentSession(
                    id=new_id("ags"),
                    bridge_session_id=session_id,
                    agent=agent,
                )
                for agent in ("deepseek", "codex")
            ]
            event = EventRecord(
                id=new_id("evt"),
                session_id=session_id,
                agent="system",
                type="SESSION_CREATED",
                message=f"Created session workspace on {branch}",
            )
            await self._database.insert_session_bundle(context, agent_sessions, event)
        except BaseException:
            await self._workspaces.remove(workspace.path)
            raise
        return context

    async def close_session(self, session_id: str) -> SessionContext:
        context = await self._database.get_session(session_id)
        if context is None:
            raise SessionError(f"session not found: {session_id}")
        if context.status == "closed":
            return context

        try:
            await self._workspaces.remove(context.workspace)
        except BaseException:
            now = utc_now()
            await self._database.update_session_status(session_id, "error", now.isoformat())
            raise

        now = utc_now()
        event = EventRecord(
            id=new_id("evt"),
            session_id=session_id,
            agent="system",
            type="SESSION_CLOSED",
            message="Session workspace removed",
            created_at=now,
        )
        await self._database.close_session_records(session_id, now.isoformat(), event)
        closed = await self._database.get_session(session_id)
        if closed is None:
            raise SessionError(f"session disappeared while closing: {session_id}")
        return closed
