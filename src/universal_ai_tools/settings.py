"""The service configuration: one file that every MCP client's server process reads.

Claude Desktop and Claude Code each start their own copy of the server over stdio; MCP gives
a local client no other way to talk to a local server. They stay one service because both
registrations carry only ``UNIVERSAL_AI_TOOLS_CONFIG``, the path of the same TOML file, and so
get the same workspace, the same capabilities, the same approval rules, and the same audit
log. Nothing about permissions is set per client.

There are two ways to configure the server:

* **File mode** (``UNIVERSAL_AI_TOOLS_CONFIG`` is set): everything comes from the file.
  Setting any of the individual variables as well is an error, so no client can quietly run
  with different settings. Every capability beyond reading files is opt-in, and every
  consequential tool call needs a human's approval (see ``universal_ai_tools.gate``). Every
  call is recorded in the audit log.
* **Environment mode** (only the individual variables): read-only tools only. It has no
  server-side approval and no audit log, so ``UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true`` is
  refused (``ENVIRONMENT_WRITES_REFUSED``); no terminal, screen, or input tools. The
  trusted-runtime protection and the isolated-start check apply as in file mode. Kept so that
  existing read-only registrations keep working; ``universal-ai-tools setup`` moves a user
  to file mode. (``create_server`` given an ``AppConfig`` directly, as tests and library code
  do, still builds the old environment-mode server; no MCP client can reach that path.)

The file is refused if it, the audit log, or the state directory is inside the workspace:
otherwise a tool that can write in the workspace could rewrite its own permissions or erase
its own record. The workspace is also refused if it contains a Claude client's configuration,
for the same reason.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from universal_ai_tools.config import (
    ENV_ENABLE_WRITE_TOOLS,
    ENV_LOG_LEVEL,
    ENV_PREFIX,
    ENV_WORKSPACE,
    AppConfig,
    ConfigError,
    LogLevel,
    check_removed_settings,
    load_config,
)
from universal_ai_tools.trusted import claude_configuration_paths

ENV_CONFIG = f"{ENV_PREFIX}CONFIG"
CONFIG_VERSION = 1
STATE_DIRECTORY_NAME = ".universal-ai-tools"
CONFIG_FILE_NAME = "config.toml"
AUDIT_FILE_NAME = "audit.jsonl"
MAX_CONFIG_BYTES = 256 * 1024

TERMINAL_DISABLED = (
    "general command execution (run_command) is disabled for safety. A started program runs "
    "with your own rights and cannot be confined on Windows by this server, so it could change "
    "the server's own code, its configuration, or its audit log, whatever the approval said. "
    "Set 'enabled = false'."
)

ENVIRONMENT_WRITES_REFUSED = (
    f"Invalid configuration: {ENV_ENABLE_WRITE_TOOLS} is true, but environment mode has no "
    "approval and no audit log, so it cannot change files. Run 'universal-ai-tools setup' to "
    f"move to a config file ({ENV_CONFIG}), where every change is approved and recorded, or "
    f"unset {ENV_ENABLE_WRITE_TOOLS} to keep this registration read-only."
)

ApprovalPolicy = Literal["every_call", "once_per_session"]
ApprovalChannelName = Literal["auto", "client", "desktop_dialog"]

_SECTION = ConfigDict(extra="forbid", strict=True, frozen=True)


def default_state_directory(home: Path | None = None) -> Path:
    """Where ``setup`` puts the config file and the audit log: ``~/.universal-ai-tools``.

    Not under ``%APPDATA%``: Claude Desktop from the Microsoft Store runs in an MSIX
    container whose writes to AppData are redirected to a private copy, which would split
    the audit log between the two clients.
    """
    return (home or Path.home()) / STATE_DIRECTORY_NAME


def default_config_path(home: Path | None = None) -> Path:
    return default_state_directory(home) / CONFIG_FILE_NAME


class ApprovalSettings(BaseModel):
    model_config = _SECTION

    channel: ApprovalChannelName = "auto"
    """``auto``: the client's own dialog (MCP elicitation) when the client supports it, else a
    Windows dialog on this PC. ``client``: only the client's dialog. ``desktop_dialog``: only
    the Windows dialog, the same in every client. When no channel is available the call is
    refused."""

    timeout_seconds: Annotated[float, Field(ge=5, le=900)] = 120
    """How long to wait for an answer. No answer means no."""

    session_grant_minutes: Annotated[float, Field(ge=1, le=480)] = 30
    """How long an approval given under ``once_per_session`` lasts, at most."""


class FilesystemSettings(BaseModel):
    model_config = _SECTION

    write: bool = False
    """Registers write_file, edit_file, create_directory, and move_path."""

    approval: ApprovalPolicy = "every_call"


class TerminalSettings(BaseModel):
    model_config = _SECTION

    enabled: bool = False
    programs: tuple[Annotated[str, Field(min_length=1, max_length=1024)], ...] = ()
    """Programs ``run_command`` may start: a name found on PATH (``python``, ``pytest``) or an
    absolute path to an executable. Shells and system-administration tools are refused."""

    default_timeout_seconds: Annotated[float, Field(ge=1, le=3600)] = 60
    max_timeout_seconds: Annotated[float, Field(ge=1, le=3600)] = 600
    max_output_bytes: Annotated[int, Field(ge=1024, le=4 * 1024 * 1024)] = 200_000
    """Per stream: stdout and stderr are each cut to this many bytes."""

    @field_validator("programs", mode="before")
    @classmethod
    def _programs_as_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class ScreenSettings(BaseModel):
    model_config = _SECTION

    enabled: bool = False
    approval: ApprovalPolicy = "once_per_session"
    max_image_width: Annotated[int, Field(ge=320, le=3840)] = 1280


class InputSettings(BaseModel):
    model_config = _SECTION

    enabled: bool = False
    approval: ApprovalPolicy = "every_call"
    allowed_processes: tuple[Annotated[str, Field(min_length=1, max_length=260)], ...] = ()
    """When not empty, input goes only to windows of these programs (``notepad.exe``)."""

    grants: bool = False
    """Adds ``request_input_grant``: one approval allows input to named programs (which must
    be in ``allowed_processes``) for a limited time. See ``desktop/grants.py``."""

    max_grant_minutes: Annotated[int, Field(ge=1, le=120)] = 30

    launchable: tuple[Annotated[str, Field(min_length=1, max_length=64)], ...] = ()
    """Programs ``launch_app`` may start (``notepad.exe``); empty means no ``launch_app``.
    Shells, script hosts, and administration tools are refused whatever this says
    (``desktop/launch.py``)."""

    @field_validator("launchable", mode="before")
    @classmethod
    def _launchable_as_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("allowed_processes", mode="before")
    @classmethod
    def _processes_as_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class SandboxSettings(BaseModel):
    """``run_in_sandbox``: commands in a throwaway Docker container (``sandbox.py``)."""

    model_config = _SECTION

    enabled: bool = False
    image: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-z0-9./_:@-]+$")] = (
        "python:3.13-slim"
    )
    """The only image commands run in. It must already be on the PC (``sandbox pull``)."""
    writable: bool = False
    """Whether commands may change files in the workspace. Read-only by default."""
    network: bool = False
    cpus: Annotated[float, Field(ge=0.5, le=16)] = 2
    memory_mb: Annotated[int, Field(ge=256, le=32768)] = 2048
    default_timeout_seconds: Annotated[int, Field(ge=5, le=3600)] = 120
    max_timeout_seconds: Annotated[int, Field(ge=5, le=3600)] = 900
    max_output_bytes: Annotated[int, Field(ge=1024, le=4 * 1024 * 1024)] = 200_000
    docker: Annotated[str, Field(max_length=1024)] = ""
    """The trusted docker.exe, set by ``universal-ai-tools sandbox trust``."""
    docker_sha256: Annotated[str, Field(pattern=r"^([0-9a-f]{64})?$")] = ""
    """Its SHA-256; a run refuses if the file no longer matches."""


class AuditSettings(BaseModel):
    model_config = _SECTION

    path: str = ""
    """The audit log. Empty means ``audit.jsonl`` next to the config file."""

    max_bytes: Annotated[int, Field(ge=64 * 1024, le=1024 * 1024 * 1024)] = 10 * 1024 * 1024
    """The log is rotated to ``<name>.1`` when it grows past this size."""


class GuardSettings(BaseModel):
    """The Claude Code guard (``universal_ai_tools.guard``), which ``guard.load_settings``
    reads without pydantic; keep the two in step."""

    model_config = _SECTION

    commands: Literal["ask", "claude"] = "ask"
    """``ask``: Claude Code asks you before every Bash or PowerShell command, whatever its own
    rules or mode say. ``claude``: Claude Code's own permission rules decide, except that a
    command naming a protected location is always asked."""


class ConfigFile(BaseModel):
    """The TOML file, as written. Unknown keys are errors, so a typo never goes unnoticed."""

    model_config = _SECTION

    version: Literal[1]
    workspace: str
    log_level: str = "INFO"
    approval: ApprovalSettings = ApprovalSettings()
    filesystem: FilesystemSettings = FilesystemSettings()
    terminal: TerminalSettings = TerminalSettings()
    screen: ScreenSettings = ScreenSettings()
    input: InputSettings = InputSettings()
    audit: AuditSettings = AuditSettings()
    guard: GuardSettings = GuardSettings()
    sandbox: SandboxSettings = SandboxSettings()


@dataclass(frozen=True)
class ServiceConfig:
    """Everything the server needs to know, whichever way it was configured."""

    app: AppConfig
    source: Literal["environment", "file"]
    config_path: Path | None = None
    audit_path: Path | None = None
    audit_max_bytes: int = 10 * 1024 * 1024
    approval: ApprovalSettings = field(default_factory=ApprovalSettings)
    filesystem: FilesystemSettings = field(default_factory=FilesystemSettings)
    terminal: TerminalSettings = field(default_factory=TerminalSettings)
    screen: ScreenSettings = field(default_factory=ScreenSettings)
    input: InputSettings = field(default_factory=InputSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)

    @classmethod
    def from_environment(cls, app: AppConfig) -> ServiceConfig:
        """Environment mode: the server as it was, with nothing new enabled."""
        return cls(
            app=app,
            source="environment",
            filesystem=FilesystemSettings(write=app.enable_write_tools),
        )

    @property
    def file_mode(self) -> bool:
        return self.source == "file"

    @property
    def workspace_root(self) -> Path:
        return self.app.workspace_root


def load_service_config(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None
) -> ServiceConfig:
    """Read the configuration from the config file, or from the environment.

    Raises:
        ConfigError: If the configuration is missing, invalid, or ambiguous.
    """
    source = os.environ if environ is None else environ
    check_removed_settings(source)
    config_path = source.get(ENV_CONFIG, "").strip()
    if not config_path:
        if not source.get(ENV_WORKSPACE, "").strip():
            raise ConfigError(
                f"Not configured. Set {ENV_CONFIG} to the path of a config file (run "
                "'universal-ai-tools setup' to create one), or set "
                f"{ENV_WORKSPACE} to the absolute path of the project directory this "
                "server is authorized to work in."
            )
        app = load_config(source, home=home)
        if app.enable_write_tools:
            raise ConfigError(ENVIRONMENT_WRITES_REFUSED)
        return ServiceConfig.from_environment(app)
    mixed = [
        name
        for name in (ENV_WORKSPACE, ENV_LOG_LEVEL, ENV_ENABLE_WRITE_TOOLS)
        if source.get(name, "").strip()
    ]
    if mixed:
        raise ConfigError(
            f"Invalid configuration: {ENV_CONFIG} is set, so every setting comes from that "
            f"file; unset {', '.join(mixed)}"
        )
    return load_config_file(Path(config_path), home=home)


def load_config_file(path: Path, *, home: Path | None = None) -> ServiceConfig:
    """Read and validate a config file.

    Raises:
        ConfigError: If the file cannot be read, is not valid TOML, does not match the
            schema, or puts the configuration where the workspace could change it.
    """
    if not path.is_absolute():
        raise ConfigError(f"{ENV_CONFIG} must be an absolute path, got '{path}'")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ConfigError(f"config file is not a regular file: '{resolved}'")
        if resolved.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"config file is larger than {MAX_CONFIG_BYTES} bytes")
        text = resolved.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(
            f"config file does not exist: '{path}'. Run 'universal-ai-tools setup' to create it."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read config file '{path}': {type(exc).__name__}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file '{resolved}' is not valid TOML: {exc}") from None
    return service_config_from_data(data, resolved, home=home)


def service_config_from_data(
    data: Mapping[str, object], config_path: Path, *, home: Path | None = None
) -> ServiceConfig:
    """Validate already-parsed config data that was read from ``config_path``."""
    try:
        parsed = ConfigFile.model_validate(dict(data))
    except ValidationError as exc:
        raise ConfigError(_format(exc, config_path)) from None
    app = load_config(
        {
            ENV_WORKSPACE: parsed.workspace,
            ENV_LOG_LEVEL: parsed.log_level,
            ENV_ENABLE_WRITE_TOOLS: "true" if parsed.filesystem.write else "false",
        },
        home=home,
    )
    terminal = parsed.terminal
    if terminal.enabled:
        raise ConfigError(
            f"Invalid configuration in '{config_path}':\n  terminal.enabled: {TERMINAL_DISABLED}"
        )
    if terminal.default_timeout_seconds > terminal.max_timeout_seconds:
        raise ConfigError(
            f"Invalid configuration in '{config_path}':\n  terminal.default_timeout_seconds "
            "must not exceed terminal.max_timeout_seconds"
        )
    audit_path = (
        Path(parsed.audit.path) if parsed.audit.path else config_path.parent / AUDIT_FILE_NAME
    )
    if not audit_path.is_absolute():
        raise ConfigError(
            f"Invalid configuration in '{config_path}':\n  audit.path must be absolute"
        )
    _refuse_self_modification(app.workspace_root, config_path, audit_path, home)
    return ServiceConfig(
        app=app,
        source="file",
        config_path=config_path,
        audit_path=audit_path,
        audit_max_bytes=parsed.audit.max_bytes,
        approval=parsed.approval,
        filesystem=parsed.filesystem,
        terminal=terminal,
        screen=parsed.screen,
        input=parsed.input,
        sandbox=parsed.sandbox,
    )


def _refuse_self_modification(
    workspace: Path, config_path: Path, audit_path: Path, home: Path | None
) -> None:
    def inside(path: Path) -> bool:
        try:
            candidate = path.resolve()
        except OSError:
            candidate = path
        return candidate == workspace or candidate.is_relative_to(workspace)

    problems = []
    if inside(config_path):
        problems.append("the config file is inside the workspace")
    if inside(config_path.parent):
        problems.append("the directory holding the config file is inside the workspace")
    if inside(audit_path):
        problems.append("the audit log is inside the workspace")
    for path in claude_configuration_paths(home):
        if inside(path):
            problems.append(f"the workspace contains Claude's settings ('{path.name}')")
    if problems:
        raise ConfigError(
            "Invalid configuration: "
            + "; ".join(problems)
            + ". Tools that can change the workspace could then change their own permissions "
            "or records. Choose a project directory that holds neither."
        )


def _format(exc: ValidationError, path: Path) -> str:
    lines = [f"Invalid configuration in '{path}':"]
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"]) or "(file)"
        lines.append(f"  {location}: {error['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


# Writing the file. ``setup`` and ``config`` generate it whole from a ``ConfigFile``.


def render_config(config: ConfigFile) -> str:
    """The TOML text of ``config``, with comments explaining every setting."""

    def text(value: str) -> str:
        if "'" not in value and not any(ord(c) < 0x20 or c == "\x7f" for c in value):
            return f"'{value}'"
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = "".join(
            c if ord(c) >= 0x20 and c != "\x7f" else f"\\u{ord(c):04x}" for c in escaped
        )
        return f'"{escaped}"'

    def boolean(value: bool) -> str:
        return "true" if value else "false"

    def number(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else repr(value)

    def strings(values: tuple[str, ...]) -> str:
        return "[" + ", ".join(text(v) for v in values) + "]"

    sb = config.sandbox
    a, f, t, s, i, au, g = (
        config.approval,
        config.filesystem,
        config.terminal,
        config.screen,
        config.input,
        config.audit,
        config.guard,
    )
    return f"""# Universal AI Tools configuration, shared by Claude Desktop and Claude Code.
