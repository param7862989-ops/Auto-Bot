"""``OpenAIProvider``: OpenAI Chat Completions behind the neutral ``Provider`` interface.

Why Chat Completions and not the Responses API: Chat Completions is stateless, so the
whole conversation is in each request and nothing is kept on OpenAI's side (``store`` is
also set to ``false``). Its messages map one to one onto the neutral models. The Responses
API keeps state by default and returns reasoning items that would have to be carried
between turns in a form the neutral models do not have.

Request conversion:

* ``SystemMessage`` -> ``{"role": "system"}``; ``UserMessage`` -> ``{"role": "user"}``.
* ``AssistantMessage`` -> ``{"role": "assistant", "tool_calls": [...]}``, each call with
  ``type: "function"`` and its arguments encoded as JSON text.
* ``ToolResultMessage`` -> ``{"role": "tool", "tool_call_id": ...}``. The API has no error
  flag, so an error result's content is prefixed with ``ERROR_PREFIX``.
* Tool definitions -> ``{"type": "function", "function": {name, description, parameters,
  strict: false}}``. Strict mode is not used: it requires every property to be listed as
  required, and the registry's input models have optional arguments with defaults.
  Rewriting them would change what the model is told. Arguments are validated strictly
  after the model answers instead (``parse_tool_arguments``, then the registry).
* ``max_completion_tokens`` carries the output bound, ``n`` is 1.

Response conversion: exactly one choice is required. ``finish_reason`` maps ``stop`` to
``end_turn``, ``tool_calls`` to ``tool_calls``, ``length`` to ``max_tokens``, and
``content_filter`` to ``content_filter``. Tool calls are returned only when the finish
reason is ``tool_calls`` or ``stop``; with ``length`` they may be cut off and are dropped.
A tool call that is not of type ``function``, or whose arguments are not a JSON object
(invalid JSON, duplicate keys, non-finite numbers), makes the response invalid. A refusal
is returned as ``content_filter`` with the refusal text as content.

The SDK is imported when the provider is constructed. The client takes nothing from the
environment: the key and endpoint are explicit; ``OPENAI_ORG_ID``, ``OPENAI_PROJECT_ID``,
``OPENAI_ADMIN_KEY``, ``OPENAI_WEBHOOK_SECRET``, and ``OPENAI_CUSTOM_HEADERS`` are discarded
after construction; the HTTP client ignores proxy variables; the SDK's retries are off.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, ValidationError

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
    parse_tool_arguments,
)

if TYPE_CHECKING:
    import httpx2

logger = logging.getLogger(__name__)

OPENAI_BASE_URL = "https://api.openai.com/v1"
ERROR_PREFIX = "ERROR: "

_FINISH_REASONS: Mapping[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_calls",
    "length": "max_tokens",
    "content_filter": "content_filter",
}


class OpenAIProvider(Provider):
    """OpenAI Chat Completions adapter."""

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
            ImportError: If the ``openai`` package is not installed.
        """
        import openai

        quiet_sdk_loggers()
        self._sdk = openai
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=OPENAI_BASE_URL,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=build_http_client(transport),
        )
        # The constructor fills these from OPENAI_* variables when they are not passed,
        # and sends the organization and project as headers. Nothing may come from the
        # environment.
        self._client.organization = None
        self._client.project = None
        self._client.admin_api_key = None
        self._client.webhook_secret = None
        self._client._custom_headers = {}

    @property
    def name(self) -> str:
        return "openai"

    async def aclose(self) -> None:
        await self._client.close()

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        params = to_openai_params(request)

        async def attempt() -> object:
            try:
                return await self._client.chat.completions.create(**params)
            except Exception as exc:
                error = classify_sdk_error(
                    exc,
                    status_error=self._sdk.APIStatusError,
                    timeout_error=self._sdk.APITimeoutError,
                    connection_error=self._sdk.APIConnectionError,
                    validation_error=self._sdk.APIResponseValidationError,
                    sdk_error=self._sdk.OpenAIError,
                )
                if error is None:
                    raise
                raise error from None

        completion = await with_retries(
            attempt,
            provider=self.name,
            max_retries=self._max_retries,
            timeout_seconds=self._timeout_seconds,
        )
        return from_openai_completion(completion)


