"""The ``universal-ai-tools`` command.

* ``universal-ai-tools`` (no arguments) or ``serve``: run the MCP server over stdio. This is
  what Claude Desktop and Claude Code start.
* ``setup``: create or update the config file, then register the server with Claude Desktop
  and Claude Code. Every change to a Claude configuration is shown first and needs a yes.
* ``doctor``: check the installation end to end, including a live MCP handshake with the
  server exactly as the clients start it.
* ``config``: show the config file, or turn a capability on or off.
* ``audit``: show the latest audit records.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from universal_ai_tools import APP_NAME, __version__
from universal_ai_tools.config import ConfigError
from universal_ai_tools.settings import (
    CONFIG_VERSION,
    ENV_CONFIG,
    TERMINAL_DISABLED,
    ConfigFile,
    default_config_path,
    load_config_file,
    read_config_file,
    render_config,
)

CAPABILITIES = ("filesystem-write", "screen", "input", "grants", "sandbox")
# Can be turned off (an older config may have it on), never on: see settings.TERMINAL_DISABLED.
DISABLE_ONLY = ("terminal",)


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "serve":
        from universal_ai_tools.server import main as serve

        serve()
        return
    parser = _parser()
    options = parser.parse_args(args)
    try:
        code = options.handler(options)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print("\ncancelled; nothing more was changed", file=sys.stderr)
        code = 130
    raise SystemExit(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Local execution service for Claude Desktop and Claude Code. Run without "
        "arguments to serve MCP over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="create the config and register both clients")
    setup.add_argument("--workspace", type=Path, help="the project directory to authorize")
    setup.add_argument(
        "--create-workspace",
        action="store_true",
        help="create the workspace as a new, empty scratch directory if it does not exist",
    )
    setup.add_argument("--config", type=Path, default=None, help="config file path")
    setup.add_argument(
        "--enable",
        default="",
        help=f"comma-separated capabilities to turn on: {', '.join(CAPABILITIES)}",
    )
    setup.add_argument(
        "--clients", default="desktop,code", help="which clients to register: desktop, code"
    )
    setup.add_argument("--yes", action="store_true", help="apply every change without asking")
    setup.add_argument("--dry-run", action="store_true", help="show the changes, change nothing")
    setup.set_defaults(handler=_setup)

    doctor = commands.add_parser("doctor", help="check the installation")
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument("--no-handshake", action="store_true", help="skip the live MCP test")
    doctor.set_defaults(handler=_doctor)

    config = commands.add_parser("config", help="show or change the config file")
    config.add_argument("--config", type=Path, default=None)
    actions = config.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="print the config file").set_defaults(handler=_config_show)
    actions.add_parser("path", help="print the config file path").set_defaults(
        handler=lambda o: _print(str(_config_path(o)))
    )
    for verb, choices in (("enable", CAPABILITIES), ("disable", (*CAPABILITIES, *DISABLE_ONLY))):
        action = actions.add_parser(verb, help=f"{verb} a capability")
        action.add_argument("capability", choices=choices)
        action.set_defaults(handler=_config_toggle, enable=verb == "enable")
    allow = actions.add_parser(
        "allow-input",
        help="limit mouse and keyboard input to these programs (e.g. notepad.exe)",
    )
    allow.add_argument("programs", nargs="*", help="executable file names, e.g. notepad.exe")
    allow.add_argument(
        "--clear", action="store_true", help="remove the limit (every unprotected program)"
    )
    allow.set_defaults(handler=_config_allow_input, field="allowed_processes")
    launch = actions.add_parser(
        "allow-launch", help="programs launch_app may start (e.g. notepad.exe)"
    )
    launch.add_argument("programs", nargs="*", help="executable file names, e.g. notepad.exe")
    launch.add_argument("--clear", action="store_true", help="remove every program")
    launch.set_defaults(handler=_config_allow_input, field="launchable")

    audit = commands.add_parser("audit", help="show the latest audit log records")
    audit.add_argument("--config", type=Path, default=None)
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(handler=_audit)

    install = commands.add_parser(
        "install", help="install or update the protected copy that the clients and guard run"
    )
    install.add_argument("--source", type=Path, default=None, help="the project to install")
    install.add_argument("--yes", action="store_true", help="run without asking")
    install.add_argument("--dry-run", action="store_true", help="show the commands only")
    install.set_defaults(handler=_install)

    guard = commands.add_parser("guard", help="the Claude Code guard (hooks)")
    guard.add_argument("--config", type=Path, default=None)
    guard_actions = guard.add_subparsers(dest="action", required=True)
    remove = guard_actions.add_parser("remove", help="take the guard out of Claude Code")
    remove.add_argument("--yes", action="store_true", help="apply without asking")
    remove.add_argument("--dry-run", action="store_true", help="show the change only")
    remove.set_defaults(handler=_guard_remove)
    guard_actions.add_parser(
        "test", help="run the installed guard on sample tool calls"
    ).set_defaults(handler=_guard_test)

    input_command = commands.add_parser("input", help="the emergency stop for mouse and keyboard")
    input_command.add_argument("--config", type=Path, default=None)
    input_actions = input_command.add_subparsers(dest="action", required=True)
    for verb, help_text in (
        ("stop", "stop all input now, in every server process"),
        ("resume", "allow input again after a stop"),
        ("status", "show whether input is stopped"),
    ):
        input_actions.add_parser(verb, help=help_text).set_defaults(handler=_input_stop, verb=verb)

    sandbox = commands.add_parser("sandbox", help="the Docker sandbox for commands")
    sandbox.add_argument("--config", type=Path, default=None)
    sandbox_actions = sandbox.add_subparsers(dest="action", required=True)
    trust = sandbox_actions.add_parser("trust", help="trust this docker.exe by its fingerprint")
    trust.add_argument("--docker", type=Path, default=None, help="path to docker.exe")
    trust.add_argument("--yes", action="store_true", help="apply without asking")
    trust.set_defaults(handler=_sandbox_trust)
    sandbox_actions.add_parser(
        "pull", help="download the sandbox image (uses the network)"
    ).set_defaults(handler=_sandbox_pull)
    sandbox_actions.add_parser("status", help="check Docker and the image").set_defaults(
        handler=_sandbox_status
    )

    grants = commands.add_parser("grants", help="time-limited input permissions")
    grants.add_argument("--config", type=Path, default=None)
    grants_actions = grants.add_subparsers(dest="action", required=True)
    grants_actions.add_parser(
        "revoke", help="end every input grant now, in every server process"
    ).set_defaults(handler=_grants_revoke)
    return parser


def _print(text: str) -> int:
    print(text)
    return 0


def _config_path(options: argparse.Namespace) -> Path:
    if options.config is not None:
        configured: Path = options.config
        return configured.expanduser().absolute()
    from_env = os.environ.get(ENV_CONFIG, "").strip()
    return Path(from_env) if from_env else default_config_path()


def _confirm(question: str, options: argparse.Namespace) -> bool:
    if getattr(options, "dry_run", False):
        print("  (dry run: not applied)")
        return False
    if getattr(options, "yes", False):
        return True
    if not sys.stdin.isatty():
        print("  (not applied: no terminal to ask; rerun with --yes to apply)")
        return False
    return input(f"{question} [y/N] ").strip().casefold() in ("y", "yes")


# setup


MINIMUM_PYTHON = (3, 13)


def _setup(options: argparse.Namespace) -> int:
    """Create or update the config, initialise the audit log, and register both clients.

    Safe to run again: an existing config file keeps every setting not named on the command
    line, and nothing already in the wanted state is touched. Each change is shown in full
    and needs a yes (``--yes`` answers yes; ``--dry-run`` shows everything and changes
    nothing, not even the workspace or the config file).
    """
    from universal_ai_tools import clients

    path = _config_path(options)
    dry_run = bool(options.dry_run)
    print(
        f"{APP_NAME} {__version__} setup"
        + (" (dry run: nothing will be changed)" if dry_run else "")
    )
    problem = _environment_problem()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    print(
        f"Python: {sys.executable} ({platform.python_version()}); dependencies: {_dependencies()}"
    )

    # The config file's contents, and the workspace it names.
    existing = read_config_file(path) if path.exists() else None
    if existing is None and options.workspace is None:
        print("error: --workspace is required to create the config file", file=sys.stderr)
        return 2
    base = existing or ConfigFile(version=CONFIG_VERSION, workspace="")
    workspace = (
        str(options.workspace.expanduser().resolve()) if options.workspace else base.workspace
    )
    enable = {c.strip() for c in options.enable.split(",") if c.strip()}
    if enable & set(DISABLE_ONLY):
        print(f"error: terminal: {TERMINAL_DISABLED}", file=sys.stderr)
        return 2
    unknown = enable - set(CAPABILITIES)
    if unknown:
        print(f"error: unknown capability: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    updated = _with_capabilities(base.model_copy(update={"workspace": workspace}), enable, True)
    text = render_config(updated)

    # 0. The workspace.
    workspace_path = Path(workspace)
    print(f"\n[0/4] Workspace: {workspace_path}")
    if workspace_path.is_dir():
        print("  exists")
    elif workspace_path.exists():
        print("error: the workspace is not a directory", file=sys.stderr)
        return 2
    elif not options.create_workspace:
        print(
            "error: the workspace does not exist; pass --create-workspace to create it as a "
            "new, empty scratch directory",
            file=sys.stderr,
        )
        return 2
    else:
        _check_planned_workspace(workspace_path, updated, path)
        print("  will be created, empty (a scratch workspace: put only files Claude may use there)")
        if _confirm("Create the workspace directory?", options):
            workspace_path.mkdir(parents=True)
            print("  created")
    if workspace_path.is_dir():
        _validate_rendered(text, path)

    # 1. The config file.
    print(f"\n[1/4] Config file: {path}")
    if path.exists() and path.read_text(encoding="utf-8-sig") == text:
        print("  unchanged")
    else:
        if path.exists():
            print("  existing settings are kept; only these lines change:")
            _show_diff(path.read_text(encoding="utf-8-sig"), text, str(path))
            print(f"  the current file is kept as {path.with_name(path.name + '.bak')}")
        else:
            print("  will be created:\n" + _indent(text))
        if _confirm("Write the config file?", options):
            _write_with_backup(path, text)
            print("  written")
        elif not path.exists() and not dry_run:
            print("  not written; the clients cannot be registered without it")
            return 1

    # 2. The audit log.
    audit_path = _audit_path(updated, path)
    print(f"\n[2/4] Audit log: {audit_path}")
    if path.exists() and not dry_run:
        from universal_ai_tools.audit import AuditError, AuditLog

        try:
            AuditLog(audit_path).record(
                "setup", {"version": __version__, "config": str(path)}, durable=True
            )
        except AuditError as exc:
            print(f"  FAILED: {exc}; the server will refuse to start until it can be written")
            return 1
        print("  initialised (a 'setup' record was written)")
    else:
        print("  will be created when the config file is written")

    runtime = _installed_runtime()
    entry = _expected_entry(path)
    wanted = {c.strip() for c in options.clients.split(",") if c.strip()}
    if runtime is None:
        print(
            f"\nNo protected runtime is installed ({clients.runtime_directory()}), so the "
            f"clients would run this Python's copy of the code. Run '{APP_NAME} install' "
            "first; the Claude Code guard needs it."
        )
    print("\nBoth clients will start the server as:")
    print(f"  {clients.display([entry['command'], *entry['args']])}")
    print(f"  with {ENV_CONFIG}={path}")
    print(
        "  (-I: Python ignores the current directory and PYTHON* variables, so no file in a "
        "project can replace the server's code)"
    )

    # 3. Claude Desktop.
    print("\n[3/4] Claude Desktop")
    if "desktop" not in wanted:
        print("  skipped")
    else:
        desktop = clients.desktop_config_path()
        if desktop is None:
            print("  Claude Desktop does not seem to be installed; skipped")
        else:
            try:
                plan = clients.plan_desktop(desktop, entry)
            except clients.ClientConfigError as exc:
                print(f"  not changed: {exc}")
            else:
                if plan.action == "unchanged":
                    print(f"  already registered in {desktop}")
                else:
                    print(
                        f"  {plan.action} '{clients.SERVER_NAME}' in {desktop}; every other "
                        "setting and server is kept exactly:"
                    )
                    print(_indent(plan.diff()))
                    if desktop.exists():
                        print(f"  the current file is copied first to {desktop.name}.bak-<time>")
                    if _confirm("Change Claude Desktop's configuration?", options):
                        backup = clients.apply_desktop(plan)
                        print(f"  written{f'; backup: {backup}' if backup else ''}")
                        print(
                            "  Quit Claude Desktop completely (tray icon > Quit) and start it "
                            "again."
                        )

    # 4. Claude Code.
    print("\n[4/4] Claude Code")
    if "code" not in wanted:
        print("  skipped")
    else:
        code = _setup_code(entry, options)
        if code:
            return code
        code = _setup_guard(path, runtime, options)
        if code:
            return code

    print(
        "\nNothing here auto-approves any tool. The guard only adds checks: it refuses or asks, "
        "and never approves."
    )
    print(f"Done. Check everything with: {APP_NAME} doctor")
    return 0


def _setup_code(entry: dict[str, Any], options: argparse.Namespace) -> int:
    from universal_ai_tools import clients

    registrations = clients.code_registrations()
    settings = clients.claude_code_settings_path()
    planned: list[tuple[list[str], Path | None]] = []
    if clients.same_registration(registrations.user, entry):
        print("  already registered (user scope, all projects)")
    else:
        if registrations.user is not None:
            print("  the current user-scope registration will be replaced:")
            print(f"    {json.dumps(registrations.user)[:400]}")
        planned += [
            (command, None)
            for command in clients.code_add_commands(entry, replace=registrations.user is not None)
        ]
    for project, local in registrations.local.items():
        if clients.same_registration(local, entry):
            continue
        reason = clients.registration_problem(local) or "it differs from the shared registration"
        print(
            f"  project '{project}' has its own (local scope) registration of "
            f"'{clients.SERVER_NAME}', which takes precedence there; {reason}:"
        )
        print(f"    {json.dumps(local)[:400]}")
        if Path(project).is_dir():
            planned.append((clients.code_remove_local_command(), Path(project)))
        else:
            print("    the project folder no longer exists; remove it by hand if it matters")
    if not planned:
        return 0
    if clients.claude_cli() is None:
        print("  the 'claude' command was not found; run these later:")
        for command, cwd in planned:
            print(f"    {_shell_quote(command)}" + (f"   (in {cwd})" if cwd else ""))
        return 0
    print(f"  will run (after copying {settings} to {settings.name}.bak-<time>):")
    for command, cwd in planned:
        print(f"    {_shell_quote(command)}" + (f"   (in {cwd})" if cwd else ""))
    if not _confirm("Change Claude Code's registrations?", options):
        return 0
    backup = clients.backup_code_settings()
    if backup is not None:
        print(f"  backup: {backup}")
    for command, cwd in planned:
        code, output = clients.run_claude(command, cwd=cwd)
        print(_indent(output or f"exit code {code}"))
        if code != 0:
            print("  failed; nothing further was changed. Restore with the backup if needed.")
            return 1
    return 0


def _installed_runtime() -> Path | None:
    """The runtime's Python, if ``install`` has made one."""
    from universal_ai_tools import clients

    python = clients.runtime_python(clients.runtime_directory())
    return python if python.is_file() else None


