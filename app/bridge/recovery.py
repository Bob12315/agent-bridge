from __future__ import annotations

from app.bridge.projects import ProjectManager
from app.bridge.protocol import new_id, utc_now
from app.storage.database import Database
from app.storage.models import EventRecord, TaskRecord


class RecoveryError(RuntimeError):
    pass


class RecoveryManager:
    """Conservative recovery: validate state, surface degradation, never recreate silently."""

    def __init__(self, database: Database, projects: ProjectManager) -> None:
        self._database = database
        self._projects = projects

    async def recover_task(self, task_id: str) -> TaskRecord:
        task = await self._database.get_task(task_id)
        if task is None:
            raise RecoveryError(f"task not found: {task_id}")
        session = await self._database.get_session(task.bridge_session_id)
        if session is None:
            raise RecoveryError(f"task session not found: {task.bridge_session_id}")
        if not session.workspace.exists():
            await self._projects.transition_task(task.id, "recovery")
            raise RecoveryError("worktree is missing; manual recovery is required")
        for agent in await self._database.list_agent_sessions(session.id):
            if agent.external_session_id is None:
                continue
            await self._database.update_agent_session_status(session.id, agent.agent, "resuming")
        if task.status in {"error", "recovery"}:
            task = await self._projects.transition_task(task.id, "active")
        await self._database.insert_event(EventRecord(
            id=new_id("evt"), session_id=session.id, type="AGENT_SESSION_RESUMED", agent="system",
            message="Recovery validation completed; persisted adapter sessions will resume lazily.",
            created_at=utc_now(),
        ))
        return task
