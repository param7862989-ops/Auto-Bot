"""Stage 1 hardening: no general command execution, and refusal whenever a safeguard fails.

* ``run_command`` cannot be turned on: the config file refuses it, the CLI refuses it, and a
  configuration forced on in code still registers no such tool (``test_file_mode_server`` and
  ``test_no_subprocess`` cover that last layer over MCP and in a real process).
* A code-executing tool is asked about on every call, whatever the configured policy.
* When the audit log cannot be written, nothing runs that is not recorded first, and no
  result is returned that the log does not show; the server does not start.
* Environment mode cannot change files, and the server does not start where Python would
  import from the current directory (``test_settings``, ``test_server``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ElicitResult

from service_support import TEST_DIALOG_LIMITS, Dialog, service, write_config
from universal_ai_tools import cli
from universal_ai_tools import server as server_module
from universal_ai_tools.audit import AuditError, AuditLog
from universal_ai_tools.config import ConfigError
from universal_ai_tools.gate import Gate
from universal_ai_tools.settings import ENV_CONFIG, load_config_file, read_config_file
from universal_ai_tools.tools import (
    STRICT_INPUT,
    ActionDescription,
    ToolCallError,
    ToolRegistry,
    ToolRisk,
    ToolSpec,
    build_registry,
)

APPROVE = ElicitResult(action="accept", content={"decision": "approve"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "hello.txt").write_bytes(b"hello\n")
    return root


# No general command execution.


def test_the_config_file_refuses_the_terminal(workspace: Path, tmp_path: Path) -> None:
    path = write_config(tmp_path / "state", workspace, {"terminal": {"enabled": True}})

    with pytest.raises(ConfigError, match="disabled for safety"):
        load_config_file(path)


def test_setup_refuses_to_enable_the_terminal(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "state" / "config.toml"
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "setup",
                "--workspace",
                str(workspace),
                "--config",
                str(config_path),
                "--enable",
                "terminal",
                "--clients",
                "",
                "--yes",
            ]
        )

    assert exit_info.value.code == 2
    assert "disabled for safety" in capsys.readouterr().err
    assert not config_path.exists()


@pytest.mark.parametrize("command", [["enable", "terminal"], ["add-program", "python"]])
def test_the_config_command_cannot_turn_the_terminal_on(
    workspace: Path, tmp_path: Path, command: list[str]
) -> None:
    path = write_config(tmp_path / "state", workspace)
    before = path.read_bytes()

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["config", "--config", str(path), *command])

    assert exit_info.value.code == 2  # argparse: not a valid choice or command
    assert path.read_bytes() == before


def test_an_old_config_with_the_terminal_on_can_be_switched_off(
    workspace: Path, tmp_path: Path
) -> None:
    path = write_config(
        tmp_path / "state", workspace, {"terminal": {"enabled": True, "programs": ["python"]}}
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["config", "--config", str(path), "disable", "terminal"])

    assert exit_info.value.code == 0
    assert read_config_file(path).terminal.enabled is False
    load_config_file(path)  # now accepted


def test_no_registered_tool_can_execute_code(workspace: Path, tmp_path: Path) -> None:
    config = service(
        tmp_path / "state",
        workspace,
        {"filesystem": {"write": True}, "screen": {"enabled": True}, "input": {"enabled": True}},
    )

    registry = build_registry(config)

    assert "run_command" not in registry
    assert [spec.name for spec in registry if spec.risk is ToolRisk.EXECUTE] == []


# A code-executing tool is asked about on every call.


def _executing_spec(capability: str, runs: list[str]) -> ToolSpec[Any, Any]:
    from mcp.types import ToolAnnotations
    from pydantic import BaseModel

    class ProbeInput(BaseModel):
        model_config = STRICT_INPUT
        label: str = "x"

    class ProbeOutput(BaseModel):
        ok: bool = True

    def handler(arguments: ProbeInput) -> ProbeOutput:
        runs.append(arguments.label)
        return ProbeOutput()

    return ToolSpec(
        name="probe_exec",
        title="Probe exec",
        description="A stand-in for any tool that runs code.",
        input_model=ProbeInput,
        output_model=ProbeOutput,
        handler=handler,
        risk=ToolRisk.EXECUTE,
        annotations=ToolAnnotations(
            title="Probe exec", read_only_hint=False, destructive_hint=True
        ),
        capability=capability,
        describe=lambda a: ActionDescription(f"Run probe {a.label}", "Code runs."),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("capability", ["filesystem", "screen", "input", "terminal"])
async def test_code_execution_is_approved_afresh_every_time(
    workspace: Path, tmp_path: Path, capability: str
) -> None:
    config = service(
        tmp_path / "state",
        workspace,
        {
            "filesystem": {"write": True, "approval": "once_per_session"},
            "screen": {"approval": "once_per_session"},
            "input": {"approval": "once_per_session"},
        },
    )
    runs: list[str] = []
    spec = _executing_spec(capability, runs)
    dialog = Dialog("yes")
    gate = Gate(
        config,
        ToolRegistry([spec]),
        show_dialog=dialog,
        dialog_available=lambda: True,
        dialog_limits=TEST_DIALOG_LIMITS,
    )

    assert gate.policy_for(spec) == "every_call"
    for label in ("one", "two", "three"):
        await gate.call("probe_exec", {"label": label})

    assert runs == ["one", "two", "three"]
    assert len(dialog.asked) == 3
    assert not any("without asking again" in question for question in dialog.asked)


# The audit log cannot be written: nothing unrecorded runs or is returned.


class FailingAudit(AuditLog):
    """An audit log that cannot write the given events."""

    def __init__(self, path: Path, failing: set[str]) -> None:
        super().__init__(path)
        self.failing = failing

    def record(self, event: str, fields: Mapping[str, object], *, durable: bool = False) -> None:
        if event in self.failing:
            raise AuditError("cannot write the audit log: OSError")
        super().record(event, fields, durable=durable)


def _gate(config: Any, audit: AuditLog, dialog: Dialog | None = None) -> Gate:
    return Gate(
        config,
        build_registry(config),
        audit=audit,
        show_dialog=dialog or Dialog("yes"),
        dialog_available=lambda: True,
        dialog_limits=TEST_DIALOG_LIMITS,
    )


@pytest.mark.anyio
async def test_a_read_whose_record_fails_returns_nothing(workspace: Path, tmp_path: Path) -> None:
    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    gate = _gate(config, FailingAudit(tmp_path / "state" / "audit.jsonl", {"finished"}))

    with pytest.raises(ToolCallError, match="result withheld") as refused:
        await gate.call("read_file", {"path": "docs/hello.txt"})

    assert "hello" not in str(refused.value)


@pytest.mark.anyio
async def test_a_change_whose_start_cannot_be_recorded_does_not_happen(
    workspace: Path, tmp_path: Path
) -> None:
    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    dialog = Dialog("yes")
    gate = _gate(config, FailingAudit(tmp_path / "state" / "audit.jsonl", {"started"}), dialog)

    with pytest.raises(ToolCallError, match="audit log cannot be written"):
        await gate.call("write_file", {"path": "docs/new.txt", "content": "x"})

    assert len(dialog.asked) == 1  # approved by the human, and still not run
    assert not (workspace / "docs" / "new.txt").exists()


@pytest.mark.anyio
async def test_a_change_whose_end_cannot_be_recorded_reports_it(
    workspace: Path, tmp_path: Path
) -> None:
    """It already ran, after its durable 'started' record: the result says exactly that."""
    log = tmp_path / "state" / "audit.jsonl"
    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    gate = _gate(config, FailingAudit(log, {"finished"}))

    with pytest.raises(ToolCallError, match=r"ran .* but its result could not be recorded"):
        await gate.call("write_file", {"path": "docs/new.txt", "content": "x"})

    started = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in started] == ["started"]
    assert (workspace / "docs" / "new.txt").read_bytes() == b"x"


@pytest.mark.anyio
async def test_an_approval_question_that_cannot_be_recorded_is_not_asked(
    workspace: Path, tmp_path: Path
) -> None:
    class Ctx:
        protocol_version = "2026-07-28"
        request_id = "1"
        session = type("Session", (), {"can_send_request": False, "client_params": None})()
        client_capabilities = type(
            "Caps", (), {"elicitation": type("E", (), {"form": {}, "url": None})()}
        )()
        request_state: str | None = None
        input_responses: dict[str, object] | None = None

    config = service(tmp_path / "state", workspace, {"filesystem": {"write": True}})
    gate = _gate(config, FailingAudit(tmp_path / "state" / "audit.jsonl", {"approval_requested"}))

    with pytest.raises(ToolCallError, match="audit log cannot be written"):
        await gate.call("write_file", {"path": "docs/new.txt", "content": "x"}, Ctx())  # type: ignore[arg-type]
    assert not (workspace / "docs" / "new.txt").exists()


def test_the_server_does_not_start_without_an_audit_log(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_bytes(b"")
    path = write_config(
        tmp_path / "state",
        workspace,
        {"audit": {"path": str(blocked / "audit.jsonl")}},
    )
    monkeypatch.setenv(ENV_CONFIG, str(path))
    # The isolation check is tested in a real process (test_server); here pytest's own
    # import path would stop the start before the audit log is tried.
    monkeypatch.setattr(server_module, "current_directory_on_import_path", lambda: False)
    served: list[bool] = []
    monkeypatch.setattr(server_module, "create_server", lambda *a, **k: served.append(True))

    with pytest.raises(SystemExit) as exit_info:
        server_module.main()

    assert exit_info.value.code == 2
    assert "refusing to start" in capsys.readouterr().err
    assert served == []


def test_the_server_does_not_start_where_it_would_import_from_the_current_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "current_directory_on_import_path", lambda: True)

    with pytest.raises(ConfigError, match="python -I"):
        server_module.check_isolated_start()
