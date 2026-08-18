from __future__ import annotations

from pathlib import Path

from mcp import Client

from app.adapters.mock import MockAdapter, MockBehavior
from app.config import AppConfig, BridgeConfig, RuntimeConfig
from app.mcp.server import build_service, create_mcp_server
from app.storage.database import Database


async def test_complete_rework_cycle_requires_four_explicit_chatgpt_sends(
    git_repository: Path,
    tmp_path: Path,
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
    codex = MockAdapter("codex", MockBehavior(mode="changes_required"))
    service = build_service(
        config,
        database=database,
        adapters={"deepseek": deepseek, "codex": codex},
    )
    server = create_mcp_server(config, service=service)

    async with Client(server, raise_exceptions=True) as client:
        created = await client.call_tool(
            "bridge_create_session",
            {
                "project_name": "stage-eight-e2e",
                "repo_path": str(git_repository),
                "base_branch": "main",
            },
        )
        session_id = created.structured_content["session_id"]

        first_execution = await client.call_tool(
            "bridge_send",
            {
                "session_id": session_id,
                "receiver": "deepseek",
                "type": "task",
                "content": {
                    "text": "Implement the requested change.",
                    "constraints": ["Stay inside the session workspace"],
                    "acceptance_criteria": ["The change is covered by tests"],
                },
                "task_id": "task_stage_8",
                "stage": 8,
                "round": 1,
            },
        )
        first_result = first_execution.structured_content["response"]
        assert first_result["type"] == "result"
        assert deepseek.turn_count == 1
        assert codex.turn_count == 0
        assert len(await database.list_requests(session_id)) == 1
        assert len(await database.list_messages(session_id)) == 2

        first_review = await client.call_tool(
            "bridge_send",
            {
                "session_id": session_id,
                "receiver": "codex",
                "type": "review_request",
                "content": {
                    "text": "Review the first implementation.",
                    "commit": "round-one-commit",
                    "acceptance_criteria": ["The change is covered by tests"],
                },
                "task_id": "task_stage_8",
                "stage": 8,
                "round": 1,
                "reply_to": first_result["id"],
            },
        )
        changes_required = first_review.structured_content["response"]
        assert changes_required["type"] == "review_result"
        assert changes_required["content"]["verdict"] == "CHANGES_REQUIRED"
        assert deepseek.turn_count == 1
        assert codex.turn_count == 1

        observed = await client.call_tool(
            "bridge_status",
            {"request_id": first_review.structured_content["request_id"]},
        )
        assert observed.structured_content["status"] == "completed"
        assert deepseek.turn_count == 1
        assert codex.turn_count == 1
        assert len(await database.list_requests(session_id)) == 2
        assert len(await database.list_messages(session_id)) == 4

        rework = await client.call_tool(
            "bridge_send",
            {
                "session_id": session_id,
                "receiver": "deepseek",
                "type": "task",
                "content": {
                    "text": "Address only the blocking review findings.",
                    "constraints": ["Do not expand scope"],
                    "acceptance_criteria": ["All review findings are resolved"],
                },
                "task_id": "task_stage_8",
                "stage": 8,
                "round": 2,
                "reply_to": changes_required["id"],
            },
        )
        rework_result = rework.structured_content["response"]
        assert rework_result["type"] == "result"
        assert deepseek.turn_count == 2
        assert codex.turn_count == 1

        codex.behavior.mode = "success"
        final_review = await client.call_tool(
            "bridge_send",
            {
                "session_id": session_id,
                "receiver": "codex",
                "type": "review_request",
                "content": {
                    "text": "Review the corrected implementation.",
                    "commit": "round-two-commit",
                    "acceptance_criteria": ["All review findings are resolved"],
                },
                "task_id": "task_stage_8",
                "stage": 8,
                "round": 2,
                "reply_to": rework_result["id"],
            },
        )
        passed = final_review.structured_content["response"]
        assert passed["type"] == "review_result"
        assert passed["content"]["verdict"] == "PASS"
        assert deepseek.turn_count == 2
        assert codex.turn_count == 2

        messages = await database.list_messages(session_id)
        assert len(messages) == 8
        assert [message.sender for message in messages] == [
            "chatgpt",
            "deepseek",
            "chatgpt",
            "codex",
            "chatgpt",
            "deepseek",
            "chatgpt",
            "codex",
        ]
        assert [message.type for message in messages] == [
            "task",
            "result",
            "review_request",
            "review_result",
            "task",
            "result",
            "review_request",
            "review_result",
        ]
        assert [message.round for message in messages] == [1, 1, 1, 1, 2, 2, 2, 2]
        for incoming_index in (2, 4, 6):
            assert messages[incoming_index].reply_to == messages[incoming_index - 1].id
        for response_index in (1, 3, 5, 7):
            assert messages[response_index].reply_to == messages[response_index - 1].id

        requests = list(reversed(await database.list_requests(session_id)))
        assert [request.agent for request in requests] == [
            "deepseek",
            "codex",
            "deepseek",
            "codex",
        ]
        assert all(request.status == "completed" for request in requests)
        events = await database.list_events(session_id)
        assert events[0].type == "SESSION_CREATED"
        assert [event.type for event in events[1:]] == [
            event_type
            for _ in range(4)
            for event_type in (
                "MESSAGE_RECEIVED",
                "AGENT_STARTED",
                "MESSAGE_ROUTED",
                "AGENT_FINISHED",
            )
        ]
        agent_sessions = await database.list_agent_sessions(session_id)
        assert {item.agent: item.status for item in agent_sessions} == {
            "codex": "idle",
            "deepseek": "idle",
        }

        closed = await client.call_tool(
            "bridge_close_session", {"session_id": session_id}
        )
        assert closed.structured_content["status"] == "completed"
