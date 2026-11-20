"""What both provider adapters share: the HTTP client, retries, and error classification.

Nothing here knows a provider's request or response format. The adapters import their SDK
lazily, so importing this module loads ``httpx2`` (which the MCP SDK already depends on)
but never ``anthropic`` or ``openai``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import anyio
import httpx2

from universal_ai_tools.providers.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

# Loggers of the SDKs and their HTTP stack. At DEBUG the SDKs log whole request options,
# prompts included, and ANTHROPIC_LOG / OPENAI_LOG can switch that on from the environment.
# The adapters hold them at WARNING whatever the server's own log level is.
SDK_LOGGERS = ("anthropic", "openai", "httpx2", "httpcore2", "httpx", "httpcore")

# Backoff between retries, and the longest Retry-After the adapter will wait for. A longer
# wait is reported as a rate limit instead of blocking the task.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0

REJECTED_REQUEST = "the model provider rejected the request"
TIMED_OUT = "the model provider did not answer in time"


def quiet_sdk_loggers() -> None:
    """Hold the SDK loggers at WARNING, so no debug record can carry a prompt."""
    for name in SDK_LOGGERS:
        sdk_logger = logging.getLogger(name)
        if sdk_logger.getEffectiveLevel() < logging.WARNING or sdk_logger.level < logging.WARNING:
            sdk_logger.setLevel(logging.WARNING)


def build_http_client(transport: httpx2.AsyncBaseTransport | None) -> httpx2.AsyncClient:
    """An HTTP client that takes nothing from the environment.

    ``trust_env=False`` makes it ignore ``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY``,
    ``NO_PROXY``, ``SSL_CERT_FILE``, ``SSL_CERT_DIR``, and ``.netrc``. Redirects are not
    followed. ``transport`` replaces the network for tests.
    """
    return httpx2.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    )


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """The delay a ``Retry-After`` (or ``retry-after-ms``) header asks for, if valid."""
    if not headers:
        return None
    try:
        milliseconds = headers.get("retry-after-ms")
        if milliseconds is not None:
            value = float(milliseconds) / 1000
        else:
            raw = headers.get("retry-after")
            if raw is None:
                return None
            try:
                value = float(raw)
            except ValueError:
                when = parsedate_to_datetime(raw)
                value = (when - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, value)


def classify_status(status: int, headers: Mapping[str, str] | None) -> ProviderError:
    """Map an HTTP error status to a provider error with a fixed message."""
    if status in (401, 403):
        return ProviderAuthenticationError()
    if status == 429:
        return ProviderRateLimitError(retry_after_seconds=retry_after_seconds(headers))
    if status in (408, 409) or status >= 500:
        return ProviderUnavailableError()
    return ProviderError(REJECTED_REQUEST)


def classify_sdk_error(
    exc: Exception,
    *,
    status_error: type[Exception],
    timeout_error: type[Exception],
    connection_error: type[Exception],
    validation_error: type[Exception],
    sdk_error: type[Exception],
) -> ProviderError | None:
    """Map one of an SDK's exceptions to a provider error, or ``None`` if it is not one.

    Only the exception's class, status code, and retry headers are used: never its
    message, body, or request, which can quote a prompt or a header.
    """
    if isinstance(exc, timeout_error):
        return ProviderUnavailableError(TIMED_OUT)
    if isinstance(exc, connection_error):
        return ProviderUnavailableError()
    if isinstance(exc, validation_error):
        return ProviderResponseError()
    if isinstance(exc, status_error):
        status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if isinstance(status, int):
            return classify_status(status, headers if isinstance(headers, Mapping) else None)
        return ProviderError()
    if isinstance(exc, sdk_error):
        return ProviderError()
    return None


async def with_retries[T](
    attempt: Callable[[], Awaitable[T]],
    *,
    provider: str,
    max_retries: int,
    timeout_seconds: float,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> T:
    """Run ``attempt``, retrying retryable provider errors up to ``max_retries`` times.

    Each attempt is bounded by ``timeout_seconds`` of wall-clock time, however the HTTP
    client's own timeouts behave. Only ``retryable`` errors are retried: an unavailable
    provider (including a timeout) and a rate limit whose ``Retry-After`` is at most
    ``MAX_RETRY_AFTER_SECONDS``. Authentication failures, rejected requests, and invalid
    responses are raised at once. Cancellation is never caught, so a cancelled task stops
    during an attempt or a backoff.
    """
    retries = 0
    while True:
        try:
            with anyio.fail_after(timeout_seconds):
                return await attempt()
        except TimeoutError:
            error: ProviderError = ProviderUnavailableError(TIMED_OUT)
        except ProviderError as exc:
            error = exc
        if not error.retryable or retries >= max_retries:
            raise error from None
        delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2**retries)
        if isinstance(error, ProviderRateLimitError) and error.retry_after_seconds is not None:
            if error.retry_after_seconds > MAX_RETRY_AFTER_SECONDS:
                raise error from None
            delay = max(delay, error.retry_after_seconds)
        retries += 1
        logger.info(
            "provider %s: %s, retry %d of %d in %.1f s",
            provider,
            type(error).__name__,
            retries,
            max_retries,
            delay,
        )
        await (sleep or anyio.sleep)(delay)