# Request conversion


def to_openai_params(request: ProviderRequest) -> dict[str, Any]:
    """The keyword arguments of ``chat.completions.create`` for ``request``."""
    params: dict[str, Any] = {
        "model": request.model,
        "messages": [_message(message) for message in request.messages],
        "max_completion_tokens": request.max_output_tokens,
        "n": 1,
        "store": False,
    }
    if request.tools:
        params["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": False,
                },
            }
            for tool in request.tools
        ]
    return params


def _message(message: Message) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolResultMessage):
        content = ERROR_PREFIX + message.content if message.is_error else message.content
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": content}
    if isinstance(message, AssistantMessage):
        result: dict[str, Any] = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, separators=(",", ":")),
                    },
                }
                for call in message.tool_calls
            ]
        return result
    raise TypeError(f"unexpected message type {type(message).__name__}")


# Response conversion


def from_openai_completion(completion: object) -> ProviderResponse:
    """Validate an SDK ``ChatCompletion`` (or an object of the same shape) and normalize it.

    Raises:
        ProviderResponseError: If anything in it is missing, of the wrong type, or unknown.
    """
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderResponseError("the model provider did not return exactly one choice")
    choice = choices[0]
    raw_finish = getattr(choice, "finish_reason", None)
    if not isinstance(raw_finish, str):
        raise ProviderResponseError("the model provider returned no stop reason")
    message = getattr(choice, "message", None)
    if message is None:
        raise ProviderResponseError()

    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise ProviderResponseError()
    refusal = getattr(message, "refusal", None)
    if refusal is not None and not isinstance(refusal, str):
        raise ProviderResponseError()
    raw_calls = getattr(message, "tool_calls", None) or []
    if not isinstance(raw_calls, list):
        raise ProviderResponseError()

    stop_reason = _FINISH_REASONS.get(raw_finish, "other")
    if refusal:
        stop_reason = "content_filter"
        content = refusal
    calls = [_tool_call(raw) for raw in raw_calls]
    if stop_reason == "tool_calls" and not calls:
        raise ProviderResponseError("the model provider stopped for tool use without a tool call")
    if stop_reason == "end_turn" and calls:
        # Some models end a forced tool call with "stop". The calls are complete: their
        # arguments parsed as whole JSON objects.
        stop_reason = "tool_calls"
    try:
        return ProviderResponse(
            content=content or "",
            tool_calls=calls if stop_reason == "tool_calls" else (),
            stop_reason=stop_reason,
            provider_stop_reason=raw_finish if stop_reason == "other" else None,
            usage=_usage(getattr(completion, "usage", None)),
            model=_model_name(getattr(completion, "model", None)),
        )
    except ValidationError:
        raise ProviderResponseError() from None


def _tool_call(raw: object) -> ToolCall:
    if getattr(raw, "type", None) != "function":
        raise ProviderResponseError("the model provider returned an unsupported tool call type")
    function = getattr(raw, "function", None)
    name = getattr(function, "name", None)
    arguments_text = getattr(function, "arguments", None)
    if not isinstance(arguments_text, str):
        raise ProviderResponseError("a tool call's arguments are not JSON text")
    arguments = parse_tool_arguments(arguments_text)
    try:
        return ToolCall.model_validate(
            {"id": getattr(raw, "id", None), "name": name, "arguments": arguments}
        )
    except ValidationError:
        raise ProviderResponseError("the model provider returned a malformed tool call") from None


def _usage(usage: object) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=_count(getattr(usage, "prompt_tokens", None)),
        output_tokens=_count(getattr(usage, "completion_tokens", None)),
    )


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
    "ERROR_PREFIX",
    "OPENAI_BASE_URL",
    "OpenAIProvider",
    "from_openai_completion",
    "to_openai_params",
]
