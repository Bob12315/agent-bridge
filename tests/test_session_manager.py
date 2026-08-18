from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from app.bridge.session_manager import SessionError, SessionManager
from app.runtime.workspace import WorkspaceManager
from app.storage.database import Database


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


async def test_session_lifecycle_uses_isolated_worktree(
    database: Database, git_repository: Path, tmp_path: Path
) -> None:
    workspaces = WorkspaceManager(tmp_path / "runtime" / "workspaces")
    manager = SessionManager(database, workspaces)

    session = await manager.create_session("demo", git_repository, "main")

    assert session.workspace.is_dir()
    assert session.base_commit == git(git_repository, "rev-parse", "HEAD")
    assert git(session.workspace, "branch", "--show-current") == session.current_branch
    stored = await database.get_session(session.id)
    assert stored == session
    agent_sessions = await database.list_agent_sessions(session.id)
    assert {item.agent for item in agent_sessions} == {"deepseek", "codex"}
    assert all(item.status == "idle" for item in agent_sessions)
    assert (await database.list_events(session.id))[0].type == "SESSION_CREATED"

    closed = await manager.close_session(session.id)

    assert closed.status == "closed"
    assert not session.workspace.parent.exists()
    assert git(
        git_repository,
        "show-ref",
        "--verify",
        f"refs/heads/{session.current_branch}",
    )
    assert all(
        item.status == "closed"
        for item in await database.list_agent_sessions(session.id)
    )
    assert [event.type for event in await database.list_events(session.id)] == [
        "SESSION_CREATED",
        "SESSION_CLOSED",
    ]
    assert (await manager.close_session(session.id)).status == "closed"


async def test_unknown_session_cannot_be_closed(
    database: Database, tmp_path: Path
) -> None:
    manager = SessionManager(database, WorkspaceManager(tmp_path / "workspaces"))
    with pytest.raises(SessionError, match="not found"):
        await manager.close_session("ses_missing")


async def test_invalid_project_rolls_back_created_worktree(
    database: Database, git_repository: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    manager = SessionManager(database, WorkspaceManager(root))
    with pytest.raises(SessionError, match="project name"):
        await manager.create_session("", git_repository)
    assert not root.exists() or not any(root.iterdir())
