"""Snapshot of everything an MCP client can observe of the server.

``tests/snapshots/mcp_surface.json`` was recorded from the server as it was before the tool
registry existed, when every tool was registered by hand in ``server.py``. The test rebuilds
the same observations and requires them to be identical: the server identity and
instructions, every field of ``tools/list`` (names, titles, descriptions, input and output
schemas, annotations), and the results of a fixed sequence of calls. The calls cover
successes, refusals, schema violations, values the MCP SDK coerces, and arguments it
ignores, so a change in how a call is validated or dispatched shows up here too.

A deliberate change to the MCP surface needs a new recording, made on purpose and reviewed
as part of the change: ``python tests/test_mcp_surface.py --update``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent

from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.filesystem import FilesystemLimits
from universal_ai_tools.server import create_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_surface.json"

# pydantic error messages link to documentation for the installed pydantic version.
_PYDANTIC_DOCS = re.compile(r"https://errors\.pydantic\.dev/[^/]+/")

# Lower limits than the defaults, so the snapshot also shows that limits reach the
# tool descriptions.
_SMALL_LIMITS = FilesystemLimits(
    max_list_entries=50,
    max_read_bytes=4096,
    max_search_results=20,
    max_files_scanned=10,
    max_search_file_bytes=2048,
    search_timeout_seconds=1.5,
    max_write_bytes=1024,
    max_response_bytes=65536,
)

Call = tuple[str, dict[str, object]]

READ_CALLS: list[Call] = [
    ("get_platform_info", {}),
    ("get_platform_info", {"unexpected": 1}),
    ("list_files", {}),
    ("list_files", {"recursive": True}),
    ("list_files", {"path": "docs", "max_entries": 1}),
    ("list_files", {"recursive": "true"}),
    ("list_files", {"max_entries": "2"}),
    ("list_files", {"max_entries": 0}),
    ("list_files", {"max_entries": 1001}),
    ("list_files", {"recursive": "maybe"}),
    ("list_files", {"path": ".."}),
    ("list_files", {"path": "missing"}),
    ("list_files", {"path": ".git"}),
    ("list_files", {"path": "docs/hello.txt"}),
    ("list_files", {"unexpected": "ignored", "path": "docs"}),
    ("read_file", {"path": "docs/hello.txt"}),
    ("read_file", {"path": "docs\\sub\\deep.txt"}),
    ("read_file", {"path": "docs/control.txt"}),
    ("read_file", {"path": ".env"}),
    ("read_file", {"path": ".git/config"}),
    ("read_file", {"path": "../outside.txt"}),
    ("read_file", {"path": "/etc/passwd"}),
    ("read_file", {"path": "docs"}),
    ("read_file", {"path": "missing.txt"}),
    ("read_file", {}),
    ("read_file", {"path": 5}),
    ("search_files", {"pattern": "hello"}),
    ("search_files", {"pattern": "HELLO", "case_sensitive": False}),
    ("search_files", {"pattern": "hello", "path": "docs/sub"}),
    ("search_files", {"pattern": "hello", "max_results": 1}),
    ("search_files", {"pattern": "hello", "case_sensitive": "false"}),
    ("search_files", {"pattern": "KEY"}),
    ("search_files", {"pattern": ""}),
    ("search_files", {"pattern": "x" * 201}),
    ("search_files", {"pattern": "a\nb"}),
    ("search_files", {"pattern": "x", "max_results": 501}),
    ("search_files", {"pattern": "hello", "path": ".git"}),
    ("search_files", {}),
    ("write_file", {"path": "docs/new.txt", "content": "new"}),
    ("run_command", {"executable": "pytest"}),
]

WRITE_CALLS: list[Call] = [
    *READ_CALLS,
    ("write_file", {"path": "docs/new.txt", "content": "first\r\nline\n"}),
    ("write_file", {"path": "docs/new.txt", "content": "again"}),
    ("read_file", {"path": "docs/new.txt"}),
    ("write_file", {"path": "docs/empty.txt", "content": ""}),
    ("write_file", {"path": ".env.local", "content": "x"}),
    ("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}),
    ("write_file", {"path": "node_modules/x.js", "content": "x"}),
    ("write_file", {"path": "missing/new.txt", "content": "x"}),
    ("write_file", {"path": "docs", "content": "x"}),
    ("write_file", {"path": "docs/", "content": "x"}),
    ("write_file", {"path": "docs/nul.txt", "content": "a\x00b"}),
    ("write_file", {"path": "docs/new2.txt"}),
    ("write_file", {"content": "x"}),
    ("write_file", {"path": "docs/new3.txt", "content": 5}),
    ("write_file", {"path": "docs/new4.txt", "content": "x", "mode": "overwrite"}),
]

SMALL_LIMIT_CALLS: list[Call] = [
    ("list_files", {"max_entries": 1000}),
    ("read_file", {"path": "docs/big.txt"}),
    ("search_files", {"pattern": "line", "max_results": 500}),
    ("write_file", {"path": "docs/too-big.txt", "content": "x" * 1025}),
]

CONFIGURATIONS: dict[str, tuple[bool, FilesystemLimits | None, list[Call]]] = {
    "default": (False, None, READ_CALLS),
    "write_enabled": (True, None, WRITE_CALLS),
    "write_enabled_small_limits": (True, _SMALL_LIMITS, SMALL_LIMIT_CALLS),
}


def _build_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    files: dict[str, bytes] = {
        "docs/hello.txt": b"hello world\nsecond line\n",
        "docs/sub/deep.txt": b"deep hello\n",
        "docs/control.txt": b"bell\x07 and escape\x1b[31m\n",
        "docs/big.txt": b"line\n" * 1000,
        ".env": b"KEY=value\n",
        ".git/config": b"[core]\n",
        "notes.md": b"# Notes\nhello again\n",
    }
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (workspace / "empty").mkdir()
    (workspace / "node_modules").mkdir()
    return workspace


def _normalize(text: str) -> str:
    return _PYDANTIC_DOCS.sub("https://errors.pydantic.dev/<version>/", text)


async def _observe(
    workspace: Path, enable_write_tools: bool, limits: FilesystemLimits | None, calls: list[Call]
) -> dict[str, object]:
    environ = {ENV_WORKSPACE: str(workspace)}
    if enable_write_tools:
        environ[ENV_ENABLE_WRITE_TOOLS] = "true"
    config = load_config(environ)
    server = create_server(config) if limits is None else create_server(config, limits)

    results: list[dict[str, object]] = []
    async with Client(server) as client:
        assert client.server_info is not None
        tools = (await client.list_tools()).tools
        for name, arguments in calls:
            observed: dict[str, object] = {"tool": name, "arguments": arguments}
            try:
                result = await client.call_tool(name, arguments)
            except MCPError as exc:
                observed["protocol_error"] = _normalize(str(exc))
            else:
                observed["is_error"] = result.is_error
                observed["structured_content"] = result.structured_content
                observed["text"] = [
                    _normalize(block.text) if isinstance(block, TextContent) else block.type
                    for block in result.content
                ]
            results.append(observed)
        return {
            "server_info": client.server_info.model_dump(mode="json", by_alias=True),
            "instructions": client.instructions,
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools],
            "calls": results,
        }


async def capture(root: Path) -> dict[str, object]:
    """Observe every configuration, each against its own fresh synthetic workspace."""
    surface: dict[str, object] = {}
    for label, (enable_write_tools, limits, calls) in CONFIGURATIONS.items():
        workspace = _build_workspace(root / label)
        surface[label] = await _observe(workspace, enable_write_tools, limits, calls)
    # A round trip through JSON, so the comparison sees exactly what the file holds.
    loaded: dict[str, object] = json.loads(json.dumps(surface))
    return loaded


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_surface_matches_the_recording(tmp_path: Path) -> None:
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    observed = await capture(tmp_path)

    assert observed.keys() == recorded.keys()
    for label, expected in recorded.items():
        actual = observed[label]
        assert isinstance(actual, dict)
        assert actual["server_info"] == expected["server_info"], label
        assert actual["instructions"] == expected["instructions"], label
        assert [tool["name"] for tool in actual["tools"]] == [
            tool["name"] for tool in expected["tools"]
        ], label
        for actual_tool, expected_tool in zip(actual["tools"], expected["tools"], strict=True):
            assert actual_tool == expected_tool, (label, expected_tool["name"])
        for actual_call, expected_call in zip(actual["calls"], expected["calls"], strict=True):
            assert actual_call == expected_call, (label, expected_call["tool"])
        assert actual == expected, label


def test_recording_covers_every_configuration() -> None:
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert set(recorded) == set(CONFIGURATIONS)
    assert [tool["name"] for tool in recorded["default"]["tools"]] == [
        "get_platform_info",
        "list_files",
        "read_file",
        "search_files",
    ]
    assert [tool["name"] for tool in recorded["write_enabled"]["tools"]] == [
        "get_platform_info",
        "list_files",
        "read_file",
        "search_files",
        "write_file",
    ]


def _update() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        surface = anyio.run(capture, Path(scratch))
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps(surface, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        raise SystemExit("usage: python tests/test_mcp_surface.py --update")
    _update()
