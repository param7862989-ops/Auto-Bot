"""The tool registry: what it contains, how it validates, and what it calls.

``tests/test_mcp_surface.py`` shows that the MCP server built from the registry is the
server that existed before it. These tests cover the registry as a consumer other than
MCP will use it: by name, with arguments that nothing has validated yet.
"""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

import universal_ai_tools.tools as tools_module
from support import link_directory
from universal_ai_tools import APP_NAME, __version__, server
from universal_ai_tools import filesystem as filesystem_module
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, AppConfig, load_config
from universal_ai_tools.filesystem import (
    FilesystemLimits,
    ListFilesResult,
    ReadFileResult,
    SearchFilesResult,
    WriteFileResult,
)
from universal_ai_tools.server import create_server
from universal_ai_tools.tools import (
    STRICT_INPUT,
    PlatformInfo,
    ToolInputError,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    UnknownToolError,
    build_registry,
)
from universal_ai_tools.workspace import WorkspaceError

READ_ONLY_TOOLS = ("get_platform_info", "list_files", "read_file", "search_files")
WRITE_ENABLED_TOOLS = (*READ_ONLY_TOOLS, "write_file")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    (path / "docs").mkdir(parents=True)
    (path / "docs" / "hello.txt").write_bytes(b"hello world\n")
    (path / ".git").mkdir()
    (path / ".git" / "config").write_bytes(b"[core]\n")
    (path / ".env").write_bytes(b"KEY=value\n")
    return path


@pytest.fixture
def read_only(workspace: Path) -> AppConfig:
    return load_config({ENV_WORKSPACE: str(workspace)})


@pytest.fixture
def write_enabled(workspace: Path) -> AppConfig:
    return load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: "true"})


@pytest.fixture
def registry(write_enabled: AppConfig) -> ToolRegistry:
    """The largest registry the configuration allows: write tools enabled."""
    return build_registry(write_enabled)


def _workspace_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# Construction


def test_default_registry_holds_exactly_the_read_only_tools(read_only: AppConfig) -> None:
    registry = build_registry(read_only)

    assert registry.names == READ_ONLY_TOOLS
    assert [spec.name for spec in registry] == list(READ_ONLY_TOOLS)
    assert len(registry) == len(READ_ONLY_TOOLS)
    assert "write_file" not in registry


def test_write_enabled_registry_adds_only_write_file(write_enabled: AppConfig) -> None:
    assert build_registry(write_enabled).names == WRITE_ENABLED_TOOLS


@pytest.mark.parametrize("value", ["", "false", "FALSE", " false "])
def test_write_file_is_absent_unless_enabled(workspace: Path, value: str) -> None:
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: value})

    registry = build_registry(config)

    assert "write_file" not in registry
    with pytest.raises(UnknownToolError, match="unknown tool: 'write_file'"):
        registry.invoke("write_file", {"path": "docs/new.txt", "content": "x"})
    assert not (workspace / "docs" / "new.txt").exists()


def test_risks_and_annotations(registry: ToolRegistry) -> None:
    risks = {spec.name: spec.risk for spec in registry}

    assert risks == {
        "get_platform_info": ToolRisk.READ_ONLY,
        "list_files": ToolRisk.READ_ONLY,
        "read_file": ToolRisk.READ_ONLY,
        "search_files": ToolRisk.READ_ONLY,
        "write_file": ToolRisk.CREATE,
    }
    for spec in registry:
        assert spec.annotations.read_only_hint is (spec.risk is ToolRisk.READ_ONLY), spec.name
        assert spec.annotations.destructive_hint is False, spec.name
        assert spec.annotations.open_world_hint is False, spec.name
        assert spec.annotations.title == spec.title, spec.name


def test_every_input_model_is_strict_and_closed(registry: ToolRegistry) -> None:
    for spec in registry:
        config = spec.input_model.model_config
        assert config.get("strict") is True, spec.name
        assert config.get("extra") == "forbid", spec.name
        assert spec.input_model.model_json_schema().get("additionalProperties") is False


def test_output_models(registry: ToolRegistry) -> None:
    outputs = {spec.name: spec.output_model for spec in registry}

    assert outputs == {
        "get_platform_info": PlatformInfo,
        "list_files": ListFilesResult,
        "read_file": ReadFileResult,
        "search_files": SearchFilesResult,
        "write_file": WriteFileResult,
    }


