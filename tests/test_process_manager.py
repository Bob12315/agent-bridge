from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import psutil

from app.runtime.process import ProcessManager, ProcessManagerError, ProcessTimeout


async def test_start_and_wait_with_cwd_and_environment(tmp_path: Path) -> None:
    cwd = tmp_path / "working directory"
    cwd.mkdir()
    manager = ProcessManager()
    process = await manager.start(
        sys.executable,
        "-c",
        "import os; print(os.getcwd()); print(os.environ['BRIDGE_TEST'])",
        cwd=cwd,
        env={"BRIDGE_TEST": "ready"},
    )
    result = await manager.wait(process, timeout=5)
    assert result.returncode == 0
    assert str(cwd) in result.stdout
    assert "ready" in result.stdout
    assert result.stderr == ""


async def test_timeout_then_terminate() -> None:
    manager = ProcessManager()
    process = await manager.start(sys.executable, "-c", "import time; time.sleep(30)")
    with pytest.raises(ProcessTimeout, match="did not finish"):
        await manager.wait(process, timeout=0.02)
    await manager.terminate(process, timeout=0.2)
    assert process.returncode is not None


async def test_kill_tree_and_finished_process_are_safe() -> None:
    manager = ProcessManager()
    process = await manager.start(sys.executable, "-c", "import time; time.sleep(30)")
    await manager.kill_tree(process)
    assert process.returncode is not None
    await manager.kill_tree(process)
    await manager.terminate(process)


async def test_kill_tree_stops_child_process(tmp_path: Path) -> None:
    manager = ProcessManager()
    code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "pathlib.Path('child.pid').write_text(str(child.pid)); time.sleep(30)"
    )
    process = await manager.start(
        sys.executable,
        "-c",
        code,
        cwd=tmp_path,
        env={"COV_CORE_DATAFILE": ""},
    )
    pid_file = tmp_path / "child.pid"
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    assert psutil.pid_exists(child_pid)

    await manager.kill_tree(process)

    assert process.returncode is not None
    assert not psutil.pid_exists(child_pid)


async def test_rejects_missing_executable_and_working_directory(tmp_path: Path) -> None:
    manager = ProcessManager()
    with pytest.raises(ProcessManagerError, match="not found"):
        await manager.start("missing-agent-bridge-executable")
    with pytest.raises(ProcessManagerError, match="does not exist"):
        await manager.start(sys.executable, cwd=tmp_path / "missing")


def test_process_manager_uses_supported_platform() -> None:
    assert os.name in {"nt", "posix"}
