"""FakeProvider, and the contract every Provider shares through ``Provider.complete``."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from universal_ai_tools.providers import (
    AssistantMessage,
    FakeProvider,
    FakeProviderExhaustedError,
    Provider,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    TokenUsage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    text_response,
    tool_call_response,
)

SENTINEL = "sk-do-not-echo-0123456789"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def request(text: str = "Hello.") -> ProviderRequest:
    return ProviderRequest(
        model="test-model", max_output_tokens=256, messages=[UserMessage(content=text)]
    )


def read(call_id: str, path: str) -> ToolCall:
    return ToolCall(id=call_id, name="read_file", arguments={"path": path})


# Scripted responses


@pytest.mark.anyio
async def test_text_response() -> None:
    provider = FakeProvider(
        [text_response("Hi there.", usage=TokenUsage(input_tokens=3, output_tokens=2))]
    )

    response = await provider.complete(request())

    assert response.content == "Hi there."
    assert response.stop_reason == "end_turn"
    assert response.tool_calls == ()
    assert response.usage.total_tokens == 5


@pytest.mark.anyio
async def test_tool_call_response() -> None:
    provider = FakeProvider([tool_call_response(read("c1", "docs/a.txt"))])

    response = await provider.complete(request())

    assert response.stop_reason == "tool_calls"
    assert response.tool_calls == (read("c1", "docs/a.txt"),)


@pytest.mark.anyio
async def test_multiple_tool_calls_keep_their_order() -> None:
    calls = (read("c1", "a"), read("c2", "b"), ToolCall(id="c3", name="list_files", arguments={}))
    provider = FakeProvider([tool_call_response(*calls, content="Reading three things.")])

    response = await provider.complete(request())

    assert response.tool_calls == calls
    assert response.content == "Reading three things."


@pytest.mark.anyio
async def test_empty_response() -> None:
    response = await FakeProvider([text_response()]).complete(request())

    assert response.content == ""
    assert response.tool_calls == ()
    assert response.stop_reason == "end_turn"


@pytest.mark.anyio
async def test_missing_usage() -> None:
    response = await FakeProvider([{"content": "x", "stop_reason": "end_turn"}]).complete(request())

    assert response.usage == TokenUsage()
    assert response.usage.total_tokens is None


@pytest.mark.anyio
async def test_max_tokens_stop() -> None:
    provider = FakeProvider([text_response("The answer is cut o", stop_reason="max_tokens")])

    response = await provider.complete(request())

    assert response.stop_reason == "max_tokens"
    assert response.tool_calls == ()


@pytest.mark.anyio
async def test_unexpected_stop_reason_is_other() -> None:
    provider = FakeProvider(
        [text_response("x", stop_reason="other", provider_stop_reason="new_reason_v9")]
    )

    response = await provider.complete(request())

    assert response.stop_reason == "other"
    assert response.provider_stop_reason == "new_reason_v9"


@pytest.mark.anyio
async def test_a_stop_reason_with_no_neutral_name_is_an_invalid_response() -> None:
    provider = FakeProvider([{"content": "x", "stop_reason": "new_reason_v9"}])

    with pytest.raises(ProviderResponseError):
        await provider.complete(request())


@pytest.mark.anyio
async def test_mapping_steps_are_validated_like_a_provider_answer() -> None:
    provider = FakeProvider(
        [
            {
                "content": "",
                "stop_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}],
                "usage": {"input_tokens": 12},
                "model": "test-model-2",
            }
        ]
    )

    response = await provider.complete(request())

    assert response.tool_calls == (read("c1", "a"),)
    assert response.usage == TokenUsage(input_tokens=12)
    assert response.model == "test-model-2"


@pytest.mark.parametrize(
    "arguments",
    [
        '{"path": "a"}',
        '{"path": ',
        ["a"],
        None,
        5,
        {"path": float("nan")},
        {"path": ("a",)},
        {"path": object()},
    ],
)
@pytest.mark.anyio
async def test_invalid_tool_call_arguments_are_an_invalid_response(arguments: object) -> None:
    provider = FakeProvider(
        [
            {
                "stop_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": arguments}],
            }
        ]
    )

    with pytest.raises(ProviderResponseError) as error:
        await provider.complete(request())

    assert str(error.value) == ProviderResponseError.default_message
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"stop_reason": "end_turn", "content": 5},
        {"stop_reason": "end_turn", "unexpected": True},
        {"stop_reason": "tool_calls"},
        {"stop_reason": "max_tokens", "tool_calls": [{"id": "c", "name": "x", "arguments": {}}]},
        {
            "stop_reason": "tool_calls",
            "tool_calls": [
                {"id": "same", "name": "x", "arguments": {}},
                {"id": "same", "name": "x", "arguments": {}},
            ],
        },
        {"stop_reason": "end_turn", "usage": {"input_tokens": -1}},
        {"stop_reason": "end_turn", "content": SENTINEL, "usage": {"output_tokens": "many"}},
    ],
)
@pytest.mark.anyio
async def test_invalid_responses(payload: dict[str, object]) -> None:
    provider = FakeProvider([payload])

    with pytest.raises(ProviderResponseError) as error:
        await provider.complete(request())

    assert SENTINEL not in str(error.value)


# Errors


@pytest.mark.parametrize(
    "error",
    [
        ProviderError(),
        ProviderUnavailableError(),
        ProviderResponseError(),
        ProviderAuthenticationError(),
        ProviderRateLimitError(retry_after_seconds=3),
    ],
)
@pytest.mark.anyio
async def test_scripted_errors_are_raised(error: ProviderError) -> None:
    provider = FakeProvider([error, text_response("recovered")])

    with pytest.raises(type(error)) as raised:
        await provider.complete(request())
    response = await provider.complete(request("again"))

    assert raised.value is error
    assert response.content == "recovered"
    assert len(provider.requests) == 2


# Sequences and exhaustion


@pytest.mark.anyio
async def test_steps_are_used_in_order_and_requests_are_recorded() -> None:
    provider = FakeProvider([tool_call_response(read("c1", "a")), text_response("a says hello")])
    first = request("Read a.")

    turn_one = await provider.complete(first)
    second = ProviderRequest(
        model="test-model",
        max_output_tokens=256,
        messages=[
            *first.messages,
            AssistantMessage(tool_calls=turn_one.tool_calls),
            ToolResultMessage(tool_call_id="c1", content="hello"),
        ],
    )
    turn_two = await provider.complete(second)

    assert turn_one.stop_reason == "tool_calls"
    assert turn_two.content == "a says hello"
    assert provider.requests == (first, second)
    assert provider.remaining == 0


@pytest.mark.anyio
async def test_exhaustion() -> None:
    provider = FakeProvider([text_response("only one")])
    await provider.complete(request())

    for _ in range(2):
        with pytest.raises(FakeProviderExhaustedError, match="no scripted response left"):
            await provider.complete(request())

    assert provider.remaining == 0
    assert len(provider.requests) == 3


@pytest.mark.anyio
async def test_an_empty_script_is_exhausted_at_once() -> None:
    provider = FakeProvider([])

    with pytest.raises(FakeProviderExhaustedError):
        await provider.complete(request())


def test_exhaustion_is_a_provider_error() -> None:
    assert issubclass(FakeProviderExhaustedError, ProviderError)


@pytest.mark.anyio
async def test_identical_scripts_give_identical_results() -> None:
    def script() -> list[Any]:
        return [
            tool_call_response(read("c1", "a")),
            ProviderRateLimitError(),
            {"stop_reason": "end_turn", "content": "done"},
            {"stop_reason": "bogus"},
        ]

    async def run() -> list[object]:
        provider = FakeProvider(script())
        outcomes: list[object] = []
        for _ in range(5):
            try:
                outcomes.append((await provider.complete(request())).model_dump())
            except ProviderError as exc:
                outcomes.append((type(exc).__name__, str(exc)))
        return outcomes

    assert await run() == await run()


def test_the_script_is_copied() -> None:
    script: list[Any] = [text_response("a")]
    provider = FakeProvider(script)
    script.append(text_response("b"))

    assert provider.remaining == 1


@pytest.mark.anyio
async def test_mapping_steps_are_copied() -> None:
    step: dict[str, object] = {"stop_reason": "end_turn", "content": "original"}
    provider = FakeProvider([step])
    step["content"] = "changed"

    assert (await provider.complete(request())).content == "original"


@pytest.mark.parametrize("step", ["text", 5, None, ValueError("x"), [("stop_reason", "end_turn")]])
def test_invalid_script_steps_are_refused(step: object) -> None:
    with pytest.raises(TypeError, match="script step"):
        FakeProvider([step])  # type: ignore[list-item]


def test_name() -> None:
    assert FakeProvider([]).name == "fake"
    assert FakeProvider([], name="scripted").name == "scripted"


@pytest.mark.anyio
async def test_requests_must_be_provider_requests() -> None:
    provider = FakeProvider([text_response()])

    with pytest.raises(TypeError, match="ProviderRequest"):
        await provider.complete({"model": "m", "messages": []})  # type: ignore[arg-type]
    assert provider.requests == ()
    assert provider.remaining == 1


# The Provider contract, for adapters that get it wrong


class _Broken(Provider):
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    @property
    def name(self) -> str:
        return "broken"

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome  # type: ignore[return-value]


def test_provider_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        Provider()  # type: ignore[abstract]


@pytest.mark.parametrize(
    "exception",
    [
        RuntimeError(f"HTTP 401: Authorization: Bearer {SENTINEL}"),
        KeyError(SENTINEL),
        ValueError(f"prompt was: {SENTINEL}"),
        TimeoutError(SENTINEL),
    ],
)
@pytest.mark.anyio
async def test_unexpected_exceptions_become_safe_provider_errors(
    exception: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderError) as error:
        await _Broken(exception).complete(request())

    assert type(error.value) is ProviderError
    assert str(error.value) == ProviderError.default_message
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert SENTINEL not in caplog.text
    assert type(exception).__name__ in caplog.text


@pytest.mark.anyio
async def test_provider_errors_from_adapters_pass_through() -> None:
    error = ProviderAuthenticationError()

    with pytest.raises(ProviderAuthenticationError) as raised:
        await _Broken(error).complete(request())

    assert raised.value is error


@pytest.mark.parametrize("outcome", [None, {"stop_reason": "end_turn"}, "text", 5])
@pytest.mark.anyio
async def test_an_adapter_must_return_a_provider_response(
    outcome: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderResponseError):
        await _Broken(outcome).complete(request())

    assert "end_turn" not in caplog.text


@pytest.mark.anyio
async def test_cancellation_is_not_swallowed() -> None:
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        await _Broken(asyncio.CancelledError()).complete(request())


@pytest.mark.anyio
async def test_keyboard_interrupt_is_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        await _Broken(KeyboardInterrupt()).complete(request())
