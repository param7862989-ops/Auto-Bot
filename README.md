# Universal AI Tools

Universal AI Tools gives **Claude Desktop** and **Claude Code** secure, approved access to
your Windows PC through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP).
You type an ordinary task into Claude. Claude plans it and calls this server's tools one at
a time. The server checks each call against your permissions, asks you before anything
consequential happens, runs it, and returns the result to Claude, which decides the next
step and verifies the outcome.

```
You ──► Claude Desktop / Claude Code ──MCP (stdio)──► Universal AI Tools ──► your PC
          (reasoning, planning,                        (permissions, approval,
           choosing tools, verifying)                   audit log, execution)

        Claude Code's own tools ──► the guard (a hook) ──► your PC
          (Bash, PowerShell,          (refuses protected changes,
           Write, Edit, Read)          asks before every command, audit log)
```

In Claude Code, its own built-in tools do most of the work. They never reach the MCP server,
so Universal AI Tools also installs [the Claude Code guard](#the-claude-code-guard): a hook
that Claude Code runs before and after every tool call.

**No API keys.** Claude, through your existing Claude subscription, is the only AI involved.
This package never calls a model API, has no provider SDK dependency, and reads no
credentials. The earlier built-in API agent (`run_task`) was removed from the product and
archived unchanged in [legacy/api-agent](legacy/api-agent/README.md).

> **Status: Milestone 6 (Claude-native execution).** See
> [What is implemented and tested](#what-is-implemented-and-tested) for the exact state of
> each capability, and [Not implemented yet](#not-implemented-yet).

## Capabilities

Everything except reading files is **off until you turn it on**, and everything that
changes something needs **your approval**.

| Capability | Tools | Risk | Approval (default) |
| --- | --- | --- | --- |
| Always on | `get_platform_info`, `list_files`, `read_file`, `search_files` | read-only | none |
| `[filesystem] write` | `write_file` (new files), `edit_file`, `create_directory`, `move_path` | create / modify | every call |
| `[sandbox] enabled` | `run_in_sandbox`: a command in a throwaway, locked-down Docker container | execute | every call, always |
| `[terminal] enabled` | **Disabled for safety**: `true` is refused (see [Command execution](#command-execution-disabled)) | execute | - |
| `[screen] enabled` | `list_windows`, `capture_screen`, `inspect_window`, `read_text`, `read_table`, `wait_for` | observe | once per session |
| `[screen] enabled` (file mode) | `save_screenshot`: a full-size screenshot saved as a **new** `.png` in the workspace | create | once per session, approved separately from looking |
| `[input] enabled` | `focus_window`, `type_text`, `press_keys`; with the screen also `click`, `scroll`, `invoke_element`, `set_element_value` | input | every call |
| `[input] launchable` | `launch_app`: starts a listed program with no arguments | input | every call, always |
| `[input] grants` | `request_input_grant`: one approval covers input to named programs for a few minutes | input | every call (the request itself) |

There is no delete tool, and no way to run commands, a shell, Python, or PowerShell. The
screen and input tools are Windows-only.

## Setup (once, for both clients)

Requirements: Windows 10 or 11 (Linux works for the file tools), Python 3.13+,
and Claude Desktop and/or Claude Code signed in with your Claude account.

```powershell
# 1. Install, from the repository root
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .

# 1b. Install the protected copy that the clients and the guard run
#     (~\.universal-ai-tools\runtime; pip may download dependencies)
universal-ai-tools install

# 2. See every change setup would make, without making any
universal-ai-tools setup --workspace "$HOME\UniversalAITools-Scratch" --create-workspace `
    --enable filesystem-write --dry-run

# 3. Make them (each one is shown again and needs a yes)
universal-ai-tools setup --workspace "$HOME\UniversalAITools-Scratch" --create-workspace `
    --enable filesystem-write

# 4. Check everything, including a live MCP connection
universal-ai-tools doctor
```

Start with a **scratch workspace**: a new, empty folder that holds only files you are happy for
Claude to read and change. `--create-workspace` creates it; it is checked first (not a drive
root, not your home folder or a parent of it, not holding the config, the audit log, or
Claude's settings).

`setup` checks Python (3.13+) and its dependencies, then does the following, and **shows each
change and asks before making it**:

1. Writes `~\.universal-ai-tools\config.toml`, the one file both clients use. Everything is
   off but reading files and what `--enable` names; command execution cannot be enabled. Run
   again, it keeps every setting you did not name and copies the old file to
   `config.toml.bak`.
2. Initialises the audit log, `~\.universal-ai-tools\audit.jsonl`, with a `setup` record.
3. Adds a `universal-ai-tools` entry to Claude Desktop's `claude_desktop_config.json`. Every
   other setting in that file is kept, the old file is copied to
   `claude_desktop_config.json.bak-<date>` first, and a file that is not valid JSON is never
   overwritten. The Microsoft Store build keeps this file in
   `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`; the classic installer in
   `%APPDATA%\Claude\`. `setup` finds the right one.
4. Registers the server with Claude Code for all projects, through Claude Code's own CLI:
   `claude mcp add-json --scope user universal-ai-tools ...`, after copying `~\.claude.json`
   to `.claude.json.bak-<date>`. If a project has an older project-only (`local`)
   registration of the same name, `setup` says so and why it is weaker (no `-I`, environment
   mode), because that one takes precedence there, and offers to remove it
   (`claude mcp remove --scope local universal-ai-tools`, run in that project).
5. Installs [the Claude Code guard](#the-claude-code-guard): three hook entries in
   `~\.claude\settings.json` (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, every tool).
   Every other setting and hook in that file is kept, and the old file is copied to
   `settings.json.bak-<date>` first. Needs `universal-ai-tools install` first.

Both clients start the server as `python -I -m universal_ai_tools.server`, with only
`UNIVERSAL_AI_TOOLS_CONFIG` in its environment; once `install` has run, that is the runtime's
Python, not the checkout's. `setup` never auto-approves anything: the guard only ever refuses
or asks.

Useful flags: `--enable filesystem-write,screen,input`, `--clients desktop` or
`--clients code`, `--dry-run` (show everything, change nothing at all), `--yes` (answer yes
to every question; use it only after a dry run).

Then **restart Claude Desktop completely** (tray icon ▸ Quit, then start it). In Claude Code,
start a new session, or run `/mcp` and reconnect.

### Turning capabilities on and off

```powershell
universal-ai-tools config enable filesystem-write
universal-ai-tools config enable screen
universal-ai-tools config enable input
universal-ai-tools config disable input
universal-ai-tools config show
```

Each change shows a diff and keeps the previous file as `config.toml.bak`. Restart Claude
Desktop and reconnect in Claude Code afterwards: the server reads the file at startup.

### What it looks like

In Claude Desktop or Claude Code, just ask, for example:

- *"Find every TODO in my notes and list them with their file."* (read-only: runs at once)
- *"Create notes/todo-summary.txt with those TODOs."* (you get an approval prompt first)
- *"Take a screenshot and tell me which windows are open."* (needs `screen`)
- *"In Notepad, type a shopping list and save it as list.txt."* (needs `screen` and `input`)

Claude decides which tools to call. The server never plans anything itself.

## Approval

Any tool that is not read-only runs only after a human approves it. The approval prompt
shows exactly what will happen: the path and new content, the full command line with its
program path, working directory, and time limit, the window and the text to type, and so on.
Only an explicit approval lets the call run. Deny, dismiss, cancel, an unclear answer, no
answer within `[approval] timeout_seconds` (120 by default), and no way to ask all mean the
call does not run, and Claude is told why.

**The prompt is complete and exact.** Nothing in it is ever shortened: every path, the whole
file content, both texts of an edit, every command argument, and all text to be typed are
shown in full. Values are shown so that two different requests can never look the same:

- a single value is quoted; if it contains anything that cannot be seen or that changes how
  text displays (line breaks, tabs, control and bidirectional characters, zero-width and
  unusual spaces), it is marked `escaped` and those characters appear as `\n`, `\t`, `\x1b`,
  `\u202e`, ..., with each backslash shown as `\\`;
- multi-line text is shown line by line behind a `│` gutter (`┆` where a long line wraps),
  so file content cannot imitate the prompt's own words; a header gives the number of lines
  and characters and whether it ends with a line break, and trailing spaces are escaped;
- command arguments are numbered, one per line, so one argument containing a line break never
  looks like two.

A request too large for the dialog to show in full is **refused, not cut**: the call does not
run, Claude is told why (split the change into smaller steps), and the audit log records
`too_large`. The Windows dialog's size is estimated conservatively from the screen's work area
and message font (between 40 and 90 characters wide, 12 to 60 lines). The client's dialog is
allowed 200 lines of 100 characters (20,000 characters); MCP gives a server no way to learn
how much a client really displays, and that Claude Code or Claude Desktop shows a prompt of
that size without cutting it has **not been verified**.

What runs is exactly what was shown: the arguments are validated once, reduced to a canonical
form whose SHA-256 digest is recorded in the audit log, and both the prompt and the call are
built from that form, never from the caller's objects. A 2026-07-28 answer is bound to that
digest by a single-use nonce, so it cannot approve any other call.

Where the question is asked is set by `[approval] channel`:

| `channel` | Behavior |
| --- | --- |
| `auto` (default) | The client's own dialog when the client supports MCP elicitation, otherwise a Windows dialog on this PC. |
| `client` | Only the client's dialog. If it cannot show one, the call is refused. |
| `desktop_dialog` | Only the Windows dialog ("Universal AI Tools - approval required", No is the default button). The same in every client. |

`approval = "once_per_session"` (allowed for `[filesystem]`, `[screen]`, and `[input]`; never
applied to a tool that executes code) means one approval allows that capability for at most
`[approval] session_grant_minutes` (30 by default) in that client's server process; the
prompt says so. There is no setting that skips approval.

**Client-specific behavior** (observed with Claude Code 2.1.319; see
[Verification](#what-is-implemented-and-tested)):

- **Claude Code** negotiates MCP 2026-07-28 and declares elicitation, so in an interactive
  session the question appears in Claude Code. In non-interactive use (`claude -p`) no dialog
  can be shown: Claude Code cancels the question and the call is refused. Use
  `channel = "desktop_dialog"` if you want approvals for `claude -p` runs on this PC.
- **Claude Desktop**: if it declares MCP elicitation, the question appears in Claude Desktop;
  otherwise (`auto`) the Windows dialog appears. This has not been verified against Claude
  Desktop yet; `doctor` and the audit log (`client_elicitation`) show what it declared.
- **Claude Code's own tools do not go through this server.** Its built-in Bash, PowerShell,
  Write, Edit, and Read tools are checked by [the Claude Code guard](#the-claude-code-guard)
  instead, if it is installed; without it, only Claude Code's own permissions apply to them.
- Both clients also have their own "allow this tool?" prompts. Those are the client's, and
  are separate from, and in addition to, this server's approval.

## The Claude Code guard

Claude Code's built-in tools act on your PC directly, not through the MCP server. The guard is
a [Claude Code hook](https://code.claude.com/docs/en/hooks): Claude Code runs
`python -I -m universal_ai_tools.guard --config <config file>` (the runtime's Python, with no
shell in between) before every tool call, and again after it. Its code runs outside the model,
so Claude cannot talk it out of a decision. It answers in one of three ways:

| Answer | When |
| --- | --- |
| **Refuse** (the call does not run, whatever Claude Code's rules or mode say) | A change (Write, Edit, MultiEdit, NotebookEdit) to: Universal AI Tools' own code, its runtime and the Python running it, its config, state directory, and audit log; Claude's settings (`~\.claude`, `~\.claude.json`, Claude Desktop's folder), except Claude Code's memory and plans; a project's `.claude\settings.json`, `.claude\settings.local.json`, or `.claude\hooks` (a project's settings can switch the guard off); `.git`, `.hg`, `.svn` (hooks run programs); `~\.bashrc`, `~\.profile` and the other shell startup files, `~\.gitconfig`; credential folders (`.ssh`, `.aws`, ...); Windows' Startup and PowerShell profile folders; a file with several hard links; a network or device path, or an alternate data stream. A read of Claude Code's login token (`~\.claude\.credentials.json`). |
| **Ask you** (Claude Code shows its permission prompt with the guard's reason, even in auto mode or when an allow rule matches) | **Every Bash and PowerShell command**, by default (`[guard] commands = "ask"`). A change to anything else in a project's `.claude` folder, to `.mcp.json`, to `.vscode\tasks.json`, or to a credential-looking file (`.env`, `*.pem`, ...). A read of a credential-looking file or folder. |
| **No answer** (Claude Code's own permissions decide, as without the guard) | Everything else: ordinary files in your projects, other tools. |

It never approves anything, so it only ever adds checks. It **fails closed**: if it cannot
read its configuration, only reads run; if it fails in any other way, or the audit record of a
change or command cannot be written, the call is refused. It is registered with
`"onFailure": "block"`, so a guard that cannot even start blocks the call. If that ever
locks you out, run `universal-ai-tools doctor` in your own terminal, or remove the guard with
`universal-ai-tools guard remove`.

Every tool call is recorded in the audit log (`guard_check` before a change or command, with
the decision; `guard_finished` after every call, with `succeeded`, `failed`, or
`interrupted`). Records hold the tool, the paths, the decision, and the command with
credential-looking values redacted; never file contents or edit text.

`[guard] commands = "claude"` leaves commands to Claude Code's own rules, except that a
command whose text names a protected location (`.claude`, `.gitconfig`, `$PROFILE`,
`schtasks`, ...) is still asked.

**What it does not do.**

- **It does not confine commands.** An approved command runs with your rights and can change
  anything, including everything in the table above. Read what you approve. Its check of a
  command's text is a warning for you, not a barrier, and is easy to evade.
- A project's own settings could still switch it off (`disableAllHooks`) if they are changed
  by a command you approve or by hand. Only a managed settings file with
  `allowManagedHooksOnly`, which needs an administrator, cannot be overridden by a project;
  this is not set up.
- It sees only the paths a tool names. What a program does after you approve it, and what
  other MCP servers do, is not checked (Universal AI Tools' own MCP tools have their own
  approvals).

**The protected runtime.** The guard refuses changes to the code it runs, so it must not run
from a checkout you develop in. `universal-ai-tools install` copies the package into a
virtual environment at `~\.universal-ai-tools\runtime` (inside the protected state
directory), and `setup` points the hook and both MCP registrations at that copy. A change to
the checkout takes effect only when you run `install` again; `doctor` says when the two
differ.

```powershell
universal-ai-tools install          # create or update the runtime (shows the commands first)
universal-ai-tools setup --clients code
universal-ai-tools guard test       # run the installed hook on three sample calls
universal-ai-tools guard remove     # take the guard out of Claude Code's settings
```

## The shared configuration

Both registrations contain only `UNIVERSAL_AI_TOOLS_CONFIG=<path of config.toml>`. MCP over
stdio means each client starts its own server process; they behave as one service because
they read the same file and append to the same audit log, and a machine-wide lock keeps
them from sending mouse or keyboard input at the same moment. Nothing about permissions can
be set per client.

The file is generated with a comment on every setting (`universal-ai-tools config show`).
Unknown keys and wrong types are errors, so a typo never silently changes behavior. The
server refuses to start if the config file or the audit log is inside the workspace, or if
the workspace contains Claude's own settings: otherwise the file tools could change their
own permissions.

**The server never changes its own code.** The file tools refuse to create, change, or move
anything in the server's trusted runtime, whatever the workspace is, including a checkout of
this repository. It is found from the running process at startup, not from file names: the
`universal_ai_tools` package directory, the location of every module already loaded, every
directory on Python's import path (with an editable install, that includes the checkout's
`src`), the per-user site directories, the virtual environment and base Python, and in file
mode the config file, its directory, the audit log, and Claude's settings. Every location is
compared by real path, so junctions, symlinks, other spellings, and 8.3 short names are
caught; moving a directory that contains a protected location is refused too. Ordinary
project files stay writable. Two consequences:

- To work on *this* repository through Universal AI Tools, run the server from a separate,
  non-editable install (`pip install .` into another virtual environment). With an editable
  install, the checkout's `src` is the running server's code and stays read-only.
- `setup` registers the server as `python -I -m universal_ai_tools.server`. Without `-I`,
  Python imports from the directory the client starts it in; if that is the workspace, a
  file such as `json.py` there would replace a module on the next start, so the server then
  refuses every change in the workspace and logs a warning. Re-run `setup` to fix an older
  registration.

The protected set follows the running server: a module imported later, a directory added to
the import path later, every directory a `.pth` file names (even before it exists), and the
source directories of setuptools editable installs are protected from the moment they
appear. So are the places Windows runs programs from by itself (the per-user and all-users
Startup folders) and the folders holding PowerShell profiles, because a file written there
would run a command later.

**The server refuses to start** if Python would import from the directory it was started in
(launched without `python -I` or `-P`), because a file there could already have replaced its
code. `setup` registers it with `-I`. An older registration without `-I` stops working until
`setup` is run again.

**Environment mode.** A registration that sets `UNIVERSAL_AI_TOOLS_WORKSPACE` (and optionally
`UNIVERSAL_AI_TOOLS_LOG_LEVEL`) instead of a config file is **read-only**: it has no
server-side approval and no audit log, so `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true` is
refused and none of the other capabilities exist. The trusted-runtime protection and the
start check above apply as in file mode. `setup` moves you to the config file. Setting both
the config file and these variables is an error.

## The audit log

Every call in file mode, read-only ones included, is appended to
`~\.universal-ai-tools\audit.jsonl`, one JSON object per line, by both clients' server
processes. A record holds:

| Field | Meaning |
| --- | --- |
| `ts`, `pid` | When (UTC, milliseconds) and which server process |
| `session`, `call`, `request` | The server session, this call, and the client's MCP request id |
| `client`, `protocol`, `client_elicitation` | Client name and version, MCP version, whether it can show approval dialogs |
| `tool`, `operation`, `risk` | The tool, its kind of operation (`read`, `search`, `list`, `create_file`, `edit`, `move`, `execute`, ...), and its risk |
| `target` | What it touched: the path (and for a search the pattern's *length*, never the pattern), the program, arguments and working directory, the window |
| `arguments_sha256` | Digest of the canonical arguments: the call that was approved is the call that ran |
| `permission`, `policy` | `allowed` (read-only) or `approval_required`, and `every_call` / `once_per_session` |
| `approval` | Decision (`approved`, `denied`, `cancelled`, `timed_out`, `invalid`, `unavailable`, `too_large`) and channel |
| `event`, `status` | `started`/`running`, `finished`/`succeeded`, `failed`, `cancelled`, `refused`, `approval_requested`/`waiting` |
| `error`, `error_class` | A short safe message, and a fixed class: `not_found`, `policy_refused`, `protected_path`, `outside_workspace`, `link_refused`, `hard_link_refused`, `invalid_path`, `too_large`, `not_approved`, `invalid_arguments`, `unknown_tool`, ... |

A consequential action is recorded durably *before* it runs; if that record cannot be written,
the action is refused. Calls refused before they could run (unknown tool, invalid arguments,
not approved) are recorded as `refused`. File contents, search patterns, typed text,
screenshots, and command output are never recorded, and command arguments that look like
credentials (`--token=...`, `--password <value>`, `API_KEY=...`, the password in
`https://user:pass@host`) are replaced by `<redacted>`; that rule goes by name, so a secret
passed any other way is recorded as given. Show the latest records with
`universal-ai-tools audit`. The log rotates to `audit.jsonl.1` at 10 MiB. Environment mode has
no audit log.

It is a record for you, not a tamper-proof one: anything running as your user account,
including a command you approved, could change it.

## Commands in a sandbox (Docker)

Commands never run on the PC itself. `run_in_sandbox` runs one in a **new Docker container
that is deleted afterwards**, so Claude can run your tests and builds without a command
being able to reach the rest of your PC:

| Limit | Setting |
| --- | --- |
| Files | Only the workspace, at `/work`; **read-only** unless `[sandbox] writable = true` |
| Network | **None** unless `[sandbox] network = true` |
| Rights | An unprivileged user, no Linux capabilities, no privilege escalation, a read-only system with a 512 MB `/tmp` |
| Resources | `cpus` (2), `memory_mb` (2048, no extra swap), 256 processes; killed after the time limit |
| Image | Only `[sandbox] image` (`python:3.13-slim`), already on the PC: nothing is downloaded during a run |
| The `docker` program | Trusted by fingerprint: `sandbox trust` records its SHA-256, and a run refuses if it changed |

Every command needs your approval, showing the exact command, as every code-executing call
does; grants never cover it. Output is returned with control characters escaped, as
untrusted data. The audit log records the command with credential-looking values redacted.

```powershell
universal-ai-tools sandbox trust       # trust docker.exe by its fingerprint (after updates too)
universal-ai-tools sandbox pull        # download the image once (uses the network)
universal-ai-tools sandbox status      # check Docker, the fingerprint, and the image
universal-ai-tools config enable sandbox
```

Docker's isolation is strong but not perfect: a vulnerability in the Linux kernel or Docker
could let a command escape its container. With `writable = true`, a command can change or
delete anything in the workspace.

## Command execution (disabled)

General command execution (`run_command`) is **disabled for safety**, and nothing replaces it
yet. A started program runs with your own rights: Windows offers this server no way to stop
it from changing the server's own code, its configuration, or its audit log, so no approval
could make it safe. An allowlist of program names does not help, because `python`, `pytest`,
`npm`, and most developer tools run whatever code the project or their arguments contain.

- `[terminal] enabled = true` in the config file is an error, `setup --enable terminal` and
  `config enable terminal` are refused, and `config add-program` no longer exists. An older
  config with the terminal on stops loading; `universal-ai-tools config disable terminal`
  fixes it.
- The server never registers `run_command`, whatever its configuration says, and never
  imports the code that starts processes. A tool that executes code would in any case need
  a fresh approval for every call: the gate ignores `once_per_session` for it.
- The file tools cannot write into Startup folders or PowerShell profile folders, so they
  cannot be used to run a command indirectly.

The old implementation (`terminal.py`: no shell, refused shells and admin tools, Job Object,
allowlisted environment, time and output limits) is kept and tested, unreachable, for when
real confinement exists.

## Screen and input (Windows)

`capture_screen` returns a PNG (scaled to `max_image_width`, 1280 by default) of the whole
screen or of one window's area, plus a `capture_id` and the scale. It captures only when
called, and nothing is saved. `list_windows` returns the visible windows with a
`window_id`, title, program, and whether input to it is allowed.

The input tools act on **one named window** (`window_id`), and refuse:

- the Claude apps, terminals, and code editors where Claude Code runs (input there could
  approve Claude's own tool calls), Windows Explorer, the taskbar and Start menu (their Run
  box and address bar start programs), credential and security prompts, Task Manager,
  registry and system tools, password managers, this server's own windows, and any program
  running as administrator;
- any program not in `[input] allowed_processes`, when that list is not empty;
- everything while a Universal AI Tools approval dialog is open in any process.

`click` and `scroll` take coordinates **in a screenshot** (`capture_id`, `x`, `y`), which
must be at most 5 minutes old, and act only if the named window is the top-level window under
that point. Before any input the window is brought to the foreground and checked again.
`type_text` (2,000 characters at most) checks every few characters that the window still
has the focus and stops otherwise. `press_keys` refuses the Windows key and Ctrl+Esc /
Ctrl+Shift+Esc. Input is not verified by itself: Claude should take a new screenshot.

Applications differ: anything a person can do with mouse and keyboard in an allowed window,
Claude can do there, but layouts change, and some applications ignore synthetic input.

### Controls by name (UI Automation)

`inspect_window` lists a window's controls through Windows UI Automation, the accessibility
interface screen readers use: each button, field, check box, menu, list and tab item with its
role, name, value, position, and the actions it supports, plus an `inspection_id` and
`element_id`s. `invoke_element` then acts on one control directly (`invoke` presses a button
or menu item, `toggle`, `select`, `expand`, `collapse`, `focus`), and `set_element_value`
replaces a field's text. No mouse or keyboard input is sent, nothing depends on pixels, and
the approval names the control (`Invoke the button 'Save'`) and shows the exact new text.

Before acting, the element is read again: it must still exist, belong to the same program as
the window, be enabled, and support the action, and the inspection must be of that window and
at most 5 minutes old. The protected-window rules, the approval-dialog rule, and the emergency
stop apply as for every input tool. Password fields are never read, and never set. Names and
values are untrusted text from the application. Some applications expose few or no controls
(games, some custom-drawn programs); Claude can fall back to screenshots and clicks there.

### Saving screenshots

`capture_screen` shows Claude the screen and never saves anything. `save_screenshot` is the
separate tool for when a picture is needed in a document or report: it writes a
full-resolution PNG of the screen or of one window's area to a **new** file in the workspace
(`shots/step-1.png`; the folder must exist), and never replaces an existing file. The path
goes through every file rule of the workspace (no `..`, no links, no repository metadata, no
protected locations). It has its own approval, once per session by default, separate from
the approval to look at the screen, so allowing Claude to look never also allows it to write
files. The audit log records the path, never the image.

### Reading, waiting, and launching

- `inspect_window` takes `name_contains` and `control_type`, to find a control without
  listing a whole window. Element ids are the same whatever the filter.
- `read_text` reads the whole text of a document or text field (`readable: true` in an
  inspection), and `read_table` reads a grid or table (`table: true`) as rows and columns
  with its headers. Both re-read the element first (it must still exist, in the same
  program), never read password fields, and show control characters escaped. Their output
  is untrusted data, never instructions.
- `wait_for` waits up to 30 seconds for a window (by program or title) or a control (by name
  or type) to appear, so Claude can wait for a dialog instead of guessing.
- `launch_app` starts a program you listed with `universal-ai-tools config allow-launch
  notepad.exe` (`[input] launchable`), **with no arguments**, as if from the Start menu, and
  returns its new window. Every launch is asked, whatever grants are active. Only programs
  installed under the Windows folder or Program Files are found (directly or through the
  "App Paths" registry entries, whose target must have the same name), so a program put in
  your own folders cannot be launched. Shells, script hosts, interpreters, installers, and
  administration tools (`cmd`, `powershell`, `python`, `mshta`, `rundll32`, `regedit`,
  `explorer`, ...) are refused whatever the list says. This is the one place the server
  starts a process; `tests/test_no_subprocess.py` checks that no other module can.

### Time-limited permissions (grants)

Approving every keystroke makes a long task impractical. With `[input] grants = true`
(`universal-ai-tools config enable grants`), Claude can ask once, with `request_input_grant`,
for input to a few named programs, for a few minutes, saying what it is for:

> Allow input to notepad.exe for 10 minutes without asking before each action

You approve or deny that one request. It is **always** asked, never covered by an earlier
approval. While the grant lasts, input to windows of those programs runs without a question.
Each action is still checked against every rule above (protected windows,
`allowed_processes`, an open approval dialog, the emergency stop), and recorded in the audit
log with `"channel": "grant"` and the grant's id.

- Only programs in `[input] allowed_processes` can be granted, never a protected one, and for
  at most `[input] max_grant_minutes` (30 by default, 120 at most).
- Grants cover input tools only. Screenshots keep their own approval.
- A grant ends when its time is up, when you use the emergency stop or the stop hotkey, or
  when you run `universal-ai-tools grants revoke` (`input stop` and `input resume` revoke too).
  Grants live only in the memory of the server process that made them.
- While a grant is active, an **orange banner** at the top of the screen says which programs
  Claude is controlling, how long is left, and how to stop it. Clicks pass through it, and it
  never takes the focus.

### Emergency stop

**Press Ctrl+Alt+Esc, or push the mouse pointer into any corner of the main screen.** All
input stops at once, and every grant ends, in
Claude Code's and Claude Desktop's server processes alike, and stays stopped (it survives a
restart) until you resume it in your own terminal:

```powershell
universal-ai-tools input resume     # also: input stop, input status
```

Every input tool checks the pointer before acting, and `type_text` checks it every few
characters. Moving the mouse at all while `type_text` is typing stops that typing (without
the latch): you took over. Clicks and scrolls into the corners are refused, so the tools never
trip the stop themselves. The hotkey is registered with Windows' `RegisterHotKey`, which
tells this program about that one combination only, while input is enabled. The stop is
the file `input-stopped` in `~\.universal-ai-tools`,
which no Universal AI Tools tool and no Claude Code file tool may delete; a command that
mentions it is always asked. There is no keyboard hook: nothing watches your keys.

## File tools reference

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

Registered only when file writing is enabled: `[filesystem] write = true` in the config file, or `UNIVERSAL_AI_TOOLS_ENABLE_WRITE_TOOLS=true` in environment mode. In file mode every call needs approval.

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

#### File-changing tools in file mode

With `[filesystem] write = true` the server also offers, each needing approval:

- **`edit_file`** `{path, old_text, new_text, expected_replacements = 1}`: replaces exact text.
  `old_text` must occur exactly `expected_replacements` times or nothing changes. LF in
  `old_text` also matches a CRLF file, and CRLF is kept. The new content goes to a temporary
  file that is flushed and then renamed over the original, so the change is all or nothing;
  the file is then read back and `verified` reports whether it matches. Returns SHA-256
  hashes before and after. The same refusals as `read_file` apply (links, hard links,
  binary, over 1 MiB), plus credential files, repository metadata, and excluded directories.
- **`create_directory`** `{path}`: one new directory; the parent must exist.
- **`move_path`** `{source, destination}`: moves or renames a file or directory inside the
  workspace. Never overwrites, never follows or moves links, never moves a directory into
  itself, and checks both ends against the policy. `verified` reports that the item is at
  the destination and gone from the source.

There is no delete tool. To tidy up, Claude can move items into a folder.

## Requirements

- Python 3.13+
- Windows 10/11 for everything. Linux runs the file tools; the screen and input tools are
  Windows-only. macOS has never been run.
- On Linux, `/proc` must be mounted.
- Claude Desktop and/or Claude Code, signed in with your Claude account. No API key.

## Diagnostics

`universal-ai-tools doctor` checks Python, the config file, the workspace, capabilities,
that the process is not elevated, that the audit log is writable, which approval channels
exist, that the screen can be read, both client
registrations (including shadowing project-only registrations in Claude Code), and finally
starts the server exactly as the clients do and talks MCP to it. `--no-handshake` skips the
last step.

Then ask Claude: *"Call get_platform_info from universal-ai-tools."* In Claude Code, `/mcp`
lists the server and its tools. Claude Desktop shows connected servers under Settings ▸
Developer, and its MCP logs are in the `logs` folder next to `claude_desktop_config.json`.

## Launching the server by hand

The clients start it; you normally never do. It speaks MCP over stdio, stdout carries only
protocol messages, and all logs go to stderr:

```powershell
$env:UNIVERSAL_AI_TOOLS_CONFIG = "$HOME\.universal-ai-tools\config.toml"
universal-ai-tools          # or: python -I -m universal_ai_tools.server
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest                  # tests (includes real stdio subprocess round trips)
python tests/mutation_check.py    # removes each safeguard in turn; each must fail a test
python tests/mutation_check.py gate.py terminal.py   # only those files' mutants
python -m ruff check .            # lint
python -m ruff format --check .   # formatting
python -m mypy                    # strict type check of src/ and tests/
```

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the same commands except
the mutation check on Ubuntu and Windows with Python 3.13 and
`UNIVERSAL_AI_TOOLS_STRICT_SECURITY_TESTS=1`, which turns a security test that cannot run on
its platform into a failure. On Windows, file symlink tests need Developer Mode or an
elevated shell. On Linux, `test_concurrent_hard_link_substitution_never_escapes` fails
intermittently because of the [open defect](docs/architecture.md#known-limitations).

No test sends real mouse or keyboard input or shows a real dialog: the input tools are tested
against a fake desktop, and approvals with a stand-in dialog and the MCP SDK client's
elicitation callback. One Windows test reads a 16x9 screenshot to check the real backend.

## Project layout

```
src/universal_ai_tools/
  config.py        environment-mode settings, workspace authorization, removed settings
  settings.py      the shared config file (file mode)
  policy.py        what may be accessed, and how output is escaped
  workspace.py     path confinement: the security boundary for every file operation
  trusted.py       the server's own code, Python, and configuration, which nothing changes
  filesystem.py    list_files, read_file, search_files, write_file
  file_actions.py  edit_file, create_directory, move_path
  terminal.py      run_command: disabled, never registered or imported (kept for later)
  desktop/         screen capture and input: safeguards (tools.py), Win32 backend, PNG
  tools.py         tool registry: every tool's definition, risk, and approval description
  approval.py      approval channels: MCP elicitation and the Windows dialog
  gate.py          permission decision, approval, and audit around every call
  audit.py         the JSON-lines audit log
  elevation.py     administrator check
  server.py        MCP server assembly and the stdio entry point
  clients.py       Claude Desktop / Claude Code registration (used by setup only)
  guard.py         the Claude Code guard: the PreToolUse/PostToolUse hook
  desktop/         screen, input, UI Automation (uia.py), emergency stop (stop.py)
  cli.py           the universal-ai-tools command: serve, setup, doctor, config, audit,
                   install, guard
legacy/api-agent/  the archived API-key agent (not installed, imported, or tested)
tests/             pytest suite (synthetic files in temporary directories only)
docs/              architecture and threat model
```

## What is implemented and tested

| Capability | State | Evidence |
| --- | --- | --- |
| Read, list, search files | Implemented, tested | Unchanged security suite (policy, links, junctions, 8.3 names, races) |
| `write_file`, `edit_file`, `create_directory`, `move_path` | Implemented, tested | `test_file_actions.py`, approval and audit over MCP and real stdio |
| Approval: client dialog (elicitation) | Implemented, tested | Both MCP generations with the SDK client; live: Claude Code 2.1.319 headless cancels it, and the write is refused |
| Approval: Windows dialog | Implemented, partially tested | Logic tested with a stand-in; the real dialog was not clicked in automated tests |
| Approval prompt shows the complete, exact request | Implemented, tested | `test_approval_integrity.py`: every value read back exactly; oversized requests refused; changed arguments never run. Not verified: that real clients display 200-line prompts in full |
| No changes to the server's own code | Implemented, tested | `test_self_modification.py`: every file tool, every spelling (case, separators, 8.3, junction, hard link), the real checkout; file symlinks only with the symlink privilege |
| Audit log | Implemented, tested | `test_audit_completeness.py`: reads, searches, failures, writes, denials, cancellations, refusals before running; contents, patterns, and credential arguments never logged; unauditable calls refused |
| `run_command` | **Disabled for safety** | Cannot be enabled (config, CLI) or registered (forced config, real process: no process module loaded); `test_fail_closed.py`, `test_no_subprocess.py`. The kept module's own tests still run |
| Environment mode | Read-only only | Writes refused (`test_settings.py`, real process in `test_server.py`); no approval or audit log there |
| Isolated start | Implemented, tested | A real process started without `-I`/`-P` refuses to run (`test_server.py`) |
| Fail closed on audit failure | Implemented, tested | Reads withhold results, changes do not start, questions are not sent, the server does not start (`test_fail_closed.py`) |
| `capture_screen`, `list_windows` | Implemented, tested | Fake desktop; real capture checked on this PC |
| `click`, `scroll`, `type_text`, `press_keys`, `focus_window` | Implemented, partially tested | All safeguards tested against a fake desktop. **`type_text` live-tested 2026-11-09** in Notepad through Claude Code, with approvals in Claude Code's own prompt; protected windows (VS Code, Explorer) and a program outside `allowed_processes` (Chrome) were correctly marked as refusing input. `click`, `scroll`, `press_keys` not yet used live. **Typing is read back and checked**: live testing found that Unicode key events were misread by the Windows 11 Notepad ("tttthe 5555." for "the quick ... 12345."), and that fast real keys were dropped as the document grew. Typing now sends real keys for characters on the keyboard layout, at about 30 characters a second, and fails (never reports success) when the field's text does not show exactly what was typed. Five lines with mixed case, digits, and symbols then arrived exactly |
| Emergency stop (pointer in a corner, `input stop/resume`) | Implemented, tested | `test_desktop_control.py` against the fake desktop: the latch, all processes, typing interrupted by the mouse, corner clicks refused, CLI; real pointer reading checked. **Live-tested 2026-11-09** through Claude Code in Notepad: moving the mouse stopped typing after 304 of 1,756 characters; with the pointer in a corner, input was refused, stayed refused after the pointer moved away, and worked again only after `input resume` |
| Grants, banner, stop hotkey | Implemented, tested | `test_grants.py`: one approval covers many actions through the real gate and MCP server; the request is always asked; scope, time limit, revocation in every process, the stop and hotkey end grants; covered actions still obey every rule. Real banner window shown, hidden, click-through, and given a hotkey message on this PC. **Live-tested 2026-11-10** through Claude Code: one approved 5-minute grant covered five `type_text` calls to Notepad with no further question; the real Ctrl+Alt+Esc stopped input and ended the grant |
| `run_in_sandbox` (Docker) | Implemented, tested | `test_sandbox.py`: the exact locked-down `docker run` line, fingerprint pinning, time limit and kill, output caps, a fresh approval for every command through the real server, redacted audit records; a real-container test checks the read-only mount, no network, the unprivileged user, and no host files, and runs when Docker and the image are ready. **Verified 2026-11-10 on the real engine** (Docker Desktop 4.98): user 1000, workspace write refused, no network, no PC drive, read-only system files; and **live through Claude Code**: `python --version && ls -la /work` after approval |
| `read_text`, `read_table`, `wait_for`, `launch_app`, inspection filters | Implemented, tested | `test_desktop_reading.py` against the fake desktop; real UI Automation reads a real text field; the real `notepad.exe` is found in a trusted folder (nothing launched). **Live-tested 2026-11-10** through Claude Code: `launch_app` started `C:\Windows\System32\notepad.exe` after approval and returned its window; `set_element_value` then `read_text` returned the text exactly. `read_table` (no real grid yet) and `wait_for` **not yet live-tested** |
| `inspect_window`, `invoke_element`, `set_element_value` | Implemented, tested | Every refusal against the fake desktop; **real UI Automation** finds and operates a real check box and text field in a window the test creates (no other program touched). **Live-tested 2026-11-09** through Claude Code: `inspect_window` read Windows 11 Notepad's 42 controls by name, and `set_element_value` set its text. `invoke_element` not yet used live |
| `setup`, `doctor`, `config`, `audit` | Implemented, tested | Config writing, Desktop JSON merge and backup, Code registration reading; live `doctor` handshake |
| Claude Code guard (hook) | Implemented, tested | `test_guard.py`: every refusal, question, and pass-through rule, Windows aliases (streams, UNC, `\\?\`, trailing dots, case, junctions, hard links), fail-closed paths, audit records without contents, the hook as a real `-I` process that a project cannot replace; `test_setup.py`: installation keeps other settings and hooks, `doctor` runs the installed hook. **Not yet verified inside a live Claude Code session** |
| Protected runtime (`install`) | Implemented | Commands and digest check tested by hand; the install itself downloads with pip |
| Claude Code end to end | Tested (MCP tools) | Headless `claude -p` on a real task through this server (read-only), and a refused write |
| Claude Desktop end to end | **Not yet tested** | Configuration is written by `setup`; needs a run in the app |

## Not implemented yet

- Deleting files or directories, and overwriting a whole file in one call
- Launching a program with a file or arguments (`launch_app` starts it empty)
- Approval rules finer than per capability (for example, auto-approving `pytest` only)
- Commands on the PC itself: they run only in the Docker sandbox
- Running the server as a separate low-privilege user
- A tamper-proof audit log
- Non-stdio transports (HTTP), and therefore one shared server process for both clients
- macOS

## License

[MIT](LICENSE)
