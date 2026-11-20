"""Evidence that the provider layer stays a layer of data and interfaces.

It must not reach the network, start processes, touch files, read the environment, run
tools, or depend on the server, the workspace, or a provider SDK. Three kinds of evidence,
each with its limits:

* The source of ``universal_ai_tools/providers`` is parsed for imports and names that
  would give it those abilities. ``universal_ai_tools.tools`` may be imported only for type
  annotations, under ``TYPE_CHECKING``.
* A fresh interpreter imports the package and reports which modules that loaded.
* Python audit hooks record process, socket, file, and directory events while a scripted
  conversation runs through ``FakeProvider``, with a control showing the recorder fires.
  Audit hooks see Python's own APIs, not native code, so this is evidence, not proof.
"""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import universal_ai_tools.providers as providers_package
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.providers import (
    AssistantMessage,
    FakeProvider,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
    tool_call_response,
    tool_definitions,
)
from universal_ai_tools.tools import build_registry

PACKAGE = Path(providers_package.__file__).parent

FORBIDDEN_MODULES = frozenset(
    {
        # Files, processes, and dynamic code.
        "os",
        "pathlib",
        "io",
        "shutil",
        "tempfile",
        "glob",
        "subprocess",
        "multiprocessing",
        "pty",
        "ctypes",
        "importlib",
        "runpy",
        "pickle",
        "marshal",
        # Networking, including event-loop libraries that can open connections or processes.
        "socket",
        "ssl",
        "select",
        "selectors",
        "asyncio",
        "anyio",
        "http",
        "urllib",
        "httpx",
        "httpcore",
        "requests",
        "aiohttp",
        "websockets",
        # Provider SDKs and the MCP SDK.
        "anthropic",
        "openai",
        "mcp",
    }
)
FORBIDDEN_FIRST_PARTY = frozenset(
    {
        "universal_ai_tools.server",
        "universal_ai_tools.workspace",
        "universal_ai_tools.filesystem",
        "universal_ai_tools.config",
        "universal_ai_tools.tools",
    }
)
FORBIDDEN_NAMES = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint"})
FORBIDDEN_ATTRIBUTES = frozenset({"environ", "getenv", "putenv", "invoke", "handler"})


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _type_checking_imports(tree: ast.Module) -> set[int]:
    """Line numbers of imports inside ``if TYPE_CHECKING:`` blocks."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in node.body:
                if isinstance(child, ast.Import | ast.ImportFrom):
                    lines.add(child.lineno)
    return lines


def test_the_package_cannot_be_found_empty() -> None:
    assert {path.name for path in _sources()} == {
        "__init__.py",
        "base.py",
        "errors.py",
        "fake.py",
        "models.py",
    }


@pytest.mark.parametrize("path", _sources(), ids=lambda path: path.name)
def test_source_uses_no_forbidden_api(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    annotation_only = _type_checking_imports(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if isinstance(node, ast.Import):
                modules = {alias.name for alias in node.names}
            else:
                modules = {node.module or ""}
            for module in modules:
                assert module.partition(".")[0] not in FORBIDDEN_MODULES, (path.name, module)
                if node.lineno not in annotation_only:
                    assert module not in FORBIDDEN_FIRST_PARTY, (path.name, module)
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, (path.name, node.lineno, node.id)
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.lineno, node.attr)


def test_importing_the_package_loads_nothing_it_must_not_use() -> None:
    probe = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import universal_ai_tools.providers\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    loaded = set(json.loads(completed.stdout))

    first_party = {name for name in loaded if name.startswith("universal_ai_tools")}
    assert first_party == {
        "universal_ai_tools",
        "universal_ai_tools.policy",
        "universal_ai_tools.providers",
        "universal_ai_tools.providers.base",
        "universal_ai_tools.providers.errors",
        "universal_ai_tools.providers.fake",
        "universal_ai_tools.providers.models",
    }
    roots = {name.partition(".")[0] for name in loaded}
    assert not roots & {
        "anthropic",
        "openai",
        "httpx",
        "httpcore",
        "requests",
        "aiohttp",
        "mcp",
        "socket",
        "ssl",
        "subprocess",
        "asyncio",
        "anyio",
    }, sorted(roots)
    assert completed.stderr == ""


# Runtime recording

_RECORDED_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
    "os.fork",
    "os.putenv",
    "os.unsetenv",
    "os.listdir",
    "os.scandir",
    "os.remove",
    "os.rename",
    "os.mkdir",
    "os.rmdir",
    "os.chmod",
    "shutil.",
    "urllib.",
    "http.",
    "ctypes.",
    "open",
)

# Audit hooks cannot be removed, so this one stays installed and records only when asked.
_recorded: list[str] = []
_recording = False


def _record(event: str, _args: tuple[object, ...]) -> None:
    if _recording and event.startswith(_RECORDED_PREFIXES):
        _recorded.append(event)


sys.addaudithook(_record)


@pytest.fixture
def recording() -> Iterator[list[str]]:
    global _recording
    _recorded.clear()
    _recording = True
    try:
        yield _recorded
    finally:
        _recording = False


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_recorder_sees_files_sockets_and_processes(recording: list[str], tmp_path: Path) -> None:
    """Control: each kind of event the scenario must not cause is recorded."""
    (tmp_path / "probe.txt").write_text("x", encoding="utf-8")
    socket.socket().close()
    subprocess.run([sys.executable, "-c", "pass"], check=True)

    assert "open" in recording
    assert "socket.__new__" in recording
    assert "subprocess.Popen" in recording


async def _scenario(registry_tools: tuple[ToolDefinition, ...]) -> list[object]:
    """A conversation through FakeProvider, using everything the package offers."""
    provider = FakeProvider(
        [
            tool_call_response(
                ToolCall(
                    id="c1", name="read_file", arguments=parse_tool_arguments('{"path": "a"}')
                ),
                ToolCall(id="c2", name="list_files", arguments={}),
            ),
            ProviderRateLimitError(retry_after_seconds=1),
            {"stop_reason": "end_turn", "content": "done", "usage": {"input_tokens": 9}},
            {"stop_reason": "unknown_reason"},
        ]
    )
    first = ProviderRequest(
        model="test-model",
        max_output_tokens=64,
        messages=[UserMessage(content="Read a and list.")],
        tools=registry_tools,
    )
    outcomes: list[object] = []
    turn = await provider.complete(first)
    outcomes.append(turn.model_dump())
    follow_up = ProviderRequest(
        model="test-model",
        max_output_tokens=64,
        messages=[
            *first.messages,
            AssistantMessage(tool_calls=turn.tool_calls),
            ToolResultMessage(tool_call_id="c1", content="a"),
            ToolResultMessage(tool_call_id="c2", content="[]", is_error=True),
        ],
    )
    for _ in range(4):
        try:
            outcomes.append((await provider.complete(follow_up)).model_dump())
        except ProviderError as exc:
            outcomes.append(type(exc).__name__)
    return outcomes


@pytest.mark.anyio
async def test_recorder_sees_events_inside_an_async_test(tmp_path: Path) -> None:
    """Control: recording the same way as below does capture a file opened in between."""
    global _recording
    _recorded.clear()
    _recording = True
    try:
        (tmp_path / "probe.txt").write_text("x", encoding="utf-8")
    finally:
        _recording = False

    assert "open" in _recorded


@pytest.mark.anyio
async def test_a_scripted_conversation_touches_no_file_socket_or_process(tmp_path: Path) -> None:
    global _recording
    registry = build_registry(
        load_config({ENV_WORKSPACE: str(tmp_path), ENV_ENABLE_WRITE_TOOLS: "true"})
    )
    # Once without recording, so lazy imports and caches are warm. Recording starts only
    # now, after the fixtures and the event loop have made their own files and sockets.
    warm = await _scenario(tool_definitions(registry))
    _recorded.clear()
    _recording = True
    try:
        outcomes = await _scenario(tool_definitions(registry))
    finally:
        _recording = False

    assert _recorded == []
    assert outcomes == warm
    assert outcomes[1:] == [
        "ProviderRateLimitError",
        {
            "content": "done",
            "tool_calls": (),
            "stop_reason": "end_turn",
            "provider_stop_reason": None,
            "usage": {"input_tokens": 9, "output_tokens": None},
            "model": None,
        },
        "ProviderResponseError",
        "FakeProviderExhaustedError",
    ]


@pytest.fixture(autouse=True)
def _never_leave_recording_on() -> Iterator[None]:
    yield
    assert _recording is False
