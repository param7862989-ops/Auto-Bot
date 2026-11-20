"""Provider-neutral models: what they accept, what they refuse, and tool definitions."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.providers import (
    AssistantMessage,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
    tool_definitions,
)
from universal_ai_tools.tools import ToolRegistry, build_registry

SNAPSHOT = Path(__file__).parent / "snapshots" / "mcp_surface.json"
MODEL = "test-model"


def call(call_id: str = "call_1", name: str = "read_file", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments or {"path": "docs/a.txt"})


def request(*messages: object, **fields: Any) -> ProviderRequest:
    return ProviderRequest.model_validate(
        {"model": MODEL, "max_output_tokens": 100, "messages": list(messages), **fields}
    )


def rejected(error: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {item["type"] for item in error.value.errors()}


# Tool calls


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"path": "docs/a.txt"},
        {"nested": {"list": [1, 2.5, "x", True, None, {"deep": []}]}},
        {"unicode": "é中"},
    ],
)
def test_tool_call_accepts_json_objects(arguments: dict[str, Any]) -> None:
    assert ToolCall(id="call_1", name="read_file", arguments=arguments).arguments == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        "{}",
        '{"path": "x"}',
        ["path", "x"],
        5,
        {1: "x"},
        {"value": (1, 2)},
        {"value": {1, 2}},
        {"value": b"bytes"},
        {"value": Path("x")},
        {"value": object()},
        {"value": print},
        {"value": float("nan")},
        {"value": float("inf")},
        {"nested": [{"value": -math.inf}]},
    ],
)
def test_tool_call_arguments_must_be_a_json_object(arguments: object) -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"id": "call_1", "name": "read_file", "arguments": arguments})


@pytest.mark.parametrize("call_id", ["", "has space", "a/b", "x" * 129, "é", "a\nb", None, 5])
def test_tool_call_ids_are_restricted(call_id: object) -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"id": call_id, "name": "read_file", "arguments": {}})


@pytest.mark.parametrize("name", ["", "has space", "a.b", "x" * 65, "read_file\x00", "é", 5])
def test_tool_call_names_are_restricted(name: object) -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate({"id": "call_1", "name": name, "arguments": {}})


def test_a_tool_call_may_name_an_unknown_tool() -> None:
    # The model may invent a name. Deciding what exists is not the provider layer's job.
    assert call(name="Not_A_Real-Tool").name == "Not_A_Real-Tool"


def test_tool_calls_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError) as error:
        ToolCall.model_validate(
            {"id": "call_1", "name": "read_file", "arguments": {}, "execute": True}
        )
    assert rejected(error) == {"extra_forbidden"}


def test_tool_calls_are_frozen() -> None:
    with pytest.raises(ValidationError):
        call().name = "write_file"


# parse_tool_arguments, for providers that send arguments as JSON text


def test_parse_tool_arguments() -> None:
    assert parse_tool_arguments('{"path": "a", "n": [1, 2.5, null, true]}') == {
        "path": "a",
        "n": [1, 2.5, None, True],
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "not valid JSON"),
        ('{"path": ', "not valid JSON"),
        ("{'path': 'x'}", "not valid JSON"),
        ('{"a": NaN}', "not valid JSON"),
        ('{"a": Infinity}', "not valid JSON"),
        ('{"a": -Infinity}', "not valid JSON"),
        ('{"a": 1e999}', "not valid JSON"),
        ('{"path": "a", "path": "b"}', "not valid JSON"),
        ('{"x": {"k": 1, "k": 2}}', "not valid JSON"),
        ("[" * 100_000 + "]" * 100_000, "not valid JSON"),
        ('{"a": ' + "9" * 5000 + "}", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('"text"', "not a JSON object"),
        ("5", "not a JSON object"),
        ("null", "not a JSON object"),
    ],
)
def test_parse_tool_arguments_refuses(text: str, message: str) -> None:
    with pytest.raises(ProviderResponseError, match=message) as error:
        parse_tool_arguments(text)
    # Nothing from the JSON parser, which can quote the text, is attached to the error.
    assert error.value.__cause__ is None
    assert error.value.__context__ is None or error.value.__suppress_context__


def test_parse_tool_arguments_never_quotes_the_text() -> None:
    sentinel = "sk-do-not-echo-0123456789"

    with pytest.raises(ProviderResponseError) as error:
        parse_tool_arguments('{"key": "' + sentinel + '"')

    assert sentinel not in str(error.value)


def test_parse_tool_arguments_requires_text() -> None:
    with pytest.raises(ProviderResponseError, match="not JSON text"):
        parse_tool_arguments(b"{}")  # type: ignore[arg-type]


# Messages


def test_message_roles() -> None:
    parsed = request(
        {"role": "system", "content": "Be careful."},
        {"role": "user", "content": "Read a."},
        {"role": "assistant", "content": "", "tool_calls": [call().model_dump()]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a", "is_error": False},
    )

    assert [type(message) for message in parsed.messages] == [
        SystemMessage,
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
    ]
    assert [message.role for message in parsed.messages] == ["system", "user", "assistant", "tool"]


@pytest.mark.parametrize("role", ["developer", "function", "User", "", None, 1])
def test_invalid_roles_are_refused(role: object) -> None:
    with pytest.raises(ValidationError) as error:
        request({"role": role, "content": "hi"})
    assert rejected(error) & {"union_tag_invalid", "union_tag_not_found"}


def test_a_message_must_say_its_role() -> None:
    with pytest.raises(ValidationError) as error:
        request({"content": "hi"})
    assert rejected(error) == {"union_tag_not_found"}


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "hi", "name": "alice"},
        {"role": "user", "content": "hi", "tool_calls": []},
        {"role": "system", "content": "x", "cache": True},
        {"role": "tool", "tool_call_id": "call_1", "content": "x", "name": "read_file"},
    ],
)
def test_messages_reject_unknown_fields(message: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        request(message)
    assert "extra_forbidden" in rejected(error)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": ""},
        {"role": "user", "content": 5},
        {"role": "user", "content": ["hi"]},
        {"role": "user", "content": {"type": "text", "text": "hi"}},
        {"role": "user"},
        {"role": "system", "content": ""},
    ],
)
def test_message_content_is_non_empty_text(message: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(message)


def test_assistant_messages_need_content_or_tool_calls() -> None:
    with pytest.raises(ValidationError, match="needs content or tool calls"):
        AssistantMessage()


def test_assistant_tool_call_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="ids must be unique"):
        AssistantMessage(tool_calls=(call("same"), call("same")))


def test_tool_results_need_a_valid_call_id() -> None:
    with pytest.raises(ValidationError):
        ToolResultMessage.model_validate({"content": "x"})
    with pytest.raises(ValidationError):
        ToolResultMessage(tool_call_id="has space", content="x")


@pytest.mark.parametrize("is_error", ["true", 1, None])
def test_tool_result_error_flag_is_strict(is_error: object) -> None:
    with pytest.raises(ValidationError):
        ToolResultMessage.model_validate(
            {"tool_call_id": "call_1", "content": "x", "is_error": is_error}
        )


# Requests


def test_a_complete_tool_round_trip() -> None:
    parsed = request(
        SystemMessage(content="Be careful."),
        UserMessage(content="Read a and b."),
        AssistantMessage(content="Reading.", tool_calls=(call("c1"), call("c2"))),
        ToolResultMessage(tool_call_id="c2", content="b"),
        ToolResultMessage(tool_call_id="c1", content="refused", is_error=True),
        AssistantMessage(content="Done reading."),
        UserMessage(content="Thanks."),
    )

    assert len(parsed.messages) == 7
    assert isinstance(parsed.messages, tuple)


@pytest.mark.parametrize(
    ("messages", "message"),
    [
        ([SystemMessage(content="x")], "must start with a user message"),
        ([UserMessage(content="a"), SystemMessage(content="x")], "system messages must come"),
        (
            [UserMessage(content="a"), AssistantMessage(content="b")],
            "must end with a user message or a tool result",
        ),
        ([AssistantMessage(content="b"), UserMessage(content="a")], "must start with a user"),
        (
            [UserMessage(content="a"), ToolResultMessage(tool_call_id="c1", content="x")],
            "must answer a pending tool call",
        ),
        (
            [
                UserMessage(content="a"),
                AssistantMessage(tool_calls=(call("c1"), call("c2"))),
                ToolResultMessage(tool_call_id="c1", content="x"),
            ],
            "every tool call must be answered",
        ),
        (
            [
                UserMessage(content="a"),
                AssistantMessage(tool_calls=(call("c1"),)),
                UserMessage(content="never mind"),
            ],
            "every tool call must be answered",
        ),
        (
            [
                UserMessage(content="a"),
                AssistantMessage(tool_calls=(call("c1"),)),
                ToolResultMessage(tool_call_id="c1", content="x"),
                ToolResultMessage(tool_call_id="c1", content="again"),
            ],
            "must answer a pending tool call, once",
        ),
        (
            [
                UserMessage(content="a"),
                AssistantMessage(tool_calls=(call("c1"),)),
                ToolResultMessage(tool_call_id="c9", content="x"),
            ],
            "must answer a pending tool call",
        ),
        (
            [
                UserMessage(content="a"),
                AssistantMessage(tool_calls=(call("c1"),)),
                ToolResultMessage(tool_call_id="c1", content="x"),
                AssistantMessage(tool_calls=(call("c1"),)),
                ToolResultMessage(tool_call_id="c1", content="x"),
            ],
            "unique in a conversation",
        ),
    ],
)
def test_invalid_conversations_are_refused(messages: list[object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        request(*messages)


def test_requests_need_messages() -> None:
    with pytest.raises(ValidationError):
        request()


@pytest.mark.parametrize("value", [0, -1, "100", 100.0, True, None])
def test_max_output_tokens_is_a_positive_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        request(UserMessage(content="a"), max_output_tokens=value)


def test_max_output_tokens_is_required() -> None:
    with pytest.raises(ValidationError) as error:
        ProviderRequest.model_validate(
            {"model": MODEL, "messages": [{"role": "user", "content": "a"}]}
        )
    assert rejected(error) == {"missing"}


@pytest.mark.parametrize("model", ["", " model", "model name", "a\nb", "x" * 129, 5])
def test_model_names_are_restricted(model: object) -> None:
    with pytest.raises(ValidationError):
        request(UserMessage(content="a"), model=model)


def test_requests_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError) as error:
        request(UserMessage(content="a"), api_key="sk-x", temperature=0)
    assert rejected(error) == {"extra_forbidden"}


def test_requests_refuse_duplicate_tool_names() -> None:
    tool = ToolDefinition(name="read_file", description="Read.", input_schema={"type": "object"})
    with pytest.raises(ValidationError, match="tool names must be unique"):
        request(UserMessage(content="a"), tools=[tool, tool])


def test_requests_are_frozen() -> None:
    parsed = request(UserMessage(content="a"))
    with pytest.raises(ValidationError):
        parsed.max_output_tokens = 10**9
    assert isinstance(parsed.messages, tuple)
    assert isinstance(parsed.tools, tuple)


@pytest.mark.parametrize("container", [{"role": "user", "content": "a"}, "a", 5])
def test_message_lists_must_be_sequences(container: object) -> None:
    with pytest.raises(ValidationError):
        ProviderRequest.model_validate(
            {"model": MODEL, "max_output_tokens": 1, "messages": container}
        )


# Usage


def test_usage_may_be_missing() -> None:
    usage = TokenUsage()

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None


def test_partial_usage_has_no_total() -> None:
    assert TokenUsage(input_tokens=10).total_tokens is None
    assert TokenUsage(output_tokens=5).total_tokens is None
    assert TokenUsage(input_tokens=10, output_tokens=5).total_tokens == 15
    assert TokenUsage(input_tokens=0, output_tokens=0).total_tokens == 0


@pytest.mark.parametrize("value", [-1, "10", 10.0, True])
def test_usage_counts_are_non_negative_integers(value: object) -> None:
    with pytest.raises(ValidationError):
        TokenUsage.model_validate({"input_tokens": value})


def test_usage_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TokenUsage.model_validate({"input_tokens": 1, "cost_usd": 0.5})


# Responses and stop reasons


@pytest.mark.parametrize(
    "stop_reason", ["end_turn", "max_tokens", "stop_sequence", "content_filter", "other"]
)
def test_stop_reasons_without_tool_calls(stop_reason: str) -> None:
    response = ProviderResponse.model_validate({"content": "x", "stop_reason": stop_reason})

    assert response.stop_reason == stop_reason
    assert response.tool_calls == ()
    assert response.usage == TokenUsage()


def test_tool_calls_stop_reason() -> None:
    response = ProviderResponse(stop_reason="tool_calls", tool_calls=[call("c1"), call("c2")])

    assert [item.id for item in response.tool_calls] == ["c1", "c2"]
    assert isinstance(response.tool_calls, tuple)


@pytest.mark.parametrize(
    "stop_reason", ["stop", "length", "tool_use", "pause_turn", "", None, "END_TURN"]
)
def test_unknown_stop_reasons_are_refused(stop_reason: object) -> None:
    with pytest.raises(ValidationError):
        ProviderResponse.model_validate({"stop_reason": stop_reason})


def test_the_provider_stop_reason_is_kept_for_diagnostics() -> None:
    response = ProviderResponse(stop_reason="other", provider_stop_reason="pause_turn")

    assert response.provider_stop_reason == "pause_turn"


@pytest.mark.parametrize("value", ["", "has space", "x" * 65, "a\nb", "<script>"])
def test_provider_stop_reasons_are_restricted(value: str) -> None:
    with pytest.raises(ValidationError):
        ProviderResponse(stop_reason="other", provider_stop_reason=value)


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "content_filter", "other"])
def test_tool_calls_require_the_tool_calls_stop_reason(stop_reason: str) -> None:
    # A response cut off at max_tokens may hold a half-written call; it is never runnable.
    with pytest.raises(ValidationError, match="exactly when stop_reason is 'tool_calls'"):
        ProviderResponse.model_validate({"stop_reason": stop_reason, "tool_calls": [call()]})


def test_the_tool_calls_stop_reason_requires_tool_calls() -> None:
    with pytest.raises(ValidationError, match="exactly when stop_reason is 'tool_calls'"):
        ProviderResponse(stop_reason="tool_calls")


def test_response_tool_call_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="ids must be unique"):
        ProviderResponse(stop_reason="tool_calls", tool_calls=[call("same"), call("same")])


def test_an_empty_response_is_valid() -> None:
    response = ProviderResponse(stop_reason="end_turn")

    assert response.content == ""
    assert response.tool_calls == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"stop_reason": "end_turn", "content": None},
        {"stop_reason": "end_turn", "content": ["text"]},
        {"stop_reason": "end_turn", "usage": None},
        {"stop_reason": "end_turn", "usage": {"input_tokens": "5"}},
        {"stop_reason": "end_turn", "raw": {"id": "msg_1"}},
        {"stop_reason": "tool_calls", "tool_calls": [{"id": "c1", "name": "x"}]},
        {"stop_reason": "tool_calls", "tool_calls": {"id": "c1", "name": "x", "arguments": {}}},
    ],
)
def test_invalid_responses_are_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProviderResponse.model_validate(payload)


# Tool definitions from the registry


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    config = load_config({ENV_WORKSPACE: str(tmp_path), ENV_ENABLE_WRITE_TOOLS: "true"})
    return build_registry(config)


def test_tool_definitions_follow_the_registry(registry: ToolRegistry) -> None:
    definitions = tool_definitions(registry)

    assert [definition.name for definition in definitions] == list(registry.names)
    for definition, spec in zip(definitions, registry, strict=True):
        assert definition.description == spec.description
        assert definition.input_schema == spec.input_model.model_json_schema()
        assert definition == ToolDefinition.from_spec(spec)


def test_tool_definitions_hold_only_the_configured_tools(tmp_path: Path) -> None:
    registry = build_registry(load_config({ENV_WORKSPACE: str(tmp_path)}))

    assert [definition.name for definition in tool_definitions(registry)] == [
        "get_platform_info",
        "list_files",
        "read_file",
        "search_files",
    ]


def test_tool_definitions_match_the_mcp_surface(registry: ToolRegistry) -> None:
    """Name, description, and schema agree with what MCP clients were shown before 5B."""
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["write_enabled"]["tools"]

    definitions = tool_definitions(registry)

    assert [definition.name for definition in definitions] == [tool["name"] for tool in recorded]
    for definition, tool in zip(definitions, recorded, strict=True):
        schema = definition.input_schema
        mcp_schema = tool["inputSchema"]
        assert definition.description == tool["description"], tool["name"]
        assert schema["type"] == mcp_schema["type"] == "object"
        assert schema["properties"] == mcp_schema["properties"], tool["name"]
        assert schema.get("required", []) == mcp_schema.get("required", []), tool["name"]
        # The only differences: the schema's title, and that the registry refuses
        # undeclared arguments, which the definition says.
        assert schema["additionalProperties"] is False
        assert set(schema) - set(mcp_schema) == {"additionalProperties"}
        assert schema["title"] != mcp_schema["title"]


def test_tool_definitions_are_independent_copies(registry: ToolRegistry) -> None:
    first = tool_definitions(registry)[1]
    first.input_schema["properties"] = {}

    assert tool_definitions(registry)[1].input_schema["properties"] != {}


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "x", "description": "", "input_schema": {"type": "object"}},
        {"name": "x", "description": "d", "input_schema": {"type": "string"}},
        {"name": "x", "description": "d", "input_schema": {}},
        {"name": "x", "description": "d", "input_schema": []},
        {"name": "bad name", "description": "d", "input_schema": {"type": "object"}},
        {"name": "x", "description": "d", "input_schema": {"type": "object"}, "handler": "x"},
    ],
)
def test_invalid_tool_definitions_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ToolDefinition.model_validate(fields)


# Errors


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [
        (ProviderError, False),
        (ProviderUnavailableError, True),
        (ProviderResponseError, False),
        (ProviderAuthenticationError, False),
        (ProviderRateLimitError, True),
    ],
)
def test_error_hierarchy(error_type: type[ProviderError], retryable: bool) -> None:
    error = error_type()

    assert isinstance(error, ProviderError)
    assert error.retryable is retryable
    assert str(error) == error_type.default_message
    assert str(error).startswith("the model provider")


def test_error_messages_are_escaped_and_bounded() -> None:
    error = ProviderUnavailableError("bad\x1b[31m\nthing " + "x" * 1000)

    assert "\x1b" not in str(error)
    assert "\n" not in str(error)
    assert len(str(error)) <= 250


def test_rate_limit_retry_after() -> None:
    assert ProviderRateLimitError().retry_after_seconds is None
    assert ProviderRateLimitError(retry_after_seconds=2.5).retry_after_seconds == 2.5
    for value in (-1.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="retry_after_seconds"):
            ProviderRateLimitError(retry_after_seconds=value)