def _expected_entry(path: Path) -> dict[str, Any]:
    """The registration setup writes: the runtime's Python if installed, else this one."""
    from universal_ai_tools import clients

    runtime = _installed_runtime()
    return clients.server_entry(path, python=str(runtime) if runtime else None)


def _setup_guard(config_path: Path, runtime: Path | None, options: argparse.Namespace) -> int:
    from universal_ai_tools import clients

    settings = clients.claude_user_settings_path()
    print(f"\n  The Claude Code guard (hooks in {settings})")
    print(
        "  Claude Code's own tools (Bash, PowerShell, Write, Edit, Read) do not go through the "
        "server. The guard is a hook that checks each call before it runs: it refuses changes "
        "to protected locations and makes Claude Code ask you before every command."
    )
    if runtime is None:
        print(f"  not installed: run '{APP_NAME} install' first, then setup again")
        return 0
    try:
        plan = clients.plan_guard_hooks(settings, str(runtime), config_path)
    except clients.ClientConfigError as exc:
        print(f"  not changed: {exc}")
        return 0
    if plan.action == "unchanged":
        print("  already installed")
        return 0
    print(f"  {plan.action}; every other setting and hook is kept:")
    print(_indent(plan.diff()))
    if settings.exists():
        print(f"  the current file is copied first to {settings.name}.bak-<time>")
    print("  Claude Code applies it to running sessions within a few seconds.")
    if _confirm("Install the guard in Claude Code?", options):
        backup = clients.apply_hooks(plan)
        print(f"  written{f'; backup: {backup}' if backup else ''}")
        print(f"  check it with: {APP_NAME} guard test")
    return 0


