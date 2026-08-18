from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.bridge.session import SessionContext

InspectOperation = Literal[
    "list_files",
    "read_file",
    "search_text",
    "git_status",
    "git_log",
    "git_diff",
]


class InspectionError(RuntimeError):
    """A safe, user-facing inspection operation failed."""


class InspectionResult(BaseModel):
    operation: InspectOperation
    output: str
    truncated: bool = False


class WorkspaceInspector:
    """Read-only workspace inspection with fixed operations and no shell."""

    _MAX_BYTES = 1_000_000
    _MAX_LINES = 500

    async def inspect(
        self,
        context: SessionContext,
        operation: InspectOperation,
        *,
        path: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> InspectionResult:
        if limit < 1 or limit > self._MAX_LINES:
            raise InspectionError(f"limit must be between 1 and {self._MAX_LINES}")
        root = context.workspace.resolve(strict=True)
        if operation == "list_files":
            return self._list_files(root, path, limit)
        if operation == "read_file":
            if not path:
                raise InspectionError("path is required for read_file")
            return self._read_file(root, path)
        if operation == "search_text":
            if not query:
                raise InspectionError("query is required for search_text")
            return self._search_text(root, path, query, limit)
        if operation == "git_status":
            return InspectionResult(operation=operation, output=await self._git(root, "status", "--short"))
        if operation == "git_log":
            return InspectionResult(operation=operation, output=await self._git(root, "log", f"--max-count={limit}", "--oneline"))
        if operation == "git_diff":
            return InspectionResult(
                operation=operation,
                output=await self._git(root, "diff", "--no-ext-diff", "--no-textconv"),
            )
        raise InspectionError(f"unsupported inspection operation: {operation}")

    async def status_snapshot(self, context: SessionContext) -> str:
        return await self._git(context.workspace.resolve(strict=True), "status", "--porcelain")

    def _list_files(self, root: Path, path: str | None, limit: int) -> InspectionResult:
        start = self._resolve(root, path) if path else root
        if not start.is_dir():
            raise InspectionError("path must be a directory for list_files")
        entries: list[str] = []
        for item in sorted(start.rglob("*")):
            relative = item.relative_to(root)
            if ".git" in relative.parts or not item.is_file() or not self._is_inside_root(root, item):
                continue
            entries.append(relative.as_posix())
            if len(entries) >= limit:
                break
        return InspectionResult(
            operation="list_files",
            output="\n".join(entries),
            truncated=len(entries) >= limit,
        )

    def _read_file(self, root: Path, path: str) -> InspectionResult:
        target = self._resolve(root, path)
        if not target.is_file():
            raise InspectionError("path must be a file for read_file")
        if target.stat().st_size > self._MAX_BYTES:
            raise InspectionError("file is too large to inspect")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise InspectionError("file is not UTF-8 text") from exc
        lines = content.splitlines()
        return InspectionResult(
            operation="read_file",
            output="\n".join(lines[: self._MAX_LINES]),
            truncated=len(lines) > self._MAX_LINES,
        )

    def _search_text(
        self, root: Path, path: str | None, query: str, limit: int
    ) -> InspectionResult:
        start = self._resolve(root, path) if path else root
        if not start.is_dir():
            raise InspectionError("path must be a directory for search_text")
        matches: list[str] = []
        for item in sorted(start.rglob("*")):
            relative = item.relative_to(root)
            if (
                ".git" in relative.parts
                or not item.is_file()
                or not self._is_inside_root(root, item)
                or item.stat().st_size > self._MAX_BYTES
            ):
                continue
            try:
                lines = item.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(f"{relative.as_posix()}:{number}:{line}")
                    if len(matches) >= limit:
                        return InspectionResult(
                            operation="search_text",
                            output="\n".join(matches),
                            truncated=True,
                        )
        return InspectionResult(operation="search_text", output="\n".join(matches))

    @staticmethod
    def _resolve(root: Path, requested: str | None) -> Path:
        if not requested:
            return root
        candidate = (root / requested).resolve(strict=False)
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise InspectionError("path escapes the session workspace") from exc
        if ".git" in relative.parts:
            raise InspectionError("direct .git inspection is not allowed")
        if not candidate.exists():
            raise InspectionError("path does not exist in the session workspace")
        return candidate

    @staticmethod
    def _is_inside_root(root: Path, item: Path) -> bool:
        try:
            item.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    async def _git(root: Path, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise InspectionError("git inspection timed out") from exc
        output = stdout.decode(errors="replace").strip()
        error = stderr.decode(errors="replace").strip()
        if process.returncode != 0:
            raise InspectionError(error or output or "git inspection failed")
        return output
