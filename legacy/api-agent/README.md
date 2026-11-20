# Archived: the API-key agent (Milestone 5G)

This directory holds the opt-in `run_task` agent that Milestone 5G added to Universal AI
Tools. It was moved here unchanged on 2026-11-06, when the architecture was corrected.

**It is not part of the product, not installed, not imported, and not tested.** Nothing in
`src/universal_ai_tools` imports it. `pytest`, `ruff`, and `mypy` do not look at this
directory.

## Why it was removed from the product

The agent ran its own planning loop and called the Anthropic or OpenAI API with a separate
API key. That is the wrong architecture for this project:

- Claude (in Claude Desktop or Claude Code) is the planner and orchestrator. A second model
  loop inside the MCP server duplicates it and costs extra.
- It needed provider API keys and SDKs, which the user does not have and does not want.

Universal AI Tools now only executes operations that Claude asks for through MCP, under the
server's own permission policy, approval, and audit log. See `docs/architecture.md`.

## What was reused

The idea and much of the code of the MCP elicitation approval channel
(`agent/mcp_tool.py`: `ApprovalForm`, `client_supports_form_elicitation`,
`ElicitationApprovalChannel`, the `InputRequiredResult` round trip, and the
`ApprovalDecision` vocabulary from `agent/permissions.py`) now lives, adapted for direct tool
calls, in `src/universal_ai_tools/approval.py`.

## Contents

| Path | What it was |
| ---- | ----------- |
| `src/universal_ai_tools/agent/` | Agent config, budgets, permission policy, planner, orchestrator, verifier, `run_task` MCP tool |
| `src/universal_ai_tools/providers/` | Provider-neutral model interface and `FakeProvider` |
| `src/universal_ai_tools/adapters/` | Anthropic and OpenAI adapters (lazy SDK imports) |
| `tests/` | The agent, provider, and adapter test suites and the agent MCP surface snapshot |
| `docs/agent-security.md` | The agent's threat model |

## Restoring it

The files are byte-for-byte what they were in the working tree before the move. To run them
again you would move the three packages back under `src/universal_ai_tools/`, move the tests
back under `tests/`, restore the `anthropic`, `openai`, and `agent` extras in
`pyproject.toml`, and restore the agent hooks in `server.py` and its settings in
`config.py`. That is not recommended: it reintroduces the API-key dependency.

The server now treats the agent's former environment variables
(`UNIVERSAL_AI_TOOLS_ENABLE_AGENT_TOOLS`, `UNIVERSAL_AI_TOOLS_AGENT_*`,
`UNIVERSAL_AI_TOOLS_ANTHROPIC_API_KEY`, `UNIVERSAL_AI_TOOLS_OPENAI_API_KEY`) as removed
settings: any value stops it with a configuration error, so a stale registration fails
visibly and no key is left in a client configuration unnoticed.