def _install(options: argparse.Namespace) -> int:
    from universal_ai_tools import clients

    source = options.source.expanduser().resolve() if options.source else clients.checkout_source()
    if source is None or not (source / "pyproject.toml").is_file():
        print("error: pass --source <project directory with pyproject.toml>", file=sys.stderr)
        return 2
    directory = clients.runtime_directory()
    commands = clients.install_commands(source, directory)
    print(f"{APP_NAME} {__version__} install")
    print(f"  from: {source}")
    print(f"  into: {directory}")
    print(
        "  The clients and the guard will run this copy, which the guard protects; changes in "
        "the project take effect only when you run install again."
    )
    print("  will run (pip may download the package's dependencies and build tools):")
    for command in commands:
        print(f"    {_shell_quote(command)}")
    if not _confirm("Install?", options):
        return 0
    for command in commands:
        code, output = clients.run_command(command)
        tail = "\n".join(output.splitlines()[-6:])
        print(_indent(tail or f"exit code {code}"))
        if code != 0:
            print("  failed; the runtime may be incomplete. Fix the error and run install again.")
            return 1
    package = clients.runtime_package(directory)
    checkout = source / "src" / "universal_ai_tools"
    if package is None:
        print("  FAILED: the package is not in the runtime after installing")
        return 1
    same = checkout.is_dir() and clients.package_digest(package) == clients.package_digest(checkout)
    print(f"  installed: {package}")
    print(f"  identical to {checkout}: {'yes' if same else 'NO'}")
    print(f"  next: {APP_NAME} setup --clients code   (points Claude Code and the guard at it)")
    return 0 if same else 1


