"""The agent core: tasks, budgets, permissions, planning, orchestration, and verification.

A model plans; the orchestrator decides and executes. The model's tool calls run only
through the ``ToolRegistry`` the server was built with, after the permission policy and
the budget have allowed them, and a human approves every call that needs approval. See
``orchestrator`` for the loop and ``permissions`` for the policy.

Nothing here is imported by the server unless the agent is enabled
(``UNIVERSAL_AI_TOOLS_ENABLE_AGENT_TOOLS=true``), and nothing here imports a provider SDK.
Importing this package has no side effects.
"""
