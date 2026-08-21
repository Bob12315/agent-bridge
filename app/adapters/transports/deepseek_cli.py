from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.adapters.base import AgentAdapterTimeout, AgentHealth
from app.adapters.transports.base import DeepSeekTransport
from app.bridge.protocol import new_id
from app.runtime.process import (
    ProcessManager,
    ProcessManagerError,
    ProcessTimeout,
)


class DeepSeekTransportError(RuntimeError):
    pass


class DeepSeekTransportTimeout(DeepSeekTransportError, AgentAdapterTimeout):
    pass


@dataclass(slots=True)
class _ExecutorSession:
    workspace: Path
    external_session_id: str | None = None


class DeepSeekCLITransport(DeepSeekTransport):
    """Run a DeepSeek/Codewhale CLI without a shell and parse its JSONL stream."""

    def __init__(
        self,
        executable: str = "deepseek",
        *,
        command_prefix: tuple[str, ...] = (),
        timeout_seconds: float = 1800,
        health_timeout_seconds: float = 15,
        processes: ProcessManager | None = None,
    ) -> None:
        if timeout_seconds <= 0 or health_timeout_seconds <= 0:
            raise ValueError("DeepSeek timeouts must be greater than zero")
        self.executable = executable
        self.command_prefix = command_prefix
        self.timeout_seconds = timeout_seconds
        self.health_timeout_seconds = health_timeout_seconds
        self._processes = processes or ProcessManager()
        self._sessions: dict[str, _ExecutorSession] = {}
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def create_session(self, workspace: Path) -> str:
        try:
            resolved = workspace.resolve(strict=True)
        except OSError as exc:
            raise DeepSeekTransportError(
                f"DeepSeek workspace does not exist: {workspace}"
            ) from exc
        if not resolved.is_dir():
            raise DeepSeekTransportError(
                f"DeepSeek workspace is not a directory: {workspace}"
            )
        session_id = new_id("dss")
        self._sessions[session_id] = _ExecutorSession(workspace=resolved)
        return session_id

    async def restore_session(self, workspace: Path, external_session_id: str) -> str:
        """Rebuild a disposable local handle around a persisted harness session."""
        session_id = await self.create_session(workspace)
        self._sessions[session_id].external_session_id = external_session_id
        return session_id

    async def send(self, session_id: str, prompt: str) -> str:
        session = self._require_session(session_id)
        return await self._run(session_id, session, prompt)

    async def resume(self, session_id: str, prompt: str) -> str:
        session = self._require_session(session_id)
        if session.external_session_id is None:
            raise DeepSeekTransportError(
                f"DeepSeek session has not completed its first turn: {session_id}"
            )
        return await self._run(session_id, session, prompt)

    def external_session_id(self, session_id: str) -> str | None:
        return self._require_session(session_id).external_session_id

    async def cancel(self, session_id: str) -> None:
        process = self._active.get(session_id)
        if process is not None:
            await self._processes.kill_tree(process)

    async def health(self) -> AgentHealth:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await self._processes.start(
                self.executable,
                *self.command_prefix,
                "doctor",
                "--json",
            )
            result = await self._processes.wait(
                process, timeout=self.health_timeout_seconds
            )
        except ProcessTimeout as exc:
            if process is not None:
                await self._processes.kill_tree(process)
            return AgentHealth(status="unavailable", detail=str(exc))
        except ProcessManagerError as exc:
            return AgentHealth(status="unavailable", detail=str(exc))
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            return AgentHealth(
                status="degraded",
                detail=detail or f"DeepSeek doctor exited with {result.returncode}",
            )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = None
        detail = "DeepSeek CLI doctor check passed"
        if isinstance(report, dict):
            detail = str(report.get("message") or report.get("status") or detail)
        return AgentHealth(status="healthy", detail=detail)

    async def close(self) -> None:
        for session_id in list(self._active):
            await self.cancel(session_id)
        self._sessions.clear()

    async def _run(
        self,
        session_id: str,
        session: _ExecutorSession,
        prompt: str,
    ) -> str:
        if not prompt.strip():
            raise DeepSeekTransportError("DeepSeek prompt must not be empty")
        arguments = [
            *self.command_prefix,
            "--workspace",
            str(session.workspace),
            "exec",
            "--auto",
            "--output-format",
            "stream-json",
        ]
        if session.external_session_id:
            arguments.extend(["--resume", session.external_session_id])
        arguments.append(prompt)
        process = await self._processes.start(
            self.executable,
            *arguments,
            cwd=session.workspace,
        )
        self._active[session_id] = process
        try:
            try:
                result = await self._processes.wait(
                    process, timeout=self.timeout_seconds
                )
            except ProcessTimeout as exc:
                await self._processes.kill_tree(process)
                raise DeepSeekTransportTimeout(str(exc)) from exc
        finally:
            self._active.pop(session_id, None)
        if result.returncode != 0:
            detail = result.stderr.strip() or "No error output was provided."
            raise DeepSeekTransportError(
                f"DeepSeek CLI exited with {result.returncode}: {detail}"
            )
        output, external_session_id = self._parse_stream(
            result.stdout, session.workspace
        )
        session.external_session_id = external_session_id
        return output

    def _require_session(self, session_id: str) -> _ExecutorSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise DeepSeekTransportError(
                f"DeepSeek session not found: {session_id}"
            ) from exc

    @staticmethod
    def _parse_stream(stream: str, workspace: Path) -> tuple[str, str]:
        content: list[str] = []
        external_session_id: str | None = None
        completed = False
        error: str | None = None
        for line_number, line in enumerate(stream.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DeepSeekTransportError(
                    f"DeepSeek returned invalid JSONL on line {line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise DeepSeekTransportError(
                    f"DeepSeek returned a non-object event on line {line_number}"
                )
            event_type = event.get("type")
            if event_type == "content" and isinstance(event.get("content"), str):
                content.append(event["content"])
            elif event_type == "metadata" and isinstance(event.get("meta"), dict):
                meta = event["meta"]
                candidate = meta.get("session_id")
                if isinstance(candidate, str) and candidate:
                    external_session_id = candidate
                reported_workspace = meta.get("workspace")
                if isinstance(reported_workspace, str):
                    if Path(reported_workspace).resolve() != workspace:
                        raise DeepSeekTransportError(
                            "DeepSeek reported a workspace outside the Session Worktree"
                        )
                status = meta.get("status")
                if status not in {None, "completed", "success"}:
                    error = str(
                        meta.get("error") or f"DeepSeek turn ended as {status}"
                    )
            elif event_type == "error":
                error = str(event.get("error") or "DeepSeek reported an error")
            elif event_type == "done":
                completed = True
        if error:
            raise DeepSeekTransportError(error)
        if not completed:
            raise DeepSeekTransportError("DeepSeek stream ended without a done event")
        if external_session_id is None:
            raise DeepSeekTransportError(
                "DeepSeek stream did not include an external session ID"
            )
        response = "".join(content).strip()
        if not response:
            raise DeepSeekTransportError("DeepSeek returned an empty response")
        return response, external_session_id
