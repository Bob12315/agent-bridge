from __future__ import annotations

from pathlib import Path

from app.adapters.mock import MockAdapter
from app.config import AppConfig, BridgeConfig, RuntimeConfig
from app.mcp.server import build_service
from app.bridge.protocol import MessageContent
from app.storage.database import Database


async def test_v2_project_task_and_idempotent_request(
    git_repository: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        runtime=RuntimeConfig(
            database=tmp_path / "bridge.db",
            workspace_root=tmp_path / "workspaces",
            log_root=tmp_path / "logs",
        ),
        bridge=BridgeConfig(synchronous_wait_seconds=1),
    )
    database = Database(config.runtime.database)
    await database.initialize()
    deepseek = MockAdapter("deepseek")
    service = build_service(
        config, database=database, adapters={"deepseek": deepseek, "codex": MockAdapter("codex")}
    )

    project = await service.bridge_add_project("v2-demo", str(git_repository))
    assert project.status == "completed" and project.project is not None
    task = await service.bridge_create_task(project.project.id, "Persistent sessions")
    assert task.status == "completed" and task.task is not None and task.session is not None
    assert task.task.bridge_session_id == task.session.id
    assert (await service.bridge_list_tasks(project.project.id)).tasks == [task.task]
    completed = await service.bridge_transition_task(task.task.id, "completed")
    assert completed.status == "completed" and completed.task is not None
    assert completed.task.status == "completed"
    resumed = await service.bridge_transition_task(task.task.id, "active")
    assert resumed.status == "failed" and resumed.error is not None
    # Restore the active state only through the documented recovery path.
    archived = await service.bridge_transition_task(task.task.id, "archived")
    assert archived.status == "completed"

    # Use a fresh active task for request execution.
    task = await service.bridge_create_task(project.project.id, "Idempotent request")
    assert task.status == "completed" and task.task is not None and task.session is not None

    first = await service.bridge_send(
        task.session.id,
        "deepseek",
        "task",
        MessageContent(text="Implement one turn."),
        "develop",
        request_id="req_idempotent",
    )
    second = await service.bridge_send(
        task.session.id,
        "deepseek",
        "task",
        MessageContent(text="This payload is replayed."),
        "develop",
        request_id="req_idempotent",
    )
    assert first.status == second.status == "completed"
    assert first.request_id == second.request_id == "req_idempotent"
    assert deepseek.turn_count == 1
    agent_session = await database.get_agent_session(task.session.id, "deepseek")
    assert agent_session is not None and agent_session.external_session_id


async def test_git_preflight_requires_explicit_confirmation(
    git_repository: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        runtime=RuntimeConfig(database=tmp_path / "bridge.db", workspace_root=tmp_path / "workspaces"),
    )
    database = Database(config.runtime.database)
    await database.initialize()
    service = build_service(config, database=database)
    created = await service.bridge_create_session("git-v2", str(git_repository), "main", "develop")
    assert created.status == "completed" and created.session_id
    result = await service.bridge_git_preflight(created.session_id, "apply")
    assert result.status == "completed" and result.preflight is not None
    assert result.preflight["confirmation_id"]
    denied = await service.bridge_git_apply(created.session_id, "not-a-confirmation", "invalid")
    assert denied.status == "failed" and denied.error is not None
    assert denied.error.code == "PRECONDITION_FAILED"
