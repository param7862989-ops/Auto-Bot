"""Every call is in the audit log, read-only calls included, with what it touched and why.

Required in every record: when, which session and call, which tool and kind of operation,
the target, the permission decision, the outcome, and for failures an error class. Never:
file contents, search patterns, typed text, or credentials in command arguments.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import ElicitRequestParams, ElicitResult

from service_support import (
    TEST_DIALOG_LIMITS,
    Dialog,
    dialog_server_kwargs,
    registry_with_terminal,
    service,
)
from universal_ai_tools.audit import AuditLog, read_records
from universal_ai_tools.gate import Gate
from universal_ai_tools.server import create_server
from universal_ai_tools.terminal import REDACTED, redacted_arguments
from universal_ai_tools.tools import build_registry

SECRET = "correct-horse-battery-staple-4711"  # noqa: S105 - a marker that must not be logged
REQUIRED = {
    "ts",
    "event",
    "pid",
    "session",
    "call",
    "tool",
    "operation",
    "risk",
    "target",
    "arguments_sha256",
    "permission",
    "status",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "secret.txt").write_bytes(f"password = {SECRET}\n".encode())
    (root / ".env").write_bytes(b"TOKEN=x\n")
    return root


def config(tmp_path: Path, workspace: Path, **sections: dict[str, Any]) -> Any:
    return service(tmp_path / "state", workspace, {"filesystem": {"write": True}, **sections})


def log_text(tmp_path: Path) -> str:
    return (tmp_path / "state" / "audit.jsonl").read_text(encoding="utf-8")


def records(tmp_path: Path) -> list[dict[str, Any]]:
    found = read_records(tmp_path / "state" / "audit.jsonl", limit=1000)
    for record in found:
        missing = REQUIRED - set(record)
        assert not missing, (record.get("tool"), missing)
        datetime.fromisoformat(str(record["ts"]))
    return found


@pytest.mark.anyio
async def test_reads_searches_and_listings_are_recorded(workspace: Path, tmp_path: Path) -> None:
    server = create_server(config(tmp_path, workspace), **dialog_server_kwargs(Dialog()))

    async with Client(server) as client:
        await client.call_tool("read_file", {"path": "docs/secret.txt"})
        await client.call_tool("search_files", {"pattern": SECRET, "path": "docs"})
        await client.call_tool("list_files", {"path": "docs", "recursive": True})

    read, search, listing = records(tmp_path)
    assert (read["event"], read["tool"], read["operation"]) == ("finished", "read_file", "read")
    assert read["target"] == {"path": "docs/secret.txt"}
    assert read["status"] == "succeeded"
    assert read["permission"] == "allowed"
    assert read["risk"] == "read_only"
    assert read["request"] is not None
    assert search["operation"] == "search"
    assert search["target"] == {
        "path": "docs",
        "pattern_chars": len(SECRET),
        "case_sensitive": True,
    }
    assert listing["target"] == {"path": "docs", "recursive": True}
    assert read["session"] == search["session"] == listing["session"]
    assert len({read["call"], search["call"], listing["call"]}) == 3
    assert SECRET not in log_text(tmp_path)  # neither the content nor the pattern


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "error_class"),
    [
        ("docs/missing.txt", "not_found"),
        (".env", "policy_refused"),
        ("../outside.txt", "invalid_path"),
        ("docs", "filesystem_error"),
    ],
)
async def test_failed_reads_are_recorded_with_their_class(
    workspace: Path, tmp_path: Path, path: str, error_class: str
) -> None:
    server = create_server(config(tmp_path, workspace), **dialog_server_kwargs(Dialog()))

    async with Client(server) as client:
        result = await client.call_tool("read_file", {"path": path})

    assert result.is_error is True
    (record,) = records(tmp_path)
    assert record["status"] == "failed"
    assert record["error_class"] == error_class
    assert record["target"] == {"path": path}
    assert record["error"]


@pytest.mark.anyio
async def test_an_approved_write_is_recorded_before_and_after(
    workspace: Path, tmp_path: Path
) -> None:
    server = create_server(config(tmp_path, workspace), **dialog_server_kwargs(Dialog("yes")))

    async with Client(server) as client:
        await client.call_tool("write_file", {"path": "docs/new.txt", "content": SECRET})

    started, finished = records(tmp_path)
    for record in (started, finished):
        assert record["operation"] == "create_file"
        assert record["permission"] == "approval_required"
        assert record["policy"] == "every_call"
        assert record["approval"] == {"decision": "approved", "channel": "desktop_dialog"}
        assert record["target"] == {"path": "docs/new.txt", "bytes": len(SECRET)}
    assert started["arguments_sha256"] == finished["arguments_sha256"]
    assert (started["event"], finished["event"], finished["status"]) == (
        "started",
        "finished",
        "succeeded",
    )
    assert SECRET not in log_text(tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        (ElicitResult(action="accept", content={"decision": "deny"}), "denied"),
        (ElicitResult(action="decline"), "denied"),
        (ElicitResult(action="cancel"), "cancelled"),
    ],
    ids=["denied", "declined", "cancelled"],
)
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_denied_and_cancelled_approvals_are_recorded(
    workspace: Path, tmp_path: Path, answer: ElicitResult, decision: str, mode: str
) -> None:
    async def elicit(context: Any, params: ElicitRequestParams) -> ElicitResult:
        return answer

    server = create_server(config(tmp_path, workspace), **dialog_server_kwargs(Dialog()))

    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        await client.call_tool("write_file", {"path": "docs/new.txt", "content": SECRET})

    found = records(tmp_path)
    refused = found[-1]
    assert refused["event"] == "refused"
    assert refused["status"] == "refused"
    assert refused["approval"] == {"decision": decision, "channel": "client"}
    assert refused["error_class"] == "not_approved"
    assert refused["target"] == {"path": "docs/new.txt", "bytes": len(SECRET)}
    if mode == "auto":  # 2026-07-28: the question went out first, and is recorded too
        assert found[0]["event"] == "approval_requested"
        assert found[0]["arguments_sha256"] == refused["arguments_sha256"]
    assert not any(r["event"] == "started" for r in found)
    assert SECRET not in log_text(tmp_path)


@pytest.mark.anyio
async def test_a_policy_refusal_of_a_write_is_recorded(workspace: Path, tmp_path: Path) -> None:
    server = create_server(config(tmp_path, workspace), **dialog_server_kwargs(Dialog("yes")))

    async with Client(server) as client:
        result = await client.call_tool("write_file", {"path": "docs/.env.local", "content": "x"})

    assert result.is_error is True
    finished = records(tmp_path)[-1]
    assert finished["status"] == "failed"
    assert finished["error_class"] == "policy_refused"


@pytest.mark.anyio
async def test_calls_refused_before_they_start_are_recorded(
    workspace: Path, tmp_path: Path
) -> None:
    service_config = config(tmp_path, workspace)
    gate = Gate(
        service_config,
        build_registry(service_config),
        audit=AuditLog(tmp_path / "state" / "audit.jsonl"),
    )

    with pytest.raises(Exception, match="unknown tool"):
        await gate.call("delete_everything", {})
    with pytest.raises(Exception, match="invalid arguments"):
        await gate.call("read_file", {"path": 5})

    unknown, invalid = read_records(tmp_path / "state" / "audit.jsonl")
    assert (unknown["event"], unknown["error_class"]) == ("refused", "unknown_tool")
    assert unknown["tool"] == "delete_everything"
    assert (invalid["event"], invalid["error_class"]) == ("refused", "invalid_arguments")
    assert invalid["tool"] == "read_file"


def test_credentials_in_command_arguments_are_redacted() -> None:
    args = [
        "--token=abc123",
        "--password",
        "hunter2",
        "API_KEY=sk-live",
        "https://user:pa55@example.com/repo",
        "/auth:xyz",
        "--verbose",
        "plain",
    ]

    assert redacted_arguments(args) == [
        f"--token={REDACTED}",
        "--password",
        REDACTED,
        f"API_KEY={REDACTED}",
        f"https://user:{REDACTED}@example.com/repo",
        f"/auth:{REDACTED}",
        "--verbose",
        "plain",
    ]


@pytest.mark.anyio
async def test_commands_are_recorded_without_credentials_but_shown_in_full(
    workspace: Path, tmp_path: Path
) -> None:
    dialog = Dialog("yes")
    service_config = config(tmp_path, workspace)
    gate = Gate(
        service_config,
        registry_with_terminal(service_config),
        audit=AuditLog(tmp_path / "state" / "audit.jsonl"),
        show_dialog=dialog,
        dialog_available=lambda: True,
        dialog_limits=TEST_DIALOG_LIMITS,
    )

    await gate.call(
        "run_command", {"program": "python", "args": ["-c", "pass", f"--token={SECRET}"]}
    )

    started, finished = records(tmp_path)
    assert started["operation"] == "execute"
    assert started["target"]["args"] == ["-c", "pass", f"--token={REDACTED}"]
    assert finished["status"] == "succeeded"
    assert SECRET not in log_text(tmp_path)
    assert SECRET in dialog.asked[0]  # the human still sees exactly what will run