def test_limits_reach_the_descriptions(write_enabled: AppConfig) -> None:
    limits = FilesystemLimits(max_read_bytes=4321, max_write_bytes=1234)

    registry = build_registry(write_enabled, limits)

    assert "4321 bytes" in registry.get("read_file").description
    assert "1234 bytes" in registry.get("write_file").description


def test_registry_is_immutable(registry: ToolRegistry) -> None:
    for name in ("register", "add", "add_tool", "remove", "update", "__setitem__", "__delitem__"):
        assert not hasattr(registry, name), name
    with pytest.raises(TypeError):
        registry._tools["evil"] = registry.get("read_file")  # type: ignore[index]
    with pytest.raises(AttributeError):
        registry.get("read_file").handler = print  # type: ignore[misc]


# ToolSpec and ToolRegistry invariants


class _Echo(BaseModel):
    model_config = STRICT_INPUT

    text: str


class _Echoed(BaseModel):
    text: str
    tool: str


def _spec(name: str, calls: list[str], **overrides: object) -> ToolSpec[_Echo, _Echoed]:
    def handler(arguments: _Echo) -> _Echoed:
        calls.append(name)
        return _Echoed(text=arguments.text, tool=name)

    fields: dict[str, object] = {
        "name": name,
        "title": name.title(),
        "description": "Echo text.",
        "input_model": _Echo,
        "output_model": _Echoed,
        "handler": handler,
        "risk": ToolRisk.READ_ONLY,
        "annotations": ToolAnnotations(
            title=name.title(), read_only_hint=True, destructive_hint=False
        ),
    }
    fields.update(overrides)
    return ToolSpec(**fields)  # type: ignore[arg-type]


def test_dispatch_calls_only_the_named_handler() -> None:
    calls: list[str] = []
    registry = ToolRegistry([_spec("first", calls), _spec("second", calls)])

    result = registry.invoke("second", {"text": "hi"})

    assert calls == ["second"]
    assert result == _Echoed(text="hi", tool="second")


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolRegistry([_spec("same", []), _spec("same", [])])


@pytest.mark.parametrize("name", ["", "Upper", "has space", "dash-ed", "1st", "x" * 65, "é"])
def test_invalid_tool_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        _spec(name, [])


@pytest.mark.parametrize(
    "config",
    [
        ConfigDict(extra="forbid"),
        ConfigDict(strict=True),
        ConfigDict(strict=True, extra="ignore"),
        ConfigDict(strict=False, extra="forbid"),
    ],
)
def test_lax_input_models_are_refused(config: ConfigDict) -> None:
    lax = type("Lax", (BaseModel,), {"__annotations__": {"text": str}, "model_config": config})

    with pytest.raises(ValueError, match="must be strict and forbid extra fields"):
        _spec("lax", [], input_model=lax)


@pytest.mark.parametrize(
    ("risk", "annotations"),
    [
        (ToolRisk.READ_ONLY, ToolAnnotations(title="Bad", read_only_hint=False)),
        (ToolRisk.READ_ONLY, ToolAnnotations(title="Bad", destructive_hint=False)),
        (ToolRisk.CREATE, ToolAnnotations(title="Bad", read_only_hint=True)),
        (
            ToolRisk.CREATE,
            ToolAnnotations(title="Bad", read_only_hint=False, destructive_hint=True),
        ),
        (ToolRisk.CREATE, ToolAnnotations(title="Bad", read_only_hint=False)),
    ],
)
def test_annotations_must_agree_with_risk(risk: ToolRisk, annotations: ToolAnnotations) -> None:
    with pytest.raises(ValueError, match=r"contradicts risk|no tool may be destructive"):
        _spec("bad", [], title="Bad", risk=risk, annotations=annotations)


def test_annotation_title_must_match() -> None:
    with pytest.raises(ValueError, match="title must match"):
        _spec("named", [], annotations=ToolAnnotations(title="Other", read_only_hint=True))


def test_a_handler_returning_the_wrong_type_is_an_error() -> None:
    def handler(arguments: _Echo) -> _Echoed:
        return arguments  # type: ignore[return-value]

    registry = ToolRegistry([_spec("wrong", [], handler=handler)])

    with pytest.raises(TypeError, match="the handler returned _Echo"):
        registry.invoke("wrong", {"text": "x"})


# Unknown tools


@pytest.mark.parametrize("name", ["run_command", "", "READ_FILE", "read_file ", "read_file\x00"])
def test_unknown_tools_are_refused(registry: ToolRegistry, name: str) -> None:
    with pytest.raises(UnknownToolError, match=r"^unknown tool: '"):
        registry.invoke(name, {})


