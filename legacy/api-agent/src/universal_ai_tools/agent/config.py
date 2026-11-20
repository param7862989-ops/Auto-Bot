"""Validated configuration of the agent, separate from ``AppConfig``.

The agent is off unless ``ENV_ENABLE_AGENT_TOOLS`` is exactly ``true`` (case-insensitive).
``load_agent_config`` reads that one variable first and, when the agent is off, returns
``None`` without reading anything else: no provider, model, key, or limit is needed, read,
or validated, and the server behaves exactly as it does without this module.

When the agent is on, only the variables named by the ``ENV_*`` constants below are read.
The provider's API key is read only from this package's own variable for the selected
provider (``UNIVERSAL_AI_TOOLS_ANTHROPIC_API_KEY`` or ``UNIVERSAL_AI_TOOLS_OPENAI_API_KEY``),
never from ``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, or any other generic variable. Base
URLs and proxies are not configurable at all: each adapter talks to its provider's public
endpoint, and ignores proxy variables.

The key is held as a ``SecretStr``: its ``repr`` and ``str`` are masked, and no error
message, log line, or task record contains it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from universal_ai_tools.config import ENV_PREFIX, ConfigError

ENV_ENABLE_AGENT_TOOLS = f"{ENV_PREFIX}ENABLE_AGENT_TOOLS"
ENV_AGENT_PROVIDER = f"{ENV_PREFIX}AGENT_PROVIDER"
ENV_AGENT_MODEL = f"{ENV_PREFIX}AGENT_MODEL"
ENV_ANTHROPIC_API_KEY = f"{ENV_PREFIX}ANTHROPIC_API_KEY"
ENV_OPENAI_API_KEY = f"{ENV_PREFIX}OPENAI_API_KEY"
ENV_REQUEST_TIMEOUT = f"{ENV_PREFIX}AGENT_REQUEST_TIMEOUT_SECONDS"
ENV_MAX_RETRIES = f"{ENV_PREFIX}AGENT_MAX_RETRIES"
ENV_MAX_TASK_SECONDS = f"{ENV_PREFIX}AGENT_MAX_TASK_SECONDS"
ENV_MAX_STEPS = f"{ENV_PREFIX}AGENT_MAX_STEPS"
ENV_MAX_TOOL_CALLS = f"{ENV_PREFIX}AGENT_MAX_TOOL_CALLS"
ENV_MAX_INPUT_TOKENS = f"{ENV_PREFIX}AGENT_MAX_INPUT_TOKENS"
ENV_MAX_OUTPUT_TOKENS = f"{ENV_PREFIX}AGENT_MAX_OUTPUT_TOKENS"
ENV_MAX_TOOL_RESULT_CHARS = f"{ENV_PREFIX}AGENT_MAX_TOOL_RESULT_CHARS"
ENV_MAX_CONTEXT_CHARS = f"{ENV_PREFIX}AGENT_MAX_CONTEXT_CHARS"
ENV_MAX_REPEATED_CALLS = f"{ENV_PREFIX}AGENT_MAX_REPEATED_CALLS"
ENV_APPROVAL_TIMEOUT = f"{ENV_PREFIX}AGENT_APPROVAL_TIMEOUT_SECONDS"

ProviderName = Literal["anthropic", "openai"]

API_KEY_VARIABLES: Mapping[str, str] = {
    "anthropic": ENV_ANTHROPIC_API_KEY,
    "openai": ENV_OPENAI_API_KEY,
}

# Optional settings: environment variable -> field name.
_OPTIONAL: Mapping[str, str] = {
    ENV_REQUEST_TIMEOUT: "request_timeout_seconds",
    ENV_MAX_RETRIES: "max_retries",
    ENV_MAX_TASK_SECONDS: "max_task_seconds",
    ENV_MAX_STEPS: "max_steps",
    ENV_MAX_TOOL_CALLS: "max_tool_calls",
    ENV_MAX_INPUT_TOKENS: "max_input_tokens",
    ENV_MAX_OUTPUT_TOKENS: "max_output_tokens",
    ENV_MAX_TOOL_RESULT_CHARS: "max_tool_result_chars",
    ENV_MAX_CONTEXT_CHARS: "max_context_chars",
    ENV_MAX_REPEATED_CALLS: "max_repeated_calls",
    ENV_APPROVAL_TIMEOUT: "approval_timeout_seconds",
}
_FIELD_TO_ENV: Mapping[str, str] = {
    "provider": ENV_AGENT_PROVIDER,
    "model": ENV_AGENT_MODEL,
    **{field: env for env, field in _OPTIONAL.items()},
}

# The same pattern as ``providers.models.ModelName``, so a configured model is always one a
# ``ProviderRequest`` accepts.
_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$"


class AgentConfigError(ConfigError):
    """The agent is enabled but its configuration is missing or invalid."""


class AgentConfig(BaseModel):
    """Immutable, validated agent configuration. Only exists when the agent is enabled."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    provider: ProviderName
    model: str = Field(pattern=_MODEL_PATTERN)
    api_key: SecretStr = Field(repr=False)
    """The selected provider's key. Masked in ``repr``, ``str``, and JSON dumps."""

    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    """Wall-clock limit on one provider request, including reading the whole answer."""
    max_retries: int = Field(default=2, ge=0, le=5)
    """Retries of one request after a timeout, connection failure, overload, or rate limit."""
    max_task_seconds: float = Field(default=300.0, gt=0, le=3600)
    """Wall-clock limit on a whole task, including waiting for approval."""
    max_steps: int = Field(default=20, ge=1, le=200)
    """Model turns (provider requests) per task."""
    max_tool_calls: int = Field(default=50, ge=1, le=1000)
    """Tool calls the model may request per task, whether or not they run."""
    max_input_tokens: int = Field(default=500_000, ge=1, le=50_000_000)
    """Input tokens summed over every request of a task."""
    max_output_tokens: int = Field(default=32_000, ge=1, le=5_000_000)
    """Output tokens summed over every request of a task."""
    max_output_tokens_per_request: int = Field(default=4096, ge=1, le=128_000)
    """Upper bound sent with each request. Not an environment setting."""
    max_tool_result_chars: int = Field(default=50_000, ge=256, le=2_000_000)
    """Characters of one tool result shown to the model; longer results are cut."""
    max_context_chars: int = Field(default=400_000, ge=1_000, le=20_000_000)
    """Characters of the whole conversation, checked before each request."""
    max_repeated_calls: int = Field(default=3, ge=1, le=50)
    """How often the same tool call (name and arguments) or the same model turn may occur."""
    max_consecutive_failures: int = Field(default=5, ge=1, le=50)
    """Failed or refused tool calls in a row before the task stops. Not an environment setting."""
    approval_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    """How long to wait for a human answer to an approval request. No answer is a denial."""

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value