def _guard_remove(options: argparse.Namespace) -> int:
    from universal_ai_tools import clients

    settings = clients.claude_user_settings_path()
    plan = clients.plan_remove_guard_hooks(settings)
    if plan.action == "unchanged":
        print(f"the guard is not installed in {settings}")
        return 0
    print(_indent(plan.diff()))
    if _confirm("Remove the guard from Claude Code?", options):
        backup = clients.apply_hooks(plan)
        print(f"removed{f'; backup: {backup}' if backup else ''}")
    return 0


def _guard_test(options: argparse.Namespace) -> int:
    from universal_ai_tools import clients

    handlers = clients.installed_guard(clients.claude_user_settings_path())
    pre = [h for h in handlers if h["_event"] == "PreToolUse"]
    if not pre:
        print("the guard is not installed; run setup")
        return 1
    try:
        print(_guard_self_test(pre[0], _config_path(options)))
    except RuntimeError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


def _guard_self_test(handler: dict[str, Any], config_path: Path) -> str:
    """Run an installed PreToolUse handler on three sample calls.

    Raises:
        RuntimeError: If any answer is not the one expected.
    """
    import tempfile

    from universal_ai_tools import clients
    from universal_ai_tools.guard import load_settings

    commands = load_settings(config_path).commands
    base = {
        "hook_event_name": "PreToolUse",
        "session_id": "doctor-self-test",
        "tool_use_id": "doctor-self-test",
        "cwd": str(Path.home()),
        "permission_mode": "default",
    }
    protected = Path.home() / ".claude" / "settings.json"
    run = clients.run_guard_handler(
        handler,
        {**base, "tool_name": "Write", "tool_input": {"file_path": str(protected), "content": ""}},
    )
    if run.code != 2:
        raise RuntimeError(f"a Write to {protected} was not refused (exit {run.code})")
    with tempfile.TemporaryDirectory() as scratch:
        ordinary = Path(scratch) / "ordinary.txt"
        run = clients.run_guard_handler(
            handler,
            {
                **base,
                "tool_name": "Write",
                "tool_input": {"file_path": str(ordinary), "content": ""},
            },
        )
    if run.code != 0 or run.stdout.strip():
        raise RuntimeError(f"an ordinary Write was not left to Claude Code (exit {run.code})")
    run = clients.run_guard_handler(
        handler, {**base, "tool_name": "Bash", "tool_input": {"command": "echo self-test"}}
    )
    decision = "pass"
    if run.code == 0 and run.stdout.strip():
        decision = json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"]
    expected = "ask" if commands == "ask" else "pass"
    if run.code != 0 or decision != expected:
        raise RuntimeError(f"a command got '{decision}' (exit {run.code}), expected '{expected}'")
    return "refuses a change to Claude's settings, leaves an ordinary file to Claude Code, and " + (
        "asks before every command" if commands == "ask" else "leaves commands to Claude Code"
    )


