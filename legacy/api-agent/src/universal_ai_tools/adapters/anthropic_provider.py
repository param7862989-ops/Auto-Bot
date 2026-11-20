"""``AnthropicProvider``: the Anthropic Messages API behind the neutral ``Provider`` interface.

Request conversion:

* System messages are joined into the ``system`` parameter.
* User messages and tool results become ``user`` turns; consecutive ones are merged into
  one turn, tool results first, because the API requires turns to alternate and wants
  ``tool_result`` blocks at the start of the turn that answers a ``tool_use``.
* Assistant messages become ``assistant`` turns with a ``text`` block and ``tool_use``
  blocks. ``ToolResultMessage.is_error`` becomes the block's ``is_error``.
* Tool definitions become ``{name, description, input_schema}``.

Response conversion: ``text`` blocks are joined; ``tool_use`` blocks become ``ToolCall``s,
but only when the stop reason is ``tool_use``: a ``tool_use`` block in a response cut off
for any other reason is incomplete and is dropped. ``thinking`` blocks are ignored. Any
other block type, a missing or unknown field, or an input that is not a JSON object makes
the whole response invalid (``ProviderResponseError``).

The SDK is imported when the provider is constructed, never at module import. The client
is built so that it takes nothing from the environment: the key, the endpoint, and the
timeout are explicit, ``ANTHROPIC_CUSTOM_HEADERS`` is discarded, OpenTelemetry is off, the
HTTP client ignores proxy variables, and the SDK's own retries are off (``with_retries``
retries instead, bounded and cancellable).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, SecretStr, ValidationError

from universal_ai_tools.adapters._common import (
    build_http_client,
    classify_sdk_error,
    quiet_sdk_loggers,
    with_retries,
)
from universal_ai_tools.providers.base import Provider
from universal_ai_tools.providers.errors import ProviderResponseError
from universal_ai_tools.providers.models import (
    AssistantMessage,
    Message,
    ProviderRequest,
    ProviderResponse,
    StopReason,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

if TYPE_CHECKING:
    import httpx2

logger = logging.getLogger(__name__)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

_STOP_REASONS: Mapping[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_calls",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "content_filter",
    "pause_turn": "other",
}
_IGNORED_BLOCKS = frozenset({"thinking", "redacted_thinking"})
# The API refuses an empty tool result string in some cases; an empty result is said so.
EMPTY_RESULT = "(empty result)"


class AnthropicProvider(Provider):
    """Anthropic Messages API adapter."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        timeout_seconds: float,
        max_retries: int,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the SDK client. ``transport`` replaces the network, for tests.

        Raises:
            ImportError: If the ``anthropic`` package is not installed.
        """
        import anthropic

        quiet_sdk_loggers()
        self._sdk = anthropic
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=ANTHROPIC_BASE_URL,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=build_http_client(transport),
            open_telemetry=False,
        )
        # The constructor merges ANTHROPIC_CUSTOM_HEADERS from the environment into the
        # headers of every request. No header may come from the environment.
        self._client._custom_headers = {}

    @property
    def name(self) -> str:
        return "anthropic"

    async def aclose(self) -> None:
        await self._client.close()

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        params = to_anthropic_params(request)

        async def attempt() -> object:
            try:
                return await self._client.messages.create(**params)
            except Exception as exc:
                error = classify_sdk_error(
                    exc,
                    status_error=self._sdk.APIStatusError,
                    timeout_error=self._sdk.APITimeoutError,
                    connection_error=self._sdk.APIConnectionError,
                    validation_error=self._sdk.APIResponseValidationError,
                    sdk_error=self._sdk.AnthropicError,
                )
                if error is None:
                    raise
                raise error from None

        message = await with_retries(
            attempt,
            provider=self.name,
            max_retries=self._max_retries,
            timeout_seconds=self._timeout_seconds,
        )
        return from_anthropic_message(message)


# Request conversion


def to_anthropic_params(request: ProviderRequest) -> dict[str, Any]:
    """The keyword arguments of ``messages.create`` for ``request``."""
    system = [m.content for m in request.messages if isinstance(m, SystemMessage)]
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_output_tokens,
        "messages": _turns(request.messages),
    }
    if system:
        params["system"] = "\n\n".join(system)
    if request.tools:
        params["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ]
    return params


def _turns(messages: Sequence[Message]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        role = "assistant" if isinstance(message, AssistantMessage) else "user"
        blocks = _blocks(message)
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})
    for turn in turns:
        if turn["role"] == "user":
            # tool_result blocks must lead the turn that answers the tool_use blocks.
            turn["content"].sort(key=lambda block: block["type"] != "tool_result")
    return turns


def _blocks(message: Message) -> list[dict[str, Any]]:
    if isinstance(message, UserMessage):
        return [{"type": "text", "text": message.content}]
    if isinstance(message, ToolResultMessage):
        return [
            {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content or EMPTY_RESULT,
                "is_error": message.is_error,
            }
        ]
    if isinstance(message, AssistantMessage):
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            for call in message.tool_calls
        )
        return blocks
    raise TypeError(f"unexpected message type {type(message).__name__}")


# Response conversion


def from_anthropic_message(message: object) -> ProviderResponse:
    """Validate an SDK ``Message`` (or an object of the same shape) and normalize it.

    Raises:
        ProviderResponseError: If anything in it is missing, of the wrong type, or unknown.
    """
    if getattr(message, "type", "message") != "message":
        raise ProviderResponseError()
    raw_stop = getattr(message, "stop_reason", None)
    if not isinstance(raw_stop, str):
        raise ProviderResponseError("the model provider returned no stop reason")
    stop_reason = _STOP_REASONS.get(raw_stop, "other")

    content = getattr(message, "content", None)
    if not isinstance(content, list):
        raise ProviderResponseError()
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        kind = getattr(block, "type", None)
        if kind == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise ProviderResponseError()
            texts.append(text)
        elif kind == "tool_use":
            calls.append(_tool_call(block))
        elif kind not in _IGNORED_BLOCKS:
            raise ProviderResponseError("the model provider returned an unsupported content block")

    if stop_reason == "tool_calls" and not calls:
        raise ProviderResponseError("the model provider stopped for tool use without a tool call")
    try:
        return ProviderResponse(
            content="".join(texts),
            tool_calls=calls if stop_reason == "tool_calls" else (),
            stop_reason=stop_reason,
            provider_stop_reason=raw_stop if stop_reason == "other" else None,
            usage=_usage(getattr(message, "usage", None)),
            model=_model_name(getattr(message, "model", None)),
        )
    except ValidationError:
        raise ProviderResponseError() from None


def _tool_call(block: object) -> ToolCall:
    call_id = getattr(block, "id", None)
    name = getattr(block, "name", None)
    arguments = getattr(block, "input", None)
    if not isinstance(arguments, dict):
        raise ProviderResponseError("a tool call's arguments are not a JSON object")
    try:
        # A round trip through JSON proves the arguments are plain JSON values.
        plain: dict[str, JsonValue] = json.loads(json.dumps(arguments, allow_nan=False))
        return ToolCall.model_validate({"id": call_id, "name": name, "arguments": plain})
    except (TypeError, ValueError, ValidationError):
        raise ProviderResponseError("the model provider returned a malformed tool call") from None


def _usage(usage: object) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    base = _count(getattr(usage, "input_tokens", None))
    output = _count(getattr(usage, "output_tokens", None))
    # Cached input is reported apart from input_tokens; it is still input the request used.
    cache_write = _count(getattr(usage, "cache_creation_input_tokens", None)) or 0
    cache_read = _count(getattr(usage, "cache_read_input_tokens", None)) or 0
    total_input = None if base is None else base + cache_write + cache_read
    return TokenUsage(input_tokens=total_input, output_tokens=output)


def _count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderResponseError("the model provider returned invalid token usage")
    return value


def _model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return ProviderResponse.model_validate({"stop_reason": "other", "model": value}).model
    except ValidationError:
        return None


__all__ = [
    "ANTHROPIC_BASE_URL",
    "AnthropicProvider",
    "from_anthropic_message",
    "to_anthropic_params",
]