# Both clients start the server with UNIVERSAL_AI_TOOLS_CONFIG pointing at this file, so
# they get the same workspace, capabilities, approvals, and audit log.
# Generated by 'universal-ai-tools'. Edit it, or use 'universal-ai-tools config'. Restart
# the Claude clients (or reconnect the server) after a change.

version = {config.version}

# The one directory the file tools may use. Not your home directory or a drive root.
workspace = {text(config.workspace)}

# DEBUG, INFO, WARNING, ERROR, or CRITICAL. Logs go to the client's MCP log (stderr).
log_level = {text(config.log_level)}

[approval]
# How consequential actions are approved: "auto" (the client's dialog if it supports
# MCP elicitation, else a Windows dialog), "client", or "desktop_dialog".
channel = {text(a.channel)}
# Seconds to wait for your answer. No answer means no.
timeout_seconds = {number(a.timeout_seconds)}
# How long an approval given under "once_per_session" lasts, in minutes.
session_grant_minutes = {number(a.session_grant_minutes)}

[filesystem]
# true adds write_file, edit_file, create_directory, and move_path (no deletion).
write = {boolean(f.write)}
# "every_call" asks before every change; "once_per_session" asks once.
approval = {text(f.approval)}

[terminal]
# Disabled for safety: a started program runs with your rights and cannot be confined, so
# it could change the server's own code or audit log. 'enabled = true' is refused.
enabled = {boolean(t.enabled)}
# Names found on PATH ("python", "pytest") or absolute paths to .exe files.
# Shells (cmd, powershell, bash) and administration tools are always refused.
programs = {strings(t.programs)}
default_timeout_seconds = {number(t.default_timeout_seconds)}
max_timeout_seconds = {number(t.max_timeout_seconds)}
# Bytes kept from stdout and from stderr.
max_output_bytes = {t.max_output_bytes}

