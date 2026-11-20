"""Shared adapter machinery: retries, error classification, the HTTP client, and the factory."""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest

from agent_support import agent_config
from universal_ai_tools.adapters._common import (
    MAX_RETRY_AFTER_SECONDS,
    build_http_client,
    classify_status,
    retry_after_seconds,
    with_retries,
)
from universal_ai_tools.adapters.factory import ProviderSetupError, create_provider
from universal_ai_tools.agent.config import AgentConfigError
from universal_ai_tools.providers import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def failing(*errors: BaseException, result: str = "ok") -> Callable[[], Awaitable[str]]:
    remaining = list(errors)

    async def attempt() -> str:
        if remaining:
            raise remaining.pop(0)
        return result

    return attempt


# Error classification


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderAuthenticationError),
        (429, ProviderRateLimitError),
        (408, ProviderUnavailableError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
        (529, ProviderUnavailableError),
        (400, ProviderError),
        (404, ProviderError),
        (413, ProviderError),
        (422, ProviderError),
    ],
)
def test_status_classification(status: int, error: type[ProviderError]) -> None:
    classified = classify_status(status, {})
    assert type(classified) is error


def test_rejected_request_is_not_retryable() -> None:
    assert classify_status(400, None).retryable is False


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "3"}, 3.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "-4"}, 0.0),
        ({"retry-after": "soon"}, None),
        ({"retry-after": "nan"}, None),
        ({}, None),
    ],
)
def test_retry_after(headers: dict[str, str], expected: float | None) -> None:
    assert retry_after_seconds(headers) == expected


# Retries


@pytest.mark.anyio
async def test_retries_unavailable_then_succeeds() -> None:
    sleeps = Sleeps()
    result = await with_retries(
        failing(ProviderUnavailableError(), ProviderUnavailableError()),
        provider="p",
        max_retries=2,
        timeout_seconds=5,
        sleep=sleeps,
    )
    assert result == "ok"
    assert sleeps.delays == [0.5, 1.0]


@pytest.mark.anyio
async def test_retries_are_bounded() -> None:
    sleeps = Sleeps()
    with pytest.raises(ProviderUnavailableError):
        await with_retries(
            failing(*[ProviderUnavailableError() for _ in range(5)]),
            provider="p",
            max_retries=2,
            timeout_seconds=5,
            sleep=sleeps,
        )
    assert len(sleeps.delays) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ProviderAuthenticationError(), ProviderResponseError(), ProviderError("rejected")]
)
async def test_non_retryable_errors_are_raised_at_once(error: ProviderError) -> None:
    sleeps = Sleeps()
    with pytest.raises(type(error)):
        await with_retries(
            failing(error), provider="p", max_retries=3, timeout_seconds=5, sleep=sleeps
        )
    assert sleeps.delays == []


@pytest.mark.anyio
async def test_rate_limit_waits_for_retry_after() -> None:
    sleeps = Sleeps()
    result = await with_retries(
        failing(ProviderRateLimitError(retry_after_seconds=4)),
        provider="p",
        max_retries=1,
        timeout_seconds=5,
        sleep=sleeps,
    )
    assert result == "ok"
    assert sleeps.delays == [4]


@pytest.mark.anyio
async def test_a_long_retry_after_is_not_waited_for() -> None:
    sleeps = Sleeps()
    with pytest.raises(ProviderRateLimitError):
        await with_retries(
            failing(ProviderRateLimitError(retry_after_seconds=MAX_RETRY_AFTER_SECONDS + 1)),
            provider="p",
            max_retries=3,
            timeout_seconds=5,
            sleep=sleeps,
        )
    assert sleeps.delays == []


@pytest.mark.anyio
async def test_each_attempt_is_bounded_in_time() -> None:
    async def hang() -> str:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.fail_after(5):
        with pytest.raises(ProviderUnavailableError, match="did not answer in time"):
            await with_retries(hang, provider="p", max_retries=0, timeout_seconds=0.05)


@pytest.mark.anyio
async def test_cancellation_is_not_swallowed() -> None:
    attempts = 0

    async def slow() -> str:
        nonlocal attempts
        attempts += 1
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    with anyio.move_on_after(0.05) as scope:
        await with_retries(slow, provider="p", max_retries=5, timeout_seconds=10)
    assert scope.cancelled_caught
    assert attempts == 1


# The HTTP client


def test_http_client_ignores_the_environment() -> None:
    client = build_http_client(None)
    assert client.trust_env is False
    assert client.follow_redirects is False


# The factory


def test_factory_builds_the_selected_provider() -> None:
    pytest.importorskip("anthropic")
    pytest.importorskip("openai")
    assert create_provider(agent_config(provider="anthropic")).name == "anthropic"
    assert create_provider(agent_config(provider="openai")).name == "openai"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_missing_sdk_is_a_clear_setup_error(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def without_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.partition(".")[0] == provider:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)

    with pytest.raises(ProviderSetupError, match=rf"universal-ai-tools\[{provider}\]") as excinfo:
        create_provider(agent_config(provider=provider))
    assert issubclass(ProviderSetupError, AgentConfigError)
    assert "SECRET" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_initialization_failure_is_a_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    import anthropic

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom with sk-test-SECRET-0123456789abcdef")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", broken)

    with pytest.raises(ProviderSetupError, match="could not be initialized") as excinfo:
        create_provider(agent_config(provider="anthropic"))
    assert "SECRET" not in str(excinfo.value)
    assert "boom" not in str(excinfo.value)


def test_importing_adapters_loads_no_sdk() -> None:
    probe = (
        "import json, sys\n"
        "import universal_ai_tools.adapters, universal_ai_tools.adapters.anthropic_provider\n"
        "import universal_ai_tools.adapters.openai_provider, universal_ai_tools.agent.mcp_tool\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('anthropic', 'openai'))))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(completed.stdout) == []
