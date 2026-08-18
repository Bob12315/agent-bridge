from __future__ import annotations

from pathlib import Path
import subprocess

import pytest_asyncio

from app.bridge.session import AgentSession, SessionContext
from app.storage.database import Database


def run_git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest_asyncio.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "source repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    run_git(repository, "config", "user.email", "tests@example.invalid")
    run_git(repository, "config", "user.name", "Agent Bridge Tests")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(repository, "add", "tracked.txt")
    run_git(repository, "commit", "-m", "initial")
    return repository


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "bridge.db")
    await database.initialize()
    return database


@pytest_asyncio.fixture
async def session(database: Database, tmp_path: Path) -> SessionContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_git(workspace, "init", "-b", "main")
    run_git(workspace, "config", "user.email", "tests@example.invalid")
    run_git(workspace, "config", "user.name", "Agent Bridge Tests")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    run_git(workspace, "add", "tracked.txt")
    run_git(workspace, "commit", "-m", "initial")
    context = SessionContext(
        id="ses_test",
        project_name="test-project",
        workspace=workspace,
        base_branch="main",
        current_branch="bridge/ses_test",
        access_mode="develop",
    )
    await database.insert_session(context)
    for agent in ("deepseek", "codex"):
        await database.insert_agent_session(
            AgentSession(
                id=f"ags_{agent}",
                bridge_session_id=context.id,
                agent=agent,
            )
        )
    return context