[screen]
# true adds list_windows, capture_screen, and inspect_window (Windows only).
enabled = {boolean(s.enabled)}
approval = {text(s.approval)}
# Screenshots are scaled down to at most this width.
max_image_width = {s.max_image_width}

[input]
# true adds focus_window, type_text, press_keys, and with [screen] also click, scroll,
# invoke_element, and set_element_value (Windows only). Emergency stop: push the mouse
# into a corner of the main screen; resume with 'universal-ai-tools input resume'.
enabled = {boolean(i.enabled)}
approval = {text(i.approval)}
# If not empty, input goes only to windows of these programs, e.g. ["notepad.exe"].
allowed_processes = {strings(i.allowed_processes)}
# true lets Claude ask once for input to programs listed above, for up to max_grant_minutes,
# instead of asking before every action. A banner shows while one is active. Stop at any
# time: Ctrl+Alt+Esc, the mouse in a screen corner, or 'universal-ai-tools grants revoke'.
grants = {boolean(i.grants)}
max_grant_minutes = {i.max_grant_minutes}
# Programs launch_app may start, with no arguments, e.g. ["notepad.exe"]. Only programs
# installed under Windows or Program Files; shells and admin tools are always refused.
launchable = {strings(i.launchable)}

[audit]
# Empty means audit.jsonl next to this file. Must be outside the workspace.
path = {text(au.path)}
max_bytes = {au.max_bytes}

