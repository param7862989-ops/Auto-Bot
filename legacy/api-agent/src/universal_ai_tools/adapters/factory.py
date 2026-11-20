"""Build the provider an ``AgentConfig`` selects, importing its SDK only then."""

from __future__ import annotations

from typing import TYPE_CHECKING

from universal_ai_tools.agent.config import AgentConfigError

if TYPE_CHECKING:
    import httpx2

    from universal_ai_tools.agent.config import AgentConfig
    from universal_ai_tools.providers.base import Provider

_EXTRAS = {"anthropic": "anthropic", "openai": "openai"}


class ProviderSetupError(AgentConfigError):
    """The selected provider cannot be used: its SDK is missing or it failed to start."""


def create_provider(
    config: AgentConfig, *, transport: httpx2.AsyncBaseTransport | None = None
) -> Provider:
    """Construct the adapter for ``config.provider``.

    Only the selected provider's adapter module and SDK are imported. There is no
    fallback: a provider that cannot be built is an error, never a switch to another.

    Raises:
        ProviderSetupError: If the SDK is not installed or the client cannot be built. The
            message names the provider and the extra to install, and never the key.
    """
    try:
        if config.provider == "anthropic":
            from universal_ai_tools.adapters.anthropic_provider import AnthropicProvider

            return AnthropicProvider(
                api_key=config.api_key,
                timeout_seconds=config.request_timeout_seconds,
                max_retries=config.max_retries,
                transport=transport,
            )
        if config.provider == "openai":
            from universal_ai_tools.adapters.openai_provider import OpenAIProvider

            return OpenAIProvider(
                api_key=config.api_key,
                timeout_seconds=config.request_timeout_seconds,
                max_retries=config.max_retries,
                transport=transport,
            )
    except ImportError:
        raise ProviderSetupError(
            f"the '{config.provider}' provider needs its SDK, which is not installed; "
            f"install it with: pip install 'universal-ai-tools[{_EXTRAS[config.provider]}]'"
        ) from None
    except Exception as exc:
        raise ProviderSetupError(
            f"the '{config.provider}' provider could not be initialized ({type(exc).__name__})"
        ) from None
    raise ProviderSetupError("unsupported provider")
