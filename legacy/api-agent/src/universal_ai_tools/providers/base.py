"""The provider interface: one model turn, in provider-neutral models.

A provider adapter subclasses ``Provider`` and implements ``_complete``: translate a
``ProviderRequest`` into its provider's format, send it, and translate the answer into a
``ProviderResponse``. Callers use ``complete``, which every provider shares and none can
override. It is where the contract below is enforced, so an adapter that gets it wrong
still cannot hand a caller anything else:

* ``complete`` returns a validated ``ProviderResponse`` or raises a ``ProviderError``.
  Any other exception from an adapter becomes a ``ProviderError`` with a fixed message and
  no chained cause; only its type name is logged, never its message, which may quote a
  prompt, a header, or a response body. ``asyncio.CancelledError`` and other
  ``BaseException`` subclasses pass through, so cancellation still works.
* A provider never runs tools. Tool calls in a response are requests for the caller.

Credentials, endpoints, and timeouts belong to the adapter's constructor and are passed in
explicitly. Nothing in this package reads the environment.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import final

from universal_ai_tools.providers.errors import ProviderError, ProviderResponseError
from universal_ai_tools.providers.models import ProviderRequest, ProviderResponse

logger = logging.getLogger(__name__)


class Provider(ABC):
    """A source of model turns."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short, fixed identifier of the provider, for logs and records."""

    @final
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Ask the model for its next turn in the conversation ``request`` describes.

        Raises:
            TypeError: If ``request`` is not a ``ProviderRequest``.
            ProviderError: If no valid response was obtained. The message is safe to show.
        """
        if not isinstance(request, ProviderRequest):
            raise TypeError("request must be a ProviderRequest")
        try:
            response = await self._complete(request)
        except ProviderError:
            raise
        except Exception as exc:
            logger.warning("provider %s failed: %s", self.name, type(exc).__name__)
            raise ProviderError() from None
        if not isinstance(response, ProviderResponse):
            logger.warning("provider %s returned %s", self.name, type(response).__name__)
            raise ProviderResponseError()
        return response

    @abstractmethod
    async def _complete(self, request: ProviderRequest) -> ProviderResponse:
        """Send ``request`` and return the provider's answer as a ``ProviderResponse``.

        Raise a ``ProviderError`` subclass, ``from None``, for every failure the adapter
        can classify, with a fixed message.
        """
