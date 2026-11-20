# Architecture

This document describes Universal AI Tools as of **Milestone 6 (Claude-native execution)**:
a local MCP execution service that Claude Desktop and Claude Code use to work on the user's
Windows PC. Claude does all reasoning and planning. This package exposes individual
operations (file tools, screen capture, mouse and keyboard input; command execution is
disabled),
enforces permissions, asks the human for approval, records an audit log, and executes. It
never calls a model API and needs no API key.

[Threat model](#threat-model) lists what the design defends against and what it does not.
[Verification status](#verification-status) says what has been tested and what has not.

## The correction in Milestone 6

Milestone 5G had added an opt-in `run_task` tool: a second agent inside the server that
called the Anthropic or OpenAI API with its own key and ran its own planning loop over the
tools. That duplicated what Claude already does, and it needed API keys the user does not
have. Milestone 6 removed it from the product:

| Component | Decision |
| --- | --- |
| `workspace.py`, `policy.py`, `filesystem.py` | **Kept**, extended with `replace_file`, `make_directory`, `move`, `pinned_directory` and `Operation.MODIFY` |
| `tools.py` (registry) | **Kept**, extended: more risks, async handlers, approval descriptions, image outputs |
| `config.py` | **Kept** for environment mode; the agent's variables became removed settings |
| `server.py` | **Modified**: every call goes through the gate; agent hooks removed |
| `agent/mcp_tool.py` elicitation approval, `agent/permissions.py` decisions | **Reused**, adapted into `approval.py` and `gate.py` |
| `agent/` (orchestrator, planner, budget, task, verify, config), `providers/`, `adapters/` | **Removed from the package**, archived unchanged in `legacy/api-agent/` with their tests and `agent-security.md` |
| `anthropic`, `openai`, `agent` extras | **Removed** from `pyproject.toml` and CI |

Nothing was deleted: the archived files are byte-for-byte what they were. The server refuses
to start if any former agent variable (`UNIVERSAL_AI_TOOLS_ENABLE_AGENT_TOOLS`,
`UNIVERSAL_AI_TOOLS_AGENT_*`, `UNIVERSAL_AI_TOOLS_*_API_KEY`) is set, without echoing the
value, so a stale registration and a key left in a client's settings are both noticed.

## Goals that shape the design

- **Claude is the orchestrator.** The server has no planner, no model loop, and no notion of
  a task. Each tool is one operation with a result Claude can check. Multi-step work is
  Claude calling tools in sequence.
- **Same service for both clients.** Claude Desktop and Claude Code each start a server
  process over stdio (MCP gives local clients no other way), but both registrations carry
  only the path of one config file, so they get the same capabilities, rules, and audit log.
- **Least privilege by default.** One workspace directory, explicitly named. Everything
  beyond reading files is off until enabled in the config file, and every consequential
  call needs a human's approval. No setting skips approval.
- **Evidence, not claims.** Tools report what actually happened (hashes read back, exit
  codes, a new screenshot), and the audit log records decisions and outcomes.
- **Testable without a client.** Tool logic runs as plain functions, in process over MCP with
  the SDK's own client (both protocol generations), and through a real stdio subprocess. The
  desktop layer runs against a fake backend, so no test sends real input.

## Layers

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Claude Desktop / Claude Code: reasoning, planning, tool choice, verifying   │
└───────────────┬────────────────────────────────────────────────────────────┘
                │ MCP over stdio (one server process per client)
┌───────────────▼────────────────────────────────────────────────────────────┐
│ server.py   MCPServer; one entry point per registry tool; stdio main()       │
├────────────────────────────────────────────────────────────────────────────┤
│ gate.py     validate → permission decision → approval → audit → dispatch      │
│   approval.py  client dialog (MCP elicitation, inline or InputRequiredResult) │
│                or Windows dialog; anything but "approve" is no                │
│   audit.py     JSON lines, shared lock file, durable "started" records        │
├────────────────────────────────────────────────────────────────────────────┤
│ tools.py    ToolRegistry: which tools exist, risk, annotations, descriptions │
├──────────────┬───────────────┬────────────────────┬────────────────────────┤
│ filesystem.py│ file_actions  │ (terminal.py:      │ desktop/tools.py        │
│ read/search/ │ edit, mkdir,  │  disabled, never   │ screen + input          │
│ write_file   │ move          │  registered)       │ safeguards; win32.py    │
├──────────────┴───────────────┴────────────────────┴────────────────────────┤
│ workspace.py  pinned, link-refusing path confinement │ policy.py  the rules  │
└────────────────────────────────────────────────────────────────────────────┘
  settings.py  the shared config file      config.py  environment mode
  cli.py / clients.py  setup, doctor, config, audit (never imported by the server)
```

### 1. Transport and entry point (`server.main`)

- Loads configuration first. Invalid configuration ends the process with exit code `2`
  and a message on stderr, before any MCP traffic is exchanged.
- Configures logging to **stderr**. stdout belongs to the protocol. The MCP SDK's stdio
  transport also points file descriptor 1 at stderr while it serves, so a stray `print()`
  in a handler cannot corrupt the protocol stream.
- Runs the server with the SDK's stdio transport. This is the only place that knows the
  transport. Adding HTTP later means adding another entry point, not changing tool code.

Importing any module in the package is inert: it does not read configuration, configure
logging, construct the server, or start serving. Tests enforce this.

### 2. Server assembly (`server.create_server`)

- Builds an `mcp.server.mcpserver.MCPServer` (MCP Python SDK 2.x; this class replaced
  `FastMCP` from 1.x) with the server name `universal-ai-tools` and the package version.
- Registers every tool of `tools.build_registry(config, limits)` with `add_tool`, together
  with the name, title, description, and `ToolAnnotations` from its `ToolSpec`, and registers
  nothing else. See [Tool registry](#2a-tool-registry-toolspy). The read tools are marked
  read-only, non-destructive, idempotent, and closed-world. `write_file` is marked *not*
  read-only, non-destructive (it only adds files), not idempotent (a repeated call fails
  because the file exists), and closed-world.
- Takes an `AppConfig` argument, so each server is built from explicit configuration
  rather than global state. This also lets tests build isolated servers. An optional
  `FilesystemLimits` argument overrides the default resource limits (tests use this).
- **Write tools are gated.** The read-only tools are always registered. `write_file` is
  in the registry, and so registered, only when `AppConfig.enable_write_tools` is true.
  When it is false, the tool does not exist on the server at all: it is absent from
  `tools/list`, and calling it fails as an unknown tool. The server instructions are
  chosen to match (`READ_ONLY_INSTRUCTIONS` or `WRITE_ENABLED_INSTRUCTIONS`), so the
  client is never told about a capability it does not have. `main` logs at startup
  whether write tools are enabled.
- Gives the SDK one small entry-point function per tool. The SDK builds a tool's input and
  output schemas, and validates each call, from the signature of that function, so
  `_entry_point` gives it the input model's fields in order, with their annotations
  (descriptions, `ge`/`le`/`max_length` bounds) and defaults, and the output model as its
  return type. The function's name is the tool's name, because the SDK names its argument
  model after it. The entry point calls `ToolRegistry.invoke` and turns `WorkspaceError`
  and `ToolCallError` into the SDK's `ToolError`. The client then gets an `is_error`
  result with that message, and the server logs it at INFO without a traceback. Any other
  exception is reported to the client only as `Error executing tool <name>`. Its details
  and traceback stay in the server's stderr log.
- **MCP calls are validated twice, and the SDK's rules decide what a client sees.** The SDK
  validates the arguments first, exactly as it did before the registry: it coerces some
  values (`"true"` to `true`, `"2"` to `2`, and JSON-encoded strings it parses) and drops
  properties the schema does not declare. `invoke` then validates the already typed values
  strictly, which they always pass. This keeps the server's behaviour unchanged for clients
  that send stringified arguments. `tests/test_mcp_surface.py` records it, including both
  of those leniencies. Only callers that use the registry directly get strict rejection.

### 2a. Tool registry (`tools.py`)

`tools.py` is the single source of truth for which tools exist and what they are.
`build_registry(config, limits)` is the only place that decides it: it binds the filesystem
tools to one `Workspace` for `config.workspace_root` and returns a `ToolRegistry` with exactly
the tools the configuration allows, in the order the server lists them. It has two consumers,
which use the same registry instance and so cannot drift apart: the MCP server, which
registers every spec, and the opt-in agent, which offers the same specs to the model and runs
the model's calls through `invoke`.

- **`ToolSpec`** (a frozen dataclass) holds a tool's name, title, description, input model,
  output model, handler, `ToolRisk`, and MCP `ToolAnnotations`. Construction refuses a spec
  whose name is not lower-case words joined by underscores, whose input model is not strict
  with extra fields forbidden, whose annotation title differs from its title, whose
  `read_only_hint` contradicts its risk, or whose `destructive_hint` is anything but `false`.
- **`ToolRisk`** is `READ_ONLY` (`get_platform_info`, `list_files`, `read_file`,
  `search_files`) or `CREATE` (`write_file`: adds files and never changes existing ones). It
  is a classification for whoever decides whether to allow a call. The agent's permission
  policy enforces it for the agent's calls (`READ_ONLY` allowed, `CREATE` needs a human's
  approval); direct MCP calls are, as before, up to the client.
- **Input models** (`ListFilesInput`, `ReadFileInput`, ...) use `STRICT_INPUT`: pydantic
  strict mode, extra fields forbidden, frozen. Their fields carry the descriptions and bounds
  that MCP clients see as the input schema.
- **Handlers** call the functions in `filesystem.py` with the tool's `Workspace` and
  `FilesystemLimits`. The registry has no other route to the filesystem, and a test parses
  `tools.py` to keep it free of `os`, `pathlib`, `open`, and dynamic code execution.
- **`ToolRegistry.invoke(name, arguments)`** refuses an unknown name (`UnknownToolError`),
  refuses arguments that are not an object with string keys, and validates the arguments
  against the input model in strict mode (`ToolInputError`): no coercion (`"5"` is not an
  integer, `"true"` is not a boolean, `2.0` is not an integer, `true` is not an integer), no
  ignored properties, and the schema's bounds and required fields. Only then does it call
  that tool's handler, and it checks that the handler returned the declared output model.
  `WorkspaceError` from a handler passes through unchanged. Refusal messages name fields
  but never quote argument values, escape the names they quote, and are bounded in length.
- **Immutable.** A registry has no method to add, replace, or remove a tool, and its table is
  a read-only mapping. There is no plugin loading. Nothing holding a registry, including the
  model-driven agent, can make it run a function that `build_registry` did not put there.

### 3. Tool logic (`tools.get_platform_info`, `filesystem.py`)

- Plain, typed Python functions with no MCP imports. Return values are pydantic models.
  The SDK turns those into both an `output_schema` and `structured_content`, plus a text
  block for clients that do not read structured output, and the text block is indented
  JSON, so a result crosses the wire about twice.
- `get_platform_info` returns only the application name, version, and status. It does
  not include paths, configuration, or environment data.
- `list_files`, `read_file`, `search_files`, and `write_file` take a `Workspace` and a
  `FilesystemLimits`. They never touch the filesystem except through the `Workspace`.
  They validate their own non-path arguments (pattern length, result counts, content)
  again, so they are safe to call without the MCP schema in front of them.
- `write_file` validates the content first (no NUL characters, valid Unicode, at most
  `max_write_bytes` once encoded as UTF-8), then hands the encoded bytes to
  `Workspace.create_file`.
- `list_files` and `search_files` measure each result as they build it. `_ResponseBudget`
  starts from the JSON size of the result in its largest possible form (truncated, with the
  longest reason and the largest counts) and admits items while they fit, so the finished
  result can only be smaller than the limit. `read_file` has no partial result and refuses.
- Names, snippets, and file content pass through `policy.escape_line` or `policy.escape_text`
  on the way out, and results carry `trust: "untrusted"`. See
  [Untrusted output](#untrusted-output).
- The SDK runs these synchronous functions in a worker thread, so a slow search does not
  block the event loop.

The README documents each tool's arguments, results, and limits. In summary:

| Limit                                   | Default    | Where enforced                    |
| --------------------------------------- | ---------- | --------------------------------- |
| `list_files` entries                    | 200 / 1000 max | schema and `FilesystemLimits` |
| `read_file` file size                   | 1 MiB      | `fstat` before reading, and read length |
| `search_files` pattern length           | 200 chars  | schema and `FilesystemLimits`     |
| `search_files` results                  | 100 / 500 max | schema and `FilesystemLimits`  |
| `search_files` files read               | 5,000      | `FilesystemLimits`                |
| `search_files` per-file size            | 1 MiB      | `FilesystemLimits`                |
| `search_files` wall-clock time          | 5 s        | checked before each file          |
| `search_files` snippet length           | 200 chars  | `FilesystemLimits`                |
| `write_file` content size               | 1 MiB      | encoded UTF-8 length, before creating |
| `list_files`, `read_file`, `search_files` result | 2 MiB of compact JSON | `FilesystemLimits.max_response_bytes` |
| any `path` argument                     | 4,096 chars | `_split_relative`, before anything else |

Search treats the pattern as literal text. It is passed through `re.escape`, so there is
no user-controlled regular expression and no catastrophic backtracking.

### 4. Path confinement (`workspace.py`)

`Workspace` is the security boundary. It holds the resolved workspace root from
`AppConfig` and never widens it. Every path argument is treated as untrusted and checked
in three stages. **What may be accessed is not decided here**: `Workspace` enforces the answers
from `policy.py` (section 5), and contains no names of its own.

1. **Lexical, before any filesystem access.**
   - Refuse NUL characters, paths over 4,096 characters, and text the platform cannot encode.
   - Refuse absolute paths, a leading `/` or `\`, drive letters (including drive-relative
     `C:file`), and UNC and `\\?\` paths. Both POSIX and Windows rules are applied on every
     platform.
   - Split on `/` and `\`, drop empty and `.` components, and refuse any `..` component.
   - On Windows, refuse components that `os.path.isreserved` flags (device names such as
     `NUL` or `COM1`, names ending in a dot or space) and any component containing `:`
     (alternate data streams).
   - Ask the policy about the path. Because this runs before the filesystem is touched, a
     refusal does not reveal whether the path exists.
2. **Pinned traversal (`Workspace._pin`).** Starting at the root, each component is opened
   without following links and is verified through that open handle, so what was checked
   is what the next step uses. A symlink, junction, or other *name-surrogate* reparse point is
   refused, whether it points inside or outside the workspace. Every non-final component
   must be a directory. Other reparse points, such as OneDrive cloud files, are ordinary files.
   - **POSIX:** `openat` with `O_DIRECTORY | O_NOFOLLOW` relative to the previous descriptor,
     after an `fstatat`, and the opened directory must be the same inode that `fstatat` saw.
     Each component is a single name with no `/` or `..`, which is why it should give what
     `openat2(RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH)` would. That comparison is reasoning, not a
     test. A checked directory *can* still be renamed here: the descriptor keeps referring
     to it, so the swap does not redirect anything.
   - **Windows:** `CreateFileW` with `FILE_FLAG_OPEN_REPARSE_POINT`, then
     `GetFileInformationByHandleEx(FileAttributeTagInfo)` on the handle. Windows has no public
     `openat`, so every handle stays open instead, without `FILE_SHARE_DELETE`. While it is open,
     another process that tries to rename or delete that directory, or one above it, is refused
     by the kernel, so the full path used for the next step keeps its meaning. This was tested
     from a second process on local NTFS only. Handles ask for `FILE_LIST_DIRECTORY` on purpose:
     the sharing rule ignores handles with only attribute access, which would pin nothing.
   - The real path of the deepest directory (`GetFinalPathNameByHandleW`, `/proc/self/fd/N`, or
     `fcntl(F_GETPATH)`) must lie inside the root and pass the policy. This catches 8.3 aliases
     such as `GIT~1` for `.git`. If the real path cannot be read, the operation is refused. On
     Linux that includes `/proc` not being mounted and a path longer than the 4,095 bytes the
     kernel will report, which any process that can rename directories can bring about.
3. **Post-open verification (file contents).** `_read_in` opens the file from the pinned
   directory without following a link, on POSIX with `O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`, on
   Windows with `FILE_FLAG_OPEN_REPARSE_POINT` and an attribute check on the handle. Then, on
   the open descriptor:
   - It must be a regular file and the *same file* (device and inode) that was inspected.
     Windows directory scans report no file ID, so a scanned entry has nothing to compare; the
     other checks here still run on what was actually opened.
   - Its real path must be inside the root and pass the policy for reading, which catches
     aliases such as `ENV~1` for `.env`. As for directories, a real path that cannot be read
     refuses the read.
   - It must have one name. `st_nlink > 1` is refused, because a hard link can be any file on
     the volume, and only this check can see that. **This is not guaranteed against a
     concurrent attacker:** on Linux a process that creates and removes links at the same
     moment can beat it; see the first entry under [Known limitations](#known-limitations).
   - Files larger than the limit are refused by their `fstat` size, and then at most limit + 1
     bytes are read, in case the file grows during the read.

`list_files` and `search_files` use the same traversal. `scan_directory` lists a pinned
directory (`os.scandir` on the descriptor on POSIX, by the pinned path on Windows) and
inspects each entry with `DirEntry.stat(follow_symlinks=False)`. Links are reported as `symlink`
and never descended into. `search_files` reads through a `FileReader`, which keeps the current
parent pinned between files, because walking down from the root for each file was most of the
cost of a search. Every read still runs all of stage 3.

#### Creating files (`Workspace.create_file`)

`create_file` is the only code that changes the filesystem. It runs these steps in order,
and nothing is created until all checks before step 5 have passed:

1. **Lexical.** The stage-1 checks above, plus: the path must name a file (not `.`, and not
   ending in a separator), and the policy must allow creating it. That refuses a sensitive
   final name, and any component that is repository metadata, a sensitive directory, or an
   excluded directory, including the final name, so a `.git` *file*, which could point Git at
   another repository, is refused.
2. **Pinned parent.** The stage-2 traversal, held for the whole operation. The parent must
   exist and be a directory, and no component may be a link. Missing parents are reported as
   such and are never created.
3. **Parent real path.** The real path of the parent, joined with the new name, must pass
   the policy for creating. This catches 8.3 aliases such as `NODE_M~1`.
4. **Free target.** The name must not exist. This gives a clear error for the common case; it
   is *not* what prevents overwriting.
5. **Exclusive create.** This is what prevents overwriting, including when something
   appears between step 4 and step 5:
   - **POSIX (Linux, macOS):** `openat(parent_fd, name, O_WRONLY | O_CREAT | O_EXCL |
     O_NOFOLLOW)`. `O_EXCL` fails on any existing name, including a dangling symlink.
   - **Windows:** `CreateFileW` with `CREATE_NEW` and `FILE_FLAG_OPEN_REPARSE_POINT`, with
     share mode 0 and `DELETE` access, by a full path whose components are all pinned.
     `CREATE_NEW` never opens an existing file, directory, symlink, or junction. Share mode 0
     stops other processes from opening, renaming, or deleting the file while it is written.
   - Platforms without `dir_fd` support refuse every filesystem operation rather than get a
     weaker implementation.
6. **Verify before writing.** The new descriptor must be a regular file, and its real path must
   pass the policy and equal the parent's real path from step 3 joined with the requested name.
   On POSIX the pinned parent can be renamed, and this is what detects that: the file is then
   somewhere other than the path that was checked, so it is refused.
7. **Write and flush.** All bytes are written in a loop, then `fsync`ed, then the
   descriptor is closed. Success is reported only after all three succeed.

If steps 6 or 7 fail, or the call is interrupted, the new file is removed and the error
says whether removal succeeded:

- **Windows:** the file is marked delete-on-close through its own handle
  (`SetFileInformationByHandle(FileDispositionInfo)`). This can only ever remove the file
  this call created, whatever its name is by then.
- **POSIX:** there is no unlink-by-descriptor. The name is unlinked relative to the pinned
  parent directory, and only if `fstatat` shows it is still the same inode that was
  created. Otherwise the file is left in place and the error says it could not be removed
  safely.

Error messages are built only from the normalized, workspace-relative path and fixed text.
`OSError` details (`filename`, `strerror`) are never included, because they can contain
absolute paths. File contents never appear in errors. `server._tool_errors` escapes the message,
because it can quote a path as the client sent it.

### 5. Filesystem policy (`policy.py`)

`policy.py` is the one place that decides what may be accessed. It is a set of pure functions of
path components: no filesystem access, no state, and no MCP. Nothing else names a protected path,
and a test parses `workspace.py` and `filesystem.py` to keep it that way.

`refusal(parts, operation)` returns a reason or `None`. There are three operations:

| Path                                   | `LIST`   | `READ`   | `CREATE` |
| -------------------------------------- | -------- | -------- | -------- |
| Repository metadata (any component)    | refused  | refused  | refused  |
| Sensitive directory (any component)    | refused  | refused  | refused  |
| Sensitive file name (final component)  | allowed  | refused  | refused  |
| Excluded directory (any component)     | allowed  | allowed  | refused  |
| Anything else                          | allowed  | allowed  | allowed  |

`read_file` and `search_files` both use `READ`, and `list_files` uses `LIST`. A name in a
directory listing is not content, so the *parent's* `LIST` still shows `.git` and `.env` as
entries: hiding them makes a refusal look like a missing file. The rule is about entering them.
`is_excluded_directory_name` tells a walk where not to go.

Each answer is applied twice: to the requested path, and to the real path of what was opened.
A later exporter, such as the sandbox snapshot below, must do the same with `Operation.READ` and
must not define its own rules.

The sensitive-name list is broad on purpose and has **documented false refusals**, accepted when
this design was approved: a registry-only `.npmrc`, a harmless `test.env`, and `.envrc`, which
`write_file` also refuses because `direnv` would execute it. The name rules are a safety net.
They do not detect every secret.

#### Untrusted output

File contents and names can carry text aimed at the model reading them. This **cannot be solved in
the server**, because any text may be an instruction to a sufficiently credulous reader. What
the server does:

- It labels the data: `trust: "untrusted"` in results, and the same statement in the tool
  descriptions and server instructions.
- It removes what hides text or drives a terminal, in `escape_text` (keeps tabs and line
  endings) and `escape_line` (one-line values). The set is C0 and C1 controls, DEL, line and
  paragraph separators, bidirectional overrides and isolates, lone surrogates, and tag characters.
  Lone surrogates matter for another reason too: undecodable POSIX file names arrive as lone
  surrogates, and those cannot be encoded as JSON at all.
- It keeps `U+FEFF` and zero-width joiners, which ordinary scripts and emoji use.
- It counts replacements in `escaped_characters`, so a reader can tell whether `content` is
  exact. An escape cannot otherwise be told from the same characters in the source.

Escaping a name means the shown form cannot be used as a path. That is deliberate: such names do
not occur by accident, and a usable form would have to carry the control characters.

#### Known limitations

- **Open defect: the hard-link check can be beaten on Linux.** `read_file` and `search_files`
  can return the content of a file outside the workspace. It takes a second process that can
  write in the workspace and hard-link the target file, running at the same moment. No MCP tool
  can do this by itself, because no tool creates links. The process links the outside file into
  the workspace, the server opens it, and the process unlinks the workspace name before `fstat`
  runs, so `st_nlink` reads 1 and the check passes. `tests/test_races.py` reproduces this:
  `test_concurrent_hard_link_substitution_never_escapes` fails in many of its two-second runs
  on ext4: roughly a third when first measured, and 14 of 20 in a later run under WSL2. The rate
  depends on the machine and its load. The cause was confirmed by recording the descriptor at
  each leak.
  **It is not fixed, and hard-link protection is NOT guaranteed against a concurrent
  attacker** on any platform. The test stays in place, failing intermittently. It was not
  observed on Windows, where the open handle lacks `FILE_SHARE_DELETE`, but that is an
  observation under load and not a proof. Other POSIX systems were not tested.

  No further workaround is planned. A rejected candidate added more checks after the read
  (re-reading the descriptor's path, listing the parent directory to wait for an unlink in
  progress, and reading the link count again). Review found that it could still be beaten, for
  example by moving the name to another directory before removing it. More fundamentally,
  the link count, the file's names, and the content can only be observed at separate moments,
  and a concurrent process can change them in between. Only the containment in
  [Planned: stronger containment](#planned-stronger-containment) addresses this.
- **Mount points on POSIX.** A bind mount inside the workspace is not a link and is not detected.
  Creating one needs root. Windows volume mount points are name-surrogate reparse points and are
  refused.
- **The root's own ancestors are not pinned.** The walk starts at the root path. If a directory
  above the root is swapped, the real-path checks refuse what is then opened, on platforms that
  can report a real path.
- **Hard links created after the check.** The link count is read once, after opening. A hard link
  made to a file while it is being read does not matter, because the content already belongs
  to the name that was checked. A file whose other names are all deleted becomes readable again.
- **Hard links on filesystems that do not count them.** Some network filesystems report a count
  of 1, and such a link cannot be told apart from an ordinary file.
- **File replaced after a scan, on Windows.** A directory scan gives no file ID there, so a search
  cannot notice that an entry became a different ordinary file. Every other check still runs on
  what was opened.
- **Renaming an existing sensitive file.** Rules apply to names. If something else renames `.env`
  to `notes.txt`, `notes.txt` is an ordinary file.
- **Real paths are required on Linux.** If `/proc/self/fd` cannot be read, every filesystem
  tool refuses rather than skipping the real-path checks. Without `/proc` mounted, the tools
  are therefore unusable. A file whose absolute path exceeds 4,095 bytes is refused, because
  the kernel will not report it.
- **Other platforms.** On platforms other than Windows, Linux, and macOS, the real-path checks
  are skipped. Protection then rests on the traversal, the same-file identity check, and
  `O_NOFOLLOW`. **macOS has been type-checked but never run.**
- **Directory permissions.** Every parent is opened for listing. A file under a directory you may
  traverse but not list (mode `0o311` on POSIX, or a Windows ACL) cannot be reached.
- **Brief sharing conflicts on Windows.** A directory that another process holds open with `DELETE`
  access, such as during a rename, is reported as permission denied. Conversely, a rename by
  another process fails while a call has that directory pinned. Calls last milliseconds, but
  a search keeps the current directory pinned between its files.
- **Prompt injection stays possible.** Labels and escapes do not stop a model from following
  plain text in a file. See [Untrusted output](#untrusted-output).
- **Sensitive names are a safety net, not a secrets scanner.** Secrets stored under other
  file names are readable like any other workspace file, and `list_files` still shows the names
  of sensitive files and their sizes.
- **Cloud placeholders.** Reading files that exist only in the cloud may make the sync client
  download them. Files are opened with `FILE_FLAG_OPEN_REPARSE_POINT`, and that combination
  **has not been tested on a dehydrated placeholder**; a hydrated OneDrive folder works.
- **Name-based exclusions.** Excluded directories are recognized by name only. A user's own
  directory called `venv` or `.cache` is also skipped by recursive listing and search, and a
  renamed `.git.bak` is an ordinary directory.
- **Large directories.** A scan reads a whole directory before sorting, so the entry and response
  limits bound the result, not the cost of scanning one huge directory.
- **Search time limit.** The time limit is checked before each file, so one search can
  overrun it by the time it takes to scan a single file (at most 1 MiB).
- **No audit log in environment mode.** File mode records every call (see
  [The gate](#the-gate-gatepy)). Environment mode, kept for old registrations, is read-only
  (writes are refused because it has neither approval nor an audit log) and logs refusals to
  stderr only.
- **POSIX cleanup race.** Removing a failed file means a `fstatat` identity check followed by
  `unlinkat`. A process that renames the file away and puts its own file at the name in the
  instant between those two calls could have its file removed. This needs write access to
  the workspace and precise timing, and only matters when a write has already failed.
- **Hard kill mid-write.** If the server process is killed or the machine loses power while
  a file is being written, a partial file can remain. Normal errors and interruptions
  (exceptions, `KeyboardInterrupt`) are cleaned up. On POSIX, other processes can also see
  the file while it is being written; on Windows, share mode 0 keeps them out.
- **Created files can still matter.** `write_file` cannot overwrite anything, but a new
  file can change how other tools behave, for example a new CI workflow under `.github/`,
  a new `conftest.py`, or a new script. Only protected and excluded locations are refused.
  Review what an AI client creates before running it.
- **One coarse switch.** `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS` is fixed at startup, and there
  is no per-tool or per-directory configuration.
- **MCP validation is lenient.** MCP calls are validated by the SDK, which coerces some values
  and drops undeclared properties, as it always did; only direct registry callers get strict
  rejection. A `write_file` call with an extra property such as `"mode": "overwrite"` succeeds
  over MCP and the property has no effect: it still never overwrites.
- **Environment mode has no server-side approval.** In environment mode, calls are decided
  by the client, as before Milestone 6, and the startup switch is the only server-side
  control. File mode adds the gate: approval and an audit log for every consequential call.

### The gate (`gate.py`)

Every MCP call reaches its handler only through `Gate.call`:

1. Strict validation against the tool's input model (after the SDK's own validation), and
   reduction to one canonical JSON text (sorted keys, ASCII, no spaces) whose SHA-256 digest,
   over the tool name and that text, identifies the call. After this the caller's argument
   objects are never read again.
2. The permission decision, a function of the configuration and the tool's `risk` and
   `capability` only. Environment mode: no server-side approval, and a client can reach it
   only read-only. File mode: `READ_ONLY` runs; every other risk needs approval under the
   capability's policy, `every_call` or `once_per_session`; `EXECUTE`, the terminal, and
   unknown capabilities are always `every_call`.
3. Approval, one question at a time, bounded by `[approval] timeout_seconds`. The complete
   question is rendered for the chosen channel's display limits first; if it does not fit,
   the decision is `too_large` and nothing is shown or run.
4. A durable `started` audit record. If it cannot be written, the call is refused.
5. Dispatch through `ToolRegistry.ainvoke` on a new copy of the canonical form (plain
   handlers in a worker thread, through `invoke`, which validates strictly again; async
   handlers awaited). For a 2026-07-28 retry, the copy is of the canonical form stored with
   the pending question, not of the retry's arguments.
6. A `finished` record with the status (`succeeded`, `failed`, `cancelled`), duration, a safe
   error message (only messages written to be shown; otherwise the exception's type), and an
   `error_class`.

The description shown for approval and the audit target both come from the tool's
`describe(arguments)`, required by `ToolSpec` for every risk but `READ_ONLY` and present on
the read-only file tools too, for their audit target. It is given a model rebuilt from the
canonical form, so the human sees exactly the arguments that will run.

**Audit records.** In file mode every call is recorded, read-only ones included: `session`,
`call`, the client's `request` id, `client`, `protocol`, `tool`, `operation` (a fixed word per
tool), `risk`, `target`, `arguments_sha256`, `permission` (`allowed` or `approval_required`)
and `policy`, `approval` (decision and channel), `status`, and on failure `error` and
`error_class` (`not_found`, `policy_refused`, `protected_path`, `outside_workspace`,
`link_refused`, `hard_link_refused`, `invalid_path`, `too_large`, `not_approved`,
`invalid_arguments`, `unknown_tool`, `refused_by_tool`, `filesystem_error`, `internal_error`).
Calls refused before they could run, including an unknown tool or invalid arguments, are
`refused` records. Targets never hold file contents, search patterns (only their length),
typed text, or command output; command arguments that look like credentials are replaced by
`<redacted>` (`terminal.redacted_arguments`, a name rule).

### Approval (`approval.py`)

- **Client dialog (MCP form elicitation).** Used when the client declared the capability.
  Over MCP 2025-11-25 and earlier the server sends `elicitation/create` while the call waits.
  Over MCP 2026-07-28, where a server may not send requests mid-call, the gate returns an
  `InputRequiredResult` with the question; the client asks the human and retries the same
  call with the answer in `input_responses`. The gate keeps the pending question keyed by a
  random nonce, bound to the tool and a SHA-256 digest of its validated arguments, expiring
  with the approval timeout, and removes it on first use. A retry with other arguments, a
  forged or replayed nonce, or an expired one approves nothing. The SDK also seals
  `request_state` with an ephemeral key.
- **Windows dialog.** `MessageBoxW` (Yes/No, No is the default button, topmost) in a worker
  thread owned by the server, titled `Universal AI Tools - approval required`. On timeout or
  cancellation the gate posts "No" to the dialog's window, so it never lingers as a live
  approval. Available only when the process has an interactive input desktop.
- The form is closed (`extra="forbid"`, strict): only `{"decision": "approve"}` approves.
  Decline, cancel, `deny`, extra fields, empty content, errors, and silence all mean no.
- **The question is complete and unambiguous** (`render_approval`). Nothing is shortened.
  `ActionDescription.details` carry typed values: `ExactPath` and `ExactValue` are quoted, and
  marked `escaped` when they contain a character that cannot be seen or changes how text
  displays (Unicode categories C and Z other than U+0020, line breaks, tabs, and a combining
  mark at the start); then such characters are written as `\n`, `\t`, `\xHH`, `\uHHHH`, or
  `\UHHHHHHHH` and every backslash as `\\`. `ExactText` is shown line by line behind a `│`
  gutter (`┆` for continuations), with a header giving the line and character count and the
  final line break, the same escaping, and trailing spaces escaped. `ExactArguments` are
  numbered, one per line. Long lines are cut at a fixed width in display cells (wide
  characters count two) without changing a character. `tests/test_approval_integrity.py` reads
  rendered values back and requires the original exactly.
- **Display limits.** A request that does not fit the channel's limits is refused
  (`ApprovalTooLargeError`, decision `too_large`). The Windows dialog's limits are estimated
  from `SPI_GETWORKAREA` and the message font (`limits_for_screen`: at most 5/8 of the work
  area wide, 200 px of chrome, lines 1.5 font heights, every character one font height wide;
  40-90 cells, 12-60 lines). The client's are fixed at 200 lines of 100 cells and 20,000
  characters: MCP lets a server learn nothing about how much of a message a client displays,
  and whether Claude Code and Claude Desktop show that much in full is unverified.

### The trusted runtime (`trusted.py`)

What the file tools must never change, found from the running process rather than from file
names: see the module docstring and the threat model. `build_registry` gives the workspace
`TrustedPaths.for_service(service)`. Its runtime part (`trusted._Runtime`, one per process,
from `workspace.runtime_trusted_paths`) is refreshed before every check: modules imported
since the last check, a changed `sys.path` and the `.pth` files in its new entries,
setuptools editable-finder mappings. The Startup folders and PowerShell profile folders
(`trusted.autostart_paths`, from the Windows known folders) are protected too, because a file
written there runs a program later. `Workspace` checks it in `_require_unprotected`, called
lexically before anything is opened and again, in `_require_allowed`, on the real path of
every location a `CREATE` or `MODIFY` touches: so an 8.3 name, another case, a junction
given as the protected location, or a renamed parent is caught. A `sys.path` entry or Python
prefix that strictly contains the workspace protects nothing in it (Python imports only its
direct entries); any other protected location containing the workspace protects all of it.
`server.check_isolated_start` refuses to run at all when the current directory is on
`sys.path` (started without `-I` or `-P`): a file there may already have replaced a module.

With an editable install, the checkout's `src` is on `sys.path`, so a server run from it can
read but never change its own repository's source. Working on this repository through the
tools needs a separate, non-editable install.

### The config file (`settings.py`)

TOML, validated by strict pydantic models with unknown keys forbidden. `ServiceConfig`
carries the environment-mode `AppConfig` (workspace, log level, write switch) plus the
approval, filesystem, terminal, screen, input, and audit settings. `load_service_config`
picks the mode: `UNIVERSAL_AI_TOOLS_CONFIG` set means file mode, and then setting any
individual variable as well is an error. The config file, its directory, and the audit log
must be outside the workspace, and the workspace must not contain Claude's settings
(`~/.claude`, `~/.claude.json`, `%APPDATA%\Claude`, `%LOCALAPPDATA%\Packages\Claude_*`).

The default location is `~/.universal-ai-tools/`, not `%APPDATA%`: the Microsoft Store build
of Claude Desktop runs in an MSIX container whose writes under AppData are redirected to a
private copy, which would split the audit log between the two clients.

### Changing files (`file_actions.py`, `workspace.py`)

- `Workspace.replace_file` reads the existing file through the same pinned, verified path as
  `read_bytes`, with the real-path check under `Operation.MODIFY`, computes the new content,
  writes it to a new temporary file in the same pinned directory (created exactly as
  `create_file` creates files, `CREATE_NEW` / `O_EXCL`, flushed), checks that the original's
  identity did not change, and renames the temporary file over it (`MoveFileExW` with
  replace / `renameat`). The rename replaces the directory entry and never writes through a
  link. On any failure the temporary file is removed and the original is untouched.
- `Workspace.make_directory` creates one directory under a pinned parent, after the
  real-path check under `Operation.CREATE`.
- `Workspace.move` pins both parents, refuses links, a destination inside the source, and an
  existing destination, checks the source's real path under `MODIFY` and the destination's
  under `CREATE`, and renames without replacing (`MoveFileExW` without replace on Windows,
  `renameat2(RENAME_NOREPLACE)` on Linux; elsewhere a check then rename, with a narrow race).
  Afterwards the item at the destination must be the one that was moved.
- `Operation.MODIFY` follows the `CREATE` rules: repository metadata, sensitive directories,
  sensitive file names, and excluded directories are refused.

### Command execution: disabled (`terminal.py`)

General command execution is disabled. A program started by the server runs with the user's
rights, and on Windows this server has no boundary that would stop it from changing the
server's code, Python environment, configuration, or audit log: no restricted token,
AppContainer, or separate account is used, and none has been built and verified. A
program-name allowlist does not help, because interpreters and test runners (`python`,
`pytest`, `npm`) execute whatever the project or their arguments contain. So the capability is
off, in three independent places:

1. `settings.service_config_from_data` refuses `[terminal] enabled = true`
   (`TERMINAL_DISABLED`), so no config file can turn it on; the CLI cannot either.
2. `tools.build_registry` never registers `run_command` and never imports `terminal`,
   whatever the `ServiceConfig` says (a configuration forced on in code still gets no tool).
3. `Gate.policy_for` gives any `EXECUTE` tool `every_call`, ignoring `once_per_session`, so a
   code-executing tool added later would need a fresh approval for each call.

Indirect routes: the file tools cannot write into the protected runtime (no planted module or
`.pth` file), into the Startup folders, or into PowerShell profile folders. Input tools refuse
terminals, Explorer, the Run box, and the Windows key. What is not covered: a file the user
later opens or runs themselves, another program on the PC, and Claude Code's own shell tool,
which this server does not control.

`terminal.py` is kept, with its tests (`test_terminal.py`, and the gate's handling of an
`EXECUTE` tool in `test_approval_integrity.py` and `test_audit_completeness.py`, through
`service_support.registry_with_terminal`): resolved programs, `BLOCKED_PROGRAMS`, no shell, a
pinned working directory, an allowlisted environment, a Job Object with `KILL_ON_JOB_CLOSE`
assigned before the process resumes, capped and escaped output, one command at a time. None
of that confines the program, which is why it stays unreachable.

### Screen and input (`desktop/`)

`desktop/tools.py` makes every decision; `desktop/win32.py` only reports facts and performs
the action (`EnumWindows`, `GetForegroundWindow`, `WindowFromPoint`, GDI `StretchBlt` with
`HALFTONE`, `SendInput`, `SetForegroundWindow` with `AttachThreadInput`, process image names
and token elevation through `OpenProcess` and `OpenProcessToken`). The process is made
per-monitor DPI aware so screen coordinates are physical pixels throughout. Screenshots are
encoded as PNG in pure Python (`desktop/png.py`) and returned as MCP image content; they are
kept only in the MCP response.

Input safeguards, checked under a machine-wide named mutex
(`Local\UniversalAITools.Input`) so the two clients' servers never interleave input:
the named window must exist, must not belong to a protected program, class, this process,
or an elevated process (or one whose rights cannot be read), must be in
`[input] allowed_processes` if that is set, and no approval dialog may be open anywhere.
Then the window is brought to the foreground and, for clicks and scrolls, must be the
top-level window under the point, which must lie inside a screenshot no older than five
minutes. Typing re-checks the focus and the absence of approval dialogs every eight
characters.

### The Claude Code guard (`guard.py`)

Claude Code's built-in tools (Bash, PowerShell, Write, Edit, MultiEdit, NotebookEdit, Read,
Glob, Grep) do not call the MCP server. The guard is a Claude Code hook, registered by
`setup` in `~/.claude/settings.json` for `PreToolUse`, `PostToolUse`, and
`PostToolUseFailure` with the matcher `*`, in exec form (`command` is the runtime's
`python.exe`, `args` are `-I -m universal_ai_tools.guard --config <file>`), with
`"onFailure": "block"` on `PreToolUse`.

Per call it reads only the `version`, `[audit]`, and `[guard]` keys of the config file, with
`tomllib` and no pydantic (about 0.3 s per call on the development PC, measured end to end),
builds the same protected set as the file-mode server (`TrustedPaths.for_state`) plus the
home directory's shell and Git startup files, and classifies the tool:

- **change**: the target's real path (links and junctions resolved, Windows' short names
  expanded) is refused if it is protected, except Claude Code's memory
  (`~/.claude/projects/*/memory`) and plans (`~/.claude/plans`); if any component is
  repository metadata or a credential directory; if it is a project's
  `.claude/settings.json`, `.claude/settings.local.json`, or `.claude/hooks`; if the file
  has more than one hard link; or, on Windows, if it is a network or device path, names an
  alternate data stream, or has a component ending in a dot or space. Other `.claude`
  contents, `.mcp.json`, `.vscode/tasks.json`, and credential-looking names are asked.
- **read**: Claude Code's login token is refused; credential-looking files and directories
  are asked.
- **command**: asked by default; with `commands = "claude"`, asked only when its text names
  a protected location or a marker such as `.claude`, `hookspath`, or `schtasks`.
- **other** (MCP tools, web, agents): no answer.

A refusal is exit code 2 with the reason on stderr, which Claude Code documents as blocking
regardless of any other decision; a question is the documented
`hookSpecificOutput.permissionDecision = "ask"` JSON, which Claude Code shows as its prompt
even in auto mode. The guard never returns `allow`. A change or command is recorded durably
before it runs (`guard_check`); if that record cannot be written, it is refused. Every call
is recorded after it ran (`guard_finished`). Commands are recorded with credential-looking
values redacted (`guard.redact`); contents and edit strings are never recorded.

The guard runs from the **protected runtime**: `install` creates
`~/.universal-ai-tools/runtime` (a virtual environment made by the base Python) and installs
the package into it, non-editable; `setup` points the hook and the MCP registrations at its
`python.exe`. That directory is inside the protected state directory, so the guard protects
the code it runs, while the checkout stays editable. Updates replace only the package's own
pure-Python files (`--force-reinstall --no-deps`), which works while hooks and servers run.

Limits: the guard cannot confine an approved command, which can change everything it
protects. A project's settings can set `disableAllHooks` (Claude Code reads the value left
after settings precedence); the guard refuses Claude's file tools that change project
settings, but not a command or a manual edit. A managed settings file with
`allowManagedHooksOnly` would close that, and needs an administrator. MultiEdit's and
NotebookEdit's input fields are not in Claude Code's hook documentation; the guard checks
`file_path`, `notebook_path`, and `path` for every change tool, and treats an unknown
built-in tool whose name contains `Edit` or `Write` as a change.

### Controls by name and the emergency stop (`desktop/uia.py`, `desktop/stop.py`)

`uia.py` is the UI Automation client (through `comtypes`, which generates the COM wrappers
from Windows' `UIAutomationCore.dll` type library). One daemon thread joins COM's
multithreaded apartment and keeps it, and the `IUIAutomation` object, alive; every call runs
on a fresh daemon thread in that apartment and is waited for at most 10 s (inspection) or 5 s
(actions), so a hung application, or a button whose handler opens a modal dialog, cannot
block the server: a slow action is reported as sent but unfinished. `IUIAutomation2`
connection and transaction timeouts are set too. `inspect` walks the control view depth first
up to a limit and remembers each element under an integer handle; `Desktop` maps them to
`e<n>` ids per inspection (at most 5 inspections kept; older ones release their elements).
`act_on_element` re-reads the element (`element_state`) under the input lock and checks its
process, enabled state, password flag, and supported actions before the pattern call.

`stop.py`: the stop is a file in the state directory, so every server process sees it and it
outlives a restart; the in-memory fallback covers an unwritable directory. `Desktop.target`,
which every input action goes through, checks it and the pointer first.

### Grants, the banner, and the stop hotkey (`desktop/grants.py`, `desktop/overlay.py`)

A `ToolSpec` may carry `grant`, a function from its validated arguments to the id of an
active grant covering the call (input tools: `Desktop.grant_for`, which resolves the target
window's program, refuses protected or disallowed windows, and returns nothing while input is
stopped), and `always_ask`. The gate consults `grant` only when approval is needed and never
for an `always_ask` or code-executing tool; a covered call is recorded as approved through
channel `grant` with the grant's id, then runs through the same handler checks as any other.
`request_input_grant` is `always_ask`. Its handler, which runs only after approval, checks
the programs against `allowed_processes` and the protected list, and the minutes against
`max_grant_minutes`.

`GrantStore` keeps a process's grants in memory with a monotonic expiry and a wall-clock issue
time. Revocation writes the wall-clock time to `grants-revoked` in the state directory; a grant
issued earlier is void in every process. An unreadable file voids every grant.

`overlay.py` runs one thread with a Windows message loop: a layered, click-through,
no-activate, topmost, untitled popup that polls `Desktop.banner_text` every second, and a
`RegisterHotKey` registration of Ctrl+Alt+Esc (retried every 5 s if another process holds it)
whose message calls `Desktop.hotkey_pressed`, which stops input and revokes every grant.

### Client registration (`clients.py`, `cli.py`)

Never imported by the server. `setup` writes the config file (diff shown, backup kept), plans
the Claude Desktop change as a full before/after text, refuses JSON that is invalid or not an
object, backs up, re-reads before writing (refusing if the file changed meanwhile), and
writes atomically. Claude Code is changed only through `claude mcp add-json --scope user`;
`~/.claude.json` is only read, to detect existing and shadowing `local` registrations.
`doctor` starts the server from the registration entry and performs a real MCP handshake,
compares the runtime's package with the checkout's, checks that the installed hook entries
are exactly what `setup` writes, and runs the installed `PreToolUse` hook on three sample
calls (a refused change to Claude's settings, an ordinary change, a command).

### Removed: the API agent

See [The correction in Milestone 6](#the-correction-in-milestone-6) and
`legacy/api-agent/README.md`.

### Planned: stronger containment

The checks in `workspace.py` run inside the server process, with the rights of the user who
started it. They cannot stop a concurrent process from presenting a file the server's user
can read under a name inside the workspace (see the hard-link defect above). Stronger
protection has to come from outside the process. These are **future requirements**, and
neither is implemented or enforced today:

- **Dedicated OS user (privilege separation).** Run the server as its own account. This is
  effective **only if that account cannot read any sensitive file outside its authorized
  workspace**. A hard link does not change a file's owner or permissions, so a linked file
  stays unreadable to the server only when its permissions already deny that account. Any
  file the account can read (world- or group-readable, or granted by an ACL) remains
  exposed. On Linux, `fs.protected_hardlinks=1` also stops users from linking files they
  neither own nor can read and write.
- **Filesystem isolation.** This is effective **only if no hard link can cross the security
  boundary**: the workspace must sit on a filesystem, or btrfs subvolume, that holds no file
  the server must not read. A hard link cannot cross filesystems (`EXDEV`), so a link inside
  the workspace can then only point at another workspace file. A container or a bind mount
  does **not** solve this on its own. A bind mount shows part of the same filesystem, so a
  link can be created through any other path to it, and a linked file is the same file in
  every view.

### Planned: execution tiers

Milestone 4 removed its `run_command`. Its model, where a process receives workspace paths and
an argument policy tries to contain what it does, cannot be made sound: every serious finding
in its security review came from what a tool did on its own after passing validation. The
Milestone 6 terminal did not try: it confined nothing and asked the human to approve each
command instead. It is now disabled (see
[Command execution: disabled](#command-execution-disabled-terminalpy)), because approval
cannot stop an unconfined program from changing the server itself. The tiers below remain
the design for running commands at all; **none of them exists yet**.

The rule for all later work: **the server does all workspace I/O, and a tool never receives a
workspace path.** That removes links, imports, configuration discovery, and repository
metadata as routes, instead of validating each one.

| Tier | Runs what | Boundary | State |
| ---- | --------- | -------- | ----- |
| 0. Filesystem tools | Server code only | The policy and handle-based reads | **Implemented** |
| 1. Bytes-only adapters | A trusted tool that does not execute its input | The server reads verified bytes; the tool gets stdin, an empty directory, and fixed arguments | Planned, for Ruff only |
| 2. Sandboxed execution | Anything, including project code | OS or VM isolation over a disposable snapshot | Planned |

Tier 2 goes through a generic `SandboxBackend` interface. A backend receives a request and a
**copy** of the workspace, never a mount. The copy is built through the verified read path under
`Operation.READ`, so it leaves out `.git`, links, hard links, and sensitive files. A backend
gets no host paths and no network by default, has CPU, memory, process, disk, and time limits, and
returns bounded output and any changes as a reviewable patch. A hostile test program must fail
to reach anything outside the sandbox on that machine, or the execution tool is not registered.

`pytest`, `mypy`, and `git` can return only in Tier 2. Argument validation and `shell=False`
would remain as input hygiene and do not count as isolation. An OS sandbox shares the host
kernel, so deliberately hostile code needs a VM.

Per-capability gating and an audit log of authorized and denied calls now exist (`gate.py`, `audit.py`).

### Configuration (`config.py`)

- `load_config()` reads only the workspace, log-level, and write-tools variables, and checks
  whether the removed command variables are set. It never iterates over, stores, or returns the
  rest of the environment. Optional values are trimmed; empty or whitespace-only optional
  values are treated as unset.
- `enable_write_tools` defaults to `false`. A string value must be `true` or `false`
  (case-insensitive, after trimming); anything else is a validation error. Pydantic's usual lax
  booleans (`1`, `yes`, `on`, ...) are deliberately rejected, and the field is `StrictBool`, so
  non-boolean values passed in code are rejected too. The error message names the variable but
  does not echo the rejected value.
- `REMOVED_SETTINGS` lists `UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS` and
  `UNIVERSAL_AI_TOOLS_COMMAND_ALLOWLIST`. Any non-blank value, including `false`, is a
  `ConfigError` that does not echo the value. Nothing reads them for any other purpose, so a
  stale registration fails visibly instead of being ignored by a server expected to behave
  differently. `pytest`, `mypy`, and `git` cannot be enabled by any configuration.
- `AppConfig` is a frozen pydantic model with `extra="forbid"`, so unknown settings are
  errors rather than silently ignored.
- Workspace validation: the path must be absolute, exist, and be a directory. It is
  resolved (symlinks followed) first, and then refused if it is a filesystem or drive
  root, the home directory, or any parent of the home directory. There is **no default**;
  the user must name the workspace. The resolved path becomes the `Workspace` root.
- Validation failures raise `ConfigError`, whose message names the offending environment
  variable and does not echo unrelated input.
- Filesystem limits are not configurable through the environment. `FilesystemLimits` is a
  frozen dataclass with documented defaults.

## Threat model

**Assets.** The user's files, accounts, and machine; whatever is on the screen.

**Trusted.** The human at the PC; the Claude client's process and its approval UI; this
package's code; the user's Windows account. **Untrusted.** Everything Claude reads through
these tools (file names and contents, command output, window titles, screen content) and,
through it, the model's requests: a web page or file can try to steer Claude (prompt
injection). The model is treated as able to request anything its tools allow.

| Threat | Defence | Residual risk |
| --- | --- | --- |
| Injected text makes Claude change, run, or type something harmful | Every consequential call needs a human's approval showing exactly what will happen; outputs are labelled untrusted and control characters escaped | A human who approves without reading. Approval is only as good as the attention behind it |
| Claude approves its own action | Approval goes to a human-facing dialog, never back to the model; input is refused while any approval dialog is open, and to Claude's, terminals', and editors' windows | A client that auto-answers elicitation. Claude Code headless cancels (verified); other clients unverified |
| Escaping the workspace with file tools | Pinned traversal, link and junction refusal, real-path and 8.3-alias checks, hard-link refusal, policy on both ends of a move | The documented Linux hard-link race ([Known limitations](#known-limitations)) |
| Rewriting its own permissions or log | Config file, its directory, and the audit log must be outside the workspace and are protected locations; workspace may not contain Claude's settings; no tool runs commands | A GUI action in an allowed application can edit them, as the user could |
| Rewriting its own code (the workspace contains the checkout, the virtual environment, or a directory on the import path) | `trusted.TrustedPaths`, refreshed before every check: the package directory, every loaded module's location (including later imports), every `sys.path` entry (including later ones), every directory a `.pth` file names, editable-finder mappings, the user site directories, the Python prefixes and interpreter directory, the config, audit log, and Claude's settings, all as real paths; `Workspace` refuses to create, change, or move anything at or below them (lexically and on the real path of what it opened) and to move a directory containing one; the server refuses to start where it would import from the current directory | A location only future code would import from; a confusable but different path that the user approves; another program, or Claude Code's own tools |
| Approving something other than what runs | The prompt shows every value in full and exactly, or the call is refused as too large; the call runs from the canonical arguments that were shown, and a deferred answer is bound to their digest by a single-use nonce | A client that cuts a long prompt it was sent (unverified for Claude Code and Claude Desktop); Unicode confusables and canonically equivalent spellings (`é` and `e` + U+0301) look alike |
| Running arbitrary code | General command execution is disabled and cannot be turned on; no tool can execute code; any future `EXECUTE` tool needs a fresh approval per call; file tools cannot write to Startup or PowerShell profile folders | A file the user later runs; GUI input to an allowed application that can itself run commands |
| GUI input launching programs (Run box, address bar, Start) | Explorer, taskbar, Start, search, and the Windows key refused | Any allowed application that can itself run commands |
| Privilege escalation | Server refuses input when elevated; it starts no processes; no input to elevated windows | None known within this design |
| Background or leftover processes | The server starts no processes | None within this design |
| Secrets leaking into logs | The audit log never stores file contents, search patterns, typed text, output, or screenshots; credential-looking command arguments are redacted; the child gets an allowlisted environment | A secret in a command argument that the name rule does not recognise is recorded |
| The two clients acting at once | Machine-wide input mutex; one command at a time per process; append lock on the audit log | Two commands from the two clients can run at the same time |

## Dependencies

| Dependency          | Why                                                          |
| ------------------- | ------------------------------------------------------------ |
| `mcp` (>=2.3, <3)   | Official MCP Python SDK: server, stdio transport, test client |
| `pydantic` (>=2.12) | Config validation and tool output models (also used by `mcp`) |
| `pytest` (dev)      | Test runner; async tests use anyio's bundled pytest plugin   |
| `ruff` (dev)        | Lint and formatting                                          |
| `mypy` (dev)        | Strict static type checking                                  |

There is no model-provider SDK and no network client: the package never calls a model API.
The terminal, screen, and input layers use only the standard library (`subprocess`,
`ctypes`, `zlib`). The filesystem tools use only the standard library (`os`, `stat`, `re`, `ctypes` and
`msvcrt` on Windows, `fcntl` on macOS). The build backend is `hatchling`. The version is
defined once, in `universal_ai_tools.__version__`.

## Testing strategy

- **Unit.** Tool functions and `load_config` are called directly.
  `tests/test_filesystem.py` builds synthetic workspaces under pytest's `tmp_path` and
  covers:
  - normal, empty, and missing cases
  - absolute and `..` paths
  - symlinks, and directory junctions where symlinks need privileges (Windows)
  - binary, invalid UTF-8, and oversized files
  - every truncation reason
  - permission errors, simulated by wrapping `_open_file` and `_scan_entries`, plus real
    permission bits on POSIX
  - for `write_file`: exact bytes on disk (Unicode, `\n`/`\r\n`/`\r`, no added BOM), UTF-8
    byte counts, the 1 MiB boundary in ASCII and multibyte text, refusal of every kind of
    existing target, cleanup after write, flush, and interrupt failures, Windows share mode
    locking, POSIX cleanup that refuses to remove a replacement file, and excluded locations
- **Policy.** `tests/test_policy.py` runs each rule on a real filesystem through every tool:
  - every sensitive name and every allowed exception, through `read_file`, `search_files`,
    `write_file`, and `list_files`
  - `.git`, `.hg`, and `.svn` as directories and as files, nested, in other letter cases, and with
    trailing characters
  - sensitive directories, excluded directories, and hard links
  - symlinks, junctions, and 8.3 aliases of files, directories, and metadata, with a control
    showing an ordinary alias still works
  - **real races:** wrappers around `_open_child`, `_open_file`, `_scan_entries`, and
    `_create_new_file` change the disk at the moment between a check and its use, swapping a
    file or a parent directory for a symlink, junction, hard link, or other file
  - proof on Windows that every pinned component refuses a rename, and that a pin ends with the
    call
  - the response limit at and just beyond the exact boundary, and in bytes rather than characters
  - no leaked descriptors or handles, on success or refusal
  - escaping, labels, and undecodable file names
- **No child processes.** `tests/test_no_subprocess.py` records process creation with Python audit
  hooks while every tool is called, in-process and in a real server process whose hook is
  installed before anything else is imported. Each recorder has a control showing it does fire.
  Audit hooks see Python's own process APIs, and a test also parses this package's source and
  refuses any such API. Native code inside a dependency, or a direct OS call through `ctypes`,
  would be seen by neither. `ctypes` is used only for the file APIs in `workspace.py`, which was
  checked by reading it, so this is evidence for the calls that ran and not a guarantee.
- **Races against a second process.** `tests/test_races.py` starts a real process. Deterministic
  tests have it act while a call holds a directory or file. Concurrent tests run
  `tests/race_attacker.py` in a loop while the tools run, replacing a parent directory with a
  link, or a file with a symlink, a hard link, or another file. On Linux it also swaps names
  atomically with `renameat2(RENAME_EXCHANGE)`. Each run that can has a control: a naive
  check-then-open reader, with its gap deliberately widened, in the same loop. If the control
  never reads the outside file, the run does not pass. A control shows that the attacker changes
  the disk between a check and a use, not that it reached every window in the hardened code.
- **MCP surface snapshot.** `tests/test_mcp_surface.py` compares everything a client can
  observe with `tests/snapshots/mcp_surface.json`, recorded from the server before the tool
  registry existed: server identity, instructions, every field of `tools/list`, and the
  results of a fixed sequence of calls (successes, refusals, schema violations, SDK
  coercions, ignored properties), for the default configuration, with write tools, and with
  lowered limits. A deliberate change to the surface needs a new recording
  (`python tests/test_mcp_surface.py --update`), reviewed with the change.
- **Tool registry.** `tests/test_registry.py` covers the exact tool set for each
  configuration, risks and annotations, `ToolSpec` invariants, unknown names, unexpected
  properties, invalid types and values the SDK would coerce (without reaching the
  filesystem), bounds, error messages that never echo values, dispatch to only the named
  handler, the filesystem refusals and link refusals through the registry, that the server
  registers exactly the registry and routes every call through `invoke`, and that `tools.py`
  uses no filesystem or dynamic-code APIs.
- **In-process protocol.** `mcp.Client(create_server(config))` runs the real MCP handshake,
  `tools/list`, and `tools/call` without a subprocess, including refusals and schema
  violations. The tool list and server instructions are checked for unset, empty,
  `false`, and `true` values of the write-tools setting, and calling `write_file` on a
  default server is shown to fail without creating anything. `run_command` is shown to be absent
  with write tools on and off.
- **End-to-end stdio.** `mcp.Client(StdioServerParameters(...))` spawns
  `python -m universal_ai_tools.server` over real pipes with the default configuration (four
  tools, `write_file` refused) and with write tools enabled (five tools, a file created and an
  overwrite refused). A real process started with a removed setting exits with status `2`.
- **Configuration.** `tests/test_config.py` covers the write-tools switch (the default, accepted
  spellings of `true` and `false`, empty values, a range of rejected values, and that the rejected
  value is not echoed) and the removed settings: every value refused, blank ignored, and the value
  never echoed.
- **Config file.** `tests/test_settings.py`: defaults enable nothing but reading, every
  section is read, unknown keys and wrong types are errors, no "never ask" policy exists, the
  config file, its directory, and the audit log may not be inside the workspace, the workspace
  may not be a Claude settings folder, mixing the file with variables is refused, rendering
  round-trips, and the former API-agent variables are refused without echoing their values.
- **Gate and approval.** `tests/test_gate.py`, over both MCP protocol generations with the SDK
  client: an approved write runs, and deny, decline, cancel, a wrong value, an extra field, and
  empty content all refuse; read-only tools never ask; the Windows dialog when the client cannot
  ask (yes, no, dismissed); `desktop_dialog` and `client` channels never substitute for each
  other; no channel fails closed; silence is no; `once_per_session` asks once, expires, and a
  denial grants nothing; a deferred answer for different arguments, a replay, and a forged
  nonce approve nothing; the audit log never holds content, and a call that cannot be audited
  does not run; environment mode is unchanged.
- **File changes.** `tests/test_file_actions.py`: exact-count edits, CRLF handling, atomic
  replacement with no leftovers, every refusal (sensitive, metadata, excluded, `..`, binary,
  oversized), hard links, symlinks, junctions, and 8.3 aliases; directory creation; moves that
  never overwrite, never move into themselves, never cross a junction, and leave the tree
  unchanged when refused.
- **Terminal.** `tests/test_terminal.py`: resolution to absolute paths, shells and admin tools
  refused by name and by path, programs and PATH entries inside the workspace refused, batch
  files refused, the schema offering only configured programs, exit codes and output, no
  shell interpretation, the working directory confined, the environment filtered, output caps,
  escaping, NUL arguments refused, a child process killed on timeout, on normal exit, and on
  cancellation, one command at a time, and the approval text.
- **Screen and input.** `tests/test_desktop.py`, against a fake desktop: PNG encoding, capture
  scaling and coordinate mapping, escaped titles, every protected window refused for every
  input tool, `allowed_processes`, no input while an approval dialog is open or another
  process holds the input lock, clicks only on the named window under the point, stale and
  unknown screenshots refused, typing stopped when the focus moves, refused keys, typed text
  shown for approval but never audited. One Windows test reads a real 16x9 screenshot.
- **File-mode server.** `tests/test_file_mode_server.py`: each capability adds exactly its tools,
  annotations match risks, instructions put Claude in charge, screenshots arrive as MCP image
  content, input runs only after approval, and a search-then-write task over real stdio
  subprocesses on both protocol generations, approved and denied.
- **Processes.** `tests/test_no_subprocess.py` keeps the audit-hook and source-scan evidence:
  every module except `terminal.py` and `clients.py` is scanned for process APIs; in file mode
  no process starts, even with the terminal forced on in code (no `run_command` exists then
  either); and `terminal.py`, `clients.py`, and `cli.py` are never imported by the server.
- **Client registration.** `tests/test_clients.py`: the Claude Desktop merge keeps every other
  setting, backs up, is idempotent, replaces a stale entry, never overwrites invalid JSON or a
  file changed since planning, and prefers the Store build's location; Claude Code's settings
  are read and never written; `setup --dry-run` changes nothing; `setup --yes` and `config
  enable` write a valid config with a backup.
- **Mutation check.** `tests/mutation_check.py` (optionally limited to named source files)
  removes one safeguard at a time from a scratch copy and reports how each mutant ended: killed by an assertion, by an expected exception that
  did not occur, or by an unexpected exception; timed out; survived; or could not be tested. A
  timeout is not a kill and fails the run, as do survivors and untested mutants. It is run by
  hand and covers only the safeguards it lists, so a clean run says nothing about code with no
  entry.
- **Skips.** A security test that cannot run on the platform it covers calls
  `support.security_skip`: a skip with `SECURITY COVERAGE MISSING` in the reason, or a failure when
  `UNIVERSAL_AI_TOOLS_STRICT_SECURITY_TESTS=1`, which CI sets.
- **Import hygiene.** A fresh import must not construct or run the server, change logging
  handlers, or write to stdout.
- **Type checks.** Platform-specific branches are type-checked with
  `mypy --platform linux` and `--platform darwin` as well as on the host platform.

## Verification status

Passing tests show what happened on the systems below, for the cases written. They are not
a proof, and this section keeps the different kinds of evidence apart. Type checks are not
runtime evidence: `mypy --platform darwin` passing says nothing about how the code behaves on
macOS.

**Where it has run**

| Environment | Filesystem | State |
| ----------- | ---------- | ----- |
| Windows 11, Python 3.13 | Local NTFS | Run |
| Linux under WSL2 (Ubuntu 24.04), Python 3.13 | ext4 | Run |
| GitHub Actions, Ubuntu and Windows | | Not run for Milestone 6 |
| macOS | APFS, HFS+ | **Never run; not supported** |
| Any platform | ReFS, FAT, exFAT, SMB, NFS, btrfs, overlayfs, ZFS, tmpfs | Not tested |
| Windows | Dehydrated OneDrive or other cloud placeholders | Not tested |

**How each kind of claim is supported**

| Claim | Evidence |
| ----- | -------- |
| Name, metadata, hard-link, and link rules across all tools, with no concurrent change | Tested on real files on NTFS and ext4 |
| **NT file symlinks (`IO_REPARSE_TAG_SYMLINK`) on Windows** | **Not tested.** Needs Developer Mode or an elevated run. On the development PC 16 tests skip (`SECURITY COVERAGE MISSING`), and the mutant "file open follows reparse points (Windows)" cannot be tested. The same tests run on ext4. WSL-created links (tag `0xA000001D`) were refused once by hand, and one handle check is simulated, but neither is an NT symlink |
| **Concurrent parent replacement through a junction** (`test_races.py::test_concurrent_parent_replacement_never_escapes[junction]`) | **Not verified: outstanding.** The run is skipped whenever its control reader never catches the attacker mid-swap, which on Windows is most runs, because the attacker's renames are mostly refused while files in the directory are open. The pinning it should show is covered deterministically (`test_other_process_cannot_move_or_delete_pinned_directories`) and concurrently for listings (`test_concurrent_parent_replacement_is_blocked_by_the_listing_alone`). Proposed replacement: force the interleaving instead of hoping for it, by pausing each tool at the point after a component is checked (a hook on `_Directory.descend` / `_open_file`), performing the swap from a second process at exactly that point, and requiring the swap to be refused or the result to hold nothing from outside; the control is the same forced swap against a naive check-then-open reader, which must then read the outside file |
| **Claude Code's built-in tools** | **Checked by the guard, when it is installed** (`guard.py`): refusals, forced questions, and audit records are tested in `test_guard.py` against real paths and as a real `-I` process. **Not yet verified in a live Claude Code session.** Commands that you approve are not confined |
| 8.3 aliases | Tested on real aliases on NTFS |
| Junctions | Tested on real junctions on NTFS |
| A pinned directory refuses rename, replace, and delete | Tested from a second process on NTFS |
| A swap of a pinned directory does not redirect a read, listing, or write | Tested from a second process on ext4 |
| No outside content under concurrent parent-directory swaps | Not seen under load, with a control, on ext4 (symlink, atomic exchange) and NTFS (junction). Not a proof |
| No outside content under concurrent file-symlink swaps | Not seen under load, with a control, on ext4. Not tested on NTFS |
| No outside content under concurrent hard-link swaps | **Fails on ext4; see the open defect.** Not seen on NTFS under load, with a control. Not a proof |
| A concurrent read is never torn | Not seen on ext4 or NTFS, under whole-file replacement |
| Races at one exact moment | Forced inside one process, by real changes on disk at that moment |
| No child process | Audit hooks and a source scan. Does not cover native code in dependencies |
| Response and path limits | Tested at and beyond the boundary, in compact JSON. Wire size read from the SDK source, not measured |
| `openat` chain matching `openat2` | Reasoning only |
| Swaps above the workspace root being caught by the real-path checks | Reasoning only |
| Windows volume mount points being refused | Inferred from their reparse tag; no real mount point tested |
| Linux refusing when a real path cannot be read | Tested on ext4: a real path over 4,095 bytes, and `/proc` made unreadable by a test double. Not run on a system with no `/proc` mounted |
| Platforms without `dir_fd` | Code exists; never run |
| POSIX cleanup race and hard kill mid-write | Reasoning only |
| Sharing conflicts with other programs on Windows | Reasoning only, apart from the pinning tests above |
| No child process | Audit hooks and a source scan, in environment mode and in file mode, with the terminal forced on in code; `terminal.py` is never imported. Does not cover native code in dependencies |
| Command execution cannot be turned on | Config file, `setup`, `config`, a forced `ServiceConfig`, and a real process: `test_fail_closed.py`, `test_file_mode_server.py`, `test_no_subprocess.py` |
| Approval over both MCP protocol generations | Tested in process with the MCP SDK's client, and over real stdio subprocesses |
| Approval with Claude Code | **Live:** Claude Code 2.1.319 negotiates 2026-07-28 and declares elicitation; headless `claude -p` cancels the question and the write is refused. **Interactive approval not yet observed** |
| Approval with Claude Desktop | **Not tested** |
| Windows approval dialog | Logic tested with a stand-in; the real dialog was not shown in automated tests |
| `edit_file`, `create_directory`, `move_path` | Tested on NTFS: refusals, junctions, hard links, 8.3 aliases, atomic replacement, no overwrite. File-symlink cases need Developer Mode |
| `run_command` | **Disabled**. The kept module is still tested on Windows 11, outside the server: program resolution and refusals, no shell, environment filter, output caps, escaping, time limit, children killed on exit, timeout, and cancellation, one at a time |
| Screen capture | Real capture and window listing on Windows 11 (1920x1080 at 150 %); safeguards with a fake backend |
| Mouse and keyboard input | **Safeguards tested only against a fake backend. No real input has been sent by tests** |
| Claude Code end to end | **Live:** a read-only task typed into headless Claude Code was planned by Claude and carried out through `list_files` and `search_files`, with no API key |
| Setup and doctor | Config writing and Claude Desktop JSON merge tested on temporary files; `doctor` live handshake run |

**Races not tested:** mount or unmount during a call, a filesystem that changes underneath the
server without normal rename semantics (network shares), and several attackers at once. Windows
pinning was tested against rename, replace, and delete from another process, not against
privileged tools or a kernel driver. A process with the same user's rights can always
change files by other means, such as editing a file before the server reads it.

The documented limits on secrets, prompt injection, bind mounts, and cloud placeholders are in
[Known limitations](#known-limitations).
