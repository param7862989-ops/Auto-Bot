"""Provider errors: the only exceptions a ``Provider`` lets out of ``complete``.

Every message is short, fixed text chosen by the code that raises it. A message never
carries an API key, a request header, a prompt, a tool result, or a provider's response
body, and an adapter raises these ``from None`` so that a provider SDK's own exception,
which may hold any of those, is not chained to them either. Messages are escaped and
bounded on construction as a second line of defence, not as permission to pass raw text.
"""

from __future__ import annotations

import math
from typing import ClassVar

from universal_ai_tools.policy import escape_line

MAX_MESSAGE_CHARS = 200


class ProviderError(Exception):
    """A model provider could not produce a usable response."""

    default_message: ClassVar[str] = "the model provider failed"
    retryable: ClassVar[bool] = False
    """Whether the same request may succeed if sent again later."""

    def __init__(self, message: str | None = None) -> None:
        text = self.default_message if message is None else message
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "…"
        super().__init__(escape_line(text))


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached, timed out, or reported itself unavailable."""

    default_message = "the model provider is unavailable"
    retryable = True


class ProviderResponseError(ProviderError):
    """The provider answered, but the answer is not a valid response."""

    default_message = "the model provider returned an invalid response"


class ProviderAuthenticationError(ProviderError):
    """The provider refused the credentials."""

    default_message = "the model provider rejected the credentials"


class ProviderRateLimitError(ProviderError):
    """The provider refused the request because a rate or quota limit was reached."""

    default_message = "the model provider's rate limit was reached"
    retryable = True

    def __init__(
        self, message: str | None = None, *, retry_after_seconds: float | None = None
    ) -> None:
        if retry_after_seconds is not None and not (
            math.isfinite(retry_after_seconds) and retry_after_seconds >= 0
        ):
            raise ValueError("retry_after_seconds must be a finite, non-negative number")
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