def test_non_string_tool_names_are_refused(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownToolError, match="unknown tool: 'int'"):
        registry.invoke(5, {})  # type: ignore[arg-type]


def test_unknown_tool_refusal_is_escaped_and_bounded(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownToolError) as excinfo:
        registry.invoke("evil\x1b[31m\n" + "x" * 10_000, {})

    message = str(excinfo.value)
    assert "\x1b" not in message
    assert "\n" not in message
    assert "\\x1b" in message
    assert len(message) < 200


# Strict validation


@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        # Values the MCP SDK would coerce. The registry does not.
        ("list_files", {"recursive": "true"}, "recursive"),
        ("list_files", {"recursive": 1}, "recursive"),
        ("list_files", {"max_entries": "2"}, "max_entries"),
        ("list_files", {"max_entries": 2.0}, "max_entries"),
        ("list_files", {"max_entries": True}, "max_entries"),
        ("search_files", {"pattern": "x", "case_sensitive": "false"}, "case_sensitive"),
        ("search_files", {"pattern": "x", "case_sensitive": 0}, "case_sensitive"),
        ("search_files", {"pattern": 5}, "pattern"),
        ("search_files", {"pattern": b"hello"}, "pattern"),
        ("read_file", {"path": 5}, "path"),
        ("read_file", {"path": b"docs/hello.txt"}, "path"),
        ("read_file", {"path": ["docs/hello.txt"]}, "path"),
        ("read_file", {"path": None}, "path"),
        ("read_file", {"path": Path("docs/hello.txt")}, "path"),
        ("write_file", {"path": "docs/new.txt", "content": 5}, "content"),
        ("write_file", {"path": "docs/new.txt", "content": b"bytes"}, "content"),
        ("write_file", {"path": "docs/new.txt", "content": ["x"]}, "content"),
        # Bounds the MCP schema advertises.
        ("list_files", {"max_entries": 0}, "max_entries"),
        ("list_files", {"max_entries": 1001}, "max_entries"),
        ("search_files", {"pattern": ""}, "pattern"),
        ("search_files", {"pattern": "x" * 201}, "pattern"),
        ("search_files", {"pattern": "x", "max_results": 501}, "max_results"),
        # Required fields.
        ("read_file", {}, "path"),
        ("search_files", {}, "pattern"),
        ("write_file", {"path": "docs/new.txt"}, "content"),
        ("write_file", {"content": "x"}, "path"),
    ],
)
def test_invalid_arguments_are_refused_without_calling_the_handler(
    registry: ToolRegistry,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, object],
    field: str,
) -> None:
    def never(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the filesystem was reached with invalid arguments")

    for name in ("list_files", "read_file", "search_files", "write_file"):
        monkeypatch.setattr(filesystem_module, name, never)

    with pytest.raises(ToolInputError, match=f"^invalid arguments for '{tool}': .*{field}"):
        registry.invoke(tool, arguments)
    assert not (workspace / "docs" / "new.txt").exists()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_platform_info", {"unexpected": 1}),
        ("list_files", {"path": "docs", "unexpected": "x"}),
        ("read_file", {"path": "docs/hello.txt", "follow_symlinks": True}),
        ("search_files", {"pattern": "hello", "regex": True}),
        ("write_file", {"path": "docs/new.txt", "content": "x", "mode": "overwrite"}),
        ("write_file", {"path": "docs/new.txt", "content": "x", "overwrite": True}),
    ],
)
def test_unexpected_properties_are_refused(
    registry: ToolRegistry, workspace: Path, tool: str, arguments: dict[str, object]
) -> None:
    before = _workspace_snapshot(workspace)

    with pytest.raises(ToolInputError, match="Extra inputs are not permitted"):
        registry.invoke(tool, arguments)

    assert _workspace_snapshot(workspace) == before


@pytest.mark.parametrize("arguments", [None, "path=docs", ["docs"], 5, {1: "docs"}])
def test_arguments_must_be_an_object_with_string_keys(
    registry: ToolRegistry, arguments: object
) -> None:
    with pytest.raises(ToolInputError, match="expected an object"):
        registry.invoke("list_files", arguments)  # type: ignore[arg-type]


def test_refusals_never_quote_argument_values(registry: ToolRegistry) -> None:
    sentinel = "sk-do-not-echo-0123456789"

    with pytest.raises(ToolInputError) as excinfo:
        registry.invoke("list_files", {"max_entries": sentinel, "recursive": sentinel})

    assert sentinel not in str(excinfo.value)
    assert "max_entries" in str(excinfo.value)
    assert "recursive" in str(excinfo.value)