[guard]
# The Claude Code guard, a hook that checks Claude Code's own tools (Bash, PowerShell,
# Write, Edit, Read). "ask": Claude Code asks you before every command, whatever its own
# rules or mode say. "claude": Claude Code's own rules decide, except that a command naming
# a protected location is always asked.
commands = {text(g.commands)}

[sandbox]
# true adds run_in_sandbox: a command runs in a new, locked-down Docker container that can
# see only the workspace (at /work), has no network, no admin rights, and limited CPU,
# memory, and time, and is deleted afterwards. Every command still needs your approval.
enabled = {boolean(sb.enabled)}
# The only image commands run in; fetch it once with 'universal-ai-tools sandbox pull'.
image = {text(sb.image)}
# true lets commands change files in the workspace; false mounts it read-only.
writable = {boolean(sb.writable)}
network = {boolean(sb.network)}
cpus = {number(sb.cpus)}
memory_mb = {sb.memory_mb}
default_timeout_seconds = {sb.default_timeout_seconds}
max_timeout_seconds = {sb.max_timeout_seconds}
max_output_bytes = {sb.max_output_bytes}
# The docker program you trusted, and its fingerprint: set by 'universal-ai-tools sandbox
# trust'. A run refuses if the file has changed since.
docker = {text(sb.docker)}
docker_sha256 = {text(sb.docker_sha256)}
"""


def read_config_file(path: Path) -> ConfigFile:
    """Parse a config file into a ``ConfigFile`` without the workspace checks.

    Raises:
        ConfigError: If it cannot be read or parsed.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        return ConfigFile.model_validate(data)
    except FileNotFoundError:
        raise ConfigError(f"config file does not exist: '{path}'") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config file '{path}': {exc}") from None
    except ValidationError as exc:
        raise ConfigError(_format(exc, path)) from None


__all__ = [
    "CONFIG_VERSION",
    "ENV_CONFIG",
    "ApprovalPolicy",
    "ApprovalSettings",
    "ConfigFile",
    "FilesystemSettings",
    "GuardSettings",
    "InputSettings",
    "LogLevel",
    "SandboxSettings",
    "ScreenSettings",
    "ServiceConfig",
    "TerminalSettings",
    "default_config_path",
    "default_state_directory",
    "load_config_file",
    "load_service_config",
    "read_config_file",
    "render_config",
]
