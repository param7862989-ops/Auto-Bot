# Architecture

This document describes how Universal AI Tools is structured as of Milestone 4 (read-only
filesystem tools under one shared policy, and opt-in create-only file writing), and where
later capabilities are meant to fit. The server has no command execution, and the package has
no code that starts a process. [Verification status](#verification-status) says what has been
tested and what has not.

## Goals that shape the design

- **Local-first.** The server runs on the developer's machine and talks to its client over
  stdio. It makes no network calls and calls no AI APIs.
- **AI-agnostic.** Capabilities are exposed through MCP, an open protocol. Nothing in the
  tool logic is specific to Claude or any other model or vendor.
- **Least privilege by default.** The server is authorized to act in one explicitly named
  workspace directory. Broad locations are refused at startup. By default every
  registered tool is read-only. `write_file`, which can only add new files and never
  changes existing ones, is registered only when explicitly enabled in configuration.
- **Testable without a client.** Tool behavior can be exercised as plain Python functions,
  in-process over MCP, and through a real stdio subprocess.

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│ MCP client (Claude Code, or any other MCP client)             │
└──────────────────────────────┬───────────────────────────────┘
                               │ JSON-RPC over stdin/stdout
┌──────────────────────────────▼───────────────────────────────┐
│ 1. Transport and entry point          server.main()           │
│    stdio transport from the MCP SDK; logging to stderr        │
├──────────────────────────────────────────────────────────────┤
│ 2. Server assembly                    server.create_server()  │
│    MCPServer instance; registers tools, descriptions, and     │
│    annotations; maps WorkspaceError to MCP tool errors        │
├──────────────────────────────────────────────────────────────┤
│ 3. Tool logic                         server.get_platform_info│
│    plain typed functions returning    filesystem.list_files   │
│    pydantic models; no MCP imports    filesystem.read_file    │
│                                       filesystem.search_files │
│                                       filesystem.write_file   │
├──────────────────────────────────────────────────────────────┤
│ 4. Path confinement                   workspace.Workspace     │
│    the only code that turns an untrusted path into a          │
│    filesystem location, a directory listing, file bytes,      │
│    or a newly created file; asks policy.py every time         │
├──────────────────────────────────────────────────────────────┤
│ 5. Filesystem policy                  policy.refusal          │
│    pure decisions about names and paths, and output escaping  │
└──────────────────────────────────────────────────────────────┘
        ▲
        │ AppConfig (immutable, validated once at startup)
┌───────┴──────────────────────────────────────────────────────┐
│ Configuration                         config.load_config()    │
│ allowlisted environment variables → validated AppConfig       │
└──────────────────────────────────────────────────────────────┘
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
- Registers each tool explicitly with `add_tool`, together with its name, description,
  and `ToolAnnotations`. The read tools are marked read-only, non-destructive, idempotent,
  and closed-world. `write_file` is marked *not* read-only, non-destructive (it only adds
  files), not idempotent (a repeated call fails because the file exists), and closed-world.
- Takes an `AppConfig` argument, so each server is built from explicit configuration
  rather than global state. This also lets tests build isolated servers. An optional
  `FilesystemLimits` argument overrides the default resource limits (tests use this).
- **Write tools are gated.** The read-only tools are always registered. `write_file` is
  registered (by `_add_write_tools`) only when `AppConfig.enable_write_tools` is true.
  When it is false, the tool does not exist on the server at all: it is absent from
  `tools/list`, and calling it fails as an unknown tool. The server instructions are
  chosen to match (`READ_ONLY_INSTRUCTIONS` or `WRITE_ENABLED_INSTRUCTIONS`), so the
  client is never told about a capability it does not have. `main` logs at startup
  whether write tools are enabled.
- Builds one `Workspace` from `AppConfig.workspace_root` and binds the filesystem tools to
  it with small wrapper functions. The wrappers declare the MCP argument schemas
  (descriptions, `ge`/`le`/`max_length` bounds) and turn `WorkspaceError` into the SDK's
  `ToolError`. The client then gets an `is_error` result with that message, and the
  server logs it at INFO without a traceback. Any other exception is reported to the
  client only as `Error executing tool <name>`. Its details and traceback stay in the
  server's stderr log.

### 3. Tool logic (`server.get_platform_info`, `filesystem.py`)

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
- **No audit log yet.** Refusals are logged to stderr at INFO by the SDK, but there is no
  separate audit trail. Successful writes are not logged separately either.
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

Milestone 4 removed `run_command`. Its model, where a process receives workspace paths and an
argument policy tries to contain what it does, cannot be made sound: every serious finding in
its security review came from what a tool did on its own after passing validation. **Nothing in
this section exists yet**; it records the design that replaces it.

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

Later capabilities also need per-tool gating and an audit log of authorized and denied calls.

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

## Dependencies

| Dependency          | Why                                                          |
| ------------------- | ------------------------------------------------------------ |
| `mcp` (>=2.3, <3)   | Official MCP Python SDK: server, stdio transport, test client |
| `pydantic` (>=2.12) | Config validation and tool output models (also used by `mcp`) |
| `pytest` (dev)      | Test runner; async tests use anyio's bundled pytest plugin   |
| `ruff` (dev)        | Lint and formatting                                          |
| `mypy` (dev)        | Strict static type checking                                  |

The filesystem tools use only the standard library (`os`, `stat`, `re`, `ctypes` and
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
- **Mutation check.** `tests/mutation_check.py` removes one safeguard at a time from a scratch
  copy and reports how each mutant ended: killed by an assertion, by an expected exception that
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
| GitHub Actions, Ubuntu and Windows | | Not run yet |
| macOS | APFS, HFS+ | **Never run; not supported** |
| Any platform | ReFS, FAT, exFAT, SMB, NFS, btrfs, overlayfs, ZFS, tmpfs | Not tested |
| Windows | Dehydrated OneDrive or other cloud placeholders | Not tested |

**How each kind of claim is supported**

| Claim | Evidence |
| ----- | -------- |
| Name, metadata, hard-link, and link rules across all tools, with no concurrent change | Tested on real files on NTFS and ext4 |
| **NT file symlinks (`IO_REPARSE_TAG_SYMLINK`) on Windows** | **Not tested.** Needs Developer Mode or an elevated run. The same tests run on ext4. WSL-created links (tag `0xA000001D`) were refused once by hand, and one handle check is simulated, but neither is an NT symlink |
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

**Races not tested:** mount or unmount during a call, a filesystem that changes underneath the
server without normal rename semantics (network shares), and several attackers at once. Windows
pinning was tested against rename, replace, and delete from another process, not against
privileged tools or a kernel driver. A process with the same user's rights can always
change files by other means, such as editing a file before the server reads it.

The documented limits on secrets, prompt injection, bind mounts, and cloud placeholders are in
[Known limitations](#known-limitations).
