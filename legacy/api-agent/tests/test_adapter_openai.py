"""OpenAIProvider, run through the real ``openai`` SDK over a mock HTTP transport.

No test makes a network call or uses a real key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

import anyio
import pytest

pytest.importorskip("openai")
import httpx2
from pydantic import SecretStr

from universal_ai_tools.adapters.openai_provider import ERROR_PREFIX, OpenAIProvider
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

KEY = "sk-openai-test-SECRET-0123456789"
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


def completion(
    content: str | None = "ok",
    finish_reason: str | None = "stop",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | Literal[False] | None = None,
    refusal: str | None = None,
    choices: int = 1,
) -> httpx2.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [
            {"index": i, "finish_reason": finish_reason, "message": message} for i in range(choices)
        ],
    }
    if usage is not False:
        body["usage"] = usage or {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18}
    return httpx2.Response(200, json=body)


def function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def error(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        json={"error": {"message": f"echo {KEY} {PROMPT}", "type": "x", "code": "x"}},
    )


def provider(recorder: Recorder, *, retries: int = 0) -> OpenAIProvider:
    return OpenAIProvider(
        api_key=SecretStr(KEY),
        timeout_seconds=5,
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
        model="gpt-test",
        max_output_tokens=222,
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
    recorder = Recorder(completion())
    conversation = request(
        SystemMessage(content="Be careful."),
        UserMessage(content="Read a."),
        AssistantMessage(
            content="",
            tool_calls=(
                ToolCall(id="call_1", name="read_file", arguments={"path": "a"}),
                ToolCall(id="call_2", name="read_file", arguments={"path": "b"}),
            ),
        ),
        ToolResultMessage(tool_call_id="call_1", content="A"),
        ToolResultMessage(tool_call_id="call_2", content="not found", is_error=True),
    )

    await provider(recorder).complete(conversation)

    assert recorder.requests[0].url == "https://api.openai.com/v1/chat/completions"
    body = recorder.body()
    assert body["model"] == "gpt-test"
    assert body["max_completion_tokens"] == 222
    assert "max_tokens" not in body
    assert body["n"] == 1
    assert body["store"] is False
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": READ.input_schema,
                "strict": False,
            },
        }
    ]
    assert body["messages"] == [
        {"role": "system", "content": "Be careful."},
        {"role": "user", "content": "Read a."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                function_call("call_1", "read_file", '{"path":"a"}'),
                function_call("call_2", "read_file", '{"path":"b"}'),
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "A"},
        {"role": "tool", "tool_call_id": "call_2", "content": ERROR_PREFIX + "not found"},
    ]


@pytest.mark.anyio
async def test_no_tools() -> None:
    recorder = Recorder(completion())

    await provider(recorder).complete(request(tools=()))

    assert "tools" not in recorder.body()


@pytest.mark.anyio
async def test_key_endpoint_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-environment")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-environment")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-environment")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-environment")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Leak: environment")
    recorder = Recorder(completion())

    await provider(recorder).complete(request())

    sent = recorder.requests[0]
    assert sent.url.host == "api.openai.com"
    headers = {name.lower(): value for name, value in sent.headers.items()}
    assert headers["authorization"] == f"Bearer {KEY}"
    assert "openai-organization" not in headers
    assert "openai-project" not in headers
    assert "x-leak" not in headers
    assert not any("environment" in value for value in headers.values())


# Response conversion


@pytest.mark.anyio
async def test_text_response() -> None:
    response = await provider(Recorder(completion("Hi."))).complete(request())

    assert response.content == "Hi."
    assert response.stop_reason == "end_turn"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (13, 5)
    assert response.model == "gpt-test"


@pytest.mark.anyio
async def test_multiple_tool_calls() -> None:
    recorder = Recorder(
        completion(
            None,
            "tool_calls",
            [
                function_call("call_a", "read_file", '{"path": "a"}'),
                function_call("call_b", "list_files", "{}"),
            ],
        )
    )

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "tool_calls"
    assert response.tool_calls == (
        ToolCall(id="call_a", name="read_file", arguments={"path": "a"}),
        ToolCall(id="call_b", name="list_files", arguments={}),
    )
    assert response.content == ""


@pytest.mark.anyio
async def test_tool_calls_with_stop_are_tool_calls() -> None:
    recorder = Recorder(completion(None, "stop", [function_call("c1", "list_files", "{}")]))

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "tool_calls"
    assert len(response.tool_calls) == 1


@pytest.mark.anyio
async def test_tool_calls_cut_off_at_length_are_dropped() -> None:
    recorder = Recorder(completion(None, "length", [function_call("c1", "list_files", "{}")]))

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "max_tokens"
    assert response.tool_calls == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("raw", "neutral"),
    [("stop", "end_turn"), ("length", "max_tokens"), ("content_filter", "content_filter")],
)
async def test_stop_reasons(raw: str, neutral: str) -> None:
    response = await provider(Recorder(completion("x", raw))).complete(request())
    assert response.stop_reason == neutral


@pytest.mark.anyio
async def test_refusal_is_content_filter() -> None:
    recorder = Recorder(completion(None, "stop", refusal="I can't help with that."))

    response = await provider(recorder).complete(request())

    assert response.stop_reason == "content_filter"
    assert response.content == "I can't help with that."


@pytest.mark.anyio
async def test_missing_usage_is_unknown() -> None:
    response = await provider(Recorder(completion(usage=False))).complete(request())

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None


# Malformed responses


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        completion(choices=0),
        completion(choices=2),
        completion(finish_reason=None),
        completion(None, "tool_calls", []),
        completion(None, "tool_calls", [function_call("c1", "read_file", "{not json")]),
        completion(None, "tool_calls", [function_call("c1", "read_file", '["a"]')]),
        completion(None, "tool_calls", [function_call("c1", "read_file", '{"a": 1, "a": 2}')]),
        completion(None, "tool_calls", [function_call("c1", "read_file", '{"n": NaN}')]),
        completion(None, "tool_calls", [function_call("c1", "read_file", '{"n": 1e999}')]),
        completion(None, "tool_calls", [function_call("bad id!", "read_file", "{}")]),
        completion(None, "tool_calls", [function_call("c1", "bad name", "{}")]),
        completion(
            None,
            "tool_calls",
            [function_call("c1", "list_files", "{}"), function_call("c1", "list_files", "{}")],
        ),
        completion(
            None,
            "tool_calls",
            [{"id": "c1", "type": "custom", "custom": {"name": "x", "input": "y"}}],
        ),
        completion(usage={"prompt_tokens": -3, "completion_tokens": 1, "total_tokens": 1}),
        httpx2.Response(200, content=b"not json"),
    ],
    ids=[
        "no-choice",
        "two-choices",
        "no-finish-reason",
        "tool-calls-without-call",
        "invalid-json",
        "not-an-object",
        "duplicate-keys",
        "nan",
        "infinite",
        "malformed-id",
        "malformed-name",
        "duplicate-ids",
        "custom-tool",
        "negative-usage",
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
        (503, ProviderUnavailableError),
    ],
)
async def test_http_errors(status: int, expected: type[ProviderError]) -> None:
    with pytest.raises(ProviderError) as excinfo:
        await provider(Recorder(error(status))).complete(request())

    assert type(excinfo.value) is expected
    assert KEY not in str(excinfo.value)
    assert PROMPT not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.anyio
async def test_rate_limit() -> None:
    recorder = Recorder(error(429, {"retry-after": "90"}))

    with pytest.raises(ProviderRateLimitError) as excinfo:
        await provider(recorder, retries=3).complete(request())

    assert excinfo.value.retry_after_seconds == 90
    assert len(recorder.requests) == 1


@pytest.mark.anyio
async def test_rate_limit_is_retried_then_succeeds() -> None:
    recorder = Recorder(error(429, {"retry-after": "2"}), completion("later"))

    response = await provider(recorder, retries=1).complete(request())

    assert response.content == "later"


@pytest.mark.anyio
async def test_timeout() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    with pytest.raises(ProviderUnavailableError, match="did not answer in time"):
        await provider(Recorder(slow)).complete(request())


@pytest.mark.anyio
async def test_connection_failure_is_retried() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    recorder = Recorder(refuse, completion("ok"))

    response = await provider(recorder, retries=1).complete(request())

    assert response.content == "ok"
    assert len(recorder.requests) == 2


@pytest.mark.anyio
async def test_nothing_sensitive_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(error(503), completion(f"reply {PROMPT}"))

    await provider(recorder, retries=1).complete(request())

    assert logging.getLogger("openai").getEffectiveLevel() >= logging.WARNING
    for record in caplog.records:
        text = record.getMessage()
        assert KEY not in text
        assert PROMPT not in text
