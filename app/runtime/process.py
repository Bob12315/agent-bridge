from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import psutil
from pydantic import BaseModel


class ProcessManagerError(RuntimeError):
    pass


class ProcessTimeout(ProcessManagerError):
    pass


class ProcessResult(BaseModel):
    returncode: int
    stdout: str
    stderr: str


class ProcessManager:
    """Start and stop subprocess trees consistently on Windows and Linux."""

    def __init__(self) -> None:
        self._communications: dict[
            int, asyncio.Task[tuple[bytes, bytes]]
        ] = {}

    async def start(
        self,
        executable: str,
        *arguments: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        resolved_executable = self._find_executable(executable)
        resolved_cwd = None
        if cwd is not None:
            try:
                resolved_cwd = cwd.resolve(strict=True)
            except OSError as exc:
                raise ProcessManagerError(f"working directory does not exist: {cwd}") from exc
            if not resolved_cwd.is_dir():
                raise ProcessManagerError(f"working directory is not a directory: {cwd}")

        environment = os.environ.copy()
        if env:
            environment.update(env)
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            resolved_executable,
            *arguments,
            cwd=resolved_cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
        communication = asyncio.create_task(
            process.communicate(), name=f"agent-bridge-process-{process.pid}"
        )
        self._communications[process.pid] = communication
        return process

    async def wait(
        self, process: asyncio.subprocess.Process, timeout: float | None = None
    ) -> ProcessResult:
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(self._communication_for(process)), timeout=timeout
            )
        except TimeoutError as exc:
            raise ProcessTimeout(
                f"process {process.pid} did not finish within {timeout} seconds"
            ) from exc
        self._communications.pop(process.pid, None)
        return ProcessResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def terminate(
        self, process: asyncio.subprocess.Process, timeout: float = 5
    ) -> None:
        if process.returncode is not None:
            await self._finish_communication(process)
            return
        await asyncio.to_thread(self._stop_tree, process.pid, False, timeout)
        await process.wait()
        await self._finish_communication(process)

    async def kill_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await self._finish_communication(process)
            return
        await asyncio.to_thread(self._stop_tree, process.pid, True, 0)
        await process.wait()
        await self._finish_communication(process)

    def _communication_for(
        self, process: asyncio.subprocess.Process
    ) -> asyncio.Task[tuple[bytes, bytes]]:
        task = self._communications.get(process.pid)
        if task is None:
            task = asyncio.create_task(process.communicate())
            self._communications[process.pid] = task
        return task

    async def _finish_communication(
        self, process: asyncio.subprocess.Process
    ) -> None:
        task = self._communications.pop(process.pid, None)
        if task is not None:
            await task

    @staticmethod
    def _find_executable(executable: str) -> str:
        candidate = Path(executable)
        if candidate.parent != Path("."):
            if candidate.is_file():
                return str(candidate.resolve())
            raise ProcessManagerError(f"executable does not exist: {executable}")
        found = shutil.which(executable)
        if found is None:
            raise ProcessManagerError(f"executable was not found on PATH: {executable}")
        return found

    @staticmethod
    def _stop_tree(pid: int, force: bool, timeout: float) -> None:
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        processes = list(reversed(parent.children(recursive=True))) + [parent]
        for item in processes:
            try:
                item.kill() if force else item.terminate()
            except psutil.NoSuchProcess:
                continue
        if force:
            psutil.wait_procs(processes, timeout=5)
            return
        if not force:
            _, alive = psutil.wait_procs(processes, timeout=timeout)
            for item in alive:
                try:
                    item.kill()
                except psutil.NoSuchProcess:
                    continue
