from __future__ import annotations

from pathlib import Path

from mcp import Client

from app.adapters.mock import MockAdapter, MockBehavior
from app.bridge.protocol import MessageContent
from app.config import AppConfig, BridgeConfig, RuntimeConfig
from app.mcp.server import build_service, create_mcp_server
from app.mcp.tools import CreateSessionToolResult, RequestToolResult
from app.storage.database import Database


def mcp_config(tmp_path: Path, wait_seconds: float = 1) -> AppConfig:
    return AppConfig(
        runtime=RuntimeConfig(
            database=tmp_path / "bridge.db",
            workspace_root=tmp_path / "workspaces",
            log_root=tmp_path / "logs",
        ),
        bridge=BridgeConfig(synchronous_wait_seconds=wait_seconds),
    )


async def test_tools_complete_mock_round_trip_and_force_chatgpt_sender(
    git_repository: Path, tmp_path: Path
) -> None:
    config = mcp_config(tmp_path)
    database = Database(config.runtime.database)
    await database.initialize()
    deepseek = MockAdapter("deepseek")
    service = build_service(
        config,
        database=database,
        adapters={"deepseek": deepseek, "codex": MockAdapter("codex")},
    )

    created = await service.bridge_create_session(
        "demo", str(git_repository), "main", "develop"
    )
    assert isinstance(created, CreateSessionToolResult)
    assert created.agents == {"codex": "ready", "deepseek": "ready"}

    sent = await service.bridge_send(
        created.session_id,
        "deepseek",
        "task",
        MessageContent(text="Implement the explicit task only."),
        "develop",
        task_id="task_1",
        stage=5,
        round=1,
    )
    assert sent.status == "completed"
    assert sent.response is not None
    assert sent.response.content.text == "DEEPSEEK_MOCK_OK"
    request = await database.get_request(sent.request_id)
    assert request is not None
    incoming = await database.get_message(request.message_id)
    assert incoming is not None
    assert incoming.sender == "chatgpt"
    assert deepseek.turn_count == 1

    status = await service.bridge_status(sent.request_id)
    waited = await service.bridge_wait(sent.request_id, timeout=0)
    assert status == waited == sent
    assert deepseek.turn_count == 1

    closed = await service.bridge_close_session(created.session_id)
    assert closed.status == "completed"
    assert closed.workspace_removed is True


async def test_tool_errors_are_structured_and_close_rejects_active_request(
    git_repository: Path, tmp_path: Path
) -> None:
    config = mcp_config(tmp_path, wait_seconds=0)
    database = Database(config.runtime.database)
    await database.initialize()
    service = build_service(
        config,
        database=database,
        adapters={
            "deepseek": MockAdapter(
                "deepseek", MockBehavior(delay_seconds=30)
            ),
            "codex": MockAdapter("codex"),
        },
    )

    missing = await service.bridge_status("req_missing")
    assert isinstance(missing, RequestToolResult)
    assert missing.error.code == "REQUEST_NOT_FOUND"

    created = await service.bridge_create_session(
        "demo", str(git_repository), access_mode="develop"
    )
    assert isinstance(created, CreateSessionToolResult)
    running = await service.bridge_send(
        created.session_id,
        "deepseek",
        "task",
        MessageContent(text="Long task"),
        "develop",
    )
    assert running.status == "running"
    refused = await service.bridge_close_session(created.session_id)
    assert refused.status == "failed"
    assert refused.error.code == "SESSION_ERROR"
    cancelled = await service.bridge_cancel(running.request_id)
    assert cancelled.status == "cancelled"
    assert (await service.bridge_close_session(created.session_id)).status == "completed"


async def test_mcp_server_exposes_capability_tools_and_runs_in_memory(
    git_repository: Path, tmp_path: Path
) -> None:
    config = mcp_config(tmp_path)
    service = build_service(config)
    server = create_mcp_server(config, service=service)

    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "bridge_create_session",
            "bridge_inspect",
            "bridge_send",
            "bridge_wait",
            "bridge_status",
            "bridge_cancel",
            "bridge_close_session",
            "bridge_add_project",
            "bridge_list_projects",
            "bridge_get_project",
            "bridge_create_task",
            "bridge_list_tasks",
            "bridge_get_task",
            "bridge_recover_task",
            "bridge_transition_task",
            "bridge_git_preflight",
            "bridge_git_apply",
            "bridge_git_discard",
        }
        send_tool = next(
            tool for tool in listed.tools if tool.name == "bridge_send"
        )
        assert {"sender", "id", "created_at"}.isdisjoint(
            send_tool.input_schema["properties"]
        )
        created = await client.call_tool(
            "bridge_create_session",
            {
                "project_name": "mcp-demo",
                "repo_path": str(git_repository),
                "base_branch": "main",
                "access_mode": "develop",
            },
        )
        assert created.is_error is False
        assert created.structured_content is not None
        session_id = created.structured_content["session_id"]

        sent = await client.call_tool(
            "bridge_send",
            {
                "session_id": session_id,
                "receiver": "codex",
                "type": "review_request",
                "content": {"text": "Review this stage."},
                "execution_mode": "review",
                "stage": 5,
                "round": 1,
            },
        )
        assert sent.is_error is False
        assert sent.structured_content is not None
        assert sent.structured_content["status"] == "completed"
        assert sent.structured_content["response"]["sender"] == "codex"

        closed = await client.call_tool(
            "bridge_close_session", {"session_id": session_id}
        )
        assert closed.is_error is False
        assert closed.structured_content["status"] == "completed"
