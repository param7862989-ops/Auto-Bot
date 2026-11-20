# Agent security: threat model, boundaries, and residual risks

This document covers the opt-in agent (`run_task`, Milestones 5C–5G). The filesystem
server's own threat model, including the open Linux hard-link defect, is in
[architecture.md](architecture.md#known-limitations) and applies unchanged: the agent
reaches the filesystem only through the same tools.

## What is being protected

- **The host and everything outside the workspace.** The agent must not be able to read,
  create, or change anything outside the authorized workspace, run code, or start processes.
- **The workspace.** The agent must not change existing files at all, and must not create a
  file unless a human approved that exact file.
- **Credentials.** The provider key must not appear in logs, errors, task records, tool
  results, or anything sent to the MCP client or the model.
- **The user's attention and money.** A task must stop after bounded time, steps, tool
  calls, and tokens, whatever the model does.

## Trust boundaries

| Source | Trusted? | How it is treated |
| --- | --- | --- |
| Server configuration (environment at startup) | Yes | Read once, validated, immutable. Nothing at run time can change it. |
| `SYSTEM_PROMPT` (`agent/planner.py`) | Yes | Fixed text in code, sent as the system message. |
| The objective (`run_task` input) | Partly | It comes from the MCP client, usually written by the calling model on the user's behalf. It is sent as the user message and steers the model, but it cannot change permissions, limits, tools, or the workspace: there is no input for them. |
| Tool definitions | Yes | Generated from the immutable `ToolRegistry`. |
| Tool results: file names, contents, search snippets | **No** | Untrusted data. Sent back only as tool-result messages, escaped by the filesystem layer, cut to a size limit, labelled `"trust": "untrusted"`. |
| Model output: text and tool calls | **No** | Tool calls are requests. Each is checked (registry, strict schema, permission policy, budget) before anything runs. Final text is returned labelled untrusted and is never treated as evidence. |
| Elicitation answers | Only as a human's answer | Only an accepted form whose `decision` is exactly `approve`, matching the form with no other field, approves. See [Human approval](#human-approval). |
| `request_state` echoed by the client (MCP 2026-07-28) | **No** | Sealed with AES-GCM by the MCP SDK (`RequestStateBoundary`, per-process key, 600 s expiry, bound to the request) and verified before the tool sees it. It only names a suspended task. A random nonce kept on the server must also match. |

## Enforcement, independent of the model

Everything below is code in the orchestrator, the budget, or the policy. None of it depends
on the model following instructions.

- **One path to tools.** The model's tool calls run only through `ToolRegistry.invoke`, called
  by the orchestrator. The adapters never see the registry (a source-scan test enforces it).
  The registry is immutable and has no command, network, or delete tool. `run_task` is not in
  the registry, so the agent cannot start another agent.
- **Strict arguments, twice.** Arguments are validated against the tool's input model (strict
  types, no unknown fields) before the permission decision, so a human is never asked about a
  malformed call, and again by `invoke` immediately before dispatch.
- **Central permission policy** (`agent/permissions.py`). Unknown tool: deny. Read-only: allow.
  Create: needs approval, every time. Any other risk: deny. The only inputs are the registry
  and the tool's fixed risk.
- **Budgets** (`agent/budget.py`). Steps, tool calls, wall clock (enforced with a cancel
  scope, not just checked), input and output tokens, conversation size, result size, repeated
  identical calls, repeated identical turns, and consecutive failures. Missing token counts
  are replaced by upper bounds, never by zero.
- **Workspace rules unchanged.** Every filesystem rule (paths, links, credential names,
  metadata, sizes, escaping) applies, because the agent uses the same tools.
- **Fail closed.** Invalid configuration stops the server. A provider error fails the task with
  a fixed message. An unanswerable approval is a refusal. An unexpected exception fails the
  task as `internal_error`, logging only its type.

## Human approval

Approval is asked through MCP **elicitation** in form mode, the only human-facing channel the
server has. The server never treats a model response, a tool result, silence, a timeout, a
cancelled dialog, an error, or a malformed answer as approval.

How the question reaches the human depends on the negotiated MCP protocol, and the server
chooses per request:

1. **The client did not declare the `elicitation` capability (form mode)**: nothing is asked;
   the decision is `unavailable` and the call is refused.
2. **MCP 2025-11-25 or earlier over stdio**: the server sends `elicitation/create` while the
   task waits, bounded by `UNIVERSAL_AI_TOOLS_AGENT_APPROVAL_TIMEOUT_SECONDS`.
3. **MCP 2026-07-28**: the protocol forbids server-initiated requests mid-call, so the task is
   **suspended**. `run_task` returns an `InputRequiredResult` with the question and a sealed
   `request_state`. The client asks the human and retries the call with the answer; the retry
   resumes the same task. A suspended task that is not resumed within the approval timeout is
   stopped as `cancelled`, its call recorded as not approved. At most four tasks can be
   suspended at once.

**What was verified about Claude Code** (from its official MCP documentation, checked for
this milestone):

