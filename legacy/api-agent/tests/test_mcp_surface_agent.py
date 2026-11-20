"""Snapshot of the MCP surface with the agent enabled.

``tests/snapshots/mcp_surface.json`` (the default and write-enabled servers) is not touched
by the agent; ``test_mcp_surface.py`` still compares against it byte for byte. This file
records the agent-enabled servers in ``tests/snapshots/mcp_surface_agent.json``: server
identity and instructions, every tool, and the results of a few scripted ``run_task``
calls through FakeProvider, with task ids replaced by a placeholder.

It also requires every tool other than ``run_task`` to be exactly the tool recorded for
the matching configuration in ``mcp_surface.json``: enabling the agent changes no existing
tool's name, title, description, schema, or annotations.

A deliberate change needs a new recording: ``python tests/test_mcp_surface_agent.py --update``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent

from agent_support import agent_config, call
from universal_ai_tools.agent.mcp_tool import AgentSetup
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.providers import (
    FakeProvider,
    FakeStep,
    ProviderRateLimitError,
    text_response,
    tool_call_response,
)
from universal_ai_tools.server import create_server

SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_surface_agent.json"
DEFAULT_SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_surface.json"

_TASK_ID = re.compile(r"task_[0-9a-f]{32}")
_PYDANTIC_DOCS = re.compile(r"https://errors\.pydantic\.dev/[^/]+/")

CALLS: list[dict[str, Any]] = [
    {"objective": "Say hello."},
    {"objective": "Read the greeting."},
    {"objective": "Fail please."},
    {"objective": ""},
    {"objective": "x", "max_steps": 1000},
]


def script() -> list[FakeStep]:
    return [
        text_response("Hello."),
        tool_call_response(call("c1", "read_file", path="docs/hello.txt")),
        text_response("It says hello world."),
        ProviderRateLimitError(retry_after_seconds=120),
    ]


CONFIGURATIONS: dict[str, tuple[bool, str, str]] = {
    "agent_read_only": (False, "anthropic", "default"),
    "agent_write_enabled": (True, "openai", "write_enabled"),
}


def _normalize(text: str) -> str:
    return _PYDANTIC_DOCS.sub(
        "https://errors.pydantic.dev/<version>/", _TASK_ID.sub("task_<id>", text)
    )


def _normalized(value: Any) -> Any:
    return json.loads(_normalize(json.dumps(value)))


def _build_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "hello.txt").write_bytes(b"hello world\n")
    return workspace


async def _observe(workspace: Path, write: bool, provider_name: str) -> dict[str, Any]:
    environ = {ENV_WORKSPACE: str(workspace)}
    if write:
        environ[ENV_ENABLE_WRITE_TOOLS] = "true"
    agent = AgentSetup(
        config=agent_config(provider=provider_name, model="model-under-test"),
        provider=FakeProvider(script()),
    )
    server = create_server(load_config(environ), agent=agent)
    results: list[dict[str, Any]] = []
    async with Client(server) as client:
        assert client.server_info is not None
        tools = (await client.list_tools()).tools
        for arguments in CALLS:
            observed: dict[str, Any] = {"arguments": arguments}
            try:
                result = await client.call_tool("run_task", arguments)
            except MCPError as exc:
                observed["protocol_error"] = str(exc)
            else:
                observed["is_error"] = result.is_error
                observed["structured_content"] = result.structured_content
                observed["text"] = [
                    block.text if isinstance(block, TextContent) else block.type
                    for block in result.content
                ]
            results.append(observed)
        surface = {
            "server_info": client.server_info.model_dump(mode="json", by_alias=True),
            "instructions": client.instructions,
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools],
            "calls": results,
        }
    normalized: dict[str, Any] = _normalized(surface)
    return normalized


async def capture(root: Path) -> dict[str, Any]:
    surface: dict[str, Any] = {}
    for label, (write, provider_name, _) in CONFIGURATIONS.items():
        surface[label] = await _observe(_build_workspace(root / label), write, provider_name)
    loaded: dict[str, Any] = json.loads(json.dumps(surface))
    return loaded


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_agent_surface_matches_the_recording(tmp_path: Path) -> None:
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    observed = await capture(tmp_path)

    assert observed.keys() == recorded.keys()
    for label, expected in recorded.items():
        actual = observed[label]
        assert actual["instructions"] == expected["instructions"], label
        for actual_tool, expected_tool in zip(actual["tools"], expected["tools"], strict=True):
            assert actual_tool == expected_tool, (label, expected_tool["name"])
        for actual_call, expected_call in zip(actual["calls"], expected["calls"], strict=True):
            assert actual_call == expected_call, (label, expected_call["arguments"])
        assert actual == expected, label


def test_existing_tools_are_unchanged_by_the_agent() -> None:
    agent = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    default = json.loads(DEFAULT_SNAPSHOT.read_text(encoding="utf-8"))

    for label, (_, _, default_label) in CONFIGURATIONS.items():
        tools = agent[label]["tools"]
        assert tools[-1]["name"] == "run_task"
        assert tools[:-1] == default[default_label]["tools"], label
        assert agent[label]["server_info"] == default[default_label]["server_info"]
        assert agent[label]["instructions"].startswith(default[default_label]["instructions"])


def _update() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        surface = anyio.run(capture, Path(scratch))
    SNAPSHOT.write_text(
        json.dumps(surface, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        raise SystemExit("usage: python tests/test_mcp_surface_agent.py --update")
    _update()
