from __future__ import annotations

from pathlib import Path
import sqlite3

from app.adapters.mock import MockAdapter
from app.bridge.protocol import MessageContent
from app.config import AppConfig, BridgeConfig, RuntimeConfig
from app.mcp.server import build_service
from app.storage.database import Database


def config_for(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            database=tmp_path / "bridge.db",
            workspace_root=tmp_path / "workspaces",
            log_root=tmp_path / "logs",
        ),
        bridge=BridgeConfig(synchronous_wait_seconds=1),
    )


async def test_inspect_session_uses_fixed_read_only_operations(
    git_repository: Path, tmp_path: Path
) -> None:
    config = config_for(tmp_path)
    database = Database(config.runtime.database)
    await database.initialize()
    deepseek = MockAdapter("deepseek")
    service = build_service(
        config,
        database=database,
        adapters={"deepseek": deepseek, "codex": MockAdapter("codex")},
    )
    created = await service.bridge_create_session(
        "inspect", str(git_repository), access_mode="inspect"
    )

    listed = await service.bridge_inspect(created.session_id, "list_files")
    assert listed.status == "completed"
    assert "tracked.txt" in listed.result.output

    read = await service.bridge_inspect(created.session_id, "read_file", path="tracked.txt")
    assert read.status == "completed"
    assert read.result.output == "base"

    escaped = await service.bridge_inspect(created.session_id, "read_file", path="../outside.txt")
    assert escaped.status == "failed"
    assert escaped.error.code == "INSPECTION_ERROR"

    denied = await service.bridge_send(
        created.session_id,
        "deepseek",
        "task",
        MessageContent(text="Do not run this."),
        "develop",
    )
    assert denied.status == "failed"
    assert denied.error.code == "POLICY_DENIED"
    assert deepseek.turn_count == 0


async def test_database_migrates_existing_sessions_to_safe_inspect_mode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE sessions (
            id TEXT PRIMARY KEY, project_name TEXT NOT NULL, workspace TEXT NOT NULL,
            base_branch TEXT NOT NULL, current_branch TEXT NOT NULL, base_commit TEXT,
            status TEXT NOT NULL, current_task_id TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)"""
        )
    database = Database(path)
    await database.initialize()
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert "access_mode" in columns


class WritingCodexAdapter(MockAdapter):
    async def send(self, message, context):
        (context.workspace / "review-violation.txt").write_text("changed\n", encoding="utf-8")
        return await super().send(message, context)


async def test_review_changes_are_reported_without_destructive_rollback(
    git_repository: Path, tmp_path: Path
) -> None:
    config = config_for(tmp_path)
    database = Database(config.runtime.database)
    await database.initialize()
    service = build_service(
        config,
        database=database,
        adapters={
            "deepseek": MockAdapter("deepseek"),
            "codex": WritingCodexAdapter("codex"),
        },
    )
    created = await service.bridge_create_session(
        "review", str(git_repository), access_mode="review"
    )
    result = await service.bridge_send(
        created.session_id,
        "codex",
        "review_request",
        MessageContent(text="Review without modifying files."),
        "review",
    )

    assert result.status == "failed"
    assert result.error.code == "READ_ONLY_VIOLATION"
    assert (Path(created.workspace) / "review-violation.txt").exists()
    events = await database.list_events(created.session_id)
    assert "POLICY_VIOLATION" in {event.type for event in events}
