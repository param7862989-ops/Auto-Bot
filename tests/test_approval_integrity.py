"""The approval dialog shows the complete request, exactly, and only that request runs.

The rendering is checked by reading it back: ``shown_value``, ``shown_text`` and
``shown_arguments`` parse a rendered message the way a careful reader would and must
recover the original value exactly. If two different values could be displayed the same
way, one of them would fail to come back.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import ElicitRequestParams, ElicitResult, TextContent

from service_support import (
    PYTHON,
    TEST_DIALOG_LIMITS,
    Dialog,
    dialog_server_kwargs,
    registry_with_terminal,
    service,
)
from universal_ai_tools.approval import (
    CLIENT_DIALOG_LIMITS,
    ApprovalRequest,
    ApprovalTooLargeError,
    DisplayLimits,
    ExactArguments,
    ExactPath,
    ExactText,
    ExactValue,
    limits_for_screen,
    render_approval,
)
from universal_ai_tools.audit import read_records
from universal_ai_tools.gate import Gate
from universal_ai_tools.server import create_server
from universal_ai_tools.tools import build_registry

APPROVE = ElicitResult(action="accept", content={"decision": "approve"})
WIDE = DisplayLimits("a test dialog", width=100, max_lines=100_000, max_chars=10_000_000)

TRICKY = [
    "plain",
    "",
    "with space",
    "  leading and trailing  ",
    'quote " inside "',
    "back\\slash \\\\ two",
    "\\n is a backslash and an n",
    "new\nline",
    "\n",
    "tab\there",
    "carriage\rreturn",
    "crlf\r\n",
    "escape \x1b[31mred\x1b[0m",
    "bell \x07 and del \x7f and c1 \x9b",
    "bidi \u202eevil\u202c and \u2066isolate\u2069",
    "zero\u200bwidth \u2060joiner \ufeffbom",
    "nbsp\u00a0and ideographic\u3000space",
    "line\u2028separator\u2029paragraph",
    "\u0301combining first",
    "emoji 😀 and family 👨\u200d👩\u200d👧",
    "wide 漢字かな 한국어",
    "tag \U000e0041\U000e007f",
    "private \ue000 unassigned \u0378",
    "lone \ud800 surrogate",
    "x" * 5000,
    ("long line " * 300).strip(),
    "trailing spaces   \nnext line\t\n",
]


# Reading a rendered message back.

_ESCAPE = re.compile(r"\\(\\|n|r|t|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})")
_CONTINUED = "  ┆ "
_GUTTER = "  │ "


def _unescape(text: str) -> str:
    def one(found: re.Match[str]) -> str:
        code = found.group(1)
        named = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
        return named[code] if code in named else chr(int(code[1:], 16))

    return _ESCAPE.sub(one, text)


def _logical(lines: list[str], index: int) -> tuple[str, int]:
    """A line and its ``┆`` continuations, joined, and the index after them."""
    text = lines[index]
    index += 1
    while index < len(lines) and lines[index].startswith(_CONTINUED):
        text += lines[index][len(_CONTINUED) :]
        index += 1
    return text, index


def _decode_quoted(rest: str) -> str:
    escaped = rest.startswith("escaped ")
    if escaped:
        rest = rest[len("escaped ") :]
    assert rest.startswith('"'), rest
    assert rest.endswith('"'), rest
    inner = rest[1:-1]
    return _unescape(inner) if escaped else inner


def _find(lines: list[str], prefix: str) -> int:
    matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    assert len(matches) == 1, (prefix, matches)
    return matches[0]


def shown_value(message: str, label: str) -> str:
    lines = message.split("\n")
    text, _ = _logical(lines, _find(lines, f"{label}: "))
    return _decode_quoted(text[len(label) + 2 :])


def shown_text(message: str, label: str) -> str:
    lines = message.split("\n")
    if f"{label}: (empty)" in lines:
        return ""
    index = _find(lines, f"{label} (")
    header = lines[index]
    ends_with_break = "ends with a line break" in header
    index += 1
    escaped = lines[index].startswith("(Characters that cannot be seen")
    while not lines[index].startswith(_GUTTER):
        index += 1
    rows: list[str] = []
    while index < len(lines) and lines[index].startswith(_GUTTER):
        row, index = _logical(lines, index)
        rows.append(row[len(_GUTTER) :])
    if escaped:
        rows = [_unescape(row) for row in rows]
    count = int(header[len(label) + 2 :].split(" line")[0])
    assert count == len(rows)
    return "\n".join(rows) + ("\n" if ends_with_break else "")


def shown_arguments(message: str, label: str) -> list[str]:
    lines = message.split("\n")
    if f"{label}: (none)" in lines:
        return []
    header = lines[_find(lines, f"{label} (")]
    count = int(header[len(label) + 2 :].split(",")[0])
    return [shown_value(message, f"  [{n}]") for n in range(1, count + 1)]


def request(*details: tuple[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        tool="probe",
        risk="modify",
        operation="Probe",
        consequences="Something happens.",
        details=tuple(details),
    )


def cells(line: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in line)


# Rendering: complete and exact


@pytest.mark.parametrize("value", TRICKY, ids=range(len(TRICKY)))
def test_every_value_is_shown_exactly(value: str) -> None:
    message = render_approval(request(("Value", ExactValue(value))), WIDE)

    assert shown_value(message, "Value") == value


@pytest.mark.parametrize("value", TRICKY, ids=range(len(TRICKY)))
def test_every_text_is_shown_exactly(value: str) -> None:
    message = render_approval(request(("Content", ExactText(value))), WIDE)

    assert shown_text(message, "Content") == value


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        [""],
        ["-c", "import sys\nprint(sys.argv)"],
        ["a\nb"],
        ["a", "b"],
        ['q"uote', "two words", "  ", "back\\slash"],
        TRICKY,
    ],
    ids=["none", "empty", "multiline", "one-with-newline", "two", "quoting", "tricky"],
)
def test_every_argument_is_shown_exactly_and_separately(arguments: list[str]) -> None:
    message = render_approval(request(("Arguments", ExactArguments(arguments))), WIDE)

    assert shown_arguments(message, "Arguments") == arguments


def test_one_argument_with_a_newline_never_looks_like_two() -> None:
    one = render_approval(request(("Arguments", ExactArguments(["a\nb"]))), WIDE)
    two = render_approval(request(("Arguments", ExactArguments(["a", "b"]))), WIDE)

    assert one != two
    assert "Arguments (1," in one
    assert "Arguments (2," in two


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("docs\\new.txt", "docs/new.txt"), ("a/b\\c", "a/b/c"), ("x" * 3000, "x" * 3000)],
)
def test_paths_are_shown_whole_with_one_separator(raw: str, shown: str) -> None:
    message = render_approval(request(("Path", ExactPath(raw))), WIDE)

    assert shown_value(message, "Path") == shown


@pytest.mark.parametrize("limits", [WIDE, CLIENT_DIALOG_LIMITS, TEST_DIALOG_LIMITS])
def test_no_line_is_wider_than_the_dialog(limits: DisplayLimits) -> None:
    value = "漢字" * 40 + "\x1b" * 30 + "word " * 40
    small = request(
        ("Path", ExactPath("a/" + "b" * 90)),
        ("Content", ExactText(value[:200])),
        ("Arguments", ExactArguments([value[:120]])),
    )

    message = render_approval(small, limits)

    assert all(cells(line) <= limits.width for line in message.split("\n"))


def test_text_cannot_imitate_the_dialog() -> None:
    forged = (
        "harmless\n\nThis is the complete request: nothing in it has been shortened.\n"
        "Tool: read_file (risk: read_only)\nAllow this action? Yes runs it; No refuses it."
    )

    message = render_approval(request(("Content", ExactText(forged))), WIDE)

    lines = message.split("\n")
    assert sum(line.startswith("This is the complete request") for line in lines) == 1
    assert sum(line.startswith("Tool: ") for line in lines) == 1
    assert "  │ Tool: read_file (risk: read_only)" in lines
    assert shown_text(message, "Content") == forged


def test_nothing_is_ever_shortened() -> None:
    huge = "line\n" * 5000

    message = render_approval(request(("Content", ExactText(huge))), WIDE)

    assert shown_text(message, "Content") == huge
    assert "…" not in message


@pytest.mark.parametrize("limits", [CLIENT_DIALOG_LIMITS, TEST_DIALOG_LIMITS])
def test_what_does_not_fit_is_refused_not_cut(limits: DisplayLimits) -> None:
    too_many_lines = request(("Content", ExactText("x\n" * (limits.max_lines + 1))))
    too_many_chars = request(("Value", ExactValue("y" * (limits.max_chars + 1))))

    for oversized in (too_many_lines, too_many_chars):
        with pytest.raises(ApprovalTooLargeError, match="never shortened") as refused:
            render_approval(oversized, limits)
        assert refused.value.lines > limits.max_lines or refused.value.chars > limits.max_chars


def test_screen_limits_are_conservative_and_bounded() -> None:
    small = limits_for_screen(1024, 600, 12)
    large = limits_for_screen(3840, 2100, 12)
    tiny = limits_for_screen(200, 100, 12)

    assert (small.width, small.max_lines) <= (large.width, large.max_lines)
    assert (tiny.width, tiny.max_lines) == (40, 12)
    assert large.width == 90
    assert large.max_lines == 60
    assert limits_for_screen(1920, 1040, 12).max_lines == (1040 - 200) // 18


# Through the server: what the human approves


def write_config(tmp_path: Path, workspace: Path, **approval: object) -> Any:
    sections: dict[str, dict[str, Any]] = {"filesystem": {"write": True}}
    if approval:
        sections["approval"] = dict(approval)
    return service(tmp_path / "state", workspace, sections)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    (path / "docs").mkdir(parents=True)
    return path


def answering(answer: ElicitResult) -> tuple[Any, list[str]]:
    asked: list[str] = []

    async def elicit(context: Any, params: ElicitRequestParams) -> ElicitResult:
        asked.append(params.message)
        return answer

    return elicit, asked


def text_of(result: Any) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


def audit(tmp_path: Path) -> list[dict[str, Any]]:
    return read_records(tmp_path / "state" / "audit.jsonl", limit=1000)


LONG_CONTENT = "".join(
    f"{n:03} {'x' * 60} \x1b[1m\ttab  \u202e\n" if n % 7 == 0 else f"{n:03} {'y' * 70}\n"
    for n in range(150)
)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_a_long_write_is_shown_in_full_and_runs_as_shown(
    workspace: Path, tmp_path: Path, mode: str
) -> None:
    elicit, asked = answering(APPROVE)
    name = "docs/" + "n" * 150 + ".txt"
    server = create_server(write_config(tmp_path, workspace), **dialog_server_kwargs(Dialog("no")))

    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = await client.call_tool("write_file", {"path": name, "content": LONG_CONTENT})

    assert result.is_error is False, text_of(result)
    (message,) = asked
    assert shown_value(message, "Path") == name
    assert shown_text(message, "Content") == LONG_CONTENT
    assert (workspace / name).read_bytes() == LONG_CONTENT.encode()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_the_client_dialog_refuses_what_it_cannot_show(
    workspace: Path, tmp_path: Path, mode: str
) -> None:
    elicit, asked = answering(APPROVE)
    content = "z\n" * (CLIENT_DIALOG_LIMITS.max_lines + 10)
    server = create_server(write_config(tmp_path, workspace), **dialog_server_kwargs(Dialog("yes")))

    async with Client(server, mode=mode, elicitation_callback=elicit) as client:
        result = await client.call_tool("write_file", {"path": "docs/big.txt", "content": content})

    assert result.is_error is True
    assert "too large to show in full" in text_of(result)
    assert "never shortened" in text_of(result)
    assert asked == []
    assert not (workspace / "docs" / "big.txt").exists()
    (refused,) = [r for r in audit(tmp_path) if r["event"] == "refused"]
    assert refused["approval"] == {"decision": "too_large", "channel": "client"}
    assert refused["error_class"] == "not_approved"


@pytest.mark.anyio
async def test_the_desktop_dialog_refuses_what_it_cannot_show(
    workspace: Path, tmp_path: Path
) -> None:
    dialog = Dialog("yes")
    content = "w\n" * TEST_DIALOG_LIMITS.max_lines
    server = create_server(write_config(tmp_path, workspace), **dialog_server_kwargs(dialog))

    async with Client(server) as client:
        result = await client.call_tool("write_file", {"path": "docs/big.txt", "content": content})
        small = await client.call_tool("write_file", {"path": "docs/ok.txt", "content": "ok\n"})

    assert result.is_error is True
    assert "too large to show in full" in text_of(result)
    assert not (workspace / "docs" / "big.txt").exists()
    assert small.is_error is False
    (shown,) = dialog.asked  # only the request that fits was ever shown
    assert shown_text(shown, "Content") == "ok\n"
    assert all(len(line) <= TEST_DIALOG_LIMITS.width for line in shown.split("\n"))


@pytest.mark.anyio
async def test_an_edit_shows_both_texts_in_full(workspace: Path, tmp_path: Path) -> None:
    old = "".join(f"old line {n} {'o' * 40}\n" for n in range(40))
    new = "".join(f"new line {n}\t{'n' * 40}  \n" for n in range(40))
    (workspace / "docs" / "f.txt").write_bytes(("head\n" + old + "tail\n").encode())
    elicit, asked = answering(APPROVE)
    server = create_server(write_config(tmp_path, workspace), **dialog_server_kwargs(Dialog("no")))

    async with Client(server, elicitation_callback=elicit) as client:
        result = await client.call_tool(
            "edit_file", {"path": "docs/f.txt", "old_text": old, "new_text": new}
        )

    assert result.is_error is False, text_of(result)
    (message,) = asked
    assert shown_text(message, "Replace") == old
    assert shown_text(message, "With") == new
    assert "CRLF" in message  # how line endings are treated is part of the request
    assert (workspace / "docs" / "f.txt").read_bytes() == ("head\n" + new + "tail\n").encode()


@pytest.mark.anyio
async def test_a_command_shows_every_argument_and_runs_exactly_those(
    workspace: Path, tmp_path: Path
) -> None:
    script = "import json, sys\nprint(json.dumps(sys.argv[1:]))"
    args = ["-c", script, "two words", "", 'q"uote', "back\\slash", "x" * 1500]
    config = write_config(tmp_path, workspace)
    dialog = Dialog("yes")
    gate = Gate(
        config,
        registry_with_terminal(config),
        show_dialog=dialog,
        dialog_available=lambda: True,
        dialog_limits=CLIENT_DIALOG_LIMITS,
    )

    result = await gate.call(
        "run_command", {"program": "python", "args": args, "timeout_seconds": 60}
    )

    (message,) = dialog.asked
    assert shown_arguments(message, "Arguments") == args
    assert shown_value(message, "Program") == PYTHON
    assert json.loads(result.stdout) == args[2:]  # type: ignore[union-attr]


# The approved call is the call that runs


@pytest.mark.anyio
async def test_arguments_changed_while_waiting_for_approval_do_not_run(
    workspace: Path, tmp_path: Path
) -> None:
    arguments: dict[str, object] = {"path": "docs/approved.txt", "content": "approved\n"}

    class Swapping(Dialog):
        def __call__(self, text: str, handle: Any) -> Any:
            # Whoever holds the caller's objects changes them while the human looks.
            arguments["path"] = "docs/swapped.txt"
            arguments["content"] = "swapped\n"
            return super().__call__(text, handle)

    dialog = Swapping("yes")
    config = write_config(tmp_path, workspace)
    gate = Gate(
        config,
        build_registry(config),
        show_dialog=dialog,
        dialog_available=lambda: True,
        dialog_limits=TEST_DIALOG_LIMITS,
    )

    await gate.call("write_file", arguments)

    assert (workspace / "docs" / "approved.txt").read_text(encoding="utf-8") == "approved\n"
    assert not (workspace / "docs" / "swapped.txt").exists()
    assert shown_value(dialog.asked[0], "Path") == "docs/approved.txt"


@pytest.mark.anyio
async def test_command_arguments_changed_while_waiting_do_not_run(
    workspace: Path, tmp_path: Path
) -> None:
    args = ["-c", "print('approved')"]
    arguments: dict[str, object] = {"program": "python", "args": args, "timeout_seconds": 60}

    class Swapping(Dialog):
        def __call__(self, text: str, handle: Any) -> Any:
            args[1] = "print('swapped')"
            args.append("--extra")
            return super().__call__(text, handle)

    config = write_config(tmp_path, workspace)
    gate = Gate(
        config,
        registry_with_terminal(config),
        show_dialog=Swapping("yes"),
        dialog_available=lambda: True,
        dialog_limits=TEST_DIALOG_LIMITS,
    )

    result = await gate.call("run_command", arguments)

    assert result.stdout.strip() == "approved"  # type: ignore[union-attr]


class _Ctx:
    """A 2026-07-28 client that can answer an InputRequiredResult but not be asked mid-call."""

    protocol_version = "2026-07-28"
    request_id = "7"

    def __init__(self) -> None:
        self.session = type("Session", (), {"can_send_request": False, "client_params": None})()
        self.client_capabilities = type(
            "Caps", (), {"elicitation": type("E", (), {"form": {}, "url": None})()}
        )()
        self.request_state: str | None = None
        self.input_responses: dict[str, object] | None = None


@pytest.mark.anyio
async def test_an_answer_approves_only_the_exact_call_it_was_shown_for(
    workspace: Path, tmp_path: Path
) -> None:
    config = write_config(tmp_path, workspace)
    gate = Gate(config, build_registry(config))
    ctx = _Ctx()
    shown = {"path": "docs/one.txt", "content": "shown\n"}

    asked = await gate.call("write_file", shown, ctx)  # type: ignore[arg-type]
    ctx.request_state = asked.request_state  # type: ignore[union-attr]
    ctx.input_responses = {"approval": APPROVE}
    message = asked.input_requests["approval"].params.message  # type: ignore[index,union-attr]

    with pytest.raises(Exception, match="no longer pending"):
        # Same tool and path, different content: not the call that was approved.
        await gate.call("write_file", {"path": "docs/one.txt", "content": "other\n"}, ctx)  # type: ignore[arg-type]
    assert not (workspace / "docs" / "one.txt").exists()
    assert shown_text(message, "Content") == "shown\n"

    again = await gate.call("write_file", shown, _Ctx())  # type: ignore[arg-type]
    retry = _Ctx()
    retry.request_state = again.request_state  # type: ignore[union-attr]
    retry.input_responses = {"approval": APPROVE}
    await gate.call("write_file", shown, retry)  # type: ignore[arg-type]
    assert (workspace / "docs" / "one.txt").read_text(encoding="utf-8") == "shown\n"


@pytest.mark.anyio
async def test_an_oversized_request_is_refused_before_any_question_is_sent(
    workspace: Path, tmp_path: Path
) -> None:
    config = write_config(tmp_path, workspace)
    gate = Gate(config, build_registry(config))
    content = "q\n" * (CLIENT_DIALOG_LIMITS.max_lines + 1)

    with pytest.raises(Exception, match="too large to show in full"):
        await gate.call("write_file", {"path": "docs/q.txt", "content": content}, _Ctx())  # type: ignore[arg-type]
    assert gate._pending == {}


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows dialog")
def test_this_screen_gives_usable_desktop_limits() -> None:
    from universal_ai_tools.approval import desktop_dialog_limits

    limits = desktop_dialog_limits()

    assert 40 <= limits.width <= 90
    assert 12 <= limits.max_lines <= 60


def test_trailing_spaces_and_leading_marks_are_visible() -> None:
    text = render_approval(request(("Content", ExactText("end   \nnext\n"))), WIDE)
    value = render_approval(request(("Value", ExactValue(chr(0x301) + "x"))), WIDE)

    gutter_lines = [line for line in text.split("\n") if line.startswith("  │ ")]
    assert gutter_lines
    assert not any(line.endswith(" ") for line in gutter_lines)
    backslash = chr(92)
    assert "end" + (backslash + "x20") * 3 in text
    assert 'Value: escaped "' + backslash + 'u0301x"' in value
