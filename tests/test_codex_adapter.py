from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.adapters.codex import CodexAdapter
from app.adapters.mock import MockAdapter
from app.adapters.registry import build_adapter_registry
from app.adapters.transports.codex_cli import (
    CodexCLITransport,
    CodexTransportError,
    CodexTransportTimeout,
)
from app.bridge.protocol import MessageContent, MessageEnvelope, new_id, utc_now
from app.bridge.request_manager import RequestManager
from app.bridge.router import Router
from app.bridge.session import SessionContext
from app.config import AppConfig, CodexConfig


FAKE_CODEX = r'''
import json
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
if arguments == ["doctor", "--json"]:
    print(json.dumps({"summary": {"status": "ready"}}))
    raise SystemExit(0)

trace_root = Path(__file__).parent
counter_path = trace_root / ".fake-codex-counter"
counter = int(counter_path.read_text() if counter_path.exists() else "0") + 1
counter_path.write_text(str(counter), encoding="utf-8")
workspace = Path(arguments[arguments.index("--cd") + 1]).resolve()
prompt = arguments[-1]
trace = {
    "arguments": arguments[:-1],
    "cwd": str(Path.cwd().resolve()),
    "workspace": str(workspace),
    "prompt": prompt,
}
(trace_root / f".fake-codex-trace-{counter}.json").write_text(
    json.dumps(trace), encoding="utf-8"
)
if "SLOW_REVIEW" in prompt:
    time.sleep(30)
thread_id = f"codex-review-{counter}"
if "REQUIRE_CHANGES" in prompt:
    payload = {
        "verdict": "CHANGES_REQUIRED",
        "text": "A blocking issue was found.",
        "issues": [{
            "severity": "high",
            "file": "app/example.py",
            "line": 7,
            "problem": "Incorrect behavior",
            "required_change": "Correct the behavior and add a test",
        }],
        "blocking": True,
    }
else:
    payload = {
        "verdict": "PASS",
        "text": "The reviewed diff satisfies the request.",
        "issues": [],
        "blocking": False,
    }
print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({
    "type": "item.completed",
    "item": {
        "id": "item-final",
        "type": "agent_message",
        "text": json.dumps(payload),
    },
}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


def fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    return script


def review_context(workspace: Path) -> SessionContext:
    return SessionContext(
        id="ses_codex",
        project_name="codex-test",
        workspace=workspace,
        base_branch="main",
        current_branch="agent/codex-test",
    )


def review_message(text: str, suffix: str = "one") -> MessageEnvelope:
    return MessageEnvelope(
        id=f"msg_{suffix}_{new_id('part')}",
        session_id="ses_codex",
        sender="chatgpt",
        receiver="codex",
        type="review_request",
        task_id="task_codex",
        stage=7,
        round=1,
        content=MessageContent(
            text=text,
            constraints=["Do not modify source files"],
            acceptance_criteria=["Inspect the real Git diff"],
            commit="abc123",
        ),
        created_at=utc_now(),
    )


async def test_codex_reviews_are_independent_read_only_and_structured(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "session workspace"
    workspace.mkdir()
    script = fake_cli(tmp_path)
    transport = CodexCLITransport(
        sys.executable,
        command_prefix=(str(script),),
        timeout_seconds=5,
        health_timeout_seconds=5,
    )
    adapter = CodexAdapter(transport)
    context = review_context(workspace)

    first = await adapter.send(review_message("Review the implementation"), context)
    second = await adapter.send(review_message("REQUIRE_CHANGES", "two"), context)

    assert first.external_session_id == "codex-review-1"
    assert second.external_session_id == "codex-review-2"
    assert first.response.type == "review_result"
    assert first.response.content.verdict == "PASS"
    assert first.response.content.blocking is False
    assert second.response.content.verdict == "CHANGES_REQUIRED"
    assert second.response.content.blocking is True
    assert second.response.content.issues[0].severity == "high"
    assert second.response.reply_to.startswith("msg_two_")
    trace = json.loads(
        (tmp_path / ".fake-codex-trace-1.json").read_text(encoding="utf-8")
    )
    assert trace["cwd"] == str(workspace.resolve())
    assert trace["workspace"] == str(workspace.resolve())
    assert "--ephemeral" in trace["arguments"]
    assert trace["arguments"][trace["arguments"].index("--sandbox") + 1] == (
        "read-only"
    )
    assert "resume" not in trace["arguments"]
    assert "git diff" in trace["prompt"]
    assert "Inspect the real Git diff" in trace["prompt"]
    assert not list(workspace.iterdir())
    health = await adapter.health()
    assert health.status == "healthy"
    assert health.detail == "ready"
    await adapter.close()


async def test_codex_timeout_cancel_and_missing_executable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = fake_cli(tmp_path)
    timeout_transport = CodexCLITransport(
        sys.executable,
        command_prefix=(str(script),),
        timeout_seconds=0.05,
    )
    with pytest.raises(CodexTransportTimeout, match="did not finish"):
        await timeout_transport.review(workspace, "SLOW_REVIEW")

    cancel_transport = CodexCLITransport(
        sys.executable,
        command_prefix=(str(script),),
        timeout_seconds=30,
    )
    turn = asyncio.create_task(cancel_transport.review(workspace, "SLOW_REVIEW"))
    await asyncio.sleep(0.1)
    await cancel_transport.cancel()
    with pytest.raises(CodexTransportError, match="exited with"):
        await turn

    unavailable = CodexCLITransport("missing-codex-executable")
    health = await unavailable.health()
    assert health.status == "unavailable"
    assert "not found" in (health.detail or "")


async def test_codex_health_ignores_non_blocking_terminal_failure(
    tmp_path: Path,
) -> None:
    doctor = tmp_path / "doctor.py"
    doctor.write_text(
        """import json
print(json.dumps({
    \"overallStatus\": \"fail\",
    \"checks\": {
        \"auth.credentials\": {
            \"status\": \"ok\", \"category\": \"auth\"
        },
        \"terminal.env\": {
            \"status\": \"fail\", \"category\": \"terminal\"
        }
    }
}))
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    transport = CodexCLITransport(
        sys.executable,
        command_prefix=(str(doctor),),
    )

    health = await transport.health()

    assert health.status == "healthy"
    assert "non-blocking" in (health.detail or "")


async def test_codex_timeout_is_reported_as_agent_timeout(
    database, session: SessionContext, tmp_path: Path
) -> None:
    workspace = tmp_path / "timeout workspace"
    workspace.mkdir()
    adapter = CodexAdapter(
        CodexCLITransport(
            sys.executable,
            command_prefix=(str(fake_cli(tmp_path)),),
            timeout_seconds=0.05,
        )
    )
    manager = RequestManager(database, Router({"codex": adapter}, database))
    active_session = session.model_copy(update={"workspace": workspace})
    timeout_message = review_message("SLOW_REVIEW", "timeout").model_copy(
        update={"session_id": session.id}
    )
    result = await manager.send(timeout_message, active_session, 1)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "AGENT_TIMEOUT"
    assert "codex timed out" in result.error.message


def test_codex_stream_and_payload_validation(tmp_path: Path) -> None:
    with pytest.raises(CodexTransportError, match="invalid JSONL"):
        CodexCLITransport._parse_stream("not-json")
    incomplete = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "review-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "{}"},
                }
            ),
        ]
    )
    with pytest.raises(CodexTransportError, match="turn.completed"):
        CodexCLITransport._parse_stream(incomplete)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = CodexAdapter(
        CodexCLITransport(
            sys.executable,
            command_prefix=(str(fake_cli(tmp_path)),),
        )
    )
    invalid = review_message("wrong").model_copy(update={"type": "task"})
    with pytest.raises(ValueError, match="only accepts"):
        asyncio.run(adapter.send(invalid, review_context(workspace)))


def test_registry_selects_codex_transport() -> None:
    disabled = AppConfig(codex=CodexConfig(enabled=False, transport="cli"))
    assert isinstance(build_adapter_registry(disabled)["codex"], MockAdapter)

    enabled = AppConfig(codex=CodexConfig(enabled=True, transport="cli"))
    assert isinstance(build_adapter_registry(enabled)["codex"], CodexAdapter)

    unsupported = AppConfig(
        codex=CodexConfig(enabled=True, transport="unknown")
    )
    with pytest.raises(ValueError, match="unsupported Codex transport"):
        build_adapter_registry(unsupported)
