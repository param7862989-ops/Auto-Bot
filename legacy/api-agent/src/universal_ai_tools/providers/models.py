"""Provider-neutral models of a conversation with a language model.

These models are the only shape in which the core talks to a model provider. They describe
what every provider has in common (system, user, assistant, and tool messages; tool
definitions; tool calls; why generation stopped; token usage) and nothing that belongs to
one provider's API. An adapter translates them to and from its provider's format.

Every model is strict, frozen, and closed (``NEUTRAL``): pydantic strict mode with no type
coercion, unknown fields refused, no reassignment. Collections are tuples; a list is
accepted on input and stored as a tuple. Content is plain text, and tool arguments and
schemas are JSON values only: strings, finite numbers, booleans, null, lists, and objects
with string keys. Nothing here can hold an arbitrary Python object, and nothing here runs a
tool. A ``ToolCall`` is a request from the model, to be checked by whoever decides whether
to execute it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from universal_ai_tools.providers.errors import ProviderResponseError

if TYPE_CHECKING:
    # For annotations only: the provider layer never loads the registry, the workspace,
    # or the filesystem tools at run time.
    from universal_ai_tools.tools import ToolRegistry, ToolSpec

NEUTRAL = ConfigDict(strict=True, extra="forbid", frozen=True)

MessageRole = Literal["system", "user", "assistant", "tool"]
"""Who a message is from. ``tool`` messages carry results of the model's tool calls."""

StopReason = Literal[
    "end_turn", "tool_calls", "max_tokens", "stop_sequence", "content_filter", "other"
]
"""Why the model stopped generating.

* ``end_turn``: it finished its answer.
* ``tool_calls``: it is waiting for the results of the tool calls in the response.
* ``max_tokens``: it reached ``ProviderRequest.max_output_tokens``; the answer is cut off.
* ``stop_sequence``: it produced a stop sequence.
* ``content_filter``: the provider withheld or cut off the output.
* ``other``: any reason without a neutral meaning, such as one a provider added later. The
  provider's own name for it is in ``ProviderResponse.provider_stop_reason``.
"""


def _list_as_tuple(value: object) -> object:
    # Strict mode accepts only tuples for tuple fields. A list is the natural input, so
    # it, and nothing else, is converted. The items are still validated strictly.
    return tuple(value) if isinstance(value, list) else value


def _require_finite(value: JsonValue) -> JsonValue:
    pending: list[JsonValue] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("numbers must be finite")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return value


_ListAsTuple = BeforeValidator(_list_as_tuple)

JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_require_finite)]
"""A JSON object: string keys, JSON values, finite numbers."""

ToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ToolCallId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
ModelName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")]


# Tools


class ToolDefinition(BaseModel):
    """A tool the model may ask to call: its name, what it does, and its input schema."""

    model_config = NEUTRAL

    name: ToolName
    description: str = Field(min_length=1)
    input_schema: JsonObject
    """JSON Schema for the arguments. Always an object schema."""

    @model_validator(mode="after")
    def _object_schema(self) -> ToolDefinition:
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema must describe a JSON object")
        return self

    @classmethod
    def from_spec(cls, spec: ToolSpec[Any, Any]) -> ToolDefinition:
        """Describe a registry tool: its name, its description, and its input model's schema.

        The schema is the input model's own JSON Schema, which is what ``ToolRegistry.invoke``
        validates against. Its properties, descriptions, bounds, defaults, and required
        fields are those of the MCP input schema; it also says ``additionalProperties:
        false``, because the registry refuses undeclared arguments.
        """
        return cls(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_model.model_json_schema(),
        )


def tool_definitions(registry: ToolRegistry) -> tuple[ToolDefinition, ...]:
    """Describe every tool in ``registry``, in registry order."""
    return tuple(ToolDefinition.from_spec(spec) for spec in registry)


class ToolCall(BaseModel):
    """The model asks for one tool to be run with these arguments. Nothing runs it here."""

    model_config = NEUTRAL

    id: ToolCallId
    """Chosen by the provider; a ``ToolResultMessage`` answers it by this id."""
    name: ToolName
    """As the model wrote it. It may name a tool that does not exist."""
    arguments: JsonObject


def parse_tool_arguments(text: str) -> dict[str, JsonValue]:
    """Parse tool-call arguments that a provider returns as JSON text.

    Accepts only a JSON object. Refuses invalid JSON, any other JSON value, duplicate keys
    (``{"path": "a", "path": "b"}`` is ambiguous), and numbers that are not finite
    (``NaN``, ``Infinity``, ``1e999``).

    Raises:
        ProviderResponseError: If ``text`` is not such an object. The message does not
            quote the text.
    """

    def unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def finite_float(literal: str) -> float:
        number = float(literal)
        if not math.isfinite(number):
            raise ValueError("number is not finite")
        return number

    def no_constants(_literal: str) -> float:
        raise ValueError("NaN and Infinity are not JSON")

    if not isinstance(text, str):
        raise ProviderResponseError("a tool call's arguments are not JSON text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_keys,
            parse_float=finite_float,
            parse_constant=no_constants,
        )
    except (ValueError, RecursionError):
        raise ProviderResponseError("a tool call's arguments are not valid JSON") from None
    if not isinstance(value, dict):
        raise ProviderResponseError("a tool call's arguments are not a JSON object")
    return value


