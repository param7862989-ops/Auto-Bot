"""Adapters that put real model providers behind the neutral ``Provider`` interface.

``universal_ai_tools.providers`` stays a layer of data and interfaces with no network or
SDK; that is enforced by its own tests. The adapters live here, apart from it, because
they do need both. Each adapter imports its SDK only when it is constructed, and
``create_provider`` imports an adapter module only when that provider is selected, so
importing this package loads neither ``anthropic`` nor ``openai``.

Adapters translate requests and responses and nothing else. They never run tools, read
files, touch the workspace, import the server, read the environment, or log prompts or
response content.
"""

from universal_ai_tools.adapters.factory import ProviderSetupError, create_provider

__all__ = ["ProviderSetupError", "create_provider"]
