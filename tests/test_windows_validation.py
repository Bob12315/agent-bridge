from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from app.adapters.codex import CodexAdapter
from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.mock import MockAdapter
from app.adapters.transports.codex_cli import CodexCLITransport
from app.adapters.transports.deepseek_cli import DeepSeekCLITransport
from app.bridge.protocol import MessageContent
from app.config import AppConfig, BridgeConfig, RuntimeConfig
from app.mcp.server import build_service
from app.runtime.process import ProcessManager
from app.storage.database import Database

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Stage 9 validates the native Windows runtime"
)


FAKE_DEEPSEEK = r'''
import json
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
workspace = Path(arguments[arguments.index("--workspace") + 1]).resolve()
if Path.cwd().resolve() != workspace:
    raise SystemExit(10)
prompt = arguments[-1]
if "SLOW" in prompt:
    time.sleep(30)
(workspace / "implemented.txt").write_text(
    "implemented on Windows\n", encoding="utf-8"
)
payload = json.dumps({
    "type": "result",
    "content": {
        "text": "Windows implementation complete",
        "changed_files": ["implemented.txt"]
    }
})
print(json.dumps({"type": "content", "content": payload}))
print(json.dumps({
    "type": "metadata",
    "meta": {
        "session_id": "windows-deepseek-session",
        "workspace": str(workspace),
        "status": "completed"
    }
}))
print(json.dumps({"type": "done"}))
'''


