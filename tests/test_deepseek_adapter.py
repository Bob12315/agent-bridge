from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.adapters.deepseek import DeepSeekAdapter
from app.adapters.mock import MockAdapter
from app.adapters.registry import build_adapter_registry
from app.adapters.transports.deepseek_cli import (
    DeepSeekCLITransport,
    DeepSeekTransportError,
    DeepSeekTransportTimeout,
)
from app.bridge.protocol import MessageContent, MessageEnvelope, new_id, utc_now
from app.bridge.request_manager import RequestManager
from app.bridge.router import Router
from app.bridge.session import SessionContext
from app.config import AgentConfig, AppConfig


FAKE_DEEPSEEK = r'''
import json
import os
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
if arguments == ["doctor", "--json"]:
    print(json.dumps({"status": "ready", "message": "fake doctor ready"}))
    raise SystemExit(0)

workspace = Path(arguments[arguments.index("--workspace") + 1]).resolve()
if Path.cwd().resolve() != workspace:
    print("wrong cwd", file=sys.stderr)
    raise SystemExit(4)
prompt = arguments[-1]
counter_path = workspace / ".fake-deepseek-counter"
counter = int(counter_path.read_text() if counter_path.exists() else "0") + 1
counter_path.write_text(str(counter), encoding="utf-8")
(workspace / f".fake-deepseek-prompt-{counter}").write_text(prompt, encoding="utf-8")
external_id = "deepseek-external-session"
if counter > 1:
    resume = arguments[arguments.index("--resume") + 1]
    if resume != external_id:
        print("wrong resume id", file=sys.stderr)
        raise SystemExit(5)
if "SLOW_TURN" in prompt:
    time.sleep(30)
kind = "question" if "ASK_QUESTION" in prompt else "result"
body = json.dumps({"type": kind, "content": {"text": f"turn-{counter}-ok"}})
cut = len(body) // 2
print(json.dumps({"schema": "codewhale.exec-stream", "schema_version": 1, "type": "content", "content": body[:cut]}))
print(json.dumps({"schema": "codewhale.exec-stream", "schema_version": 1, "type": "content", "content": body[cut:]}))
print(json.dumps({"schema": "codewhale.exec-stream", "schema_version": 1, "type": "metadata", "meta": {"session_id": external_id, "workspace": str(workspace), "status": "completed"}}))
print(json.dumps({"schema": "codewhale.exec-stream", "schema_version": 1, "type": "done"}))
'''


def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_deepseek.py"
    script.write_text(FAKE_DEEPSEEK, encoding="utf-8")
    return script


def context(workspace: Path) -> SessionContext:
    return SessionContext(
        id="ses_deepseek",
        project_name="deepseek-test",
        workspace=workspace,
        base_branch="main",
        current_branch="agent/deepseek-test",
    )


def message(text: str, suffix: str) -> MessageEnvelope:
    return MessageEnvelope(
        id=f"msg_{suffix}_{new_id('part')}",
        session_id="ses_deepseek",
        sender="chatgpt",
        receiver="deepseek",
        type="task",
        task_id="task_deepseek",
        stage=6,
        round=1,
        content=MessageContent(text=text),
        created_at=utc_now(),
    )


async def test_deepseek_adapter_keeps_session_resumes_and_binds_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "session workspace"
    workspace.mkdir()
    script = fake_cli(tmp_path)
    transport = DeepSeekCLITransport(
        sys.executable,
        command_prefix=(str(script),),
        timeout_seconds=5,
        health_timeout_seconds=5,
    )
    adapter = DeepSeekAdapter(transport)
    session = context(workspace)

    first = await adapter.send(message("Implement only this change", "one"), session)
    second = await adapter.send(message("ASK_QUESTION", "two"), session)

    assert first.external_session_id == second.external_session_id
    assert first.external_session_id == "deepseek-external-session"
    assert first.response.type == "result"
    assert first.response.content.text == "turn-1-ok"
    assert second.response.type == "question"
    assert second.response.content.text == "turn-2-ok"
    assert second.response.reply_to.startswith("msg_two_")
    assert (workspace / ".fake-deepseek-counter").read_text() == "2"
    first_prompt = (workspace / ".fake-deepseek-prompt-1").read_text(
        encoding="utf-8"
    )
    assert "你是代码执行器" in first_prompt
    assert "Implement only this change" in first_prompt
    assert str(workspace) in first_prompt
    health = await adapter.health()
    assert health.status == "healthy"
    assert health.detail == "fake doctor ready"
    await adapter.close()