- Claude Code supports elicitation in form and URL mode and shows a terminal dialog for it. It
  declares the capability on 2026-07-28 connections, and on stdio servers using 2025-11-25 or
  older from Claude Code v2.1.287.
- For MCP tasks, `elicitation/create` is served only in interactive sessions. Behaviour for
  ordinary tool calls in non-interactive (`-p`) mode, and from subagents, is **not documented**:
  assume no human will see the dialog. The server then refuses the write.
- Claude Code has `Elicitation` hooks that can answer elicitation requests automatically.
  **A hook configured by the user can approve on the user's behalf, and the server cannot tell.**
  Do not configure such a hook for this server unless you mean it.
- Claude Code does not document displaying MCP progress notifications, or sending
  `notifications/cancelled` when you press Esc on an ordinary tool call. The server reports
  progress when asked and handles cancellation when it arrives, but relies on neither.

What was tested: both protocol paths and the no-capability path, with the MCP SDK's own
client in-process (`tests/test_agent_mcp.py`). **Not tested:** a real Claude Code session
answering the dialog.

The server cannot prove that a human, rather than client software, produced an answer. It
relies on the client honouring the elicitation contract. That is the limit of what an MCP
server can verify.

## Credentials and data leaving the machine

- Keys are read only from `UNIVERSAL_AI_TOOLS_ANTHROPIC_API_KEY` or
  `UNIVERSAL_AI_TOOLS_OPENAI_API_KEY`, for the selected provider only, and held as `SecretStr`.
- Both SDKs would otherwise read keys, base URLs, organization and project ids, custom
  headers, and proxies from the environment. The adapters pass the key and the fixed public
  endpoint explicitly, discard `ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_CUSTOM_HEADERS`,
  `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`, and `OPENAI_ADMIN_KEY` after construction, turn off the
  Anthropic SDK's OpenTelemetry, and give the SDK an HTTP client with `trust_env=False` (no
  proxy variables, no `SSL_CERT_*`, no `.netrc`) that does not follow redirects. Tests set all
  of those variables and inspect the exact request each SDK sends.
- The SDK loggers (`anthropic`, `openai`, `httpx2`, `httpcore2`) are held at `WARNING`, because
  at `DEBUG` they log request bodies, and `ANTHROPIC_LOG` / `OPENAI_LOG` can turn that on.
- The server's own logs carry task ids, step numbers, tool names, statuses, counts, and
  exception type names. Never prompts, objectives, file contents, model text, or keys.
- **Sent to the provider:** the system prompt, the objective, tool definitions, the model's
  own turns, and every tool result: file names, file contents up to the result limit, search
  snippets. That is the purpose of the feature. Do not enable the agent on a workspace whose
  contents must not reach the provider.
- OpenAI requests set `store: false`. Provider-side retention is otherwise governed by your
  account's terms.

## Prompt injection

Text the agent reads can contain instructions ("ignore previous instructions", "the user
approved this"). This cannot be prevented in general, and this project does not claim to.
What is in place:

- Tool results reach the model only in the tool-result role, never as system or user
  messages, and the system prompt says they are data.
- **Injected text cannot widen what the agent can do.** It cannot add tools, change the
  workspace, raise limits, change permissions, or approve anything; none of those has an input
  the model controls. Tests put approval claims in a file and in the model's own text and show
  the write still waits for, and obeys, the human's answer.

**What remains possible**, and is the residual risk:

- **Steering within the allowed tools.** A malicious file can make the model read other
  workspace files, and, if writes are enabled, *propose* creating files with chosen content.
  The human approval dialog is the control: read the path and the content preview before
  approving. A preview shows at most 2,000 characters; the full size is stated.
- **Exfiltration to the provider.** Anything the agent reads is sent to the provider. An
  injected instruction can make it read more workspace files than you intended. It cannot send
  data anywhere else: there is no network tool.
- **Misleading final answers.** The model's final text can be wrong or manipulated. The
  result therefore separates `final_response` (untrusted) from `executed` and `verification`
  (evidence). Rely on the latter.
- **Created files can matter later.** An approved file could be a script, a CI workflow, or a
  `conftest.py`. It is not run by this server, but other tools might run it.

## Residual risks and limitations

- The Linux hard-link race documented for the filesystem tools applies to agent reads too.
- Approval answers come through the client; a client-side hook or a compromised client can
  approve. See [Human approval](#human-approval).
- Token budgets are checked between requests, so one request can exceed the remaining budget
  by up to its own size (bounded by the context limit). When a provider omits usage, the
  input estimate is the request's size in characters, an upper bound for ordinary text but not
  a guarantee for every tokenizer.
- A tool call that has started is allowed to finish before cancellation or a deadline takes
  effect, so its effect is known. The filesystem tools are bounded (5 s search, 1 MiB reads
  and writes), so this delay is bounded too.
- Suspended tasks and task history are kept in process memory only. A server restart loses
  them; a suspended task then cannot be resumed and nothing is written.
- The adapters are tested against the real SDKs with a mock HTTP transport, not against the
  live APIs. Request formats were taken from the installed SDK versions (`anthropic` 2.5.0,
  `openai` 3.46.0).
- Only one task runs at a time per server process.