FAKE_CODEX = r'''
import json
from pathlib import Path
import subprocess
import sys

arguments = sys.argv[1:]
workspace = Path(arguments[arguments.index("--cd") + 1]).resolve()
if Path.cwd().resolve() != workspace:
    raise SystemExit(11)
if arguments[arguments.index("--sandbox") + 1] != "read-only":
    raise SystemExit(12)
status = subprocess.run(
    ["git", "-C", str(workspace), "status", "--short"],
    check=True,
    capture_output=True,
    text=True,
).stdout
if "implemented.txt" not in status:
    raise SystemExit(13)
payload = {
    "verdict": "PASS",
    "text": "The Windows worktree change is visible and reviewable.",
    "issues": [],
    "blocking": False,
}
print(json.dumps({
    "type": "thread.started", "thread_id": "windows-codex-review"
}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "id": "final",
        "type": "agent_message",
        "text": json.dumps(payload),
    },
}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


def write_fake_cli(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for_bridge(client: httpx.AsyncClient) -> None:
    for _ in range(100):
        try:
            response = await client.get("/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("Bridge HTTP server did not start")


async def test_native_windows_full_stack_and_restart_recovery(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime with spaces"
    config = AppConfig(
        runtime=RuntimeConfig(
            database=runtime / "bridge.db",
            workspace_root=runtime / "workspaces",
            log_root=runtime / "logs",
        ),
        bridge=BridgeConfig(synchronous_wait_seconds=1),
    )
    database = Database(config.runtime.database)
    await database.initialize()
    deepseek_script = write_fake_cli(
        tmp_path, "windows_deepseek.py", FAKE_DEEPSEEK
    )
    codex_script = write_fake_cli(tmp_path, "windows_codex.py", FAKE_CODEX)
    deepseek = DeepSeekAdapter(
        DeepSeekCLITransport(
            sys.executable,
            command_prefix=(str(deepseek_script),),
            timeout_seconds=10,
        )
    )
    codex = CodexAdapter(
        CodexCLITransport(
            sys.executable,
            command_prefix=(str(codex_script),),
            timeout_seconds=10,
        )
    )
    service = build_service(
        config,
        database=database,
        adapters={"deepseek": deepseek, "codex": codex},
    )

    created = await service.bridge_create_session(
        "windows-validation", str(git_repository), "main", "develop"
    )
    assert created.status == "completed"
    session_id = created.session_id
    workspace = Path(created.workspace)
    assert workspace.is_dir()
    assert "runtime with spaces" in str(workspace)

    execution = await service.bridge_send(
        session_id,
        "deepseek",
        "task",
        MessageContent(text="Implement the Windows validation change."),
        "develop",
        task_id="task_windows",
        stage=9,
        round=1,
    )
    assert execution.status == "completed"
    assert execution.response.content.changed_files == ["implemented.txt"]
    assert (workspace / "implemented.txt").read_text(encoding="utf-8") == (
        "implemented on Windows\n"
    )
    assert not (git_repository / "implemented.txt").exists()

    review = await service.bridge_send(
        session_id,
        "codex",
        "review_request",
        MessageContent(
            text="Review the actual Windows worktree and Git status.",
            changed_files=["implemented.txt"],
        ),
        "review",
        task_id="task_windows",
        stage=9,
        round=1,
        reply_to=execution.response.id,
    )
    assert review.status == "completed"
    assert review.response.content.verdict == "PASS"
    assert review.response.content.issues == []

    service.synchronous_wait_seconds = 0.01
    running = await service.bridge_send(
        session_id,
        "deepseek",
        "task",
        MessageContent(text="SLOW cancellation check"),
        "develop",
        task_id="task_windows_cancel",
        stage=9,
        round=2,
    )
    assert running.status == "running"
    cancelled = await service.bridge_cancel(running.request_id)
    assert cancelled.status == "cancelled"

    timeout_adapter = DeepSeekAdapter(
        DeepSeekCLITransport(
            sys.executable,
            command_prefix=(str(deepseek_script),),
            timeout_seconds=0.05,
        )
    )
    timeout_service = build_service(
        config,
        database=database,
        adapters={"deepseek": timeout_adapter, "codex": codex},
    )
    timeout_service.synchronous_wait_seconds = 1
    timed_out = await timeout_service.bridge_send(
        session_id,
        "deepseek",
        "task",
        MessageContent(text="SLOW timeout check"),
        "develop",
        task_id="task_windows_timeout",
        stage=9,
        round=3,
    )
    assert timed_out.status == "failed"
    assert timed_out.error.code == "AGENT_TIMEOUT"

    restarted_database = Database(config.runtime.database)
    await restarted_database.initialize()
    restarted_service = build_service(
        config,
        database=restarted_database,
        adapters={
            "deepseek": MockAdapter("deepseek"),
            "codex": MockAdapter("codex"),
        },
    )
    restored = await restarted_service.bridge_status(review.request_id)
    assert restored.status == "completed"
    assert restored.response.content.verdict == "PASS"
    continued = await restarted_service.bridge_send(
        session_id,
        "codex",
        "review_request",
        MessageContent(text="Confirm the restored session remains usable."),
        "review",
        stage=9,
        round=4,
    )
    assert continued.status == "completed"

    port = free_port()
    app_code = (
        "from pathlib import Path; import uvicorn; "
        "from app.config import AppConfig,RuntimeConfig; "
        "from app.main import create_app; "
        f"config=AppConfig(runtime=RuntimeConfig(database=Path({str(config.runtime.database)!r}),"
        f"workspace_root=Path({str(config.runtime.workspace_root)!r}),"
        f"log_root=Path({str(config.runtime.log_root)!r}))); "
        f"uvicorn.run(create_app(config),host='127.0.0.1',port={port},log_level='error')"
    )
    processes = ProcessManager()
    bridge_process = await processes.start(
        sys.executable,
        "-c",
        app_code,
        cwd=Path(__file__).parents[1],
        env={"COV_CORE_DATAFILE": ""},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            await wait_for_bridge(client)
            dashboard = await client.get("/")
            assert dashboard.status_code == 200
            assert "windows-validation" in dashboard.text
            session_page = await client.get(f"/sessions/{session_id}")
            assert session_page.status_code == 200
            assert "Stage" in session_page.text and ">9<" in session_page.text
            async with client.stream(
                "GET", "/api/events", params={"session_id": session_id}
            ) as stream:
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith(
                    "text/event-stream"
                )
                lines = []
                async for line in stream.aiter_lines():
                    lines.append(line)
                    if line.startswith("data:"):
                        break
                assert any(line.startswith("id: evt_") for line in lines)
                assert any("SESSION_CREATED" in line for line in lines)
    finally:
        await processes.kill_tree(bridge_process)

    assert config.runtime.database.is_file()
    assert len(await restarted_database.list_messages(session_id)) == 8
    requests = list(reversed(await restarted_database.list_requests(session_id)))
    assert [request.status for request in requests] == [
        "completed",
        "completed",
        "cancelled",
        "failed",
        "completed",
    ]
    events = await restarted_database.list_events(session_id)
    assert "REQUEST_CANCELLED" in {event.type for event in events}
    assert "AGENT_FAILED" in {event.type for event in events}
    closed = await restarted_service.bridge_close_session(session_id)
    assert closed.status == "completed"
    assert closed.workspace_removed is True
    assert not workspace.exists()