def _sandbox_trust(options: argparse.Namespace) -> int:
    from universal_ai_tools import sandbox

    path = _config_path(options)
    config = read_config_file(path)
    docker = options.docker.expanduser().resolve() if options.docker else sandbox.find_docker()
    if docker is None or not docker.is_file():
        print("error: docker.exe was not found; pass --docker <path>", file=sys.stderr)
        return 2
    digest = sandbox.fingerprint(docker)
    print(f"docker program: {docker}")
    print(f"SHA-256:        {digest}")
    print(
        "The sandbox will run only this exact file. If it changes (a Docker update, or "
        "anything else), runs are refused until you trust it again."
    )
    if config.sandbox.docker == str(docker) and config.sandbox.docker_sha256 == digest:
        print("already trusted")
        return 0
    if not _confirm("Trust this docker program?", options):
        return 0
    updated = config.model_copy(
        update={
            "sandbox": config.sandbox.model_copy(
                update={"docker": str(docker), "docker_sha256": digest}
            )
        }
    )
    return _rewrite(path, updated, options)


def _trusted_docker(options: argparse.Namespace) -> tuple[Any, Path, str]:
    from universal_ai_tools import sandbox

    config = read_config_file(_config_path(options))
    docker = sandbox.verified_docker(config.sandbox.docker, config.sandbox.docker_sha256)
    return sandbox, docker, config.sandbox.image


def _sandbox_pull(options: argparse.Namespace) -> int:
    from universal_ai_tools.sandbox import SandboxError

    try:
        sandbox, docker, image = _trusted_docker(options)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"downloading {image} (this uses the network) ...")
    code, output = sandbox.pull(docker, image)
    print(_indent("\n".join(output.splitlines()[-8:]) or f"exit code {code}"))
    return 0 if code == 0 else 1


def _sandbox_status(options: argparse.Namespace) -> int:
    from universal_ai_tools.sandbox import SandboxError

    try:
        sandbox, docker, image = _trusted_docker(options)
        print(f"docker: {docker} (fingerprint matches)")
        print(sandbox.docker_check(docker, image))
    except SandboxError as exc:
        print(f"not ready: {exc}")
        return 1
    return 0


