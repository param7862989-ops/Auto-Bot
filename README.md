# Universal AI Tools

A local-first, AI-agnostic developer automation platform. Its capabilities are exposed
through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so any
MCP-capable client can use them. Claude Code is the first supported client.

> **Status: Milestone 4 (allowlisted command profiles, opt-in).** The server runs over local
> stdio and exposes a diagnostic tool and three read-only filesystem tools, all confined to
> one authorized workspace. A create-only write tool is available but **disabled by
> default**; see [Enabling write tools](#enabling-write-tools). Nothing else is implemented
> yet; see [Not implemented yet](#not-implemented-yet).

## What it does today

| Tool                | Description                                                              |
| ------------------- | ------------------------------------------------------------------------ |
| `get_platform_info` | Returns `{name, version, status}`. No arguments.                         |
| `list_files`        | Lists a workspace directory, optionally recursively.                     |
| `read_file`         | Returns the contents of a UTF-8 text file in the workspace.              |
| `search_files`      | Finds literal text in workspace files; returns paths, lines, snippets.   |
| `write_file`        | **Opt-in.** Creates a **new** UTF-8 text file in the workspace. Never overwrites. |
| `run_command`       | **Opt-in.** Runs a fixed operator-allowlisted test, lint, type-check, or Git check profile. |

By default the server is **read-only**: neither `write_file` nor `run_command` is
registered. Writing remains separately opt-in through
`UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true`. Command execution requires both
`UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS=true` and an explicit profile allowlist.
Callers cannot provide command strings or arguments. Commands run in the authorized
workspace, receive only a minimal environment, have a maximum 120-second timeout and
64 KiB output cap per stream. Running project tests can still modify files as a normal
effect of the test suite; only enable profiles you trust. The workspace sets the process
working directory but does not sandbox OS access: tests can reach anything the current
Windows user can access.

The built-in profiles are `pytest`, `ruff-check`, `mypy-check`, `git-status`, and
`git-diff-check`. Arbitrary shell execution is not exposed.

Example `get_platform_info` result:

```json
{ "name": "universal-ai-tools", "version": "0.1.0", "status": "operational" }
```

### Filesystem tools

Every `path` argument is **relative to the workspace root** (`UNIVERSAL_AI_TOOLS_WORKSPACE`).
`/` and `\` are both accepted as separators, and `.` (or an empty string) means the root.
Returned paths are always workspace-relative with `/` separators; absolute paths are never
returned.

#### `list_files`

| Argument      | Type    | Default | Notes                                                             |
| ------------- | ------- | ------- | ----------------------------------------------------------------- |
| `path`        | string  | `"."`   | Directory to list.                                                |
| `recursive`   | boolean | `false` | Depth-first listing of all descendants.                           |
| `max_entries` | integer | `200`   | 1 to 1000.                                                        |

Returns `{path, entries: [{path, type, size_bytes}], truncated, skipped_directories}`.
`type` is `file`, `directory`, `symlink` (also used for Windows junctions), or `other`.
Entries are sorted by name. Symlinks and junctions are listed but never followed. In a
recursive listing, [excluded directories](#excluded-directories) appear as entries but are
not descended into. Excluded and unreadable directories are counted in `skipped_directories`.
`truncated` is `true` when more entries existed than `max_entries`.

#### `read_file`

| Argument | Type   | Notes            |
| -------- | ------ | ---------------- |
| `path`   | string | Required. A file. |

Returns `{path, size_bytes, content}`. Refused with an error when the path is:

- a directory, a device, a FIFO, or anything other than a regular file
- a symlink or junction, or reached through one
- larger than **1 MiB** (1,048,576 bytes)
- binary (contains a NUL byte) or not valid UTF-8; the error gives the byte offset
- a [sensitive file](#sensitive-files) or inside a sensitive directory

Error messages never include file contents or absolute paths.

#### `search_files`

| Argument         | Type    | Default | Notes                                                   |
| ---------------- | ------- | ------- | ------------------------------------------------------- |
| `pattern`        | string  | —       | Required. 1 to 200 characters, one line. Literal text, not a regex. |
| `path`           | string  | `"."`   | Directory to search under.                              |
| `case_sensitive` | boolean | `true`  |                                                         |
| `max_results`    | integer | `100`   | 1 to 500.                                               |

Returns `{pattern, path, matches: [{path, line, column, snippet}], files_scanned, truncated,
truncation_reason, skipped}`. Line and column numbers are 1-based. Lines split on `\n`, and a
trailing `\r` is ignored. One match is reported per line, at its first occurrence. A snippet
is the matching line trimmed, or a 200-character window around the match (marked with `…`)
for long lines.

The search walks the tree depth-first in name order and skips:

- [excluded directories](#excluded-directories) and sensitive directories
- symlinks and junctions (never followed)
- [sensitive files](#sensitive-files)
- files over **1 MiB**, binary files, and files that are not valid UTF-8
- files and directories it cannot read

`skipped` reports a count for each reason. The search stops early at whichever limit comes
first, and sets `truncated: true` and `truncation_reason`:

| Limit                      | Value          | `truncation_reason`  |
| -------------------------- | -------------- | -------------------- |
| matches                    | `max_results`  | `max_results`        |
| files read                 | 5,000          | `max_files_scanned`  |
| wall-clock time            | 5 seconds      | `time_limit`         |

#### `write_file`

Registered only when [write tools are enabled](#enabling-write-tools).

| Argument  | Type   | Notes                                                                 |
| --------- | ------ | --------------------------------------------------------------------- |
| `path`    | string | Required. The new file. Its parent directory must already exist.      |
| `content` | string | Required. The complete text. May be empty.                            |

Returns `{path, bytes_written}`, where `bytes_written` is the length of the content in
bytes once encoded as UTF-8 (not its number of characters).

The file is **created only if nothing exists at the path**. If a file, directory, symlink,
junction, or anything else is already there, or appears while the call is running, the
call fails and leaves it untouched. There is no overwrite, append, or replace mode.

The content is written as UTF-8 exactly as given. No BOM is added (a leading U+FEFF that
you supply is kept) and newlines are not translated, so `\n` and `\r\n` stay as they are.
Refused with an error, before anything is created:

- content containing a NUL character, or that is not valid Unicode (unpaired surrogates)
- content over **1 MiB** (1,048,576 bytes) once encoded as UTF-8; exactly 1 MiB is allowed
- a path that names no file (`.`, or ending in `/` or `\`), or whose parent directory does
  not exist or is not a directory. Directories are never created.
- a symlink or junction anywhere along the path
- a [sensitive file](#sensitive-files) name, or a path inside a sensitive directory
- a path inside an [excluded directory](#excluded-directories) such as `.git`, `.venv`, or
  `node_modules`, or a file *named* like one (a `.git` file can redirect Git elsewhere)
- everything listed under [Path rules](#path-rules)

If writing fails partway (for example, the disk is full), the new file is removed again and
the error says so. The tool never reports success unless every byte was written and flushed
to disk. Error messages never include file contents or absolute paths.

#### Excluded directories

`list_files` (when recursive) and `search_files` do not descend into directories with these
names, at any depth below the starting path (matched case-insensitively): `.git`, `.hg`,
`.svn`, `.venv`, `venv`, `node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`,
`.ruff_cache`, `.tox`, `.nox`, `.cache`, `.idea`, `*.egg-info`, plus the sensitive directories
below. You can still list or search an excluded directory, other than a sensitive one, by
passing it explicitly as `path`. `write_file` refuses to create anything inside an excluded
directory, at any depth, or to create a file with one of these names.

#### Sensitive files

These are refused by every tool, at any depth, matched case-insensitively:

- **Directories:** `.ssh`, `.gnupg`, `.aws`. No tool can enter them.
- **Files:** `.env` and `.env.*` (except `.env.example`, `.env.sample`, and
  `.env.template`), `.netrc`, `.pgpass`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, and
  `*.pem`, `*.key`, `*.p12`, `*.pfx`. `read_file` refuses them, `search_files` skips them,
  and `write_file` refuses to create them. `list_files` still shows their names, but not
  their contents.

This is a safety net, not a secrets scanner: secrets stored under other names are readable
like any other file in the workspace. Authorize only a directory you are willing to expose.

#### Path rules

Each path is checked **before** anything is read or created. These are refused:

- absolute paths, drive letters (`C:\`, `C:file`), UNC paths (`\\server\share`), and
  leading `/` or `\`
- any `..` component, even one that would stay inside the workspace
- NUL characters
- on Windows: reserved device names (`CON`, `NUL`, `COM1`, …), alternate data streams
  (`file:stream`), and names ending in a dot or space
- symlinks and junctions anywhere along the path, whether they point inside or outside
  the workspace

After a file is opened, the server checks that the open handle is the same regular file
it inspected, and that its real path (as reported by the OS) is inside the workspace. For
`write_file`, the parent directory's real path is checked before creating, and the new
file's location is verified again before any content is written. See
[docs/architecture.md](docs/architecture.md#known-limitations) for what this does and does
not guarantee.

## Requirements

- Python 3.13+
- An MCP client for interactive use, for example [Claude Code](https://docs.claude.com/en/docs/claude-code)

## Installation

From the repository root:

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# macOS / Linux:         source .venv/bin/activate
python -m pip install -e ".[dev]"
```

This installs the package in editable mode with its runtime dependencies (`mcp`, `pydantic`)
and the development tools (`pytest`, `ruff`, `mypy`). It also installs a `universal-ai-tools`
console script.

## Configuration

Configuration is read **only** from the environment variables below. Other environment
variables are never read or exposed. `.env` files are **not** loaded automatically;
[.env.example](.env.example) documents the variables.

| Variable                         | Required | Description                                                                                       |
| -------------------------------- | -------- | ------------------------------------------------------------------------------------------------- |
| `UNIVERSAL_AI_TOOLS_WORKSPACE`   | yes      | Absolute path of the single directory the server is authorized to work in. Must exist and be a directory. |
| `UNIVERSAL_AI_TOOLS_LOG_LEVEL`   | no       | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, or `CRITICAL`. Case-insensitive.                    |
| `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS` | no | `false` (default) or `true`. Registers `write_file` when `true`. See [Enabling write tools](#enabling-write-tools). |
| `UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS` | no | `false` (default) or `true`. Enables the command tool only with a non-empty allowlist. |
| `UNIVERSAL_AI_TOOLS_COMMAND_ALLOWLIST` | no | Comma-separated fixed profile names: `pytest`, `ruff-check`, `mypy-check`, `git-status`, `git-diff-check`. |

The workspace is validated at startup. These values are rejected:

- unset, empty, or relative paths
- paths that do not exist, or that are not directories
- your home directory, or any parent of it (e.g. `C:\Users`, `/home`)
- a filesystem or drive root (e.g. `C:\`, `/`)

Symlinks are resolved before the checks. On invalid configuration the server prints a
clear message to stderr and exits with status `2` before it speaks any MCP.

The filesystem tools work only inside this directory. It is fixed for the life of the
process and never widened. To point the server somewhere else, change the variable and
restart it.

### Enabling write tools

Write tools are **off by default**. Without the setting below, the server registers only
`get_platform_info`, `list_files`, `read_file`, and `search_files`, and its MCP
instructions tell the client that file writing is disabled.

To allow the connected AI client to create new files in the workspace, opt in explicitly:

```
UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true
```

- Only `true` and `false` are accepted, in any letter case, with surrounding whitespace
  ignored. Unset, empty, or whitespace-only means `false`.
- Any other value (`1`, `yes`, `on`, `enabled`, a typo, ...) is a configuration error: the
  server prints a message naming the variable to stderr and exits with status `2`. A typo
  can therefore never enable writing silently, and it never falls back to disabled
  silently either.
- When enabled, `write_file` is registered and the instructions describe it. Its behavior
  is exactly as documented [above](#write_file): create-only, confined to the workspace,
  with the same limits and refusals.
- The setting is read once at startup and logged to stderr. To change it, restart the
  server.

Authorize write tools only for a workspace you are willing to let the AI client add files
to, and review what it creates before running it.

### Enabling command profiles

Command execution is also **off by default**. To expose only pytest and Git status:

```
UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS=true
UNIVERSAL_AI_TOOLS_COMMAND_ALLOWLIST=pytest,git-status
```

The allowlist accepts only the five documented profile names. The command tool has no
free-form arguments, always runs from the configured workspace, and runs without inherited
API keys or other parent environment values. Each output stream is capped at 64 KiB and
the timeout can be set from 1 to 120 seconds. A test profile executes code from the project
and may change files as a normal test side effect; enable only profiles you trust. The
configured workspace is a working directory, not an OS-level sandbox.

The resource limits (1 MiB per file, 5,000 files, 5 seconds, and so on) are fixed defaults.
They are not read from the environment. Code that embeds the server can pass a different
`FilesystemLimits` to `create_server`.

## Launching the server

The server speaks MCP over **stdio**. It is meant to be launched by an MCP client, not
used interactively. stdout carries only protocol messages; all logs go to stderr.

```bash
# PowerShell
$env:UNIVERSAL_AI_TOOLS_WORKSPACE = "C:\path\to\your\project"
universal-ai-tools            # or: python -m universal_ai_tools.server

# bash / zsh
UNIVERSAL_AI_TOOLS_WORKSPACE=/path/to/your/project universal-ai-tools
```

It then waits for an MCP client on stdin. Stop it with `Ctrl+C`, or by closing stdin.

### Connecting to Claude Code

Register the server with Claude Code. Use the absolute path to the virtualenv's Python so
it works regardless of which directory Claude Code is started in:

```powershell
# Windows (PowerShell), run from the repository root
claude mcp add universal-ai-tools --scope local `
  -e UNIVERSAL_AI_TOOLS_WORKSPACE="C:\path\to\your\project" `
  -- "$PWD\.venv\Scripts\python.exe" -m universal_ai_tools.server
```

```bash
# macOS / Linux, run from the repository root
claude mcp add universal-ai-tools --scope local \
  -e UNIVERSAL_AI_TOOLS_WORKSPACE=/path/to/your/project \
  -- "$PWD/.venv/bin/python" -m universal_ai_tools.server
```

This registers the server read-only. To also allow it to create new files, add
`-e UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true` to the command (see
[Enabling write tools](#enabling-write-tools)).

Then verify:

```bash
claude mcp list                    # should show universal-ai-tools ... ✔ Connected
claude mcp get universal-ai-tools
```

Inside a Claude Code session, run `/mcp` to see the server and its tools (four by default,
five with write tools enabled), or ask:
*"Call the get_platform_info tool."* To remove it again, run
`claude mcp remove universal-ai-tools --scope local`.

`--scope local` keeps the registration private to you and this project. Claude Code stores
it in your user config (`~/.claude.json`) under this project's entry. Nothing is written to
the repository, and other projects are unaffected.

## Development

```bash
python -m pytest                  # tests (includes a real stdio subprocess round-trip)
python -m ruff check .            # lint
python -m ruff format --check .   # formatting (drop --check to apply)
python -m mypy                    # strict type check of src/ and tests/
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the same commands on
Ubuntu and Windows with Python 3.13, on every push and pull request.

## Project layout

```
src/universal_ai_tools/
  __init__.py   package metadata (name, version); no side effects on import
  config.py     validated configuration and workspace authorization
  workspace.py  path confinement: the security boundary for filesystem access
  filesystem.py list_files, read_file, search_files, write_file logic and limits
  server.py     get_platform_info, MCP server assembly, stdio entry point
tests/          pytest suite (synthetic files in temporary directories only)
docs/           architecture notes
```

See [docs/architecture.md](docs/architecture.md) for how the layers fit together.

## Not implemented yet

These are planned for later milestones and are **not** present in this version:

- Any tool that modifies, overwrites, appends to, renames, moves, or deletes files, or
  creates directories (`write_file` only creates new files)
- Arbitrary shell or caller-defined commands
- Git commit and other repository-modifying operations
- Autonomous or multi-step workflows
- A general policy engine and audit log. Capabilities currently use separate startup
  switches, command profiles are fixed, and the file and command limits are fixed defaults.
- Non-stdio transports (HTTP/SSE) and authentication
- Adapters for MCP clients other than Claude Code (the server is client-agnostic, but only
  Claude Code has been tested)

## License

[MIT](LICENSE)
