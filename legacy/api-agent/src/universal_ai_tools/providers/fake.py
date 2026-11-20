"""A deterministic provider that replays a script, for tests.

``FakeProvider`` answers each ``complete`` call with the next step of its script and
records every request it was given. It has no network access, starts no process, reads no
file, and has no randomness: the same script and requests give the same results.

A step is one of:

* a ``ProviderResponse``, returned as it is;
* a ``ProviderError``, raised, as an adapter would on a failure;
* a mapping, validated as a ``ProviderResponse`` only when its turn comes, as an adapter
  validates what its provider sent. An invalid mapping raises ``ProviderResponseError``, so
  a script can contain the malformed answers a real provider might give.

When the script runs out, ``complete`` raises ``FakeProviderExhaustedError``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import ValidationError

from universal_ai_tools.providers.base import Provider
from universal_ai_tools.providers.errors import ProviderError, ProviderResponseError
from universal_ai_tools.providers.models import (
    ProviderRequest,
    ProviderResponse,
    StopReason,
    TokenUsage,
    ToolCall,
)

FakeStep = ProviderResponse | ProviderError | Mapping[str, object]


class FakeProviderExhaustedError(ProviderError):
    """``complete`` was called more often than the script has steps."""

    default_message = "the fake provider has no scripted response left"


class FakeProvider(Provider):
    """Replays ``script``, one step per ``complete`` call, and records the requests."""

    def __init__(self, script: Iterable[FakeStep], *, name: str = "fake") -> None:
        steps: list[FakeStep] = []
        for step in script:
            if isinstance(step, ProviderResponse | ProviderError):
                steps.append(step)
            elif isinstance(step, Mapping):
                steps.append(dict(step))
            else:
                raise TypeError(
                    "a script step must be a ProviderResponse, a ProviderError, or a mapping"
                )
        self._steps = tuple(steps)
        self._next = 0
        self._name = name
        self._requests: list[ProviderRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def requests(self) -> tuple[ProviderRequest, ...]:
        """Every request received, in order, including those answered with an error."""
        return tuple(self._requests)

    @property
    def remaining(self) -> int:
        """Steps not yet used."""
        return len(self._steps) - self._next

    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        self._requests.append(request)
        if self._next == len(self._steps):
            raise FakeProviderExhaustedError()
        step = self._steps[self._next]
        self._next += 1
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, ProviderResponse):
            return step
        try:
            return ProviderResponse.model_validate(step)
        except ValidationError:
            raise ProviderResponseError() from None


def text_response(
    content: str = "",
    *,
    stop_reason: StopReason = "end_turn",
    usage: TokenUsage | None = None,
    provider_stop_reason: str | None = None,
) -> ProviderResponse:
    """A response with text and no tool calls."""
    return ProviderResponse(
        content=content,
        stop_reason=stop_reason,
        usage=TokenUsage() if usage is None else usage,
        provider_stop_reason=provider_stop_reason,
    )


def tool_call_response(
    *calls: ToolCall, content: str = "", usage: TokenUsage | None = None
) -> ProviderResponse:
    """A response asking for ``calls`` to be run."""
    return ProviderResponse(
        content=content,
        tool_calls=calls,
        stop_reason="tool_calls",
        usage=TokenUsage() if usage is None else usage,
    )