def parse_flag(value: str) -> bool:
    """``true`` or ``false``, case-insensitive, surrounding whitespace ignored.

    Raises:
        ValueError: For anything else. The message does not quote the value.
    """
    folded = value.strip().casefold()
    if folded not in ("true", "false"):
        raise ValueError("must be 'true' or 'false' (case-insensitive)")
    return folded == "true"


def agent_tools_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``ENV_ENABLE_AGENT_TOOLS`` turns the agent on. Reads only that variable.

    Raises:
        AgentConfigError: If the value is neither ``true`` nor ``false``.
    """
    source = os.environ if environ is None else environ
    raw = source.get(ENV_ENABLE_AGENT_TOOLS, "").strip()
    if not raw:
        return False
    try:
        return parse_flag(raw)
    except ValueError as exc:
        raise AgentConfigError(
            f"Invalid configuration:\n  {ENV_ENABLE_AGENT_TOOLS}: {exc}"
        ) from None


def load_agent_config(environ: Mapping[str, str] | None = None) -> AgentConfig | None:
    """Return the agent configuration, or ``None`` when the agent is not enabled.

    Raises:
        AgentConfigError: If the agent is enabled and a setting is missing or invalid. No
            value is echoed, and the API key never appears in the message.
    """
    source = os.environ if environ is None else environ
    if not agent_tools_enabled(source):
        return None

    provider = source.get(ENV_AGENT_PROVIDER, "").strip()
    model = source.get(ENV_AGENT_MODEL, "").strip()
    problems: list[str] = []
    if not provider:
        problems.append(f"  {ENV_AGENT_PROVIDER}: is not set; use 'anthropic' or 'openai'")
    elif provider.casefold() not in API_KEY_VARIABLES:
        problems.append(
            f"  {ENV_AGENT_PROVIDER}: unsupported provider; use 'anthropic' or 'openai'"
        )
    if not model:
        problems.append(f"  {ENV_AGENT_MODEL}: is not set; name the provider's model to use")
    key_variable = API_KEY_VARIABLES.get(provider.casefold())
    key = source.get(key_variable, "").strip() if key_variable else ""
    if key_variable and not key:
        problems.append(
            f"  {key_variable}: is not set; the {provider.casefold()} API key is required"
        )
    if problems:
        raise AgentConfigError("Invalid configuration:\n" + "\n".join(problems))

    data: dict[str, object] = {"provider": provider, "model": model, "api_key": SecretStr(key)}
    for variable, field in _OPTIONAL.items():
        raw = source.get(variable, "").strip()
        if not raw:
            continue
        number = _parse_number(raw, integer=field not in _FLOAT_FIELDS)
        if number is None:
            problems.append(f"  {variable}: must be a {_kind(field)}")
        else:
            data[field] = number
    if problems:
        raise AgentConfigError("Invalid configuration:\n" + "\n".join(problems))

    try:
        return AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise AgentConfigError(_format_validation_error(exc)) from None


_FLOAT_FIELDS = frozenset(
    {"request_timeout_seconds", "max_task_seconds", "approval_timeout_seconds"}
)


def _kind(field: str) -> str:
    return "number" if field in _FLOAT_FIELDS else "whole number"


def _parse_number(raw: str, *, integer: bool) -> int | float | None:
    if integer:
        return int(raw) if raw.isascii() and raw.isdigit() else None
    try:
        number = float(raw)
    except ValueError:
        return None
    # float() accepts "nan", "inf", and "1e999"; none is a meaningful limit.
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        field = str(error["loc"][0]) if error["loc"] else ""
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {_FIELD_TO_ENV.get(field, field)}: {message}")
    return "\n".join(lines)
