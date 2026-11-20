"""Evidence that the agent and the adapters stay inside their boundaries.

* The adapters translate and nothing else: no tools, no workspace, no server, no files, no
  processes, no environment.
* The agent reaches tools only through the ``ToolRegistry``, and the filesystem only
  through tools. Only the orchestrator and the verifier call ``invoke``.
* Neither imports a provider SDK at module level; the default server loads none of them.
* A whole agent task starts no process and opens no socket (FakeProvider has no network).

Source checks parse the code; runtime checks use audit hooks, which see Python's own APIs
and not native code, so they are evidence and not proof.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import universal_ai_tools
from agent_support import ScriptedApproval, agent_config, call, make_workspace, registry
from universal_ai_tools.agent.orchestrator import Orchestrator
from universal_ai_tools.agent.permissions import ApprovalDecision
from universal_ai_tools.providers import FakeProvider, text_response, tool_call_response

PACKAGE = Path(universal_ai_tools.__file__).parent
AGENT = PACKAGE / "agent"
ADAPTERS = PACKAGE / "adapters"

FORBIDDEN_EVERYWHERE = frozenset(
    {
        "os",
        "subprocess",
        "multiprocessing",
        "pty",
        "shutil",
        "tempfile",
        "glob",
        "ctypes",
        "importlib",
        "runpy",
        "pickle",
        "marshal",
        "socket",
        "pathlib",
        "io",
    }
)
FORBIDDEN_NAMES = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint"})
FORBIDDEN_ATTRIBUTES = frozenset({"environ", "getenv", "putenv"})


def sources(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.py"))


def imports(tree: ast.Module) -> Iterator[tuple[ast.AST, str, list[str]]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, []
        elif isinstance(node, ast.ImportFrom):
            yield node, node.module or "", [alias.name for alias in node.names]


def test_the_packages_cannot_be_found_empty() -> None:
    assert {p.name for p in sources(AGENT)} >= {
        "config.py",
        "models.py",
        "task.py",
        "budget.py",
        "permissions.py",
        "planner.py",
        "verify.py",
        "orchestrator.py",
        "mcp_tool.py",
    }
    assert {p.name for p in sources(ADAPTERS)} == {
        "__init__.py",
        "_common.py",
        "anthropic_provider.py",
        "openai_provider.py",
        "factory.py",
    }


@pytest.mark.parametrize(
    "path", [*sources(AGENT), *sources(ADAPTERS)], ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_no_file_process_or_dynamic_code_api(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node, module, _names in imports(tree):
        root = module.partition(".")[0]
        # agent/config.py reads the environment mapping it is given, as config.py does.
        if path.name == "config.py" and root == "os":
            continue
        assert root not in FORBIDDEN_EVERYWHERE, (path.name, module, node.lineno)  # type: ignore[attr-defined]
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, (path.name, node.lineno, node.id)
        if isinstance(node, ast.Attribute) and path.name != "config.py":
            assert node.attr not in FORBIDDEN_ATTRIBUTES, (path.name, node.lineno, node.attr)


@pytest.mark.parametrize("path", sources(ADAPTERS), ids=lambda p: p.name)
def test_adapters_never_reach_tools_workspace_or_server(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "universal_ai_tools.server",
        "universal_ai_tools.tools",
        "universal_ai_tools.workspace",
        "universal_ai_tools.filesystem",
        "universal_ai_tools.config",
        "universal_ai_tools.agent.orchestrator",
        "universal_ai_tools.agent.mcp_tool",
        "mcp",
    }
    for _node, module, _names in imports(tree):
        assert module not in forbidden, (path.name, module)
        assert module.partition(".")[0] != "mcp", (path.name, module)
        if module.startswith("universal_ai_tools.agent"):
            assert path.name == "factory.py"
            assert module == "universal_ai_tools.agent.config"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"invoke", "handler", "call_tool"}, (path.name, node.attr)


@pytest.mark.parametrize("path", sources(AGENT), ids=lambda p: p.name)
def test_the_agent_imports_no_sdk_and_no_filesystem_function(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for _node, module, names in imports(tree):
        assert module.partition(".")[0] not in {"anthropic", "openai", "httpx", "httpx2"}
        if module == "universal_ai_tools.workspace":
            assert names == ["WorkspaceError"], (path.name, names)
        if module == "universal_ai_tools.filesystem":
            assert all(name.endswith("Result") for name in names), (path.name, names)
        assert module != "universal_ai_tools.adapters.anthropic_provider"
        assert module != "universal_ai_tools.adapters.openai_provider"


def test_only_the_orchestrator_and_verifier_invoke_tools() -> None:
    callers = set()
    for path in sources(AGENT):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"invoke", "handler"}:
                callers.add((path.name, node.attr))
            # ToolSpec.call(...) would run a handler without the registry's lookup.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
            ):
                callers.add((path.name, "call"))
    assert callers == {("orchestrator.py", "invoke"), ("verify.py", "invoke")}


def test_default_server_loads_no_agent_code_or_sdk(tmp_path: Path) -> None:
    probe = (
        "import json, sys\n"
        "from universal_ai_tools.config import load_config\n"
        "from universal_ai_tools.server import create_server\n"
        f"create_server(load_config({{'UNIVERSAL_AI_TOOLS_WORKSPACE': {str(tmp_path)!r}}}))\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    loaded = set(json.loads(completed.stdout))
    assert not {
        m
        for m in loaded
        if m.startswith(("universal_ai_tools.agent", "universal_ai_tools.adapters"))
    }
    assert not {m for m in loaded if m.partition(".")[0] in {"anthropic", "openai"}}


# Runtime recording

_PREFIXES = (
    "subprocess.",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.startfile",
    "socket.",
)
_recorded: list[str] = []
_recording = False


def _record(event: str, _args: tuple[object, ...]) -> None:
    if _recording and event.startswith(_PREFIXES):
        _recorded.append(event)


sys.addaudithook(_record)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_an_agent_task_starts_no_process_and_opens_no_socket(tmp_path: Path) -> None:
    global _recording
    workspace = make_workspace(tmp_path)

    async def scenario() -> str:
        provider = FakeProvider(
            [
                tool_call_response(
                    call("c1", "list_files", recursive=True),
                    call("c2", "read_file", path="docs/hello.txt"),
                    call("c3", "search_files", pattern="hello"),
                ),
                tool_call_response(call("w1", "write_file", path="docs/new.txt", content="x")),
                text_response("done"),
            ]
        )
        agent = Orchestrator(
            provider=provider, config=agent_config(), registry=registry(workspace, write=True)
        )
        task = await agent.run("x", approval=ScriptedApproval(ApprovalDecision.APPROVED))
        (workspace / "docs" / "new.txt").unlink()
        return str(task.outcome)

    assert await scenario() == "completed"  # warm caches and threads first
    _recorded.clear()
    _recording = True
    try:
        outcome = await scenario()
    finally:
        _recording = False

    assert outcome == "completed"
    assert _recorded == []


def test_recorder_sees_a_process() -> None:
    """Control: the recorder fires."""
    global _recording
    _recorded.clear()
    _recording = True
    try:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    finally:
        _recording = False
    assert "subprocess.Popen" in _recorded