def _grants_revoke(options: argparse.Namespace) -> int:
    from universal_ai_tools.audit import AuditLog
    from universal_ai_tools.desktop.grants import revoke_all, revoked_file

    path = _config_path(options)
    service = load_config_file(path)
    revoke_all(revoked_file(path.parent))
    AuditLog(service.audit_path, max_bytes=service.audit_max_bytes).try_record(
        "grants_revoked", {"by": "cli"}
    )
    print("every input grant is revoked; input asks before each action again")
    return 0


def _input_stop(options: argparse.Namespace) -> int:
    from universal_ai_tools.audit import AuditLog
    from universal_ai_tools.desktop.grants import revoke_all, revoked_file
    from universal_ai_tools.desktop.stop import EmergencyStop, stop_file

    path = _config_path(options)
    if options.verb in ("stop", "resume"):
        # Stopping ends every grant; resuming does not bring one back.
        revoke_all(revoked_file(path.parent))
    stop = EmergencyStop(stop_file(path.parent))
    reason = stop.reason()
    if options.verb == "status":
        print(f"input is stopped: {reason}" if reason else "input is not stopped")
        print(f"(stop file: {stop.path})")
        return 0
    service = load_config_file(path)
    audit = AuditLog(service.audit_path, max_bytes=service.audit_max_bytes)
    if options.verb == "stop":
        stop.trigger("stopped with 'universal-ai-tools input stop'")
        audit.try_record("input_stop", {"by": "cli"})
        print(
            "input is stopped in every Universal AI Tools process. Resume with: "
            f"{APP_NAME} input resume"
        )
        return 0
    if reason is None:
        print("input is not stopped")
        return 0
    if stop.path is None:
        return 1
    stop.path.unlink(missing_ok=True)
    audit.try_record("input_resume", {"by": "cli", "was": reason})
    print(f"input resumed (it was stopped: {reason})")
    return 0


def _environment_problem() -> str | None:
    """Why this Python cannot run the server, or None."""
    if sys.version_info < MINIMUM_PYTHON:
        wanted = ".".join(map(str, MINIMUM_PYTHON))
        return f"Python {wanted} or later is needed; this is {platform.python_version()}"
    try:
        import mcp  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError as exc:
        return f"a dependency is missing ({exc.name}); run: pip install -e ."
    return None


def _dependencies() -> str:
    from importlib.metadata import PackageNotFoundError, version

    found = []
    for name in ("mcp", "pydantic"):
        try:
            found.append(f"{name} {version(name)}")
        except PackageNotFoundError:
            found.append(f"{name} (version unknown)")
    return ", ".join(found)


def _audit_path(config: ConfigFile, config_path: Path) -> Path:
    from universal_ai_tools.settings import AUDIT_FILE_NAME

    return Path(config.audit.path) if config.audit.path else config_path.parent / AUDIT_FILE_NAME


def _check_planned_workspace(workspace: Path, config: ConfigFile, config_path: Path) -> None:
    """The workspace rules for a directory that does not exist yet (all but existence).

    Raises:
        ConfigError: If it would be refused once created.
    """
    from universal_ai_tools.settings import _refuse_self_modification

    resolved = workspace.resolve()
    if not resolved.is_absolute() or resolved == Path(resolved.anchor):
        raise ConfigError(f"the workspace must not be a drive root: '{resolved}'")
    if Path.home().resolve().is_relative_to(resolved):
        raise ConfigError(
            f"the workspace must not be the home directory or one of its parents: '{resolved}'"
        )
    _refuse_self_modification(resolved, config_path, _audit_path(config, config_path), None)


def _with_capabilities(config: ConfigFile, names: set[str], on: bool) -> ConfigFile:
    update: dict[str, Any] = {}
    if "filesystem-write" in names:
        update["filesystem"] = config.filesystem.model_copy(update={"write": on})
    if "terminal" in names:
        update["terminal"] = config.terminal.model_copy(update={"enabled": on})
    if "screen" in names:
        update["screen"] = config.screen.model_copy(update={"enabled": on})
    if "input" in names or "grants" in names:
        changes: dict[str, bool] = {}
        if "input" in names:
            changes["enabled"] = on
        if "grants" in names:
            changes["grants"] = on
        update["input"] = config.input.model_copy(update=changes)
    if "sandbox" in names:
        update["sandbox"] = config.sandbox.model_copy(update={"enabled": on})
    return config.model_copy(update=update)


def _validate_rendered(text: str, path: Path) -> None:
    import tomllib

    from universal_ai_tools.settings import service_config_from_data

    # Every check a server start makes, including the refusal of the terminal.
    service_config_from_data(tomllib.loads(text), path)


def _write_with_backup(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _show_diff(before: str, after: str, name: str) -> None:
    import difflib

    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{name} (now)",
            tofile=f"{name} (after)",
        )
    )
    print(_indent(diff))


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip("\n").splitlines())


def _shell_quote(command: list[str]) -> str:
    from universal_ai_tools.clients import display

    return display(command)


# config


