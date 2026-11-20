"""The Docker sandbox (run_in_sandbox).

Most tests replace the docker process, so they run without Docker. The last test uses the
real Docker engine, and runs only when Docker is running, its program is trusted in a test
config, and the image is present (otherwise it is skipped, and says why).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from service_support import Dialog, dialog_server_kwargs, service
from universal_ai_tools import cli, sandbox
from universal_ai_tools.sandbox import SandboxError, SandboxLimits
from universal_ai_tools.server import NO_COMMANDS_NOTE, create_server
from universal_ai_tools.settings import read_config_file
from universal_ai_tools.tools import ToolRisk, build_registry

LIMITS = SandboxLimits(
    image="python:3.13-slim",
    writable=False,
    network=False,
    cpus=2,
    memory_mb=2048,
    max_output_bytes=100,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def arguments(**changes: Any) -> list[str]:
    limits = SandboxLimits(**{**LIMITS.__dict__, **changes})
    return sandbox.docker_arguments(
        Path("C:/docker.exe"), limits, Path("C:/work space"), "uat-x", "pytest -q"
    )


def option(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


# The command line.


def test_the_container_is_locked_down() -> None:
    args = arguments()
    assert args[1:3] == ["run", "--rm"]
    assert option(args, "--network") == ["none"]
    assert option(args, "--cap-drop") == ["ALL"]
    assert option(args, "--security-opt") == ["no-new-privileges"]
    assert option(args, "--pull") == ["never"]
    assert option(args, "--user") == ["1000:1000"]
    assert option(args, "--pids-limit") == ["256"]
    assert option(args, "--memory") == option(args, "--memory-swap") == ["2048m"]
    assert "--read-only" in args
    assert "--privileged" not in args
    assert option(args, "--mount") == [
        f"type=bind,source={Path('C:/work space')},target=/work,readonly"
    ]
    assert args[-3:] == ["python:3.13-slim", "-c", "pytest -q"]
    # Only the workspace is mounted; nothing else from the PC.
    assert len(option(args, "--mount")) == 1
    assert "-v" not in args
    assert "--volume" not in args


def test_network_and_writing_only_when_configured() -> None:
    args = arguments(network=True, writable=True)
    assert option(args, "--network") == ["bridge"]
    assert not option(args, "--mount")[0].endswith(",readonly")


def test_the_command_is_one_argument_after_sh_c() -> None:
    args = sandbox.docker_arguments(
        Path("docker"), LIMITS, Path("C:/w"), "uat-x", "echo hi; rm -rf / --no-preserve-root"
    )
    assert args[-3:] == ["python:3.13-slim", "-c", "echo hi; rm -rf / --no-preserve-root"]
    assert option(args, "--entrypoint") == ["/bin/sh"]


@pytest.mark.parametrize("path", ["C:/a,b", "C:/a=b", 'C:/a"b'])
def test_a_workspace_docker_cannot_mount_safely_is_refused(path: str) -> None:
    with pytest.raises(SandboxError, match="cannot mount"):
        sandbox.docker_arguments(Path("docker"), LIMITS, Path(path), "uat-x", "true")


@pytest.mark.parametrize(("command", "message"), [("  ", "empty"), ("a\x00b", "NUL"),
                                                  ("x" * 4001, "longer")])  # fmt: skip
def test_bad_commands_are_refused(command: str, message: str) -> None:
    with pytest.raises(SandboxError, match=message):
        sandbox.check_command(command)


# The trusted docker program.


def test_docker_is_trusted_only_by_its_fingerprint(tmp_path: Path) -> None:
    docker = tmp_path / "docker.exe"
    docker.write_bytes(b"original")
    digest = sandbox.fingerprint(docker)
    assert sandbox.verified_docker(str(docker), digest) == docker
    docker.write_bytes(b"replaced")
    with pytest.raises(SandboxError, match="has changed"):
        sandbox.verified_docker(str(docker), digest)
    with pytest.raises(SandboxError, match="sandbox trust"):
        sandbox.verified_docker("", "")
    with pytest.raises(SandboxError, match="missing"):
        sandbox.verified_docker(str(tmp_path / "gone.exe"), digest)


# Running, with the docker process replaced.


class FakeProcess:
    def __init__(self, output: bytes, error: bytes, code: int, hang: bool = False) -> None:
        self.output, self.error, self.returncode, self.hang = output, error, code, hang
        self.calls = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.calls += 1
        if self.hang and self.calls == 1:
            raise subprocess.TimeoutExpired("docker", timeout or 0)
        return self.output, self.error

    def kill(self) -> None:
        pass


def test_output_is_capped(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    process = FakeProcess(b"o" * 500, b"e", 3)
    started: list[list[str]] = []

    def popen(args: list[str], **_: Any) -> FakeProcess:
        started.append(args)
        return process

    monkeypatch.setattr("universal_ai_tools.sandbox.subprocess.Popen", popen)
    done = sandbox.run(Path("docker"), LIMITS, workspace, "pytest", 10)
    assert (done.exit_code, done.timed_out) == (3, False)
    assert len(done.stdout) == 100
    assert done.stdout_truncated is True
    assert done.stderr == "e"
    assert started[0][-1] == "pytest"


def test_a_slow_command_is_killed(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    killed: list[str] = []
    monkeypatch.setattr(
        "universal_ai_tools.sandbox.subprocess.Popen",
        lambda *a, **k: FakeProcess(b"", b"", 0, True),
    )
    monkeypatch.setattr(sandbox, "kill", lambda docker, name: killed.append(name))
    done = sandbox.run(Path("docker"), LIMITS, workspace, "sleep 999", 1)
    assert done.timed_out is True
    assert done.exit_code is None
    assert killed == [done.container]


def test_the_docker_process_gets_no_server_settings(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    seen: dict[str, Any] = {}

    def popen(args: list[str], **kwargs: Any) -> FakeProcess:
        seen.update(kwargs)
        return FakeProcess(b"", b"", 0)

    monkeypatch.setenv("UNIVERSAL_AI_TOOLS_CONFIG", "C:/secret.toml")
    monkeypatch.setattr("universal_ai_tools.sandbox.subprocess.Popen", popen)
    sandbox.run(Path("docker"), LIMITS, workspace, "true", 5)
    assert "UNIVERSAL_AI_TOOLS_CONFIG" not in seen["env"]
    assert seen.get("shell") in (None, False)


# Through the server.


def sandbox_config(state: Path, workspace: Path, docker: Path, **changes: Any) -> Any:
    return service(
        state,
        workspace,
        {
            "sandbox": {
                "enabled": True,
                "docker": str(docker),
                "docker_sha256": sandbox.fingerprint(docker),
                **changes,
            }
        },
    )


@pytest.fixture
def docker(tmp_path: Path) -> Path:
    path = tmp_path / "docker.exe"
    path.write_bytes(b"trusted docker")
    return path


@pytest.mark.anyio
async def test_every_command_is_asked_shown_exactly_and_recorded_redacted(
    tmp_path: Path, workspace: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    def fake_run(
        docker_path: Path, limits: SandboxLimits, mount: Path, command: str, timeout: float
    ) -> sandbox.SandboxRun:
        ran.append(command)
        return sandbox.SandboxRun(0, "ok \x1b[31m", "", False, False, False, "uat-x")

    monkeypatch.setattr(sandbox, "run", fake_run)
    state = tmp_path / "state"
    dialog = Dialog("yes")
    server = create_server(sandbox_config(state, workspace, docker), **dialog_server_kwargs(dialog))
    command = "TOKEN=abc123secret python -m pytest -q"
    async with Client(server) as client:
        first = await client.call_tool("run_in_sandbox", {"command": command})
        second = await client.call_tool("run_in_sandbox", {"command": command})
        dialog.answer = "no"
        third = await client.call_tool("run_in_sandbox", {"command": command})

    assert first.is_error is False
    assert second.is_error is False
    assert third.is_error is True
    assert len(dialog.asked) == 3  # a fresh approval every time
    assert command in dialog.asked[0]
    assert "read (not change) only the workspace" in " ".join(dialog.asked[0].split())
    assert ran == [command, command]
    result = json.loads(first.content[0].text)  # type: ignore[union-attr]
    assert "\x1b" not in result["stdout"]
    log = (state / "audit.jsonl").read_text(encoding="utf-8")
    assert "abc123secret" not in log
    assert "TOKEN=<redacted>" in log


@pytest.mark.anyio
async def test_a_changed_docker_program_runs_nothing(
    tmp_path: Path, workspace: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(sandbox, "run", lambda *a: ran.append("ran"))
    server = create_server(
        sandbox_config(tmp_path / "state", workspace, docker), **dialog_server_kwargs(Dialog("yes"))
    )
    docker.write_bytes(b"something else")
    async with Client(server) as client:
        result = await client.call_tool("run_in_sandbox", {"command": "true"})
    assert result.is_error is True
    assert "has changed" in result.content[0].text  # type: ignore[union-attr]
    assert ran == []


def test_the_tool_exists_only_when_enabled(tmp_path: Path, workspace: Path, docker: Path) -> None:
    off = build_registry(service(tmp_path / "s1", workspace))
    assert "run_in_sandbox" not in off.names
    on = build_registry(sandbox_config(tmp_path / "s2", workspace, docker))
    spec = on.get("run_in_sandbox")
    assert spec.risk is ToolRisk.EXECUTE
    assert spec.grant is None


@pytest.mark.anyio
async def test_the_instructions_say_where_commands_run(
    tmp_path: Path, workspace: Path, docker: Path
) -> None:
    async with Client(create_server(sandbox_config(tmp_path / "s", workspace, docker))) as client:
        on = client.instructions or ""
    async with Client(create_server(service(tmp_path / "t", workspace))) as client:
        off = client.instructions or ""
    assert "run_in_sandbox" in on
    assert NO_COMMANDS_NOTE not in on
    assert NO_COMMANDS_NOTE in off


def test_the_cli_trusts_a_docker_program(tmp_path: Path, workspace: Path, docker: Path) -> None:
    config = tmp_path / "state" / "config.toml"
    config.parent.mkdir()
    config.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["sandbox", "--config", str(config), "trust", "--docker", str(docker), "--yes"])
    assert exit_info.value.code == 0
    trusted = read_config_file(config).sandbox
    assert trusted.docker == str(docker.resolve())
    assert trusted.docker_sha256 == sandbox.fingerprint(docker)
    assert trusted.enabled is False  # trusting docker does not turn the sandbox on


# The real Docker engine, when available.


def _real_docker() -> Path:
    docker = sandbox.find_docker()
    if docker is None:
        pytest.skip("Docker is not installed")
    try:
        sandbox.docker_check(docker, LIMITS.image)
    except SandboxError as exc:
        pytest.skip(f"Docker is not ready: {exc}")
    return docker


def test_the_real_container_is_confined(workspace: Path) -> None:
    docker = _real_docker()
    (workspace / "hello.txt").write_text("hi", encoding="utf-8")
    limits = SandboxLimits(**{**LIMITS.__dict__, "max_output_bytes": 10_000})
    script = (
        "cat /work/hello.txt; echo; id -u; "
        "touch /work/new 2>/dev/null && echo WROTE || echo READONLY; "
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\" "
        "2>/dev/null && echo NET || echo NONET; "
        "ls /mnt/c 2>/dev/null && echo HOSTFS || echo NOHOSTFS"
    )
    done = sandbox.run(docker, limits, workspace, script, 120)
    lines = done.stdout.split()
    assert done.exit_code == 0, done.stderr
    assert lines[:2] == ["hi", "1000"]
    assert "READONLY" in lines
    assert "NONET" in lines
    assert "NOHOSTFS" in lines
    assert not (workspace / "new").exists()