# Messages


class SystemMessage(BaseModel):
    """Instructions for the model. Only at the start of a conversation."""

    model_config = NEUTRAL

    role: Literal["system"] = "system"
    content: str = Field(min_length=1)


class UserMessage(BaseModel):
    model_config = NEUTRAL

    role: Literal["user"] = "user"
    content: str = Field(min_length=1)


class AssistantMessage(BaseModel):
    """An earlier model turn: its text, its tool calls, or both."""

    model_config = NEUTRAL

    role: Literal["assistant"] = "assistant"
    content: str = ""
    tool_calls: Annotated[tuple[ToolCall, ...], _ListAsTuple] = ()

    @model_validator(mode="after")
    def _not_empty(self) -> AssistantMessage:
        if not self.content and not self.tool_calls:
            raise ValueError("an assistant message needs content or tool calls")
        _require_unique_ids(self.tool_calls)
        return self


class ToolResultMessage(BaseModel):
    """The result of one tool call, given back to the model."""

    model_config = NEUTRAL

    role: Literal["tool"] = "tool"
    tool_call_id: ToolCallId
    content: str
    is_error: bool = False
    """True when the call was refused or failed and ``content`` says why."""


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


def _require_unique_ids(calls: Sequence[ToolCall]) -> None:
    ids = [call.id for call in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("tool call ids must be unique")


# Requests and responses


class ProviderRequest(BaseModel):
    """One request for the model's next turn."""

    model_config = NEUTRAL

    model: ModelName
    messages: Annotated[tuple[Message, ...], _ListAsTuple, Field(min_length=1)]
    tools: Annotated[tuple[ToolDefinition, ...], _ListAsTuple] = ()
    """The tools the model may call in this turn. Empty means none."""
    max_output_tokens: int = Field(ge=1)
    """Upper bound on generated tokens. Required, so no request is unbounded."""

    @model_validator(mode="after")
    def _valid_conversation(self) -> ProviderRequest:
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        _check_conversation(self.messages)
        return self


def _check_conversation(messages: Sequence[Message]) -> None:
    """Refuse a conversation that no provider can be sent unambiguously.

    System messages come first. The first other message is from the user. Every tool call
    is answered by exactly one tool result, and the results follow the assistant message
    that made the calls before anything else does. The conversation ends with a user
    message or a tool result: the model is asked for its next turn.
    """
    conversation = messages[_leading_system_messages(messages) :]
    if any(isinstance(message, SystemMessage) for message in conversation):
        raise ValueError("system messages must come before all other messages")
    if not conversation or not isinstance(conversation[0], UserMessage):
        raise ValueError("the conversation must start with a user message")
    if not isinstance(conversation[-1], UserMessage | ToolResultMessage):
        raise ValueError("the conversation must end with a user message or a tool result")

    seen: set[str] = set()
    pending: set[str] = set()
    for message in conversation:
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id not in pending:
                raise ValueError("a tool result must answer a pending tool call, once")
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ValueError("every tool call must be answered before the conversation goes on")
        if isinstance(message, AssistantMessage):
            ids = {call.id for call in message.tool_calls}
            if ids & seen:
                raise ValueError("tool call ids must be unique in a conversation")
            seen |= ids
            pending = ids
    if pending:
        raise ValueError("every tool call must be answered before the conversation goes on")


def _leading_system_messages(messages: Sequence[Message]) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, SystemMessage):
            break
        count += 1
    return count


class TokenUsage(BaseModel):
    """Tokens a request consumed, as far as the provider reported them.

    ``None`` means unknown, not zero. Code that budgets tokens must decide what an unknown
    count means for it; it must not treat it as free.
    """

    model_config = NEUTRAL

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @property
    def total_tokens(self) -> int | None:
        """Input plus output, or ``None`` if either is unknown."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class ProviderResponse(BaseModel):
    """The model's turn: text, tool calls, why it stopped, and what it cost."""

    model_config = NEUTRAL

    content: str = ""
    """Text from the model. Untrusted: it may repeat anything it was shown."""
    tool_calls: Annotated[tuple[ToolCall, ...], _ListAsTuple] = ()
    stop_reason: StopReason
    provider_stop_reason: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    """The provider's own name for the stop reason, for diagnostics only."""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: ModelName | None = None
    """The model that answered, if the provider said."""

    @model_validator(mode="after")
    def _consistent(self) -> ProviderResponse:
        # Tool calls are only ever a complete request for results. A response cut off by
        # max_tokens, or stopped for any other reason, carries none, so a half-written
        # call can never be mistaken for one to run.
        if (self.stop_reason == "tool_calls") != bool(self.tool_calls):
            raise ValueError("tool calls appear exactly when stop_reason is 'tool_calls'")
        _require_unique_ids(self.tool_calls)
        return self