async def test_deepseek_transport_timeout_kills_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = DeepSeekCLITransport(
        sys.executable,
        command_prefix=(str(fake_cli(tmp_path)),),
        timeout_seconds=0.05,
    )
    session_id = await transport.create_session(workspace)

    with pytest.raises(DeepSeekTransportTimeout, match="did not finish"):
        await transport.send(session_id, "SLOW_TURN")


async def test_deepseek_timeout_is_reported_as_agent_timeout(
    database, session: SessionContext, tmp_path: Path
) -> None:
    workspace = tmp_path / "timeout workspace"
    workspace.mkdir()
    adapter = DeepSeekAdapter(
        DeepSeekCLITransport(
            sys.executable,
            command_prefix=(str(fake_cli(tmp_path)),),
            timeout_seconds=0.05,
        )
    )
    manager = RequestManager(database, Router({"deepseek": adapter}, database))
    active_session = session.model_copy(update={"workspace": workspace})

    timeout_message = message("SLOW_TURN", "timeout").model_copy(
        update={"session_id": session.id}
    )
    result = await manager.send(timeout_message, active_session, 1)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "AGENT_TIMEOUT"
    assert "timed out" in result.error.message


async def test_deepseek_transport_cancel_and_health_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    transport = DeepSeekCLITransport(
        sys.executable,
        command_prefix=(str(fake_cli(tmp_path)),),
        timeout_seconds=30,
    )
    session_id = await transport.create_session(workspace)
    turn = asyncio.create_task(transport.send(session_id, "SLOW_TURN"))
    await asyncio.sleep(0.1)
    await transport.cancel(session_id)
    with pytest.raises(DeepSeekTransportError, match="exited with"):
        await turn

    unavailable = DeepSeekCLITransport("missing-deepseek-executable")
    health = await unavailable.health()
    assert health.status == "unavailable"
    assert "not found" in (health.detail or "")


def test_deepseek_stream_validation_rejects_unsafe_or_incomplete_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path.resolve()
    with pytest.raises(DeepSeekTransportError, match="invalid JSONL"):
        DeepSeekCLITransport._parse_stream("not-json", workspace)
    unsafe = (
        '{"type":"content","content":"ok"}\n'
        '{"type":"metadata","meta":{"session_id":"x",'
        '"workspace":"C:/outside","status":"completed"}}\n'
        '{"type":"done"}'
    )
    with pytest.raises(DeepSeekTransportError, match="outside"):
        DeepSeekCLITransport._parse_stream(unsafe, workspace)
    incomplete = "\n".join(
        [
            json.dumps({"type": "content", "content": "ok"}),
            json.dumps(
                {
                    "type": "metadata",
                    "meta": {
                        "session_id": "x",
                        "workspace": str(workspace),
                        "status": "completed",
                    },
                }
            ),
        ]
    )
    with pytest.raises(DeepSeekTransportError, match="done event"):
        DeepSeekCLITransport._parse_stream(incomplete, workspace)


def test_adapter_registry_selects_real_transport_only_when_enabled() -> None:
    disabled = AppConfig(
        deepseek=AgentConfig(enabled=False, transport="cli")
    )
    assert isinstance(build_adapter_registry(disabled)["deepseek"], MockAdapter)

    enabled = AppConfig(
        deepseek=AgentConfig(enabled=True, transport="cli")
    )
    assert isinstance(build_adapter_registry(enabled)["deepseek"], DeepSeekAdapter)

    unsupported = AppConfig(
        deepseek=AgentConfig(enabled=True, transport="unknown")
    )
    with pytest.raises(ValueError, match="unsupported DeepSeek transport"):
        build_adapter_registry(unsupported)