def _config_show(options: argparse.Namespace) -> int:
    path = _config_path(options)
    print(f"# {path}")
    print(path.read_text(encoding="utf-8-sig"))
    return 0


def _config_toggle(options: argparse.Namespace) -> int:
    path = _config_path(options)
    config = _with_capabilities(read_config_file(path), {options.capability}, options.enable)
    return _rewrite(path, config, options)


def _config_allow_input(options: argparse.Namespace) -> int:
    programs: list[str] = list(options.programs)
    if options.clear == bool(programs):
        print("error: name one or more programs, or pass --clear", file=sys.stderr)
        return 2
    names = []
    for program in programs:
        name = program.strip().casefold()
        if not name.endswith(".exe") or any(c in name for c in '\\/:*?"<>|') or len(name) > 260:
            print(f"error: '{program}' is not an executable file name such as notepad.exe",
                  file=sys.stderr)  # fmt: skip
            return 2
        if options.field == "launchable":
            from universal_ai_tools.desktop.launch import refusal

            reason = refusal(name)
            if reason is not None:
                print(f"error: {reason}", file=sys.stderr)
                return 2
        names.append(name)
    path = _config_path(options)
    config = read_config_file(path)
    updated = config.model_copy(
        update={"input": config.input.model_copy(update={options.field: tuple(names)})}
    )
    return _rewrite(path, updated, options)


def _rewrite(path: Path, config: ConfigFile, options: argparse.Namespace) -> int:
    text = render_config(config)
    _validate_rendered(text, path)
    before = path.read_text(encoding="utf-8-sig")
    if before == text:
        print("unchanged")
        return 0
    _show_diff(before, text, str(path))
    _write_with_backup(path, text)
    print(
        f"written (previous version: {path.name}.bak). Restart Claude Desktop and reconnect "
        "the server in Claude Code (/mcp) for the change to take effect."
    )
    return 0


# audit


def _audit(options: argparse.Namespace) -> int:
    from universal_ai_tools.audit import read_records

    service = load_config_file(_config_path(options))
    if service.audit_path is None:
        return 1
    print(f"# {service.audit_path}")
    for record in read_records(service.audit_path, limit=options.limit):
        print(json.dumps(record, ensure_ascii=False))
    return 0


# doctor


class _Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def check(self, name: str, test: Callable[[], str]) -> bool:
        try:
            detail = test()
        except _Warn as warning:
            self.warnings += 1
            print(f"  WARN  {name}: {warning}")
            return True
        except Exception as exc:
            self.failures += 1
            print(f"  FAIL  {name}: {exc}")
            return False
        print(f"  ok    {name}{': ' + detail if detail else ''}")
        return True


class _Warn(Exception):
    pass


