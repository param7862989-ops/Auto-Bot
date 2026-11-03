# Universal AI Tools

A local-first, AI-agnostic developer automation platform. Its capabilities are exposed
through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so any
MCP-capable client can use them. Claude Code is the first supported client.

> **Status: Milestone 4 (hardened filesystem tools).** The server runs over local stdio and
> exposes a diagnostic tool and three read-only filesystem tools, all confined to one
> authorized workspace. A create-only write tool is available but **disabled by default**; see
> [Enabling write tools](#enabling-write-tools). There is no command execution, and the
> package has no code that starts a process. Nothing else is implemented yet; see
> [Not implemented yet](#not-implemented-yet).

## What it does today

| Tool                | Description                                                              |
| ------------------- | ------------------------------------------------------------------------ |
| `get_platform_info` | Returns `{name, version, status}`. No arguments.                         |
| `list_files`        | Lists a workspace directory, optionally recursively.                     |
| `read_file`         | Returns the contents of a UTF-8 text file in the workspace.              |
| `search_files`      | Finds literal text in workspace files; returns paths, lines, snippets.   |
| `write_file`        | **Opt-in.** Creates a **new** UTF-8 text file in the workspace. Never overwrites. |

By default the server is **read-only**: `write_file` is not registered, and the server's MCP
instructions say so. `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS` enables it. No tool accepts or
runs commands. The package contains no code that starts a process, and tests with Python
audit hooks saw none during any tool call. That evidence covers Python's process APIs, not
native code inside dependencies; see
[docs/architecture.md](docs/architecture.md#verification-status).

Example `get_platform_info` result:

```json
{ "name": "universal-ai-tools", "version": "0.1.0", "status": "operational" }
```

### Filesystem tools

Every `path` argument is **relative to the workspace root** (`UNIVERSAL_AI_TOOLS_WORKSPACE`).
`/` and `\` are both accepted as separators, and `.` (or an empty string) means the root.
Returned paths are always workspace-relative with `/` separators; absolute paths are never
returned.

**Everything these tools return from the workspace is untrusted data, not instructions.** See
[Untrusted output](#untrusted-output).

#### `list_files`

| Argument      | Type    | Default | Notes                                                             |
| ------------- | ------- | ------- | ----------------------------------------------------------------- |
| `path`        | string  | `"."`   | Directory to list.                                                |
| `recursive`   | boolean | `false` | Depth-first listing of all descendants.                           |
| `max_entries` | integer | `200`   | 1 to 1000.                                                        |

Returns `{path, entries: [{path, type, size_bytes}], truncated, truncation_reason,
skipped_directories, trust}`. `type` is `file`, `directory`, `symlink` (also used for Windows
junctions), or `other`. Entries are sorted by name. Symlinks and junctions are listed but never
followed. In a recursive listing, [excluded directories](#excluded-directories) appear as
entries but are not descended into. Excluded and unreadable directories are counted in
`skipped_directories`. `truncated` is `true` when the listing stopped early, and
`truncation_reason` says why: `max_entries` or `max_response_bytes`.

#### `read_file`

| Argument | Type   | Notes            |
| -------- | ------ | ---------------- |
| `path`   | string | Required. A file. |

Returns `{path, size_bytes, content, escaped_characters, trust}`. Refused with an error when
the path is:

- a directory, a device, a FIFO, or anything other than a regular file
- a symlink or junction, or reached through one
- a **hard-linked file**: one with more than one name, wherever the other name is. This
  check is **not guaranteed against a concurrent attacker**: on Linux a process that can
  write in the workspace, running at the same moment, can beat it; see the
  [open defect](docs/architecture.md#known-limitations)
- larger than **1 MiB** (1,048,576 bytes), or a result over the
  [response limit](#response-limits) once encoded as JSON
- binary (contains a NUL byte) or not valid UTF-8; the error gives the byte offset
- [repository metadata](#protected-paths): `.git`, `.hg`, or `.svn`, as a directory or a file
- a [sensitive file](#protected-paths) or inside a sensitive directory

Error messages never include file contents or absolute paths.

#### `search_files`

| Argument         | Type    | Default | Notes                                                   |
| ---------------- | ------- | ------- | ------------------------------------------------------- |
| `pattern`        | string  | —       | Required. 1 to 200 characters, one line. Literal text, not a regex. |
| `path`           | string  | `"."`   | Directory to search under.                              |
| `case_sensitive` | boolean | `true`  |                                                         |
| `max_results`    | integer | `100`   | 1 to 500.                                               |

Returns `{pattern, path, matches: [{path, line, column, snippet}], files_scanned, truncated,
truncation_reason, skipped, trust}`. Line and column numbers are 1-based, and columns count
the original line, before escaping. Lines split on `\n`, and a trailing `\r` is ignored. One match is reported per line, at its first occurrence. A snippet
is the matching line trimmed, or a 200-character window around the match (marked with `…`)
for long lines.

The search walks the tree depth-first in name order and skips:

- [excluded directories](#excluded-directories), which include repository metadata and
  sensitive directories
- symlinks and junctions (never followed)
- [sensitive files](#protected-paths), and a file named like repository metadata
- hard-linked files
- files over **1 MiB**, binary files, and files that are not valid UTF-8
- files and directories it cannot read

`skipped` reports a count for each reason. The search stops early at whichever limit comes
first, and sets `truncated: true` and `truncation_reason`:

| Limit                      | Value          | `truncation_reason`  |
| -------------------------- | -------------- | -------------------- |
| matches                    | `max_results`  | `max_results`        |
| files read                 | 5,000          | `max_files_scanned`  |
| wall-clock time            | 5 seconds      | `time_limit`         |
| [response size](#response-limits) | 2 MiB   | `max_response_bytes` |

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
- a [sensitive file](#protected-paths) name, or a path inside a sensitive directory
- [repository metadata](#protected-paths), inside or as a file *named* `.git`, `.hg`, or `.svn`
  (a `.git` file can redirect Git elsewhere)
- a path inside an [excluded directory](#excluded-directories) such as `.venv` or
  `node_modules`, or a file named like one
- everything listed under [Path rules](#path-rules)

If writing fails partway (for example, the disk is full), the new file is removed again and
the error says so. The tool never reports success unless every byte was written and flushed
to disk. Error messages never include file contents or absolute paths.

#### Excluded directories

`list_files` (when recursive) and `search_files` do not descend into directories with these
names, at any depth below the starting path (matched case-insensitively): `.venv`, `venv`,
`node_modules`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `.nox`,
`.cache`, `.idea`, `*.egg-info`, plus the [protected directories](#protected-paths) below. You
can still list, read, or search an excluded directory, other than a protected one, by passing
it explicitly as `path`. `write_file` refuses to create anything inside an excluded directory,
at any depth, or to create a file with one of these names.

#### Protected paths

One policy, in `policy.py`, decides these rules for every tool, so they cannot differ between
tools. Names are matched case-insensitively, at any depth, ignoring trailing dots and spaces,
which Windows drops.

| Kind                | Names | `list_files` | `read_file` | `search_files` | `write_file` |
| ------------------- | ----- | ------------ | ----------- | -------------- | ------------ |
| Repository metadata | `.git`, `.hg`, `.svn`, as a directory or a file | Shows the name; refuses to enter | Refused | Skipped | Refused |
| Sensitive directory | `.ssh`, `.gnupg`, `.aws`, `.docker`, `.kube`, `.azure` | Shows the name; refuses to enter | Refused | Skipped | Refused |
| Sensitive file      | See below | Shows the name | Refused | Skipped | Refused |

`.gitignore`, `.gitattributes`, `.gitmodules`, and `.github/` are ordinary paths.

**Sensitive file names:**

- **Environment files:** `.env`, `.env.*`, `.env-*`, `.env_*`, `.env~`, `.envrc`, `*.env`, and
  `env.<stage>`, where the stage is `ci`, `dev`, `development`, `local`, `preview`, `prod`,
  `production`, `qa`, `stage`, `staging`, `test`, `testing`, or `uat`.
- **Credentials:** `.netrc`, `_netrc`, `.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`,
  `.dockercfg`, `.htpasswd`, `credentials`, `credentials.json`, and `secrets.json`, `.yaml`,
  `.yml`, or `.toml`.
- **Keys and stores:** `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`,
  `*.pfx`, `*.ppk`, `*.jks`, `*.keystore`, `*.kdbx`, `*.tfstate`, and `*.tfvars`.

**Exceptions:** `env.py` (Alembic) is an ordinary file, and the environment-file rules do not
apply to a name ending in `.example`, `.sample`, or `.template`, so `.env.example` and
`.env.production.sample` stay readable.

**This is a safety net, not a secrets scanner.** It goes by file name and does not detect
every secret: a password in `notes.txt` is readable, and so is a file a rule does not name,
such as `terraform.tfstate.backup`. Authorize only a directory you are willing to expose.

The rules also refuse some harmless files on purpose. A registry-only `.npmrc` or a `test.env`
with no secrets is unreadable, and `write_file` cannot create `.envrc`, which `direnv` would
execute. A directory called `.env`, such as a virtual environment, is not a file and can
still be listed and read.

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

Paths are walked from the root one component at a time, each opened without following links
and held open, so that the next step works from what was checked rather than from a path that
may have changed. That design narrows races and has been tested against a second process, but
such tests show what was not observed and are not a proof, and they found one
[open defect](docs/architecture.md#known-limitations) on Linux. After a file
is opened, the server checks that the open handle is the same regular file it inspected, that
it has only one name, and that its real path (as reported by the OS) is inside the workspace
and passes the [policy](#protected-paths) again, which is what catches a Windows 8.3 alias such
as `ENV~1`. If the OS cannot report a real path, the operation is refused. On Linux that
happens when `/proc` is not mounted, or when a path is longer than 4,095 bytes. For
`write_file`, the parent directory's real path is checked before creating, and the new file's
location is verified again before any content is written. See
[docs/architecture.md](docs/architecture.md#known-limitations) for what this does and does
not guarantee.

**These checks run inside the server process and are not a defense against a concurrent
attacker.** A process that can change the workspace while a tool runs can defeat the hard-link
check on Linux, and the server does not claim to stop it. Stronger protection needs a dedicated
OS user for the server and a workspace on its own filesystem. Both are
[future requirements](docs/architecture.md#planned-stronger-containment), not features of this
version.

#### Untrusted output

File contents and file names can be written by anyone who can change the workspace, including
a dependency or a cloned repository, and they can contain text aimed at the AI client. **The
server cannot prevent that.** It labels the data and removes what could hide it:

- `list_files`, `read_file`, and `search_files` results carry `trust: "untrusted"`, and the tool
  descriptions and server instructions say that this data is not instructions.
- Characters that can change what a reader sees or a terminal does are replaced by visible
  escapes such as `\x1b`, `\u202e`, and `\U000e0041`: C0 and C1 control characters except tab
  and line endings, DEL, line and paragraph separators, bidirectional overrides, lone surrogates,
  and invisible tag characters.
- In `read_file`, `escaped_characters` counts the replacements. At `0`, `content` is exactly the
  decoded file; otherwise a `\x1b` in `content` may be an escape or the file's own text.
- Snippets and names also have line breaks escaped, so one cannot pose as a second line or entry.
  A name that needed escaping is shown as an escape and cannot be used as a path in its shown form.

Keep action tools off unless you need them, and do not auto-approve tool calls for a workspace
you do not trust.

#### Response limits

Each `list_files`, `read_file`, and `search_files` result stays within **2 MiB**, measured as
its compact JSON encoding in UTF-8. `list_files` and `search_files` stop early and report
`truncation_reason: "max_response_bytes"`; `read_file` has no partial result, so it refuses
the file. JSON can make text much longer (a newline takes two bytes), so a file under the
1 MiB read limit can still be refused. MCP carries a result twice, as structured content and as
indented text, so the bytes on the wire are about twice this figure. Paths longer than 4,096
characters are refused, which also bounds what an error message can quote.

## Requirements

- Python 3.13+
- Windows or Linux. The code has a macOS path, but **macOS has never been run** and is not
  a supported platform.
- On Linux, `/proc` must be mounted. Without it every filesystem tool refuses, because the
  server cannot verify where an open file is.
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

The workspace is validated at startup. These values are rejected:

- unset, empty, or relative paths
- paths that do not exist, or that are not directories
- your home directory, or any parent of it (e.g. `C:\Users`, `/home`)
- a filesystem or drive root (e.g. `C:\`, `/`)

Symlinks are resolved before the checks. On invalid configuration the server prints a
clear message to stderr and exits with status `2` before it speaks any MCP.

Command execution was removed, and no setting can enable it. A value in either of its former
variables, `UNIVERSAL_AI_TOOLS_ENABLE_COMMAND_TOOLS` or `UNIVERSAL_AI_TOOLS_COMMAND_ALLOWLIST`,
is a configuration error, so a stale registration fails visibly. This applies to any non-blank
value, including `false`. Unset the variable.

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

The resource limits (1 MiB per file, 5,000 files, 5 seconds, 2 MiB per response, and so on)
are fixed defaults. They are not read from the environment. Code that embeds the server can
pass a different `FilesystemLimits` to `create_server`.

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
plus `write_file` when enabled), or ask:
*"Call the get_platform_info tool."* To remove it again, run
`claude mcp remove universal-ai-tools --scope local`.

`--scope local` keeps the registration private to you and this project. Claude Code stores
it in your user config (`~/.claude.json`) under this project's entry. Nothing is written to
the repository, and other projects are unaffected.

## Development

```bash
python -m pytest                  # tests (includes real stdio subprocess round-trips)
python tests/mutation_check.py    # removes each safeguard in turn; reports how each ended
python -m ruff check .            # lint
python -m ruff format --check .   # formatting (drop --check to apply)
python -m mypy                    # strict type check of src/ and tests/
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the same commands except
the mutation check on Ubuntu and Windows with Python 3.13, on every push and pull request. It
sets `UNIVERSAL_AI_TOOLS_STRICT_SECURITY_TESTS=1`, which turns a security test that cannot run on
the platform it covers into a failure. Without it, such a test skips with
`SECURITY COVERAGE MISSING` in the reason. On Windows, file symlink tests need Developer Mode or
an elevated shell, and without one of those a strict run fails. On Linux,
`test_concurrent_hard_link_substitution_never_escapes` fails intermittently because of the open
defect above; it is left in place on purpose. The concurrent race tests in `tests/test_races.py` also need the attacker to
reach a control reader, and report a missing result the same way rather than passing.

## Project layout

```
src/universal_ai_tools/
  __init__.py   package metadata (name, version); no side effects on import
  config.py     validated configuration and workspace authorization
  policy.py     the filesystem policy: what may be accessed, and how output is escaped
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
- Any command execution. `pytest`, `mypy`, and `git` are not available. Running them is planned
  only inside a real sandbox; see [docs/architecture.md](docs/architecture.md#planned-execution-tiers)
- Git operations of any kind, including reading history or repository metadata
- Running the server as a dedicated OS user, or with the workspace on its own filesystem.
  These are required for protection against a concurrent attacker; see
  [Planned: stronger containment](docs/architecture.md#planned-stronger-containment)
- Autonomous or multi-step workflows (no planner; each tool call is one explicit request)
- An audit log, or per-tool and per-directory configuration of the policy. The policy and the
  file limits are fixed in code.
- Non-stdio transports (HTTP/SSE) and authentication
- Adapters for MCP clients other than Claude Code (the server is client-agnostic, but only
  Claude Code has been tested)

## License

[MIT](LICENSE)