def test_unexpected_property_names_are_escaped_and_bounded(registry: ToolRegistry) -> None:
    arguments: dict[str, object] = {f"bad\x1b{index}" + "x" * 500: 1 for index in range(50)}

    with pytest.raises(ToolInputError) as excinfo:
        registry.invoke("list_files", arguments)

    message = str(excinfo.value)
    assert "\x1b" not in message
    assert "and 40 more" in message
    assert len(message) < 2000


def test_defaults_apply_when_arguments_are_omitted(registry: ToolRegistry) -> None:
    result = registry.invoke("list_files", {})

    assert isinstance(result, ListFilesResult)
    assert result.path == "."
    assert [entry.path for entry in result.entries] == [".env", ".git", "docs"]


# Dispatch to the real handlers


def test_platform_info(registry: ToolRegistry) -> None:
    assert registry.invoke("get_platform_info", {}) == PlatformInfo(
        name=APP_NAME, version=__version__, status="operational"
    )


def test_list_files(registry: ToolRegistry) -> None:
    result = registry.invoke("list_files", {"path": "docs", "recursive": True, "max_entries": 5})

    assert isinstance(result, ListFilesResult)
    assert [entry.path for entry in result.entries] == ["docs/hello.txt"]


def test_read_file(registry: ToolRegistry) -> None:
    result = registry.invoke("read_file", {"path": "docs/hello.txt"})

    assert isinstance(result, ReadFileResult)
    assert result.content == "hello world\n"
    assert result.trust == "untrusted"


def test_search_files(registry: ToolRegistry) -> None:
    result = registry.invoke(
        "search_files", {"pattern": "HELLO", "case_sensitive": False, "max_results": 3}
    )

    assert isinstance(result, SearchFilesResult)
    assert [(match.path, match.line) for match in result.matches] == [("docs/hello.txt", 1)]
    assert result.skipped.sensitive_files == 1


def test_write_file_creates_and_never_overwrites(registry: ToolRegistry, workspace: Path) -> None:
    result = registry.invoke("write_file", {"path": "docs/new.txt", "content": "a\r\nb"})

    assert result == WriteFileResult(path="docs/new.txt", bytes_written=4)
    assert (workspace / "docs" / "new.txt").read_bytes() == b"a\r\nb"
    with pytest.raises(WorkspaceError, match="already exists"):
        registry.invoke("write_file", {"path": "docs/new.txt", "content": "replaced"})
    assert (workspace / "docs" / "new.txt").read_bytes() == b"a\r\nb"


# The filesystem security checks still apply to every call through the registry.


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("read_file", {"path": ".env"}, "reading sensitive files is not allowed"),
        ("read_file", {"path": ".git/config"}, "repository metadata"),
        ("read_file", {"path": "../outside.txt"}, "'..'"),
        ("read_file", {"path": "/etc/passwd"}, "must be relative"),
        ("read_file", {"path": "C:\\Windows\\win.ini"}, "must be relative"),
        ("list_files", {"path": ".git"}, "repository metadata"),
        ("list_files", {"path": ".."}, "'..'"),
        ("search_files", {"pattern": "x", "path": ".git"}, "repository metadata"),
        ("search_files", {"pattern": "a\nb"}, "single line"),
        ("write_file", {"path": ".env.local", "content": "x"}, "sensitive files"),
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "x"}, "repository metadata"),
        ("write_file", {"path": "../escape.txt", "content": "x"}, "'..'"),
        ("write_file", {"path": "missing/new.txt", "content": "x"}, "parent directory"),
        ("write_file", {"path": "docs/nul.txt", "content": "a\x00b"}, "NUL"),
    ],
)
def test_filesystem_refusals_still_apply(
    registry: ToolRegistry, workspace: Path, tool: str, arguments: dict[str, object], message: str
) -> None:
    before = _workspace_snapshot(workspace)

    with pytest.raises(WorkspaceError, match=message):
        registry.invoke(tool, arguments)

    assert _workspace_snapshot(workspace) == before


