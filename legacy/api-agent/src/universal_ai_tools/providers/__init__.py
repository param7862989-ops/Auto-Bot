"""Provider-independent interface to language models.

The core describes a model turn with the neutral models in ``models``, asks a ``Provider``
for it, and gets a neutral ``ProviderResponse`` back or a ``ProviderError``. No provider
SDK is imported here. Adapters for real providers live apart from this package, in
``universal_ai_tools.adapters``. ``FakeProvider`` replays a script for tests.

This package defines data and an interface only. It does not run tools, touch files, start
processes, open network connections, or read the environment.
"""

from universal_ai_tools.providers.base import Provider
from universal_ai_tools.providers.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from universal_ai_tools.providers.fake import (
    FakeProvider,
    FakeProviderExhaustedError,
    FakeStep,
    text_response,
    tool_call_response,
)
from universal_ai_tools.providers.models import (
    AssistantMessage,
    JsonObject,
    Message,
    MessageRole,
    ProviderRequest,
    ProviderResponse,
    StopReason,
    SystemMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
    parse_tool_arguments,
    tool_definitions,
)

__all__ = [
    "AssistantMessage",
    "FakeProvider",
    "FakeProviderExhaustedError",
    "FakeStep",
    "JsonObject",
    "Message",
    "MessageRole",
    "Provider",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "StopReason",
    "SystemMessage",
    "TokenUsage",
    "ToolCall",
    "ToolDefinition",
    "ToolResultMessage",
    "UserMessage",
    "parse_tool_arguments",
    "text_response",
    "tool_call_response",
    "tool_definitions",
]
