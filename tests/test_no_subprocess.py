"""Evidence that the server starts no child process.

Python audit hooks see every process creation made through the standard library, and
they cannot be turned off from Python code. Each recorder has a control showing that it
does fire, so an empty recording means something. A hook sees Python's own process APIs
only: native code, or a direct OS call through ``ctypes``, would pass unseen. That is why
the source is also parsed for process APIs, and why this is evidence and not a guarantee.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

import universal_ai_tools
from service_support import PYTHON, Dialog, dialog_server_kwargs, service, write_config
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.server import create_server

PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
        "os.startfile/2",
        "pty.spawn",
    }
)
PROCESS_MODULES = frozenset({"subprocess", "multiprocessing", "pty", "pexpect", "shlex"})
PROCESS_FUNCTIONS = frozenset(
    {
        "system",
        "popen",
        "spawnl",
        "spawnv",
        "spawnve",
        "posix_spawn",
        "posix_spawnp",
        "fork",
        "forkpty",
        "startfile",
        "execv",
        "execve",
        "execvp",
    }
)

# Audit hooks cannot be removed, so this one stays installed and records only when asked.
_recorded: list[str] = []
_recording = False


def _record(event: str, _args: tuple[object, ...]) -> None:
    if _recording and event in PROCESS_EVENTS:
        _recorded.append(event)


sys.addaudithook(_record)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    (path / "docs").mkdir()
    (path / "docs" / "hello.txt").write_text("hello\n", encoding="utf-8")
    (path / ".git").mkdir()
    (path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (path / ".env").write_text("KEY=value\n", encoding="utf-8")
    return path


@pytest.fixture
def recording() -> Iterator[list[str]]:
    global _recording
    _recorded.clear()
    _recording = True
    try:
        yield _recorded
    finally:
        _recording = False


async def _call_every_tool(client: Client, *, writable: bool = True) -> None:
    """Call every registered tool, with successful and refused requests.

    ``writable=False`` is a read-only server: ``write_file`` does not exist, so it is refused.
    """
    calls: list[tuple[str, dict[str, object], bool]] = [
        ("get_platform_info", {}, False),
        ("list_files", {}, False),
        ("list_files", {"recursive": True}, False),
        ("list_files", {"path": ".git"}, True),
        ("read_file", {"path": "docs/hello.txt"}, False),
        ("read_file", {"path": ".env"}, True),
        ("read_file", {"path": ".git/config"}, True),
        ("read_file", {"path": "../outside.txt"}, True),
        ("search_files", {"pattern": "hello"}, False),
        ("search_files", {"pattern": "hello", "path": ".git"}, True),
        ("write_file", {"path": "docs/new.txt", "content": "new"}, not writable),
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}, True),
        ("run_command", {"executable": "pytest"}, True),
    ]
    names = [tool.name for tool in (await client.list_tools()).tools]
    assert names == [
        "get_platform_info",
        "list_files",
        "read_file",
        "search_files",
        *(["write_file"] if writable else []),
    ]
    for tool, arguments, refused in calls:
        result = await client.call_tool(tool, arguments)
        assert result.is_error is refused, (tool, arguments)


def test_recorder_sees_a_child_process(recording: list[str]) -> None:
    """Control: the recorder fires for each way of starting a process used here."""
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    os.system(f'"{sys.executable}" -c pass')  # noqa: S605

    assert recording[:2] == ["subprocess.Popen", "os.system"]


@pytest.mark.anyio
async def test_no_tool_call_starts_a_process(workspace: Path, recording: list[str]) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: "true"})

    async with Client(create_server(config)) as client:
        await _call_every_tool(client)

    assert recording == []


# The wrapper installs the audit hook before anything of the server is imported, then
# runs the real entry point, so the recording covers the whole life of the process.
_RECORDING_SERVER = """
import os, sys
log = open(os.environ["PROCESS_LOG"], "a", encoding="utf-8", buffering=1)
events = frozenset(sys.argv[1].split(","))
def hook(event, args):
    if event in events:
        log.write(event + "\\n")
sys.addaudithook(hook)
if sys.argv[2] == "control":
    import subprocess
    subprocess.run([sys.executable, "-c", "pass"], check=True)
