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
ENV_ENABLE_COMMAND_TOOLS = f"{ENV_PREFIX}ENABLE_COMMAND_TOOLS"
ENV_COMMAND_ALLOWLIST = f"{ENV_PREFIX}COMMAND_ALLOWLIST"

CommandName = Literal["pytest", "ruff-check", "mypy-check", "git-status", "git-diff-check"]
ALLOWED_COMMAND_NAMES = frozenset(
    ("pytest", "ruff-check", "mypy-check", "git-status", "git-diff-check")
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_FIELD_TO_ENV = {
    "workspace_root": ENV_WORKSPACE,
    "log_level": ENV_LOG_LEVEL,
    "enable_write_tools": ENV_ENABLE_WRITE_TOOLS,
    "enable_command_tools": ENV_ENABLE_COMMAND_TOOLS,
    "command_allowlist": ENV_COMMAND_ALLOWLIST,
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

    enable_command_tools: StrictBool = False
    """Whether allowlisted command profiles can be executed. Off by default."""

    command_allowlist: tuple[CommandName, ...] = ()
    """Fixed command profile names permitted by the operator."""

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

    @field_validator("enable_command_tools", mode="before")
    @classmethod
    def _parse_enable_command_tools(cls, value: object) -> object:
        if isinstance(value, str):
            folded = value.strip().casefold()
            if folded not in ("true", "false"):
                raise ValueError("must be 'true' or 'false' (case-insensitive)")
            return folded == "true"
        return value

    @field_validator("command_allowlist", mode="before")
    @classmethod
    def _parse_command_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            names = tuple(item.strip().casefold() for item in value.split(",") if item.strip())
            unknown = sorted(set(names) - ALLOWED_COMMAND_NAMES)
            if unknown:
                raise ValueError("contains an unknown command profile")
            if len(set(names)) != len(names):
                raise ValueError("must not contain duplicate command profiles")
            return names
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
            ``ENV_WORKSPACE``, ``ENV_LOG_LEVEL``, ``ENV_ENABLE_WRITE_TOOLS``,
            ``ENV_ENABLE_COMMAND_TOOLS``, and ``ENV_COMMAND_ALLOWLIST``
            are read. Optional settings that are empty or whitespace-only are
            treated as unset.
        home: Home directory used for the "too broad" check. Defaults to
            ``Path.home()``; overridable for tests.

    Raises:
        ConfigError: If a required setting is missing or any setting is invalid.
    """
    source = os.environ if environ is None else environ

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
    enable_command_tools = source.get(ENV_ENABLE_COMMAND_TOOLS, "").strip()
    if enable_command_tools:
        data["enable_command_tools"] = enable_command_tools
    command_allowlist = source.get(ENV_COMMAND_ALLOWLIST, "").strip()
    if command_allowlist:
        data["command_allowlist"] = command_allowlist

    try:
        return AppConfig.model_validate(data, context={"home": home})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for error in exc.errors(include_url=False, include_input=False):
        field = str(error["loc"][0]) if error["loc"] else ""
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {_FIELD_TO_ENV.get(field, field)}: {message}")
    return "\n".join(lines)
