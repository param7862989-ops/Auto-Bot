"""Hard limits on one task, enforced by the orchestrator whatever the model does.

A ``Budget`` is created per task from the ``AgentConfig`` (optionally lowered, never
raised, by the caller). The orchestrator asks it before every model request and every
tool call, and reports every response and every tool call to it. When a limit is reached
it raises ``BudgetExhausted`` naming the limit; the orchestrator then stops the task.

Loop detection is part of the budget: the same tool call (same name and arguments) or
the same model turn (same text and calls) may occur at most ``max_repeated_calls`` times,
and at most ``max_consecutive_failures`` tool calls in a row may fail or be refused.

Token counts come from the provider. A count the provider did not report is replaced by
an upper bound (see ``models.Usage``), so missing usage can never make tokens free.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from universal_ai_tools.agent.config import AgentConfig
from universal_ai_tools.agent.models import LimitName, Usage
from universal_ai_tools.providers.models import JsonObject, ProviderResponse, ToolCall


class BudgetExhausted(Exception):
    """A hard limit was reached. The message is safe to show."""

    def __init__(self, limit: LimitName, message: str) -> None:
        super().__init__(message)
        self.limit = limit


@dataclass(frozen=True)
class Limits:
    max_steps: int
    max_tool_calls: int
    max_task_seconds: float
    max_input_tokens: int
    max_output_tokens: int
    max_output_tokens_per_request: int
    max_tool_result_chars: int
    max_context_chars: int
    max_repeated_calls: int
    max_consecutive_failures: int

    @classmethod
    def from_config(
        cls,
        config: AgentConfig,
        *,
        max_steps: int | None = None,
        max_tool_calls: int | None = None,
    ) -> Limits:
        """The configured limits, with ``max_steps`` and ``max_tool_calls`` optionally lowered.

        A requested value above the configured one is ignored: a caller can narrow a
        task's budget but never widen it.
        """
        return cls(
            max_steps=_lower(config.max_steps, max_steps),
            max_tool_calls=_lower(config.max_tool_calls, max_tool_calls),
            max_task_seconds=config.max_task_seconds,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_output_tokens_per_request=config.max_output_tokens_per_request,
            max_tool_result_chars=config.max_tool_result_chars,
            max_context_chars=config.max_context_chars,
            max_repeated_calls=config.max_repeated_calls,
            max_consecutive_failures=config.max_consecutive_failures,
        )


def _lower(configured: int, requested: int | None) -> int:
    if requested is None:
        return configured
    if requested < 1:
        raise ValueError("a requested limit must be at least 1")
    return min(configured, requested)


def call_signature(name: str, arguments: JsonObject) -> str:
    """A canonical form of a tool call, the same for equal name and arguments."""
    return json.dumps([name, arguments], sort_keys=True, separators=(",", ":"))


def turn_signature(response: ProviderResponse) -> str:
    """A canonical form of a model turn, ignoring tool call ids and usage."""
    calls = [[call.name, call.arguments] for call in response.tool_calls]
    return json.dumps(
        [response.stop_reason, response.content, calls], sort_keys=True, separators=(",", ":")
    )


class Budget:
    """The running totals of one task, checked against its ``Limits``."""

    def __init__(self, limits: Limits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.steps = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated = False
        self.provider_requests = 0
        self.consecutive_failures = 0
        self._calls: Counter[str] = Counter()
        self._turns: Counter[str] = Counter()

    # Time

    def elapsed(self) -> float:
        return self._clock() - self._started

    def remaining_seconds(self) -> float:
        return self.limits.max_task_seconds - self.elapsed()

    def check_time(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExhausted("max_task_seconds", "the task ran out of time")

    # Model turns

    def before_request(self, context_chars: int) -> int:
        """Admit one more model request. Returns the ``max_output_tokens`` to send.

        Raises:
            BudgetExhausted: If a step, time, token, or context limit is reached.
        """
        self.check_time()
        if self.steps >= self.limits.max_steps:
            raise BudgetExhausted("max_steps", "the task reached its step limit")
        if self.input_tokens >= self.limits.max_input_tokens:
            raise BudgetExhausted("max_input_tokens", "the task used its input token budget")
        remaining_output = self.limits.max_output_tokens - self.output_tokens
        if remaining_output <= 0:
            raise BudgetExhausted("max_output_tokens", "the task used its output token budget")
        if context_chars > self.limits.max_context_chars:
            raise BudgetExhausted(
                "max_context_chars", "the conversation grew beyond the context limit"
            )
        self.steps += 1
        return min(self.limits.max_output_tokens_per_request, remaining_output)

    def after_response(
        self, response: ProviderResponse, *, request_chars: int, max_output_tokens: int
    ) -> None:
        """Count a response's tokens, and refuse a turn the model keeps repeating.

        Raises:
            BudgetExhausted: If the same turn has now occurred more often than allowed.
        """
        self.provider_requests += 1
        usage = response.usage
        if usage.input_tokens is None:
            self.estimated = True
            self.input_tokens += request_chars
        else:
            self.input_tokens += usage.input_tokens
        if usage.output_tokens is None:
            self.estimated = True
            self.output_tokens += max_output_tokens
        else:
            self.output_tokens += usage.output_tokens
        signature = turn_signature(response)
        self._turns[signature] += 1
        if self._turns[signature] > self.limits.max_repeated_calls:
            raise BudgetExhausted(
                "max_repeated_calls", "the model repeated the same turn without progress"
            )

    # Tool calls

    def admit_tool_call(self, call: ToolCall) -> None:
        """Count one requested tool call before anything else is done with it.

        Raises:
            BudgetExhausted: If the tool call limit is reached, or this exact call has
                already been requested ``max_repeated_calls`` times.
        """
        self.check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExhausted("max_tool_calls", "the task reached its tool call limit")
        self.tool_calls += 1
        signature = call_signature(call.name, call.arguments)
        self._calls[signature] += 1
        if self._calls[signature] > self.limits.max_repeated_calls:
            raise BudgetExhausted(
                "max_repeated_calls", "the model repeated the same tool call too often"
            )

    def tool_call_finished(self, *, ok: bool) -> None:
        """Record whether a tool call succeeded.

        Raises:
            BudgetExhausted: If too many tool calls in a row have failed or been refused.
        """
        if ok:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.limits.max_consecutive_failures:
            raise BudgetExhausted(
                "max_consecutive_failures", "too many tool calls in a row failed or were refused"
            )

    def usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            estimated=self.estimated,
            provider_requests=self.provider_requests,
        )


def context_chars(parts: Sequence[str]) -> int:
    return sum(len(part) for part in parts)
