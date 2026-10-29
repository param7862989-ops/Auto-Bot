"""MCP server for Universal AI Tools over local stdio transport.

This module has three layers:

* Tool logic: plain functions such as ``get_platform_info`` here and the
  filesystem tools in ``universal_ai_tools.filesystem``. They know nothing
  about MCP and can be tested directly.
* Server assembly: ``create_server`` registers tool logic on an
  ``MCPServer`` instance, binding filesystem tools to the configured
  workspace and turning ``WorkspaceError`` into MCP tool errors.
* Process entry point: ``main`` loads configuration, configures logging to
  stderr, and runs the server over stdio.

Importing this module does not start the server, read configuration, or
configure logging. Launch it with the ``universal-ai-tools`` console script
or ``python -m universal_ai_tools.server``.

stdout carries MCP protocol traffic only. All logs go to stderr.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from universal_ai_tools import APP_NAME, __version__, filesystem
from universal_ai_tools.commands import (
    MAX_TIMEOUT_SECONDS,
    CommandError,
    CommandResult,
    run_command,
)
from universal_ai_tools.config import AppConfig, CommandName, ConfigError, load_config
from universal_ai_tools.filesystem import (
    DEFAULT_LIMITS,
    DEFAULT_LIST_ENTRIES,
    DEFAULT_SEARCH_RESULTS,
    MAX_LIST_ENTRIES,
    MAX_PATTERN_LENGTH,
    MAX_SEARCH_RESULTS,
    FilesystemLimits,
    ListFilesResult,
    ReadFileResult,
    SearchFilesResult,
    WriteFileResult,
)
from universal_ai_tools.workspace import Workspace, WorkspaceError

logger = logging.getLogger(__name__)

_PATH_RULES = (
    "Paths are relative to that workspace; absolute paths, '..', and symlinks are refused."
)
READ_ONLY_INSTRUCTIONS = (
    "Universal AI Tools is a local-first developer automation platform. "
    "It exposes read-only tools: `get_platform_info` for diagnostics, and `list_files`, "
    "`read_file`, and `search_files` for one authorized workspace directory. "
    f"{_PATH_RULES} File writing is disabled: the `write_file` tool is not registered."
)
WRITE_ENABLED_INSTRUCTIONS = (
    "Universal AI Tools is a local-first developer automation platform. "
    "It exposes `get_platform_info` for diagnostics; the read-only tools `list_files`, "
    "`read_file`, and `search_files`; and `write_file`, which creates new text files but "
    "never overwrites, appends to, renames, or deletes existing files. All filesystem "
    f"tools work inside one authorized workspace directory. {_PATH_RULES}"
)


def server_instructions(
    *,
    write_tools_enabled: bool,
    command_tools_enabled: bool = False,
    command_allowlist: tuple[CommandName, ...] = (),
) -> str:
    """Return the instructions that describe exactly the tools a server registers."""
    instructions = WRITE_ENABLED_INSTRUCTIONS if write_tools_enabled else READ_ONLY_INSTRUCTIONS
    if command_tools_enabled:
        instructions += (
            " Command execution is enabled only for these fixed profiles: "
            f"{', '.join(command_allowlist)}. No shell strings or caller arguments are accepted."
        )
    return instructions


_PATH_HELP = (
    "Path relative to the workspace root, using '/' or '\\' separators. '.' is the root. "
    "Absolute paths, drive letters, and '..' are rejected."
)
DirectoryArg = Annotated[str, Field(description=f"Directory to use. {_PATH_HELP}")]
FileArg = Annotated[str, Field(description=f"File to read. {_PATH_HELP}")]
NewFileArg = Annotated[
    str,
    Field(
        description="New file to create. Nothing may exist at this path yet, and its parent "
        f"directory must already exist. {_PATH_HELP}"
    ),
]
ContentArg = Annotated[
    str,
    Field(
        description="Complete text of the new file. Written as UTF-8 exactly as given: no BOM "
        "is added and newlines are not changed. May be empty. Must not contain NUL characters "
        "or exceed the byte limit stated in the tool description."
    ),
]


class PlatformInfo(BaseModel):
    """Diagnostic information about the running platform."""

    name: str = Field(description="Application name.")
    version: str = Field(description="Application version.")
    status: Literal["operational"] = Field(
        description="Operational status. 'operational' means the server is running "
        "and handling requests."
    )


def get_platform_info() -> PlatformInfo:
    """Return the application name, version, and operational status.

    Read-only and side-effect free. Does not expose configuration,
    environment variables, or filesystem paths.
    """
    return PlatformInfo(name=APP_NAME, version=__version__, status="operational")


def create_server(config: AppConfig, limits: FilesystemLimits = DEFAULT_LIMITS) -> MCPServer:
    """Build the MCP server with its tools registered.

    The read-only tools are always registered. ``write_file`` is registered only
    when ``config.enable_write_tools`` is true. Filesystem tools are confined to
    ``config.workspace_root`` and bounded by ``limits``. The server is returned
    unstarted; call ``run()`` on it to serve.
    """
    server = MCPServer(
        name=APP_NAME,
        title="Universal AI Tools",
        version=__version__,
        instructions=server_instructions(
            write_tools_enabled=config.enable_write_tools,
            command_tools_enabled=config.enable_command_tools and bool(config.command_allowlist),
            command_allowlist=config.command_allowlist,
        ),
        log_level=config.log_level,
    )
    server.add_tool(
        get_platform_info,
        name="get_platform_info",
        title="Get platform info",
        description=(
            "Return diagnostic information about the Universal AI Tools server: its "
            "application name, version, and operational status. Takes no arguments, "
            "is read-only, and does not access files, the network, or secrets. Use it "
            "to confirm the server is connected and to check which version is running."
        ),
        annotations=ToolAnnotations(
            title="Get platform info",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    workspace = Workspace(config.workspace_root)
    _add_filesystem_tools(server, workspace, limits)
    if config.enable_write_tools:
        _add_write_tools(server, workspace, limits)
    if config.enable_command_tools and config.command_allowlist:
        _add_command_tool(server, workspace, config.command_allowlist)
    return server


def _add_command_tool(
    server: MCPServer, workspace: Workspace, allowed_commands: tuple[CommandName, ...]
) -> None:
    """Register the opt-in command tool, restricted to fixed configured profiles."""

    def run_allowed_command(
        command: Annotated[
            CommandName,
            Field(
                description="Fixed command profile. Caller-supplied shell text and arguments "
                "are not accepted."
            ),
        ],
        timeout_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_TIMEOUT_SECONDS,
                description=f"Timeout in seconds (1 to {MAX_TIMEOUT_SECONDS}).",
            ),
        ] = 30,
    ) -> CommandResult:
        try:
            return run_command(
                workspace,
                command,
                allowed_commands=allowed_commands,
                timeout_seconds=timeout_seconds,
            )
        except CommandError as exc:
            raise ToolError(str(exc)) from None

    server.add_tool(
        run_allowed_command,
        name="run_command",
        title="Run allowlisted command",
        description=(
            "Run one configured fixed command profile in the authorized workspace. The caller "
            "cannot supply an executable path, shell string, or extra arguments. Output is "
            "capped at 64 KiB per stream, the process inherits no API-key environment values, "
            "and the timeout is capped at 120 seconds. Tests may modify workspace files as an "
            "ordinary consequence of running project tests; review profiles before enabling."
        ),
        annotations=ToolAnnotations(
            title="Run allowlisted command",
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )


def _add_filesystem_tools(
    server: MCPServer, workspace: Workspace, limits: FilesystemLimits
) -> None:
    def list_files(
        path: DirectoryArg = ".",
        recursive: Annotated[
            bool,
            Field(
                description="List all descendants depth-first. Excluded directories such as "
                ".git, .venv, node_modules, and caches are listed but not descended into."
            ),
        ] = False,
        max_entries: Annotated[
            int,
            Field(ge=1, le=MAX_LIST_ENTRIES, description="Maximum number of entries to return."),
        ] = DEFAULT_LIST_ENTRIES,
    ) -> ListFilesResult:
        with _tool_errors():
            return filesystem.list_files(
                workspace, path, recursive=recursive, max_entries=max_entries, limits=limits
            )

    def read_file(path: FileArg) -> ReadFileResult:
        with _tool_errors():
            return filesystem.read_file(workspace, path, limits=limits)

    def search_files(
        pattern: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_PATTERN_LENGTH,
                description="Literal text to find (not a regular expression), on one line.",
            ),
        ],
        path: DirectoryArg = ".",
        case_sensitive: Annotated[bool, Field(description="Match case exactly.")] = True,
        max_results: Annotated[
            int,
            Field(ge=1, le=MAX_SEARCH_RESULTS, description="Maximum number of matches to return."),
        ] = DEFAULT_SEARCH_RESULTS,
    ) -> SearchFilesResult:
        with _tool_errors():
            return filesystem.search_files(
                workspace,
                pattern,
                path,
                case_sensitive=case_sensitive,
                max_results=max_results,
                limits=limits,
            )

    server.add_tool(
        list_files,
        name="list_files",
        title="List files",
        description=(
            "List files and directories in the authorized workspace, or in a subdirectory of "
            "it given as a relative path. Returns relative paths with each entry's type "
            "('file', 'directory', 'symlink', 'other') and file sizes. Symlinks and junctions "
            "are reported but never followed. Results are sorted and capped at max_entries "
            f"(at most {limits.max_list_entries}); 'truncated' is true when more exist."
        ),
        annotations=_read_only("List files"),
    )
    server.add_tool(
        read_file,
        name="read_file",
        title="Read file",
        description=(
            "Read a UTF-8 text file in the authorized workspace, given a relative path. "
            f"Files over {limits.max_read_bytes} bytes, binary files, invalid UTF-8, "
            "directories, symlinks, devices, and credential files such as .env or *.pem "
            "are refused with an error."
        ),
        annotations=_read_only("Read file"),
    )
    server.add_tool(
        search_files,
        name="search_files",
        title="Search files",
        description=(
            "Search text files in the authorized workspace (or a relative subdirectory) for "
            "literal text. Returns relative paths, 1-based line and column numbers, and short "
            "snippets. Skips symlinks, binary and non-UTF-8 files, files over "
            f"{limits.max_search_file_bytes} bytes, credential files, and directories such as "
            ".git, .venv, node_modules, and caches. Stops after max_results matches, "
            f"{limits.max_files_scanned} files, or {limits.search_timeout_seconds:g} seconds; "
            "'truncated' and 'truncation_reason' say when it stopped early."
        ),
        annotations=_read_only("Search files"),
    )


def _add_write_tools(server: MCPServer, workspace: Workspace, limits: FilesystemLimits) -> None:
    """Register the tools that change files. Only called when explicitly enabled."""

    def write_file(path: NewFileArg, content: ContentArg) -> WriteFileResult:
        with _tool_errors():
            return filesystem.write_file(workspace, path, content, limits=limits)

    server.add_tool(
        write_file,
        name="write_file",
        title="Create new file",
        description=(
            "Create a NEW UTF-8 text file in the authorized workspace, given a relative path "
            "and its complete content. This tool changes the filesystem, but it only creates "
            "files: if anything already exists at the path, it fails and leaves it untouched. "
            "It never overwrites, appends to, renames, or deletes files, and it does not create "
            "directories, so the parent directory must already exist. Content is written "
            "exactly as given, without a BOM, up to "
            f"{limits.max_write_bytes} bytes as UTF-8; NUL characters are refused. Paths "
            "outside the workspace, symlinks and junctions, credential files such as .env or "
            "*.pem, sensitive directories such as .ssh, and excluded directories such as .git, "
            ".venv, node_modules, and caches are refused with an error. Returns the path and "
            "the number of bytes written."
        ),
        annotations=ToolAnnotations(
            title="Create new file",
            read_only_hint=False,
            # Additive only: it never modifies or removes anything that already exists.
            destructive_hint=False,
            # A repeated call fails because the file now exists.
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )


def _read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Report anticipated filesystem failures to the client as tool errors."""
    try:
        yield
    except WorkspaceError as exc:
        raise ToolError(str(exc)) from None


def _configure_logging(level: str) -> None:
    # Configured before MCPServer is constructed, so the SDK's own
    # logging.basicConfig call is a no-op and every record goes to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Load configuration and serve MCP over stdio until the client disconnects."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    _configure_logging(config.log_level)
    logger.info("Starting %s %s on stdio transport", APP_NAME, __version__)
    logger.info(
        "Write tools are %s", "ENABLED (write_file)" if config.enable_write_tools else "disabled"
    )
    logger.info(
        "Command tools are %s",
        ", ".join(config.command_allowlist)
        if config.enable_command_tools and config.command_allowlist
        else "disabled",
    )
    create_server(config).run(transport="stdio")
    logger.info("%s stopped", APP_NAME)


if __name__ == "__main__":
    main()