def _doctor(options: argparse.Namespace) -> int:
    from universal_ai_tools import clients
    from universal_ai_tools.server import capability_summary, check_privileges

    report = _Report()
    path = _config_path(options)
    print(f"{APP_NAME} {__version__} doctor\n")

    def python() -> str:
        problem = _environment_problem()
        if problem:
            raise RuntimeError(problem)
        return f"{platform.python_version()} at {sys.executable}"

    report.check("Python", python)
    report.check("dependencies", _dependencies)
    report.check("platform", lambda: platform.platform(terse=True))
    report.check("no API keys needed", lambda: "the server never calls a model API")
    service_box: list[Any] = []

    def load() -> str:
        service_box.append(load_config_file(path))
        return str(path)

    if not report.check("config file", load):
        print("\nRun: universal-ai-tools setup --workspace <project directory>")
        return 1
    service = service_box[0]
    report.check("workspace", lambda: str(service.workspace_root))
    report.check(
        "capabilities",
        lambda: ", ".join(f"{k}={v}" for k, v in capability_summary(service).items()),
    )

    def privileges() -> str:
        check_privileges(service)
        return ""

    report.check("not running as administrator", privileges)

    def audit() -> str:
        from universal_ai_tools.audit import AuditLog

        AuditLog(service.audit_path, max_bytes=service.audit_max_bytes).record(
            "doctor", {"version": __version__}
        )
        return str(service.audit_path)

    report.check("audit log writable", audit)

    def approval() -> str:
        from universal_ai_tools.approval import desktop_dialog_available

        dialog = desktop_dialog_available()
        channel = service.approval.channel
        if channel == "desktop_dialog" and not dialog:
            raise RuntimeError("channel is desktop_dialog but no desktop dialog can be shown")
        return (
            f"channel={channel}; Windows dialog {'available' if dialog else 'unavailable'}; "
            "the client's own dialog is used when it supports MCP elicitation"
        )

    report.check("approval", approval)
    if service.screen.enabled or service.input.enabled:

        def screen() -> str:
            from universal_ai_tools.desktop.tools import default_backend

            backend = default_backend()
            rect = backend.screen_rect()
            bitmap = backend.capture(rect, 8, 8)
            return f"screen {rect.width}x{rect.height}; capture works ({len(bitmap.bgra)} bytes)"

        report.check("screen access", screen)

    def runtime() -> str:
        directory = clients.runtime_directory()
        package = clients.runtime_package(directory)
        if _installed_runtime() is None or package is None:
            raise _Warn(f"not installed in {directory}; run: {APP_NAME} install")
        source = clients.checkout_source()
        checkout = source / "src" / "universal_ai_tools" if source else None
        if checkout is not None and checkout.is_dir():
            if clients.package_digest(package) != clients.package_digest(checkout):
                raise _Warn(
                    f"{package} differs from {checkout}; run '{APP_NAME} install' to use the "
                    "current code"
                )
            return f"{directory} (same code as {checkout})"
        return str(directory)

    report.check("protected runtime", runtime)
    entry = _expected_entry(path)

    def isolated() -> str:
        if "-I" not in entry["args"]:
            raise RuntimeError("the registration this setup writes does not use python -I")
        return clients.display([entry["command"], *entry["args"]])

    report.check("isolated launch", isolated)

    def desktop() -> str:
        found = clients.desktop_config_path()
        if found is None:
            raise _Warn("Claude Desktop not found")
        registered = clients.desktop_entry(found)
        if registered is None:
            raise _Warn(f"not registered in {found}; run setup")
        problem = clients.registration_problem(registered)
        if problem:
            raise RuntimeError(f"the registration in {found} {problem}; run setup")
        if not clients.same_registration(registered, entry):
            raise _Warn(f"registered in {found}, but with a different command or config; run setup")
        return f"registered in {found}"

    report.check("Claude Desktop", desktop)

    def code() -> str:
        registrations = clients.code_registrations()
        unsafe = [
            f"{where} {problem}"
            for where, registered in (
                ("the user-scope registration", registrations.user),
                *((f"the local registration in {p}", e) for p, e in registrations.local.items()),
            )
            if (problem := clients.registration_problem(registered))
        ]
        if unsafe:
            raise RuntimeError("; ".join(unsafe) + "; run setup")
        problems = []
        if not clients.same_registration(registrations.user, entry):
            problems.append("no matching user-scope registration (run setup)")
        shadowing = [
            p for p, e in registrations.local.items() if not clients.same_registration(e, entry)
        ]
        if shadowing:
            problems.append(
                "different local registrations take precedence in: " + ", ".join(shadowing[:5])
            )
        if problems:
            raise _Warn("; ".join(problems))
        return "registered for all projects"

    report.check("Claude Code", code)

    def guard() -> str:
        handlers = clients.installed_guard(clients.claude_user_settings_path())
        if not handlers:
            raise _Warn(
                "not installed: Claude Code's own Bash, PowerShell, Write, and Edit tools are "
                f"checked only by Claude Code's own permissions; run: {APP_NAME} setup"
            )
        runtime = _installed_runtime()
        expected = clients.guard_hooks(str(runtime or sys.executable), path)
        found = {
            event: [
                {k: v for k, v in h.items() if not k.startswith("_")}
                for h in handlers
                if h["_event"] == event and h.get("_matcher") == "*"
            ]
            for event in clients.GUARD_EVENTS
        }
        for event, groups in expected.items():
            if found[event] != groups[0]["hooks"]:
                raise RuntimeError(
                    f"the {event} hook is missing or differs from what setup installs; run setup"
                )
        pre = next(h for h in handlers if h["_event"] == "PreToolUse")
        return _guard_self_test(pre, path)

    report.check("Claude Code guard", guard)
    if not options.no_handshake:
        report.check("live MCP handshake", lambda: _handshake(entry, cwd=service.workspace_root))
    print(
        f"\n{report.failures} failure(s), {report.warnings} warning(s)."
        + (
            ""
            if report.failures
            else ' Ask Claude: "Call get_platform_info from universal-ai-tools."'
        )
    )
    return 1 if report.failures else 0


def _handshake(entry: dict[str, Any], cwd: Path | None = None) -> str:
    """Start the server as the clients do, and talk MCP to it.

    In the workspace, as Claude Code starts it in a project, and with none of this shell's
    ``UNIVERSAL_AI_TOOLS_*`` variables, so only the registration's environment counts.
    """
    import anyio
    from mcp import Client, StdioServerParameters

    from universal_ai_tools.config import ENV_PREFIX

    environment = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIX)}

    async def run() -> str:
        params = StdioServerParameters(
            command=entry["command"],
            args=entry["args"],
            env={**environment, **entry["env"]},
            cwd=str(cwd) if cwd is not None else None,
        )
        async with Client(params, read_timeout_seconds=30) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            result = await client.call_tool("get_platform_info", {})
            if result.is_error:
                raise RuntimeError("get_platform_info failed")
            return f"{len(tools)} tools: {', '.join(tools)}"

    return anyio.run(run)


if __name__ == "__main__":
    main()
