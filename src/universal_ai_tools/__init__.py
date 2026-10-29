"""Universal AI Tools: a local-first, AI-agnostic developer automation platform.

Importing this package has no side effects: it does not configure logging,
read configuration, or start the MCP server. See ``universal_ai_tools.server``.
"""

__version__ = "0.1.0"

APP_NAME = "universal-ai-tools"

__all__ = ["APP_NAME", "__version__"]
