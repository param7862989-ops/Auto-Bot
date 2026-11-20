"""AnthropicProvider, run through the real ``anthropic`` SDK over a mock HTTP transport.

No test makes a network call or uses a real key: ``httpx2.MockTransport`` answers every
request, and records it so the exact request the SDK would send can be checked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import anyio
import pytest

pytest.importorskip("anthropic")
import httpx2
from pydantic import SecretStr

from universal_ai_tools.adapters.anthropic_provider import (
    EMPTY_RESULT,
    AnthropicProvider,
)
from universal_ai_tools.providers import (
    AssistantMessage,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponseError,
    ProviderUnavailableError,
    SystemMessage,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)

KEY = "sk-ant-test-SECRET-0123456789"
PROMPT = "PROMPT-CANARY-do-not-log"

Reply = httpx2.Response | Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        return reply(request) if callable(reply) else reply

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def message(
    content: list[dict[str, Any]],
    stop_reason: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> httpx2.Response:
    body: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7} if usage is None else usage,
        **extra,
    }
    return httpx2.Response(200, json=body)


def error(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        json={"type": "error", "error": {"type": "x", "message": f"echo {KEY} {PROMPT}"}},
    )


def provider(recorder: Recorder, *, retries: int = 0, timeout: float = 5) -> AnthropicProvider:
    return AnthropicProvider(
        api_key=SecretStr(KEY),
        timeout_seconds=timeout,
        max_retries=retries,
        transport=httpx2.MockTransport(recorder),
    )


READ = ToolDefinition(
    name="read_file",
    description="Read a file.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
)


def request(*messages: Any, tools: tuple[ToolDefinition, ...] = (READ,)) -> ProviderRequest:
    return ProviderRequest(
        model="claude-test",
        max_output_tokens=321,
        messages=messages or (UserMessage(content=PROMPT),),
        tools=tools,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(anyio, "sleep", instant)


# Request conversion


@pytest.mark.anyio
async def test_request_conversion() -> None:
    recorder = Recorder(message([{"type": "text", "text": "ok"}]))
    conversation = request(
        SystemMessage(content="Be careful."),
        SystemMessage(content="Second rule."),
        UserMessage(content="Read a."),
        AssistantMessage(
            content="Reading.",
            tool_calls=(
                ToolCall(id="toolu_1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="toolu_2", name="read_file", arguments={"path": "b"}),
            ),
        ),
        ToolResultMessage(tool_call_id="toolu_1", content="A"),
        ToolResultMessage(tool_call_id="toolu_2", content="", is_error=True),
        UserMessage(content="Continue."),
    )

    await provider(recorder).complete(conversation)

    sent = recorder.requests[0]
    assert sent.url == "https://api.anthropic.com/v1/messages"
    body = recorder.body()
    assert body["model"] == "claude-test"
    assert body["max_tokens"] == 321
    assert body["system"] == "Be careful.\n\nSecond rule."
    assert body["tools"] == [
        {"name": "read_file", "description": "Read a file.", "input_schema": READ.input_schema}
    ]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Read a."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}},
                {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "A",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": EMPTY_RESULT,
                    "is_error": True,
                },
                {"type": "text", "text": "Continue."},
            ],
        },
    ]


@pytest.mark.anyio
async def test_no_tools_and_no_system() -> None:
    recorder = Recorder(message([{"type": "text", "text": "ok"}]))

    await provider(recorder).complete(request(tools=()))

    body = recorder.body()
    assert "tools" not in body
    assert "system" not in body


@pytest.mark.anyio
async def test_key_endpoint_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-environment")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-environment")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.invalid")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Leak: environment\nX-Other: 1")
    recorder = Recorder(message([{"type": "text", "text": "ok"}]))

    await provider(recorder).complete(request())

    sent = recorder.requests[0]
    assert sent.url.host == "api.anthropic.com"
    headers = {name.lower(): value for name, value in sent.headers.items()}
    assert "x-leak" not in headers
    assert "x-other" not in headers
    assert not any("environment" in value for value in headers.values())
    credential = headers.get("x-api-key") or headers.get("authorization", "")
    assert KEY in credential


# Response conversion


@pytest.mark.anyio
async def test_text_response() -> None:
    recorder = Recorder(
        message([{"type": "text", "text": "Hello "}, {"type": "text", "text": "there."}])
    )

    response = await provider(recorder).complete(request())

    assert response.content == "Hello there."
    assert response.stop_reason == "end_turn"
    assert response.tool_calls == ()
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 7)
    assert response.model == "claude-test"


@pytest.mark.anyio
async def test_multiple_tool_calls() -> None:
    recorder = Recorder(
        message(
            [
                {"type": "text", "text": "Reading two."},
                {"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {"path": "a"}},
                {"type": "tool_use", "id": "toolu_b", "name": "list_files", "input": {}},
            ],
            stop_reason="tool_use",
        )
    )

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "tool_calls"
    assert response.tool_calls == (
        ToolCall(id="toolu_a", name="read_file", arguments={"path": "a"}),
        ToolCall(id="toolu_b", name="list_files", arguments={}),
    )
    assert response.content == "Reading two."


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw", "neutral"),
    [
        ("end_turn", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("model_context_window_exceeded", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("refusal", "content_filter"),
        ("pause_turn", "other"),
        ("a_future_reason", "other"),
    ],
)
async def test_stop_reasons(raw: str, neutral: str) -> None:
    recorder = Recorder(message([{"type": "text", "text": "x"}], stop_reason=raw))

    response = await provider(recorder).complete(request())

    assert response.stop_reason == neutral
    if neutral == "other":
        assert response.provider_stop_reason == raw


@pytest.mark.anyio
async def test_incomplete_tool_call_at_token_limit_is_dropped() -> None:
    recorder = Recorder(
        message(
            [{"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {"path": "x"}}],
            stop_reason="max_tokens",
        )
    )

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "max_tokens"
    assert response.tool_calls == ()


@pytest.mark.anyio
async def test_thinking_blocks_are_ignored() -> None:
    recorder = Recorder(
        message(
            [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "text", "text": "Answer."},
            ]
        )
    )

    response = await provider(recorder).complete(request())

    assert response.content == "Answer."


@pytest.mark.anyio
async def test_usage_includes_cached_input() -> None:
    recorder = Recorder(
        message(
            [{"type": "text", "text": "x"}],
            usage={
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
            },
        )
    )

    response = await provider(recorder).complete(request())

    assert response.usage.input_tokens == 1105
    assert response.usage.output_tokens == 2


@pytest.mark.anyio
async def test_missing_usage_is_unknown_not_zero() -> None:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
    }
    recorder = Recorder(httpx2.Response(200, json=body))

    response = await provider(recorder).complete(request())

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None


# Malformed responses


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        message([{"type": "text", "text": "x"}], stop_reason=None),
        message([{"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}]),
        message([], stop_reason="tool_use"),
        message(
            [{"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": "a"}],
            stop_reason="tool_use",
        ),
        message(
            [{"type": "tool_use", "id": "bad id!", "name": "read_file", "input": {}}],
            stop_reason="tool_use",
        ),
        message(
            [{"type": "tool_use", "id": "toolu_1", "name": "read file", "input": {}}],
            stop_reason="tool_use",
        ),
        message(
            [
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
                {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {}},
            ],
            stop_reason="tool_use",
        ),
        message([{"type": "text", "text": "x"}], usage={"input_tokens": -1, "output_tokens": 1}),
        httpx2.Response(200, json={"type": "error"}),
        httpx2.Response(200, content=b"not json"),
    ],
    ids=[
        "no-stop-reason",
        "unknown-block",
        "tool-use-without-call",
        "arguments-not-object",
        "malformed-id",
        "malformed-name",
        "duplicate-ids",
        "negative-usage",
        "not-a-message",
        "not-json",
    ],
)
async def test_malformed_responses_are_rejected(reply: httpx2.Response) -> None:
    with pytest.raises(ProviderError) as excinfo:
        await provider(Recorder(reply)).complete(request())
    assert type(excinfo.value) in (ProviderResponseError, ProviderError)
    assert KEY not in str(excinfo.value)


# Errors


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (400, ProviderError),
        (404, ProviderError),
        (500, ProviderUnavailableError),
        (529, ProviderUnavailableError),
    ],
)
async def test_http_errors(status: int, expected: type[ProviderError]) -> None:
    recorder = Recorder(error(status))

    with pytest.raises(ProviderError) as excinfo:
        await provider(recorder).complete(request())

    assert type(excinfo.value) is expected
    assert KEY not in str(excinfo.value)
    assert PROMPT not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


@pytest.mark.anyio
async def test_rate_limit_carries_retry_after() -> None:
    recorder = Recorder(error(429, {"retry-after": "120"}))

    with pytest.raises(ProviderRateLimitError) as excinfo:
        await provider(recorder, retries=2).complete(request())

    assert excinfo.value.retry_after_seconds == 120
    assert len(recorder.requests) == 1


@pytest.mark.anyio
async def test_rate_limit_is_retried_then_succeeds() -> None:
    recorder = Recorder(error(429, {"retry-after": "1"}), message([{"type": "text", "text": "x"}]))

    response = await provider(recorder, retries=1).complete(request())

    assert response.content == "x"
    assert len(recorder.requests) == 2


@pytest.mark.anyio
async def test_overload_is_retried_and_bounded() -> None:
    recorder = Recorder(error(529), error(529), error(529))

    with pytest.raises(ProviderUnavailableError):
        await provider(recorder, retries=2).complete(request())

    assert len(recorder.requests) == 3


@pytest.mark.anyio
async def test_authentication_failure_is_not_retried() -> None:
    recorder = Recorder(error(401), error(401))

    with pytest.raises(ProviderAuthenticationError):
        await provider(recorder, retries=3).complete(request())

    assert len(recorder.requests) == 1


@pytest.mark.anyio
async def test_connection_failure() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderUnavailableError):
        await provider(Recorder(refuse)).complete(request())


@pytest.mark.anyio
async def test_timeout() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    with pytest.raises(ProviderUnavailableError, match="did not answer in time"):
        await provider(Recorder(slow)).complete(request())


@pytest.mark.anyio
async def test_nothing_sensitive_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(error(500), message([{"type": "text", "text": f"reply {PROMPT}"}]))

    await provider(recorder, retries=1).complete(request())

    assert logging.getLogger("anthropic").getEffectiveLevel() >= logging.WARNING
    for record in caplog.records:
        text = record.getMessage()
        assert KEY not in text
        assert PROMPT not in text
