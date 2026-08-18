from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from pydantic import BaseModel


class WorkspaceError(RuntimeError):
    """A safe, user-facing Git workspace operation failed."""


class RepositoryInfo(BaseModel):
    root: Path
    base_branch: str
    base_commit: str


class WorkspaceInfo(BaseModel):
    path: Path
    branch: str
    repository: RepositoryInfo


class WorkspaceManager:
    """Create and remove session-scoped Git worktrees without a shell."""

    def __init__(self, workspace_root: Path, git_executable: str | None = None) -> None:
        git = git_executable or shutil.which("git")
        if not git:
            raise WorkspaceError("Git executable was not found on PATH")
        self.git_executable = git
        self.workspace_root = workspace_root.resolve()

    async def validate_repository(
        self, repo_path: Path, base_branch: str
    ) -> RepositoryInfo:
        if not base_branch.strip():
            raise WorkspaceError("base branch must not be empty")
        try:
            requested_path = repo_path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise WorkspaceError(f"repository path does not exist: {repo_path}") from exc
        if not requested_path.is_dir():
            raise WorkspaceError(f"repository path is not a directory: {repo_path}")

        root_text = await self._git(
            "-C", str(requested_path), "rev-parse", "--show-toplevel"
        )
        root = Path(root_text).resolve()
        is_bare = await self._git(
            "-C", str(root), "rev-parse", "--is-bare-repository"
        )
        if is_bare != "false":
            raise WorkspaceError("bare repositories cannot be used as session sources")

        await self._git("check-ref-format", "--branch", base_branch)
        base_commit = await self._git(
            "-C", str(root), "rev-parse", "--verify", f"{base_branch}^{{commit}}"
        )
        return RepositoryInfo(root=root, base_branch=base_branch, base_commit=base_commit)

    async def create(
        self, session_id: str, repo_path: Path, base_branch: str, branch: str
    ) -> WorkspaceInfo:
        repository = await self.validate_repository(repo_path, base_branch)
        session_dir = self._session_directory(session_id)
        workspace = session_dir / "repo"
        if session_dir.exists():
            raise WorkspaceError(f"workspace already exists for session {session_id}")

        session_dir.mkdir(parents=True)
        try:
            await self._git(
                "-C",
                str(repository.root),
                "worktree",
                "add",
                "-b",
                branch,
                str(workspace),
                repository.base_commit,
            )
        except BaseException:
            self._remove_empty_session_directory(session_dir)
            raise
        return WorkspaceInfo(path=workspace, branch=branch, repository=repository)

    async def remove(self, workspace: Path) -> None:
        resolved = workspace.resolve(strict=True)
        session_dir = resolved.parent
        self._validate_managed_workspace(resolved)
        common_git_directory = await self._git(
            "-C",
            str(resolved),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
        await self._git(
            "--git-dir",
            common_git_directory,
            "worktree",
            "remove",
            "--force",
            str(resolved),
        )
        self._remove_empty_session_directory(session_dir)

    def _session_directory(self, session_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id) is None:
            raise WorkspaceError("invalid session ID for workspace path")
        directory = (self.workspace_root / session_id).resolve()
        if directory.parent != self.workspace_root:
            raise WorkspaceError("session workspace escapes the configured root")
        return directory

    def _validate_managed_workspace(self, workspace: Path) -> None:
        if workspace.name != "repo" or workspace.parent.parent != self.workspace_root:
            raise WorkspaceError("refusing to remove a path outside the workspace root")

    @staticmethod
    def _remove_empty_session_directory(session_dir: Path) -> None:
        try:
            session_dir.rmdir()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorkspaceError(
                f"session workspace directory is not empty: {session_dir}"
            ) from exc

    async def _git(self, *arguments: str) -> str:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        process = await asyncio.create_subprocess_exec(
            self.git_executable,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode(errors="replace").strip()
        error = stderr.decode(errors="replace").strip()
        if process.returncode != 0:
            detail = error or output or f"Git exited with code {process.returncode}"
            raise WorkspaceError(detail)
        return output
