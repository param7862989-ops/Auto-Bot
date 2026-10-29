# Architecture

This document describes how Universal AI Tools is structured as of Milestone 4 (read-only
filesystem tools plus create-only file writing), and where later capabilities are meant
to fit.

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
│    or a newly created file                                    │
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
  block for clients that do not read structured output.
- `get_platform_info` returns only the application name, version, and status. It does
  not include paths, configuration, or environment data.
- `list_files`, `read_file`, `search_files`, and `write_file` take a `Workspace` and a
  `FilesystemLimits`. They never touch the filesystem except through the `Workspace`.
  They validate their own non-path arguments (pattern length, result counts, content)
  again, so they are safe to call without the MCP schema in front of them.
- `write_file` validates the content first (no NUL characters, valid Unicode, at most
  `max_write_bytes` once encoded as UTF-8), then hands the encoded bytes to
  `Workspace.create_file`. It passes the excluded-directory rule (`.git`, `.venv`,
  `node_modules`, caches, ...) as the set of directories that must not be written into.
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

Search treats the pattern as literal text. It is passed through `re.escape`, so there is
no user-controlled regular expression and no catastrophic backtracking.

### 4. Path confinement (`workspace.py`)

`Workspace` is the security boundary. It holds the resolved workspace root from
`AppConfig` and never widens it. Every path argument is treated as untrusted and checked
in three stages.