def test_links_are_still_refused(registry: ToolRegistry, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside\n")
    link_directory(workspace / "link", outside)

    with pytest.raises(WorkspaceError, match="symlink or junction"):
        registry.invoke("read_file", {"path": "link/secret.txt"})
    with pytest.raises(WorkspaceError, match="symlink or junction"):
        registry.invoke("write_file", {"path": "link/new.txt", "content": "x"})
    assert not (outside / "new.txt").exists()


# The MCP server is built from the registry and nothing else.


@pytest.fixture
async def fake_server(
    read_only: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Client, list[str]]]:
    calls: list[str] = []

    def fake_build_registry(
        _config: AppConfig, _limits: FilesystemLimits, **_options: object
    ) -> ToolRegistry:
        return ToolRegistry([_spec("echo", calls)])

    monkeypatch.setattr(server, "build_registry", fake_build_registry)
    async with Client(create_server(read_only)) as connected:
        yield connected, calls


@pytest.mark.anyio
async def test_server_registers_exactly_the_registry(
    fake_server: tuple[Client, list[str]],
) -> None:
    client, calls = fake_server

    tools = (await client.list_tools()).tools
    result = await client.call_tool("echo", {"text": "hi"})
    missing = await client.call_tool("read_file", {"path": "docs/hello.txt"})

    assert [tool.name for tool in tools] == ["echo"]
    assert result.is_error is False
    assert result.structured_content == {"text": "hi", "tool": "echo"}
    assert calls == ["echo"]
    assert missing.is_error is True


@pytest.mark.anyio
async def test_every_mcp_call_goes_through_invoke(
    read_only: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked: list[tuple[str, dict[str, object]]] = []
    original = ToolRegistry.invoke

    def recording_invoke(
        self: ToolRegistry, name: str, arguments: Mapping[str, object]
    ) -> BaseModel:
        invoked.append((name, dict(arguments)))
        return original(self, name, arguments)

    monkeypatch.setattr(ToolRegistry, "invoke", recording_invoke)
    async with Client(create_server(read_only)) as client:
        await client.call_tool("get_platform_info", {})
        await client.call_tool("list_files", {"path": "docs"})
        await client.call_tool("read_file", {"path": ".env"})
        await client.call_tool("search_files", {"pattern": "hello"})

    assert invoked == [
        ("get_platform_info", {}),
        ("list_files", {"path": "docs", "recursive": False, "max_entries": 200}),
        ("read_file", {"path": ".env"}),
        (
            "search_files",
            {"pattern": "hello", "path": ".", "case_sensitive": True, "max_results": 100},
        ),
    ]


@pytest.mark.anyio
async def test_mcp_calls_keep_the_sdk_validation(write_enabled: AppConfig, workspace: Path) -> None:
    """MCP calls are validated by the SDK, which coerces and ignores, before the registry.

    This is the behaviour the server had before the registry, kept on purpose: clients that
    send JSON-encoded strings keep working. ``tests/test_mcp_surface.py`` records it in full.
    """
    async with Client(create_server(write_enabled)) as client:
        coerced = await client.call_tool("list_files", {"recursive": "true", "max_entries": "2"})
        extra = await client.call_tool(
            "write_file", {"path": "docs/new.txt", "content": "x", "mode": "overwrite"}
        )
        invalid = await client.call_tool("list_files", {"max_entries": "lots"})

    assert coerced.is_error is False
    assert coerced.structured_content is not None
    assert len(coerced.structured_content["entries"]) == 2
    # The SDK drops the unknown property; the tool still never overwrites anything.
    assert extra.is_error is False
    assert (workspace / "docs" / "new.txt").read_bytes() == b"x"
    assert invalid.is_error is True


@pytest.mark.anyio
async def test_mcp_refusal_from_the_registry_is_a_tool_error(read_only: AppConfig) -> None:
    async with Client(create_server(read_only)) as client:
        result = await client.call_tool("read_file", {"path": ".env"})

    assert result.is_error is True
    assert json.dumps(result.content[0].model_dump()).count("reading sensitive files") == 1


# This module is a registry, not a second way to reach the filesystem.

_FORBIDDEN_MODULES = frozenset(
    {"os", "pathlib", "io", "shutil", "tempfile", "glob", "socket", "subprocess", "importlib"}
)
_FORBIDDEN_NAMES = frozenset({"open", "exec", "eval", "compile", "__import__"})


def test_registry_module_has_no_filesystem_or_dynamic_code_access() -> None:
    source = Path(tools_module.__file__).read_text(encoding="utf-8")

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported = {alias.name.partition(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported = {(node.module or "").partition(".")[0]}
        else:
            imported = set()
        assert not imported & _FORBIDDEN_MODULES, imported
        if isinstance(node, ast.Name):
            assert node.id not in _FORBIDDEN_NAMES, (node.id, node.lineno)