from universal_ai_tools.server import main
main()
"""


def _recording_server(workspace: Path, log: Path, mode: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-P", "-c", _RECORDING_SERVER, ",".join(sorted(PROCESS_EVENTS)), mode],
        # Environment mode, read-only: it can no longer enable writes. File mode would need a
        # real approval dialog in the child process.
        env={ENV_WORKSPACE: str(workspace), "PROCESS_LOG": str(log)},
    )


@pytest.mark.anyio
async def test_stdio_recorder_sees_a_child_process(workspace: Path, tmp_path: Path) -> None:
    """Control: the same wrapper records a process when the server side starts one."""
    log = tmp_path / "processes.log"

    async with Client(
        _recording_server(workspace, log, "control"), read_timeout_seconds=30
    ) as client:
        await client.list_tools()

    assert "subprocess.Popen" in log.read_text(encoding="utf-8").split()


@pytest.mark.anyio
async def test_real_server_process_starts_no_process(workspace: Path, tmp_path: Path) -> None:
    log = tmp_path / "processes.log"

    async with Client(_recording_server(workspace, log, "real"), read_timeout_seconds=30) as client:
        await _call_every_tool(client, writable=False)

    assert log.read_text(encoding="utf-8") == ""


def _source_files() -> list[Path]:
    package = Path(universal_ai_tools.__file__).parent
    return sorted(package.rglob("*.py"))


def test_package_cannot_be_found_empty() -> None:
    assert {path.name for path in _source_files()} >= {
        "server.py",
        "tools.py",
        "workspace.py",
        "policy.py",
        "filesystem.py",
    }


# The only modules that may start processes, by path in the package. terminal.py is the
# run_command tool, imported by the server only when [terminal] enabled = true; clients.py
# runs the user's `claude` CLI for `universal-ai-tools setup` and is never imported by the
# server; desktop/launch.py starts a program the user listed in [input] launchable, with no
# arguments, after approval (launch_app), and is imported only then; sandbox.py starts the
# trusted docker program to run a command in a locked-down container (run_in_sandbox), and is
# imported only when [sandbox] enabled = true. Every other module is scanned exactly as before.
PROCESS_MODULES_ALLOWED = frozenset(
    {"terminal.py", "clients.py", "desktop/launch.py", "sandbox.py"}
)


def _relative(path: Path) -> str:
    return path.relative_to(Path(universal_ai_tools.__file__).parent).as_posix()


def test_only_the_terminal_client_setup_and_launcher_may_start_processes() -> None:
    allowed = {_relative(p) for p in _source_files() if _relative(p) in PROCESS_MODULES_ALLOWED}
    assert allowed == PROCESS_MODULES_ALLOWED


@pytest.mark.parametrize(
    "path",
    [path for path in _source_files() if _relative(path) not in PROCESS_MODULES_ALLOWED],
    ids=_relative,
)
def test_source_has_no_process_api(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported = {(node.module or "").partition(".")[0]}
        else:
            imported = set()
        assert not imported & PROCESS_MODULES, (path.name, imported)
        if isinstance(node, ast.Attribute):
            assert node.attr not in PROCESS_FUNCTIONS, (path.name, node.lineno, node.attr)


def test_command_module_is_gone() -> None:
    assert importlib.util.find_spec("universal_ai_tools.commands") is None


@pytest.fixture(autouse=True)
def _never_leave_recording_on() -> Iterator[None]:
    yield
    assert _recording is False


# File mode. Approvals are answered by a stand-in dialog, so every tool really runs.


async def _call_file_tools(client: Client) -> None:
    calls: list[tuple[str, dict[str, object], bool]] = [
        ("get_platform_info", {}, False),
        ("list_files", {}, False),
        ("read_file", {"path": "docs/hello.txt"}, False),
        ("write_file", {"path": "docs/new.txt", "content": "new"}, False),
        ("edit_file", {"path": "docs/new.txt", "old_text": "new", "new_text": "newer"}, False),
        ("create_directory", {"path": "docs/sub"}, False),
        ("move_path", {"source": "docs/new.txt", "destination": "docs/sub/new.txt"}, False),
        ("edit_file", {"path": ".env", "old_text": "KEY", "new_text": "X"}, True),
        ("move_path", {"source": ".git", "destination": "git"}, True),
    ]
    for tool, arguments, refused in calls:
        result = await client.call_tool(tool, arguments)
        assert result.is_error is refused, (tool, arguments, result.content)


@pytest.mark.anyio
async def test_file_mode_without_terminal_starts_no_process(
    workspace: Path, tmp_path: Path, recording: list[str]
) -> None:
    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    server = create_server(config, **dialog_server_kwargs(Dialog("yes")))

    async with Client(server) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        await _call_file_tools(client)

    assert "run_command" not in names
    assert recording == []


@pytest.mark.anyio
async def test_a_forced_terminal_setting_starts_no_process(
    workspace: Path, tmp_path: Path, recording: list[str]
) -> None:
    """The terminal turned on past the config file's refusal: still no tool, no process."""
    from dataclasses import replace

    from universal_ai_tools.settings import TerminalSettings

    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    forced = replace(config, terminal=TerminalSettings(enabled=True, programs=(PYTHON,)))
    server = create_server(forced, **dialog_server_kwargs(Dialog("yes")))

    async with Client(server) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        await _call_file_tools(client)
        refused = await client.call_tool("run_command", {"program": "python", "args": ["-c", "1"]})

    assert "run_command" not in names
    assert refused.is_error is True
    assert recording == []


_IMPORTED_MODULES = """
import sys, json
from dataclasses import replace
from universal_ai_tools.settings import TerminalSettings, load_config_file
from universal_ai_tools.server import create_server
config = load_config_file(__import__("pathlib").Path(sys.argv[1]))
if sys.argv[2] == "forced":
    config = replace(config, terminal=TerminalSettings(enabled=True, programs=(sys.executable,)))
create_server(config)
print(json.dumps(sorted(m for m in sys.modules if m.startswith("universal_ai_tools"))))
"""


@pytest.mark.parametrize("terminal", ["off", "forced"])
def test_the_process_module_is_never_loaded(workspace: Path, tmp_path: Path, terminal: str) -> None:
    config_path = write_config(tmp_path / "state", workspace, {"filesystem": {"write": True}})

    completed = subprocess.run(
        [sys.executable, "-c", _IMPORTED_MODULES, str(config_path), terminal],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    loaded = set(json.loads(completed.stdout))
    assert "universal_ai_tools.terminal" not in loaded
    assert "universal_ai_tools.desktop.launch" not in loaded
    assert "universal_ai_tools.sandbox" not in loaded
    assert "universal_ai_tools.clients" not in loaded
    assert "universal_ai_tools.cli" not in loaded
