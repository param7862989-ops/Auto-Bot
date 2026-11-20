"""The gate: permission decisions, human approval over MCP, and the audit log.

Approval is tested over both MCP protocol generations. ``Client(server)`` negotiates
2026-07-28 (what Claude Code 2.1.319 negotiates): the server answers with an
``InputRequiredResult`` and the SDK client asks (``elicitation_callback``) and retries.
``Client(server, mode="legacy")`` negotiates 2025-11-25: the server sends
``elicitation/create`` while the call waits. The Windows dialog is replaced by ``Dialog``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.types import ElicitRequestFormParams, ElicitRequestParams, ElicitResult, TextContent

from service_support import Dialog, dialog_server_kwargs, service
from universal_ai_tools.audit import AuditLog, read_records
from universal_ai_tools.config import ENV_ENABLE_WRITE_TOOLS, ENV_WORKSPACE, load_config
from universal_ai_tools.server import create_server

APPROVE = ElicitResult(action="accept", content={"decision": "approve"})
DENY = ElicitResult(action="accept", content={"decision": "deny"})
WRITE = ("write_file", {"path": "docs/new.txt", "content": "made by Claude\n"})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    (path / "docs").mkdir(parents=True)
    (path / "docs" / "hello.txt").write_text("hello\n", encoding="utf-8")
    return path


def answering(answer: ElicitResult) -> tuple[Any, list[ElicitRequestParams]]:
    asked: list[ElicitRequestParams] = []

    async def elicit(context: Any, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params)
        return answer

    return elicit, asked


def write_service(tmp_path: Path, workspace: Path, **approval: object) -> Any:
    sections: dict[str, dict[str, Any]] = {"filesystem": {"write": True}}
    if approval:
        sections["approval"] = dict(approval)
    return service(tmp_path / "state", workspace, sections)


def text(result: Any) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


def audit_events(tmp_path: Path) -> list[dict[str, object]]:
    return read_records(tmp_path / "state" / "audit.jsonl", limit=1000)


# The client's own dialog (MCP elicitation)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_approved_write_runs(workspace: Path, tmp_path: Path, mode: str) -> None:
    elicit, asked = answering(APPROVE)
    dialog = Dialog("no")
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(dialog))

    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = await client.call_tool(*WRITE)

    assert result.is_error is False
    assert (workspace / "docs" / "new.txt").read_bytes() == b"made by Claude\n"
    (question,) = asked
    assert isinstance(question, ElicitRequestFormParams)
    assert "Create new file 'docs/new.txt'" in question.message
    assert "made by Claude" in question.message
    assert question.requested_schema["properties"]["decision"]["enum"] == ["approve", "deny"]
    assert dialog.asked == []  # the client's dialog was used, not the Windows one


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        (DENY, "denied"),
        (ElicitResult(action="decline"), "denied"),
        (ElicitResult(action="cancel"), "cancelled"),
        (ElicitResult(action="accept", content={"decision": "approved"}), "invalid"),
        (ElicitResult(action="accept", content={"decision": "approve", "x": 1}), "invalid"),
        (ElicitResult(action="accept", content={}), "invalid"),
    ],
    ids=["deny", "decline", "cancel", "wrong-value", "extra-field", "empty"],
)
async def test_anything_but_approval_refuses(
    workspace: Path, tmp_path: Path, mode: str, answer: ElicitResult, decision: str
) -> None:
    elicit, asked = answering(answer)
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(Dialog()))

    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = await client.call_tool(*WRITE)

    assert result.is_error is True
    assert "was not approved" in text(result)
    assert not (workspace / "docs" / "new.txt").exists()
    assert len(asked) == 1
    refused = [r for r in audit_events(tmp_path) if r["event"] == "refused"]
    assert refused[-1]["approval"] == {"decision": decision, "channel": "client"}


@pytest.mark.anyio
async def test_read_only_tools_never_ask(workspace: Path, tmp_path: Path) -> None:
    elicit, asked = answering(DENY)
    dialog = Dialog("no")
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(dialog))

    async with Client(server, elicitation_callback=elicit) as client:
        read = await client.call_tool("read_file", {"path": "docs/hello.txt"})
        listed = await client.call_tool("list_files", {})

    assert read.is_error is False
    assert listed.is_error is False
    assert asked == []
    assert dialog.asked == []


# The Windows dialog, and no channel at all


@pytest.mark.anyio
@pytest.mark.parametrize(("answer", "ran"), [("yes", True), ("no", False), ("dismissed", False)])
async def test_desktop_dialog_when_the_client_cannot_ask(
    workspace: Path, tmp_path: Path, answer: Any, ran: bool
) -> None:
    dialog = Dialog(answer)
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(dialog))

    async with Client(server) as client:  # no elicitation_callback: no elicitation capability
        result = await client.call_tool(*WRITE)

    assert result.is_error is not ran
    assert (workspace / "docs" / "new.txt").exists() is ran
    (question,) = dialog.asked
    assert "Create new file 'docs/new.txt'" in question


@pytest.mark.anyio
async def test_desktop_dialog_channel_is_used_even_when_the_client_could_ask(
    workspace: Path, tmp_path: Path
) -> None:
    elicit, asked = answering(APPROVE)
    dialog = Dialog("no")
    config = write_service(tmp_path, workspace, channel="desktop_dialog")
    server = create_server(config, **dialog_server_kwargs(dialog))

    async with Client(server, elicitation_callback=elicit) as client:
        result = await client.call_tool(*WRITE)

    assert result.is_error is True
    assert asked == []
    assert len(dialog.asked) == 1


@pytest.mark.anyio
async def test_no_channel_fails_closed(workspace: Path, tmp_path: Path) -> None:
    server = create_server(write_service(tmp_path, workspace), dialog_available=lambda: False)

    async with Client(server) as client:
        result = await client.call_tool(*WRITE)

    assert result.is_error is True
    assert "no approval dialog could be shown" in text(result)
    assert not (workspace / "docs" / "new.txt").exists()


@pytest.mark.anyio
async def test_client_channel_does_not_fall_back_to_the_desktop(
    workspace: Path, tmp_path: Path
) -> None:
    dialog = Dialog("yes")
    config = write_service(tmp_path, workspace, channel="client")
    server = create_server(config, **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        result = await client.call_tool(*WRITE)

    assert result.is_error is True
    assert dialog.asked == []


@pytest.mark.anyio
async def test_silence_is_no(workspace: Path, tmp_path: Path) -> None:
    async def never(context: Any, params: ElicitRequestParams) -> ElicitResult:
        await anyio.sleep(60)
        return APPROVE

    config = write_service(tmp_path, workspace, timeout_seconds=5)
    server = create_server(config, **dialog_server_kwargs(Dialog()))

    async with Client(server, mode="legacy", elicitation_callback=never) as client:
        with anyio.fail_after(30):
            result = await client.call_tool(*WRITE)

    assert result.is_error is True
    assert "no answer came in time" in text(result)
    assert not (workspace / "docs" / "new.txt").exists()


# Session grants


@pytest.mark.anyio
async def test_once_per_session_asks_once(workspace: Path, tmp_path: Path) -> None:
    dialog = Dialog("yes")
    config = service(
        tmp_path / "state",
        workspace,
        {"filesystem": {"write": True, "approval": "once_per_session"}},
    )
    server = create_server(config, **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        for name in ("a", "b", "c"):
            result = await client.call_tool(
                "write_file", {"path": f"docs/{name}.txt", "content": ""}
            )
            assert result.is_error is False

    assert len(dialog.asked) == 1
    assert "for up to 30 minutes" in dialog.asked[0]
    approvals = [r.get("approval") for r in audit_events(tmp_path) if r["event"] == "started"]
    assert [a["channel"] for a in approvals] == ["desktop_dialog", "session_grant", "session_grant"]  # type: ignore[index]


@pytest.mark.anyio
async def test_a_session_grant_expires(workspace: Path, tmp_path: Path) -> None:
    from universal_ai_tools.gate import Gate
    from universal_ai_tools.tools import build_registry

    config = service(
        tmp_path / "state",
        workspace,
        {
            "filesystem": {"write": True, "approval": "once_per_session"},
            "approval": {"session_grant_minutes": 10},
        },
    )
    now = [1000.0]
    dialog = Dialog("yes")
    gate = Gate(
        config,
        build_registry(config),
        show_dialog=dialog,
        dialog_available=lambda: True,
        clock=lambda: now[0],
    )

    await gate.call("write_file", {"path": "docs/a.txt", "content": ""})
    now[0] += 9 * 60
    await gate.call("write_file", {"path": "docs/b.txt", "content": ""})
    assert len(dialog.asked) == 1
    now[0] += 2 * 60
    await gate.call("write_file", {"path": "docs/c.txt", "content": ""})
    assert len(dialog.asked) == 2


@pytest.mark.anyio
async def test_every_call_asks_every_time(workspace: Path, tmp_path: Path) -> None:
    dialog = Dialog("yes")
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        for name in ("a", "b"):
            await client.call_tool("write_file", {"path": f"docs/{name}.txt", "content": ""})

    assert len(dialog.asked) == 2


@pytest.mark.anyio
async def test_a_denied_session_grant_grants_nothing(workspace: Path, tmp_path: Path) -> None:
    dialog = Dialog("no")
    config = service(
        tmp_path / "state",
        workspace,
        {"filesystem": {"write": True, "approval": "once_per_session"}},
    )
    server = create_server(config, **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        first = await client.call_tool("write_file", {"path": "docs/a.txt", "content": ""})
        dialog.answer = "no"
        second = await client.call_tool("write_file", {"path": "docs/b.txt", "content": ""})

    assert first.is_error
    assert second.is_error
    assert len(dialog.asked) == 2


# The 2026-07-28 round trip cannot be forged or replayed


@pytest.mark.anyio
async def test_an_answer_for_another_call_approves_nothing(workspace: Path, tmp_path: Path) -> None:
    """A retry whose arguments differ from the call that was asked about is refused."""
    from universal_ai_tools.gate import Gate
    from universal_ai_tools.tools import build_registry

    config = write_service(tmp_path, workspace)
    gate = Gate(config, build_registry(config))

    class Session:
        can_send_request = False
        client_params = None

    class Ctx:
        protocol_version = "2026-07-28"
        session = Session()
        client_capabilities = type(
            "Caps", (), {"elicitation": type("E", (), {"form": {}, "url": None})()}
        )()
        request_state: str | None = None
        input_responses: dict[str, object] | None = None

    ctx = Ctx()
    asked = await gate.call("write_file", {"path": "docs/one.txt", "content": "1"}, ctx)  # type: ignore[arg-type]
    state = asked.request_state  # type: ignore[union-attr]
    ctx.request_state = state
    ctx.input_responses = {"approval": APPROVE}

    with pytest.raises(Exception, match="no longer pending"):
        await gate.call("write_file", {"path": "docs/other.txt", "content": "1"}, ctx)  # type: ignore[arg-type]
    # The pending question was used up by the mismatched retry, so a replay fails too.
    with pytest.raises(Exception, match="no longer pending"):
        await gate.call("write_file", {"path": "docs/one.txt", "content": "1"}, ctx)  # type: ignore[arg-type]
    ctx.request_state = json.dumps({"approval": "forged"})
    with pytest.raises(Exception, match="no longer pending"):
        await gate.call("write_file", {"path": "docs/one.txt", "content": "1"}, ctx)  # type: ignore[arg-type]
    assert not (workspace / "docs" / "one.txt").exists()
    assert not (workspace / "docs" / "other.txt").exists()


# The audit log


@pytest.mark.anyio
async def test_audit_records_never_hold_contents(workspace: Path, tmp_path: Path) -> None:
    server = create_server(write_service(tmp_path, workspace), **dialog_server_kwargs(Dialog()))
    secret = "s3cret-content-that-must-not-be-logged"  # noqa: S105 - a marker, not a password

    async with Client(server) as client:
        await client.call_tool("write_file", {"path": "docs/x.txt", "content": secret})
        await client.call_tool("read_file", {"path": "docs/x.txt"})

    log = (tmp_path / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert secret not in log
    events = [(r["event"], r["tool"]) for r in audit_events(tmp_path)]
    assert events == [
        ("started", "write_file"),
        ("finished", "write_file"),
        ("finished", "read_file"),
    ]
    started = audit_events(tmp_path)[0]
    assert started["target"] == {"path": "docs/x.txt", "bytes": len(secret)}
    assert started["approval"] == {"decision": "approved", "channel": "desktop_dialog"}


@pytest.mark.anyio
async def test_a_call_that_cannot_be_audited_does_not_run(workspace: Path, tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    server = create_server(
        write_service(tmp_path, workspace),
        audit=AuditLog(blocked / "audit.jsonl"),
        **dialog_server_kwargs(Dialog()),
    )

    async with Client(server) as client:
        write = await client.call_tool(*WRITE)
        read = await client.call_tool("read_file", {"path": "docs/hello.txt"})

    assert write.is_error is True
    assert "audit log cannot be written" in text(write)
    assert not (workspace / "docs" / "new.txt").exists()
    # Fail closed for reads too: nothing is returned that the audit log does not show.
    assert read.is_error is True
    assert "audit log cannot be written" in text(read)
    assert "hello" not in text(read)


# Environment mode is the server as it was


@pytest.mark.anyio
async def test_environment_mode_has_no_gate_approvals_and_no_audit(
    workspace: Path, tmp_path: Path
) -> None:
    dialog = Dialog("no")
    config = load_config({ENV_WORKSPACE: str(workspace), ENV_ENABLE_WRITE_TOOLS: "true"})
    server = create_server(config, **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        result = await client.call_tool(*WRITE)

    assert names == ["get_platform_info", "list_files", "read_file", "search_files", "write_file"]
    assert result.is_error is False
    assert dialog.asked == []
