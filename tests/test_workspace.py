from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.runtime.workspace import WorkspaceError, WorkspaceManager


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


async def test_validate_repository_accepts_subdirectory_and_records_commit(
    tmp_path: Path, git_repository: Path
) -> None:
    nested = git_repository / "nested"
    nested.mkdir()
    manager = WorkspaceManager(tmp_path / "workspaces")
    info = await manager.validate_repository(nested, "main")
    assert info.root == git_repository.resolve()
    assert info.base_commit == git(git_repository, "rev-parse", "HEAD")


async def test_create_and_remove_worktree(
    tmp_path: Path, git_repository: Path
) -> None:
    root = tmp_path / "runtime with spaces" / "workspaces"
    manager = WorkspaceManager(root)
    info = await manager.create(
        session_id="ses_1",
        repo_path=git_repository,
        base_branch="main",
        branch="agent-bridge/ses_1",
    )
    assert info.path.is_dir()
    assert git(info.path, "branch", "--show-current") == "agent-bridge/ses_1"
    assert git(info.path, "rev-parse", "HEAD") == info.repository.base_commit

    (info.path / "worktree-only.txt").write_text("isolated\n", encoding="utf-8")
    assert not (git_repository / "worktree-only.txt").exists()

    await manager.remove(info.path)
    assert not info.path.exists()
    assert not info.path.parent.exists()
    assert "ses_1" not in git(git_repository, "worktree", "list", "--porcelain")


async def test_rejects_non_repository_invalid_branch_and_unsafe_removal(
    tmp_path: Path, git_repository: Path
) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    with pytest.raises(WorkspaceError):
        await manager.validate_repository(ordinary, "main")
    with pytest.raises(WorkspaceError):
        await manager.validate_repository(git_repository, "missing")

    outside = tmp_path / "outside" / "repo"
    outside.mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="outside"):
        await manager.remove(outside)


def test_requires_git_and_safe_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path)
    monkeypatch.setattr("app.runtime.workspace.shutil.which", lambda _: None)
    with pytest.raises(WorkspaceError, match="not found"):
        WorkspaceManager(tmp_path / "other")
    with pytest.raises(WorkspaceError, match="invalid session"):
        manager._session_directory("../escape")
    with pytest.raises(WorkspaceError, match="invalid session"):
        manager._session_directory("..\\escape")
