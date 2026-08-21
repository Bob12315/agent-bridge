from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.bridge.protocol import new_id, utc_now
from app.bridge.session import SessionContext
from app.storage.database import Database
from app.storage.models import EventRecord


class GitLifecycleError(RuntimeError):
    code = "GIT_ERROR"


class GitPreconditionFailed(GitLifecycleError):
    code = "PRECONDITION_FAILED"


class GitConflict(GitLifecycleError):
    code = "CONFLICT"


@dataclass(frozen=True, slots=True)
class GitPreflight:
    confirmation_id: str
    operation: str
    session_id: str
    expected_base_commit: str
    current_base_commit: str
    task_branch: str
    dirty: bool
    diff_stat: str


class GitLifecycleManager:
    """Explicit, auditable task-branch operations; never silently merge or delete."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def preflight(self, context: SessionContext, operation: str) -> GitPreflight:
        if operation not in {"apply", "discard"}:
            raise GitLifecycleError(f"unsupported Git operation: {operation}")
        repo = await asyncio.to_thread(self._repo_root, context.workspace)
        dirty = bool(await asyncio.to_thread(self._git, repo, "status", "--porcelain"))
        current_base = await asyncio.to_thread(self._git, repo, "rev-parse", context.base_branch)
        diff_stat = await asyncio.to_thread(self._git, repo, "diff", "--stat", context.base_branch)
        confirmation_id = new_id("gcf")
        detail = {
            "repo": str(repo), "branch": context.current_branch,
            "dirty": dirty, "diff_stat": diff_stat,
        }
        now = utc_now()
        await self._database.insert_git_operation(
            confirmation_id, context.id, operation, "preflight", json.dumps(detail),
            now.isoformat(), confirmation_id, context.base_commit,
        )
        await self._database.insert_event(EventRecord(
            id=new_id("evt"), session_id=context.id, type="GIT_PREFLIGHT", agent="system",
            message=f"Git {operation} preflight {confirmation_id}", created_at=now,
        ))
        return GitPreflight(
            confirmation_id=confirmation_id, operation=operation, session_id=context.id,
            expected_base_commit=context.base_commit or "", current_base_commit=current_base,
            task_branch=context.current_branch, dirty=dirty, diff_stat=diff_stat,
        )

    async def apply(self, context: SessionContext, confirmation_id: str, expected_base_commit: str) -> None:
        operation = await self._confirmed(context, "apply", confirmation_id, expected_base_commit)
        repo = await asyncio.to_thread(self._repo_root, context.workspace)
        current_base = await asyncio.to_thread(self._git, repo, "rev-parse", context.base_branch)
        if current_base != expected_base_commit:
            raise GitPreconditionFailed("base branch changed since task creation")
        if await asyncio.to_thread(self._git, repo, "status", "--porcelain"):
            raise GitPreconditionFailed("task worktree is dirty; commit or resolve changes first")
        # Merge is intentionally delegated to the repository's main worktree.
        main_repo = await asyncio.to_thread(self._main_worktree, repo, context.base_branch)
        if await asyncio.to_thread(self._git, main_repo, "status", "--porcelain"):
            raise GitPreconditionFailed("target worktree is dirty")
        try:
            await asyncio.to_thread(self._git, main_repo, "merge", "--no-ff", context.current_branch, "-m", f"Apply {context.id}")
        except GitLifecycleError as exc:
            raise GitConflict(str(exc)) from exc
        now = utc_now()
        await self._database.insert_git_operation(
            new_id("gop"), context.id, "apply", "completed", operation["detail_json"],
            now.isoformat(), confirmation_id, expected_base_commit,
        )
        await self._database.insert_event(EventRecord(
            id=new_id("evt"), session_id=context.id, type="GIT_APPLIED", agent="system",
            message=f"Applied {context.current_branch} after {confirmation_id}", created_at=now,
        ))

    async def discard(self, context: SessionContext, confirmation_id: str, expected_base_commit: str) -> None:
        operation = await self._confirmed(context, "discard", confirmation_id, expected_base_commit)
        if context.status not in {"completed", "archived", "error", "recovery"}:
            raise GitPreconditionFailed("task must be stopped before discard")
        repo = await asyncio.to_thread(self._repo_root, context.workspace)
        current_base = await asyncio.to_thread(self._git, repo, "rev-parse", context.base_branch)
        if current_base != expected_base_commit:
            raise GitPreconditionFailed("base branch changed since task creation")
        if await asyncio.to_thread(self._git, repo, "status", "--porcelain"):
            raise GitPreconditionFailed("task worktree is dirty; discard requires explicit cleanup first")
        main_repo = await asyncio.to_thread(self._main_worktree, repo, context.base_branch)
        await asyncio.to_thread(self._git, main_repo, "worktree", "remove", str(context.workspace))
        await asyncio.to_thread(self._git, main_repo, "branch", "-D", context.current_branch)
        now = utc_now()
        await self._database.insert_git_operation(
            new_id("gop"), context.id, "discard", "completed", operation["detail_json"],
            now.isoformat(), confirmation_id, expected_base_commit,
        )
        await self._database.insert_event(EventRecord(
            id=new_id("evt"), session_id=context.id, type="GIT_DISCARDED", agent="system",
            message=f"Discard approved for {context.current_branch} after {confirmation_id}", created_at=now,
        ))
        await self._database.update_session_status(context.id, "archived", now.isoformat())
        task = await self._database.get_task_for_session(context.id)
        if task is not None:
            await self._database.update_task_status(task.id, "archived", now.isoformat())

    async def _confirmed(
        self, context: SessionContext, operation: str, confirmation_id: str, expected_base: str,
    ) -> dict[str, object]:
        record = await self._database.get_git_operation(confirmation_id)
        if record is None or record["session_id"] != context.id or record["operation"] != operation:
            raise GitPreconditionFailed("confirmation_id is invalid for this operation")
        if record["status"] != "preflight" or record["expected_base_commit"] != expected_base:
            raise GitPreconditionFailed("confirmation preconditions do not match")
        return record

    @staticmethod
    def _repo_root(workspace: Path) -> Path:
        return Path(GitLifecycleManager._git(workspace, "rev-parse", "--show-toplevel"))

    @staticmethod
    def _main_worktree(repo: Path, branch: str) -> Path:
        # Git worktree list reports the owning checkout even when this is a task worktree.
        lines = GitLifecycleManager._git(repo, "worktree", "list", "--porcelain").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("worktree "):
                candidate = Path(line.removeprefix("worktree "))
                if candidate != repo and GitLifecycleManager._git(candidate, "branch", "--show-current") == branch:
                    return candidate
        raise GitLifecycleError(f"no writable worktree found for {branch}")

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        if result.returncode:
            raise GitLifecycleError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
        return result.stdout.strip()
