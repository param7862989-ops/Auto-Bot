"""run_command: which programs, how they run, and that nothing outlives them."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from universal_ai_tools.config import ConfigError
from universal_ai_tools.settings import TerminalSettings
from universal_ai_tools.terminal import (
    BLOCKED_PROGRAMS,
    child_environment,
    resolve_programs,
    specs,
)
from universal_ai_tools.tools import ToolCallError, ToolInputError
from universal_ai_tools.workspace import Workspace, WorkspaceError

PYTHON = sys.executable


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = (tmp_path / "workspace").resolve()
    (path / "sub").mkdir(parents=True)
    return path


def tool(root: Path, **settings: Any) -> Any:
    values: dict[str, Any] = {"enabled": True, "programs": (PYTHON,), **settings}
    (spec,) = specs(Workspace(root), TerminalSettings(**values))
    return spec


async def run(spec: Any, *args: str, **extra: object) -> Any:
    return await spec.acall({"program": "python", "args": list(args), **extra})


# Which programs


def test_programs_resolve_to_absolute_paths(root: Path) -> None:
    programs = resolve_programs(TerminalSettings(enabled=True, programs=(PYTHON,)), root)

    assert list(programs) == ["python"]
    assert programs["python"].path == Path(PYTHON)


@pytest.mark.parametrize("name", sorted({"cmd", "powershell", "pwsh", "bash", "reg", "runas"}))
def test_shells_and_admin_tools_are_refused(root: Path, name: str) -> None:
    assert name in BLOCKED_PROGRAMS
    with pytest.raises(ConfigError, match=r"always refused|not found"):
        resolve_programs(TerminalSettings(enabled=True, programs=(name,)), root)


def test_a_blocked_program_is_refused_by_path_too(root: Path, tmp_path: Path) -> None:
    fake = tmp_path / "bin" / ("powershell.exe" if sys.platform == "win32" else "bash")
    fake.parent.mkdir()
    fake.write_bytes(Path(PYTHON).read_bytes())
    fake.chmod(0o755)

    with pytest.raises(ConfigError, match="always refused"):
        resolve_programs(TerminalSettings(enabled=True, programs=(str(fake),)), root)


def test_a_program_inside_the_workspace_is_refused(root: Path) -> None:
    planted = root / ("tool.exe" if sys.platform == "win32" else "tool")
    planted.write_bytes(Path(PYTHON).read_bytes())
    planted.chmod(0o755)

    with pytest.raises(ConfigError, match="inside the workspace"):
        resolve_programs(TerminalSettings(enabled=True, programs=(str(planted),)), root)


def test_path_entries_inside_the_workspace_are_skipped(root: Path) -> None:
    planted = root / ("evil.exe" if sys.platform == "win32" else "evil")
    planted.write_bytes(Path(PYTHON).read_bytes())
    planted.chmod(0o755)

    with pytest.raises(ConfigError, match="not found"):
        resolve_programs(
            TerminalSettings(enabled=True, programs=("evil",)), root, {"PATH": str(root)}
        )


@pytest.mark.skipif(sys.platform != "win32", reason="batch files are a Windows concern")
def test_batch_files_are_refused(root: Path, tmp_path: Path) -> None:
    script = tmp_path / "build.cmd"
    script.write_text("@echo off\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="not an executable"):
        resolve_programs(TerminalSettings(enabled=True, programs=(str(script),)), root)


def test_relative_paths_empty_lists_and_duplicates_are_refused(root: Path) -> None:
    with pytest.raises(ConfigError, match="must be absolute"):
        resolve_programs(TerminalSettings(enabled=True, programs=("bin/tool",)), root)
    with pytest.raises(ConfigError, match="is empty"):
        resolve_programs(TerminalSettings(enabled=True, programs=()), root)
    with pytest.raises(ConfigError, match="both named"):
        resolve_programs(TerminalSettings(enabled=True, programs=(PYTHON, PYTHON)), root)


def test_the_schema_offers_only_the_configured_programs(root: Path) -> None:
    schema = tool(root).input_model.model_json_schema()

    assert schema["properties"]["program"]["const"] == "python"
    with pytest.raises(ToolInputError):
        tool(root).validate({"program": "cmd", "args": []})


# How they run


@pytest.mark.anyio
async def test_exit_code_and_output(root: Path) -> None:
    result = await run(
        tool(root), "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    )

    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.timed_out is False
    assert result.trust == "untrusted"


@pytest.mark.anyio
async def test_no_shell_interprets_the_arguments(root: Path) -> None:
    result = await run(
        tool(root), "-c", "import sys; print(sys.argv[1:])", "a && b", "| x", "%PATH%", "$HOME", "*"
    )

    assert result.stdout.strip() == "['a && b', '| x', '%PATH%', '$HOME', '*']"


@pytest.mark.anyio
async def test_working_directory_is_confined_to_the_workspace(root: Path) -> None:
    spec = tool(root)
    result = await run(spec, "-c", "import os; print(os.getcwd())", cwd="sub")

    assert Path(result.stdout.strip()).resolve() == root / "sub"
    for bad in ("..", "../..", "C:\\" if sys.platform == "win32" else "/", "missing"):
        with pytest.raises(WorkspaceError):
            await run(spec, "-c", "1", cwd=bad)


@pytest.mark.anyio
async def test_the_environment_is_filtered(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIVERSAL_AI_TOOLS_TEST_SECRET", "do-not-pass")
    result = await run(tool(root), "-c", "import os; print(sorted(os.environ))")

    assert "UNIVERSAL_AI_TOOLS_TEST_SECRET" not in result.stdout
    assert "PATH" in result.stdout.upper()
    assert "UNIVERSAL_AI_TOOLS_TEST_SECRET" not in child_environment()


@pytest.mark.anyio
async def test_output_is_capped_and_counted(root: Path) -> None:
    result = await run(tool(root, max_output_bytes=1024), "-c", "print('x' * 100000)")

    assert len(result.stdout) == 1024
    assert result.stdout_truncated is True
    assert result.stdout_bytes > 100000


@pytest.mark.anyio
async def test_control_characters_are_escaped(root: Path) -> None:
    result = await run(tool(root), "-c", "print('\\x1b[31mred\\x07')")

    assert "\x1b" not in result.stdout
    assert "\\x1b" in result.stdout
    assert result.escaped_characters >= 2


@pytest.mark.anyio
async def test_nul_in_arguments_is_refused(root: Path) -> None:
    with pytest.raises(ToolInputError):
        await run(tool(root), "-c", "1\x00")


# Nothing outlives the command

_CHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "{tail}"
)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == 0x102)  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        finished, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return finished == 0


def _gone_soon(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.anyio
async def test_timeout_stops_the_program_and_its_children(root: Path, tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    started = time.monotonic()
    result = await run(
        tool(root), "-c", _CHILD.format(tail="time.sleep(20)\n"), str(marker), timeout_seconds=2
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert time.monotonic() - started < 15
    assert _gone_soon(int(marker.read_text()))


@pytest.mark.anyio
async def test_children_are_stopped_when_the_program_exits(root: Path, tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    result = await run(tool(root), "-c", _CHILD.format(tail=""), str(marker), timeout_seconds=30)

    assert result.exit_code == 0
    assert _gone_soon(int(marker.read_text()))


@pytest.mark.anyio
async def test_cancellation_stops_the_program(root: Path, tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    spec = tool(root)
    with anyio.move_on_after(3):
        await run(
            spec, "-c", _CHILD.format(tail="time.sleep(120)\n"), str(marker), timeout_seconds=60
        )

    assert _gone_soon(int(marker.read_text()))


@pytest.mark.anyio
async def test_one_command_at_a_time(root: Path) -> None:
    spec = tool(root)
    errors: list[str] = []

    async def second() -> None:
        await anyio.sleep(0.5)
        try:
            await run(spec, "-c", "1")
        except ToolCallError as exc:
            errors.append(str(exc))

    async with anyio.create_task_group() as group:
        group.start_soon(run, spec, "-c", "import time; time.sleep(2)")
        group.start_soon(second)

    assert errors == ["another command is still running; wait for it to finish"]


def test_the_approval_shows_the_full_command(root: Path) -> None:
    spec = tool(root)
    described = spec.describe(
        spec.validate({"program": "python", "args": ["-m", "pytest", "-q"], "cwd": "sub"})
    )

    assert described.operation == "Run: python -m pytest -q"
    assert ("Program", PYTHON) in described.details
    assert ("Working directory", "sub") in described.details
    assert "not limited to the workspace" in described.consequences
    assert described.audit["args"] == ["-m", "pytest", "-q"]
