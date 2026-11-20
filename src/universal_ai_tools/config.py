"""Validated application configuration.

Configuration is read from an explicit allowlist of environment variables
(see the ``ENV_*`` constants below). No
other environment variables are read, stored, or exposed.

The workspace root is the single directory the filesystem tools are allowed
to work inside (see ``universal_ai_tools.workspace``). It must be authorized
explicitly; there is no default, and broad locations such as the user's home
directory or a filesystem root are rejected. Loading configuration only
validates the path; it does not read anything inside it.

Tools that change files are off unless ``ENV_ENABLE_WRITE_TOOLS`` is exactly
``true`` (case-insensitive). Anything other than ``true`` or ``false`` is an error,
so a typo can never enable them silently.

Command execution does not exist, so nothing here can enable it. The settings that
used to control it (``REMOVED_SETTINGS``) are checked only so that a value left in a
registration stops the server, instead of being ignored by a server that was expected
to behave differently.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    ValidationError,
    ValidationInfo,
    field_validator,
)

ENV_PREFIX = "UNIVERSAL_AI_TOOLS_"
ENV_WORKSPACE = f"{ENV_PREFIX}WORKSPACE"
ENV_LOG_LEVEL = f"{ENV_PREFIX}LOG_LEVEL"
ENV_ENABLE_WRITE_TOOLS = f"{ENV_PREFIX}ENABLE_WRITE_TOOLS"

# Settings of the removed command tool. Any non-blank value is a configuration error.
# (The terminal capability that exists now is configured only in the config file, under
# different names; see universal_ai_tools.settings.)
REMOVED_SETTINGS = (f"{ENV_PREFIX}ENABLE_COMMAND_TOOLS", f"{ENV_PREFIX}COMMAND_ALLOWLIST")

# Settings of the removed API-key agent (archived in legacy/api-agent). Claude, in Claude
# Desktop or Claude Code, is the orchestrator; this package never calls a model API and never
# reads an API key. Any non-blank value is a configuration error, so a stale registration
# fails visibly and a key left in a client configuration is noticed.
REMOVED_AGENT_SETTINGS = (
    f"{ENV_PREFIX}ENABLE_AGENT_TOOLS",
    f"{ENV_PREFIX}AGENT_PROVIDER",
    f"{ENV_PREFIX}AGENT_MODEL",
    f"{ENV_PREFIX}ANTHROPIC_API_KEY",
    f"{ENV_PREFIX}OPENAI_API_KEY",
    f"{ENV_PREFIX}AGENT_REQUEST_TIMEOUT_SECONDS",
    f"{ENV_PREFIX}AGENT_MAX_RETRIES",
    f"{ENV_PREFIX}AGENT_MAX_TASK_SECONDS",
    f"{ENV_PREFIX}AGENT_MAX_STEPS",
    f"{ENV_PREFIX}AGENT_MAX_TOOL_CALLS",
    f"{ENV_PREFIX}AGENT_MAX_INPUT_TOKENS",
    f"{ENV_PREFIX}AGENT_MAX_OUTPUT_TOKENS",
    f"{ENV_PREFIX}AGENT_MAX_TOOL_RESULT_CHARS",
    f"{ENV_PREFIX}AGENT_MAX_CONTEXT_CHARS",
    f"{ENV_PREFIX}AGENT_MAX_REPEATED_CALLS",
    f"{ENV_PREFIX}AGENT_APPROVAL_TIMEOUT_SECONDS",
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_FIELD_TO_ENV = {
    "workspace_root": ENV_WORKSPACE,
    "log_level": ENV_LOG_LEVEL,
    "enable_write_tools": ENV_ENABLE_WRITE_TOOLS,
}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


class AppConfig(BaseModel):
    """Immutable, validated application configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    """Absolute, resolved path of the explicitly authorized workspace directory."""

    log_level: LogLevel = "INFO"
    """Log level for messages written to stderr."""

    enable_write_tools: StrictBool = False
    """Whether tools that create files (``write_file``) are registered. Off by default."""

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("enable_write_tools", mode="before")
    @classmethod
    def _parse_enable_write_tools(cls, value: object) -> object:
        # Only the words true and false. Pydantic's lax parsing would also accept
        # values such as "1", "yes", or "on"; StrictBool rejects any non-bool left over.
        if isinstance(value, str):
            folded = value.strip().casefold()
            if folded not in ("true", "false"):
                raise ValueError("must be 'true' or 'false' (case-insensitive)")
            return folded == "true"
        return value

    @field_validator("workspace_root", mode="after")
    @classmethod
    def _validate_workspace_root(cls, value: Path, info: ValidationInfo) -> Path:
        if not value.is_absolute():
            raise ValueError(f"must be an absolute path, got '{value}'")
        try:
            resolved = value.resolve(strict=True)
        except FileNotFoundError:
            raise ValueError(f"directory does not exist: '{value}'") from None
        except OSError as exc:
            raise ValueError(f"cannot access '{value}': {exc.strerror}") from None
        if not resolved.is_dir():
            raise ValueError(f"is not a directory: '{resolved}'")
        if resolved == Path(resolved.anchor):
            raise ValueError(f"must not be a filesystem or drive root: '{resolved}'")

        context = info.context if isinstance(info.context, Mapping) else {}
        home = Path(context.get("home") or Path.home()).resolve()
        if home.is_relative_to(resolved):
            raise ValueError(
                f"must not be the home directory or one of its parents: '{resolved}'; "
                "authorize a specific project directory instead"
            )
        return resolved


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> AppConfig:
    """Build an ``AppConfig`` from allowlisted environment variables.

    Args:
        environ: Mapping to read from. Defaults to ``os.environ``. Only
            ``ENV_WORKSPACE``, ``ENV_LOG_LEVEL``, ``ENV_ENABLE_WRITE_TOOLS``, the
            ``REMOVED_SETTINGS``, and the ``REMOVED_AGENT_SETTINGS`` are read. Optional
            settings that are empty or whitespace-only are treated as unset.
        home: Home directory used for the "too broad" check. Defaults to
            ``Path.home()``; overridable for tests.

    Raises:
        ConfigError: If a required setting is missing, any setting is invalid, or a
            removed setting has a value. The value is not echoed.
    """
    source = os.environ if environ is None else environ
    check_removed_settings(source)

    workspace = source.get(ENV_WORKSPACE, "").strip()
    if not workspace:
        raise ConfigError(
            f"{ENV_WORKSPACE} is not set. Set it to the absolute path of the project "
            "directory this server is authorized to work in."
        )

    data: dict[str, str] = {"workspace_root": workspace}
    log_level = source.get(ENV_LOG_LEVEL, "").strip()
    if log_level:
        data["log_level"] = log_level
    enable_write_tools = source.get(ENV_ENABLE_WRITE_TOOLS, "").strip()
    if enable_write_tools:
        data["enable_write_tools"] = enable_write_tools

    try:
        return AppConfig.model_validate(data, context={"home": home})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None


def check_removed_settings(source: Mapping[str, str]) -> None:
    """Refuse a value in any setting of a removed feature. Values are never echoed.

    Raises:
        ConfigError: If any of ``REMOVED_SETTINGS`` or ``REMOVED_AGENT_SETTINGS`` is set.
    """
    problems = [
        f"  {name}: command execution was removed and cannot be enabled; unset this variable"
        for name in REMOVED_SETTINGS
        if source.get(name, "").strip()
    ]
    problems += [
        f"  {name}: the built-in API agent was removed; Claude Desktop or Claude Code is the "
        "orchestrator and no API key is used; unset this variable"
        for name in REMOVED_AGENT_SETTINGS
        if source.get(name, "").strip()
    ]
    if problems:
        raise ConfigError("Invalid configuration:\n" + "\n".join(problems))


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for error in exc.errors(include_url=False, include_input=False):
        field = str(error["loc"][0]) if error["loc"] else ""
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {_FIELD_TO_ENV.get(field, field)}: {message}")
    return "\n".join(lines)