1. **Lexical, before any filesystem access.**
   - Refuse NUL characters.
   - Refuse absolute paths, a leading `/` or `\`, drive letters (including drive-relative
     `C:file`), and UNC and `\\?\` paths. Both POSIX and Windows rules are applied on every
     platform.
   - Split on `/` and `\`, drop empty and `.` components, and refuse any `..` component.
   - On Windows, refuse components that `os.path.isreserved` flags (device names such as
     `NUL` or `COM1`, names ending in a dot or space) and any component containing `:`
     (alternate data streams).
   - Refuse components naming a sensitive directory (`.ssh`, `.gnupg`, `.aws`). For
     `read_file`, also refuse a sensitive final file name, such as `.env` or `*.pem`.
     Because this check runs before the filesystem is touched, a refusal does not reveal
     whether the file exists.
2. **Per-component `lstat` walk.** Starting at the root, every component is
   `lstat`-ed, so links are inspected rather than followed. A component that is a symlink,
   a Windows junction, or another *name-surrogate* reparse point is refused, whether it
   points inside or outside the workspace. Every non-final component must be a directory.
   Other reparse points, such as OneDrive cloud files, are ordinary files and are allowed.
3. **Post-open verification (file contents).** `read_bytes` opens the file with
   `O_RDONLY` plus, where available, `O_NOFOLLOW`, `O_NONBLOCK`, `O_NOCTTY`, `O_BINARY`,
   and `O_NOINHERIT`. Then:
   - It `fstat`s the open descriptor and requires a regular file that is the *same file*
     (device and inode, `os.path.samestat`) as a fresh `lstat` taken just before the open.
   - It asks the OS for the real path of the open descriptor and requires it to be inside
     the root. On Windows this uses `GetFinalPathNameByHandleW` via `ctypes`; on Linux,
     `/proc/self/fd/N`; on macOS, `fcntl(F_GETPATH)`. The sensitive-name rules are applied
     again to that real path, which catches aliases such as Windows 8.3 short names
     (`ENV~1` for `.env`).
   - It refuses files larger than the limit by their `fstat` size, then reads at most
     limit + 1 bytes, in case the file grows during the read.

   If a directory on the path is swapped for a link between stage 2 and the open, the
   opened file has a different identity or a real path outside the root, and is refused.
   File contents from outside the workspace are therefore never returned.

#### Creating files (`Workspace.create_file`)

`create_file` is the only code that changes the filesystem. It runs these steps in order,
and nothing is created until all checks before step 5 have passed:

1. **Lexical.** The stage-1 checks above, plus: the path must name a file (not `.`, and not
   ending in a separator); the final name must not be a sensitive file name; and no
   component may be a protected (excluded) directory name. This includes the final name, so
   a `.git` *file*, which could point Git at another repository, is refused.
2. **Parent walk.** The parent directory goes through the stage-2 `lstat` walk. It must
   exist, must be a directory, and no component may be a link. Missing parents are reported
   as such and are never created.
3. **Parent real path.** `os.path.realpath(strict=True)` of the parent must lie inside the
   root, outside sensitive directories, and outside protected directories. This catches
   8.3 aliases such as `GIT~1` for `.git`.
4. **Free target.** `lstat` of the target must fail with "not found". This gives a clear
   error for the common case; it is *not* what prevents overwriting.
5. **Exclusive create.** This is what prevents overwriting, including when something
   appears between step 4 and step 5:
   - **POSIX (Linux, macOS):** the root is opened with `O_DIRECTORY | O_NOFOLLOW`, then
     each parent component with `openat(..., O_DIRECTORY | O_NOFOLLOW)` relative to the
     previous descriptor. The final directory must be the same inode that step 2 saw. The
     file is created with `openat(parent_fd, name, O_WRONLY | O_CREAT | O_EXCL |
     O_NOFOLLOW)`. `O_EXCL` fails on any existing name, including a dangling symlink. A
     component swapped for a symlink after step 2 makes the walk fail, so nothing is
     created.
   - **Windows:** `CreateFileW` with `CREATE_NEW` and `FILE_FLAG_OPEN_REPARSE_POINT`, with
     share mode 0 and `DELETE` access. `CREATE_NEW` never opens an existing file, directory,
     symlink, or junction. Share mode 0 stops other processes from opening, renaming, or
     deleting the file while it is being written. Windows has no `openat` here, so the file
     is created by its full path (see step 6 and the limitations below).
   - Other platforms (no `dir_fd` support) are refused rather than given a weaker
     implementation.
6. **Verify before writing.** The new descriptor must be a regular file, and its real path
   (`GetFinalPathNameByHandleW`, `/proc/self/fd`, `F_GETPATH`) must be inside the root and
   outside sensitive and protected directories. On Windows it must also equal the parent's
   real path from step 3 joined with the requested name, which detects a parent swapped
   for a junction after step 3.
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

Directory listings use `os.scandir` and inspect each entry with
`DirEntry.stat(follow_symlinks=False)`. Links are reported as `symlink` and never
descended into. Before each directory is scanned, its real path is checked to be inside
the root.

Error messages are built only from the normalized, workspace-relative path and fixed text.
`OSError` details (`filename`, `strerror`) are never included, because they can contain
absolute paths. File contents never appear in errors.

#### Known limitations

- **Directory listing race.** Path checks and the open happen at different moments.
  For file *contents*, stage 3 makes this safe. For directory *listings*, the real-path
  check and the `scandir` call are separate steps. A process that can write inside the
  workspace could swap a directory for a link in between, and that one listing would then
  expose entry names (not contents) from the link target. Closing this fully would need
  handle-relative directory opening (`openat` and `O_DIRECTORY | O_NOFOLLOW` on POSIX, or
  native Windows handles), which is not used yet.
- **Hard links.** A hard link inside the workspace to a file elsewhere on the same volume
  *is* that file. It cannot be told apart from an ordinary file, so it is readable.
  Creating one requires write access to the workspace.
- **Other platforms.** On platforms other than Windows, Linux, and macOS, or on Linux
  without `/proc`, the real-path check is skipped. Protection then rests on stage 2, the
  same-file identity check, and `O_NOFOLLOW`.
- **Sensitive names are a safety net, not a secrets scanner.** Secrets stored under other
  file names are readable like any other workspace file.
- **Cloud placeholders.** Reading or searching OneDrive (or similar) files that exist only
  in the cloud may make the sync client download them.
- **Name-based exclusions.** Excluded directories are recognized by name only. A user's own
  directory called `venv` or `.cache` is also skipped by recursive listing and search.
- **Search time limit.** The time limit is checked before each file, so one search can
  overrun it by the time it takes to scan a single file (at most 1 MiB).
- **No audit log yet.** Refusals are logged to stderr at INFO by the SDK, but there is no
  separate audit trail. Successful writes are not logged separately either.
- **Windows create race.** On Windows the file is created by its full path. If a process
  that can write inside the workspace swaps a parent directory for a junction between the
  checks and `CREATE_NEW`, the empty file is briefly created at the junction's target,
  possibly outside the workspace. Step 6 detects this before any content is written, and
  the file is deleted through its handle. Existing files there are never opened or
  overwritten, because `CREATE_NEW` refuses them. Closing this window fully would need
  handle-relative creation (`NtCreateFile` with a root directory handle), which is not used.
- **POSIX cleanup race.** Removing a failed file means a `fstatat` identity check followed by
  `unlinkat`. A process that renames the file away and puts its own file at the name in the
  instant between those two calls could have its file removed. This needs write access to
  the workspace and precise timing, and only matters when a write has already failed.
- **Hard kill mid-write.** If the server process is killed or the machine loses power while
  a file is being written, a partial file can remain. Normal errors and interruptions
  (exceptions, `KeyboardInterrupt`) are cleaned up. On POSIX, other processes can also see
  the file while it is being written; on Windows, share mode 0 keeps them out.
- **Parent directory permissions (POSIX).** Each parent directory is opened `O_RDONLY` to
  pin it, so a directory that is writable but not readable (for example mode `0o311`)
  cannot be written into.
- **Created files can still matter.** `write_file` cannot overwrite anything, but a new
  file can change how other tools behave, for example a new CI workflow under `.github/`,
  a new `conftest.py`, or a new script. Only `.git`, `.venv`, and the other excluded
  directories are protected. Review what an AI client creates before running it.
- **One coarse switch.** `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS` turns all write tools on
  or off together. It is fixed at startup, and there is no per-tool or per-directory
  policy yet (see below).

### Planned: capability gating and auditing

Future milestones that add more capabilities will extend the policy layer on top of
`Workspace`. Current responsibilities and known gaps are:

- **Capability gating.** Each tool declares the capabilities it needs (read files, write
  files, run a command, modify Git), and config enables them explicitly. Everything beyond
  read-only access is off by default. Write tools and command profiles have separate
  opt-in settings. `run_command` is available only when command tools are enabled and the
  operator supplies a non-empty profile allowlist.
- **No unrestricted shell.** `run_command` accepts only fixed profiles (`pytest`,
  `ruff-check`, `mypy-check`, `git-status`, and `git-diff-check`). It invokes an argument
  vector with `shell=False`, no caller-supplied arguments, a minimal child environment,
  a maximum 120-second timeout, and 64 KiB captured per output stream. Project code and
  test code still execute with the current user's local permissions; the workspace cwd
  does not sandbox filesystem access by the child process.
- **Secret hygiene.** Tools never return environment variables. Configuration reads
  only allowlisted keys.
- **Auditability.** Log every authorized and denied call to stderr, and later to an
  audit file.

### Configuration (`config.py`)

- `load_config()` reads only the workspace, log-level, write-tools, command-tools, and
  command-allowlist variables. It never iterates over, stores, or returns the
  rest of the environment. Optional values are trimmed; empty or whitespace-only optional
  values are treated as unset.
- `enable_write_tools` defaults to `false`. A string value must be `true` or `false`
  (case-insensitive, after trimming); anything else is a validation error. Pydantic's
  usual lax booleans (`1`, `yes`, `on`, ...) are deliberately rejected, and the field is a
  `StrictBool`, so non-boolean values passed in code are rejected too. The error message
  names the variable but does not echo the rejected value.
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
  - excluded and sensitive paths
  - every truncation reason
  - permission errors, simulated through `os.open` and `os.scandir`, plus real
    permission bits on POSIX
  - a simulated swap between check and open
  - for `write_file`: exact bytes on disk (Unicode, `\n`/`\r\n`/`\r`, no added BOM), UTF-8
    byte counts, the 1 MiB boundary in ASCII and multibyte text, refusal of every kind of
    existing target, targets and parent links that appear *after* the checks (simulated by
    hooking `os.lstat`), cleanup after write, flush, and interrupt failures, Windows share
    mode locking, POSIX cleanup that refuses to remove a replacement file, sensitive and
    excluded locations, and 8.3 short-name aliases on Windows
- **In-process protocol.** `mcp.Client(create_server(config))` runs the real MCP handshake,
  `tools/list`, and `tools/call` without a subprocess, including refusals and schema
  violations. The tool list and server instructions are checked for unset, empty,
  `false`, and `true` values of the write-tools setting, and calling `write_file` on a
  default server is shown to fail without creating anything.
- **End-to-end stdio.** `mcp.Client(StdioServerParameters(...))` spawns
  `python -m universal_ai_tools.server` twice over real pipes: once with the default
  configuration (four tools, `write_file` refused) and once with write tools enabled
  (five tools, a file created and an overwrite refused).
- **Configuration.** `tests/test_config.py` covers the write-tools default, accepted
  spellings of `true` and `false`, empty values, a range of rejected values, and that the
  rejected value is not echoed.
- **Import hygiene.** A fresh import must not construct or run the server, change logging
  handlers, or write to stdout.
- **Type checks.** Platform-specific branches are type-checked with
  `mypy --platform linux` and `--platform darwin` as well as on the host platform.
