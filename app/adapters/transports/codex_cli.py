from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.adapters.base import AgentAdapterTimeout, AgentHealth
from app.adapters.transports.base import CodexReviewOutput, CodexTransport
from app.bridge.protocol import new_id
from app.runtime.process import ProcessManager, ProcessManagerError, ProcessTimeout


class CodexTransportError(RuntimeError):
    pass


class CodexTransportTimeout(CodexTransportError, AgentAdapterTimeout):
    pass


class CodexCLITransport(CodexTransport):
    """Execute isolated Codex reviews with an explicit read-only sandbox."""

    def __init__(
        self,
        executable: str = "codex",
        *,
        command_prefix: tuple[str, ...] = (),
        timeout_seconds: float = 1800,
        health_timeout_seconds: float = 15,
        schema_path: Path | None = None,
        processes: ProcessManager | None = None,
    ) -> None:
        if timeout_seconds <= 0 or health_timeout_seconds <= 0:
            raise ValueError("Codex timeouts must be greater than zero")
        self.executable = executable
        self.command_prefix = command_prefix
        self.timeout_seconds = timeout_seconds
        self.health_timeout_seconds = health_timeout_seconds
        self.schema_path = schema_path or (
            Path(__file__).parents[3] / "prompts" / "codex_review_schema.json"
        )
        self._processes = processes or ProcessManager()
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def review(self, workspace: Path, prompt: str) -> CodexReviewOutput:
        resolved_workspace = self._workspace(workspace)
        if not prompt.strip():
            raise CodexTransportError("Codex review prompt must not be empty")
        try:
            schema = self.schema_path.resolve(strict=True)
        except OSError as exc:
            raise CodexTransportError(
                f"Codex review schema does not exist: {self.schema_path}"
            ) from exc
        review_id = new_id("crv")
        arguments = [
            *self.command_prefix,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(schema),
            "--cd",
            str(resolved_workspace),
            prompt,
        ]
        try:
            process = await self._processes.start(
                self.executable,
                *arguments,
                cwd=resolved_workspace,
            )
        except ProcessManagerError as exc:
            raise CodexTransportError(str(exc)) from exc
        self._active[review_id] = process
        try:
            try:
                result = await self._processes.wait(
                    process, timeout=self.timeout_seconds
                )
            except ProcessTimeout as exc:
                await self._processes.kill_tree(process)
                raise CodexTransportTimeout(str(exc)) from exc
        finally:
            self._active.pop(review_id, None)
        if result.returncode != 0:
            detail = result.stderr.strip() or "No error output was provided."
            raise CodexTransportError(
                f"Codex CLI exited with {result.returncode}: {detail}"
            )
        response, external_session_id = self._parse_stream(result.stdout)
        return CodexReviewOutput(
            response=response,
            external_session_id=external_session_id,
        )

    async def cancel(self) -> None:
        for process in list(self._active.values()):
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
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = None
        if isinstance(report, dict) and isinstance(report.get("checks"), dict):
            blocking_categories = {
                "auth",
                "config",
                "git",
                "install",
                "network",
                "reachability",
                "runtime",
                "sandbox",
            }
            failed_checks = [
                check_id
                for check_id, check in report["checks"].items()
                if isinstance(check, dict)
                and check.get("status") == "fail"
                and check.get("category") in blocking_categories
            ]
            if failed_checks:
                return AgentHealth(
                    status="degraded",
                    detail="Codex doctor failed: " + ", ".join(failed_checks),
                )
            return AgentHealth(
                status="healthy",
                detail="Codex CLI is ready; non-blocking doctor warnings may remain",
            )
        if result.returncode != 0:
            if isinstance(report, dict):
                overall = report.get("overallStatus")
                detail = f"Codex doctor status: {overall or 'failed'}"
            else:
                detail = result.stderr.strip() or result.stdout.strip()
            return AgentHealth(
                status="degraded",
                detail=detail or f"Codex doctor exited with {result.returncode}",
            )
        detail = "Codex CLI doctor check passed"
        if isinstance(report, dict):
            summary = report.get("summary")
            if isinstance(summary, dict):
                detail = str(summary.get("status") or detail)
            elif report.get("overallStatus"):
                detail = f"Codex doctor status: {report['overallStatus']}"
        return AgentHealth(status="healthy", detail=detail)

    async def close(self) -> None:
        await self.cancel()
        self._active.clear()

    @staticmethod
    def _workspace(workspace: Path) -> Path:
        try:
            resolved = workspace.resolve(strict=True)
        except OSError as exc:
            raise CodexTransportError(
                f"Codex workspace does not exist: {workspace}"
            ) from exc
        if not resolved.is_dir():
            raise CodexTransportError(
                f"Codex workspace is not a directory: {workspace}"
            )
        return resolved

    @staticmethod
    def _parse_stream(stream: str) -> tuple[str, str]:
        external_session_id: str | None = None
        response: str | None = None
        completed = False
        error: str | None = None
        for line_number, line in enumerate(stream.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CodexTransportError(
                    f"Codex returned invalid JSONL on line {line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise CodexTransportError(
                    f"Codex returned a non-object event on line {line_number}"
                )
            event_type = event.get("type")
            if event_type == "thread.started":
                candidate = event.get("thread_id")
                if isinstance(candidate, str) and candidate:
                    external_session_id = candidate
            elif event_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    candidate = item.get("text")
                    if isinstance(candidate, str) and candidate.strip():
                        response = candidate
            elif event_type == "turn.completed":
                completed = True
            elif event_type in {"turn.failed", "error"}:
                error = CodexCLITransport._event_error(event)
        if error:
            raise CodexTransportError(error)
        if not completed:
            raise CodexTransportError("Codex stream ended without turn.completed")
        if external_session_id is None:
            raise CodexTransportError("Codex stream did not include a thread ID")
        if response is None:
            raise CodexTransportError("Codex stream did not include a final response")
        return response, external_session_id

    @staticmethod
    def _event_error(event: dict[str, object]) -> str:
        candidate = event.get("error") or event.get("message")
        if isinstance(candidate, dict):
            candidate = candidate.get("message")
        if isinstance(candidate, str) and candidate:
            return candidate
        return "Codex reported a failed review turn"
