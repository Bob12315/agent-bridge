from __future__ import annotations

from pathlib import Path

from app.bridge.protocol import new_id, utc_now
from app.bridge.session import AccessMode, SessionContext
from app.bridge.session_manager import SessionManager
from app.storage.database import Database
from app.storage.models import EventRecord, ProjectRecord, TaskRecord, TaskStatus


class ProjectError(RuntimeError):
    pass


_TASK_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "archived", "error"},
    "active": {"completed", "archived", "error", "recovery"},
    "recovery": {"active", "error", "archived"},
    "error": {"recovery", "archived"},
    "completed": {"archived"},
    "archived": set(),
}


class ProjectManager:
    """V2 Project -> Task facade over the existing isolated session workspace."""

    def __init__(self, database: Database, sessions: SessionManager) -> None:
        self._database = database
        self._sessions = sessions

    async def add_project(
        self, name: str, repo_path: Path, default_branch: str = "main"
    ) -> ProjectRecord:
        if not name.strip():
            raise ProjectError("project name must not be empty")
        resolved = repo_path.resolve()
        if not (resolved / ".git").exists():
            raise ProjectError("project repo_path must point to a Git repository")
        now = utc_now()
        project = await self._database.upsert_project(
            ProjectRecord(
                id=new_id("prj"),
                name=name.strip(),
                repo_path=str(resolved),
                default_branch=default_branch,
                created_at=now,
                updated_at=now,
            )
        )
        return project

    async def create_task(
        self,
        project_id: str,
        task_name: str,
        access_mode: AccessMode = "develop",
    ) -> tuple[TaskRecord, SessionContext]:
        project = await self._database.get_project(project_id)
        if project is None:
            raise ProjectError(f"project not found: {project_id}")
        if not task_name.strip():
            raise ProjectError("task name must not be empty")
        session = await self._sessions.create_session(
            project.name, Path(project.repo_path), project.default_branch, access_mode
        )
        # Sessions are created before the Task so failed task inserts leave a
        # recoverable workspace rather than a partially deleted worktree.
        session.project_id = project.id
        session.task_name = task_name.strip()
        await self._database.set_session_v2_metadata(
            session.id, project.id, session.task_name, utc_now().isoformat()
        )
        now = utc_now()
        task = TaskRecord(
            id=new_id("tsk"),
            project_id=project.id,
            task_name=session.task_name,
            bridge_session_id=session.id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        await self._database.insert_task(task)
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=session.id,
                type="TASK_CREATED",
                agent="system",
                message=f"Created task {task.id} for project {project.id}",
            )
        )
        return task, session

    async def transition_task(self, task_id: str, target: TaskStatus) -> TaskRecord:
        task = await self._database.get_task(task_id)
        if task is None:
            raise ProjectError(f"task not found: {task_id}")
        if target == task.status:
            return task
        if target not in _TASK_TRANSITIONS[task.status]:
            raise ProjectError(f"invalid task transition: {task.status} -> {target}")
        now = utc_now()
        await self._database.update_task_status(task.id, target, now.isoformat())
        await self._database.update_session_status(task.bridge_session_id, target, now.isoformat())
        await self._database.insert_event(
            EventRecord(
                id=new_id("evt"),
                session_id=task.bridge_session_id,
                type="TASK_STATE_CHANGED",
                agent="system",
                message=f"Task {task.id}: {task.status} -> {target}",
                created_at=now,
            )
        )
        changed = await self._database.get_task(task.id)
        assert changed is not None
        return changed
