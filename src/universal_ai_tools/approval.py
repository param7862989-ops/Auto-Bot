"""Asking a human to approve one tool call, and recognising the only answer that approves.

There are two ways to reach the human, and the gate (``universal_ai_tools.gate``) picks one
per call from ``[approval] channel`` in the config file:

* **The client's own dialog**, through MCP form elicitation. Claude Code shows these in
  interactive sessions. Whether Claude Desktop does depends on its version; if the client
  did not declare the capability when it connected, this channel is unavailable. Over MCP
  2025-11-25 and earlier the server sends ``elicitation/create`` while the call waits. Over
  MCP 2026-07-28, which forbids a server-initiated request mid-call, the call returns an
  ``InputRequiredResult``; the client asks the human and retries the same call with the
  answer (see ``gate.py``).
* **A Windows dialog on this PC** (``DesktopDialogChannel``), a topmost Yes/No message box
  owned by the server process, with No as the default button. It works the same whatever the
  client is. It is dismissed as No when the time limit passes or the call is cancelled.

Only ``ApprovalDecision.APPROVED`` lets a call run. Denied, dismissed, cancelled, timed out,
malformed, unavailable, too large to show: all mean no. Neither the model nor anything it
reads can answer: the question goes to a human-facing dialog, never back to the model. The
input tools refuse to act while an approval dialog is open, and never send input to one (see
``desktop/tools.py``).

**The human sees the whole request.** ``render_approval`` shows every value that decides what
the call does (paths, file content, replacement text, each command argument, typed text) in
full. Nothing is shortened. Values are shown so that two different requests can never look
the same:

* a single value is quoted; if it holds any character that cannot be seen or that changes
  what is displayed (line breaks, tabs, control and format characters, unusual spaces), it is
  marked ``escaped`` and such characters are written as ``\\n``, ``\\t``, ``\\x1b``, ``\\u200b``,
  with every backslash written as ``\\\\``;
* multi-line text is shown line by line, each line behind a ``│`` gutter (``┆`` where a long
  line continues), so the text cannot imitate the dialog's own words; the header says how
  many lines and characters it has and whether it ends with a line break; the same escaping
  applies, and trailing spaces are escaped so that they are visible;
* command arguments are numbered, one per line.

Every channel has display limits (``DisplayLimits``): a line width and a number of lines and
characters it can show at once. A request that does not fit is not shortened; it is refused
(``ApprovalDecision.TOO_LARGE``) with a message saying so, and nothing runs. The Windows
dialog's limits come from the screen's work area and the message font, conservatively; the
client's are fixed (``CLIENT_DIALOG_LIMITS``), because MCP gives a server no way to learn how
much of a message a client displays. That a client really shows a message of that size in
full has not been verified for Claude Code or Claude Desktop.
"""

from __future__ import annotations

import logging
import math
import sys
import textwrap
import threading
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

import anyio
import anyio.to_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from universal_ai_tools.policy import escape_line

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context
    from mcp.types import ElicitResult

logger = logging.getLogger(__name__)

APPROVAL_DIALOG_TITLE = "Universal AI Tools - approval required"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    """No channel can reach a human: the client cannot show a dialog, and no desktop."""
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INVALID = "invalid"
    """An answer that is not a clear approval or denial."""
    TOO_LARGE = "too_large"
    """The complete request does not fit the dialog, so it was not shown and not approved."""


# Values shown exactly. Each is a ``str`` (or ``tuple``) subclass, so code that reads a
# detail as plain text still can; only ``render_approval`` treats them differently.


class ExactValue(str):
    """One value that decides what a call does, shown quoted and in full."""

    __slots__ = ()


class ExactPath(ExactValue):
    """A workspace path. ``\\`` and ``/`` are both separators there, so both show as ``/``."""

    __slots__ = ()


class ExactText(str):
    """Multi-line text (file content, replacement text, typed text), shown in full."""

    __slots__ = ()


class ExactArguments(tuple[str, ...]):
    """A program's argument list, one numbered, quoted argument per line."""

    __slots__ = ()


DetailValue = str | ExactArguments
"""A plain ``str`` is a fact the server states in its own words, such as a time limit."""


@dataclass(frozen=True)
class ApprovalRequest:
    """What a human is asked to approve. Nothing in it is ever shortened."""

    tool: str
    risk: str
    operation: str
    """One line: what will happen, in the tool's words."""
    consequences: str
    """What approving means, in fixed words chosen by the tool."""
    details: tuple[tuple[str, DetailValue], ...] = ()
    """Labelled facts: the path, the command and each argument, the complete content."""
    scope: Literal["call", "session"] = "call"
    """``session``: approving also allows this capability for a limited time."""
    capability: str = ""
    session_minutes: float = 0
    client: str = "the MCP client"


@dataclass(frozen=True)
class DisplayLimits:
    """How much a dialog can show at once, in full, without scrolling or cutting."""

    where: str
    width: int
    """Display cells per line; a wide (East Asian) character takes two."""
    max_lines: int
    max_chars: int


CLIENT_DIALOG_LIMITS = DisplayLimits(
    "the client's approval dialog", width=100, max_lines=200, max_chars=20_000
)
DEFAULT_DESKTOP_DIALOG_LIMITS = DisplayLimits(
    "the Windows approval dialog", width=60, max_lines=24, max_chars=24 * 60
)


class ApprovalTooLargeError(Exception):
    """The complete request does not fit ``limits``. Nothing was shown."""

    def __init__(self, lines: int, chars: int, limits: DisplayLimits) -> None:
        self.lines = lines
        self.chars = chars
        self.limits = limits
        super().__init__(
            f"the complete request is {lines} lines and {chars} characters, and "
            f"{limits.where} can show at most {limits.max_lines} lines and "
            f"{limits.max_chars} characters in full; it is never shortened, so it was not "
            "shown. Split the change into smaller steps"
        )


# Exact rendering.

_NAMED_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_GUTTER = "  │ "
_CONTINUED = "  ┆ "


def _special(char: str, *, first: bool) -> bool:
    """Whether a character cannot be shown as itself without ambiguity."""
    if char == " ":
        return False
    if char in "\n\r\t":
        return True
    category = unicodedata.category(char)
    # Controls, format characters (bidirectional controls, zero-width characters), lone
    # surrogates, private use, unassigned, and every space and separator but U+0020.
    if category[0] in "CZ":
        return True
    # A combining mark at the start would combine with the quote or the gutter before it.
    return first and category[0] == "M"


def _escape(char: str) -> str:
    if char in _NAMED_ESCAPES:
        return _NAMED_ESCAPES[char]
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def _tokens(text: str, *, escaped: bool, trailing: bool = False) -> list[str]:
    """``text`` as display tokens: characters, or an escape that must stay whole."""
    if not escaped:
        return list(text)
    end = len(text.rstrip(" ")) if trailing else len(text)
    tokens = []
    for index, char in enumerate(text):
        if char == "\\" or _special(char, first=index == 0) or index >= end:
            tokens.append(_escape(char))
        else:
            tokens.append(char)
    return tokens


def _needs_escapes(text: str, *, lines: bool) -> bool:
    if not lines:
        return any(_special(char, first=index == 0) for index, char in enumerate(text))
    for line in text.split("\n"):
        if line != line.rstrip(" "):
            return True
        if any(_special(char, first=index == 0) for index, char in enumerate(line)):
            return True
    return False


def _cells(token: str) -> int:
    if len(token) != 1:
        return len(token)
    return 2 if unicodedata.east_asian_width(token) in ("W", "F") else 1


def _wrap_tokens(tokens: list[str], first_room: int, room: int) -> list[str]:
    """Cut a token list into lines of at most ``first_room``, then ``room``, cells."""
    lines: list[str] = []
    current: list[str] = []
    used = 0
    limit = first_room
    for token in tokens:
        width = _cells(token)
        if current and used + width > limit:
            lines.append("".join(current))
            current, used, limit = [], 0, room
        current.append(token)
        used += width
    lines.append("".join(current))
    return lines


def _hard_wrap(prefix: str, tokens: list[str], width: int) -> list[str]:
    """``prefix`` and then ``tokens``, never changing a character, continued with ``┆``."""
    room = max(width - len(_CONTINUED), 8)
    first_room = max(width - len(prefix), 8)
    chunks = _wrap_tokens(tokens, first_room, room)
    return [prefix + chunks[0], *(_CONTINUED + chunk for chunk in chunks[1:])]


def _value_lines(label: str, value: str, width: int) -> list[str]:
    shown = value.replace("\\", "/") if isinstance(value, ExactPath) else value
    escaped = _needs_escapes(shown, lines=False)
    prefix = f"{label}: " + ("escaped " if escaped else "")
    tokens = ['"', *_tokens(shown, escaped=escaped), '"']
    return _hard_wrap(prefix, tokens, width)


def _text_lines(label: str, text: str, width: int) -> list[str]:
    if text == "":
        return [f"{label}: (empty)"]
    escaped = _needs_escapes(text, lines=True)
    rows = text.split("\n")
    ends_with_break = rows[-1] == ""
    if ends_with_break:
        rows.pop()
    facts = [
        f"{len(rows)} line" + ("" if len(rows) == 1 else "s"),
        f"{len(text)} characters",
        "ends with a line break" if ends_with_break else "no line break at the end",
    ]
    lines = [f"{label} ({', '.join(facts)}):"]
    if escaped:
        lines += textwrap.wrap(
            "(Characters that cannot be seen are shown as escapes such as \\t or \\x1b, "
            "and each backslash as \\\\.)",
            width,
        )
    room = max(width - len(_GUTTER), 8)
    for row in rows:
        chunks = _wrap_tokens(_tokens(row, escaped=escaped, trailing=True), room, room)
        lines.append(_GUTTER + chunks[0])
        lines += [_CONTINUED + chunk for chunk in chunks[1:]]
    return lines


def _arguments_lines(label: str, arguments: ExactArguments, width: int) -> list[str]:
    if not arguments:
        return [f"{label}: (none)"]
    lines = [f"{label} ({len(arguments)}, each passed exactly as shown):"]
    for number, argument in enumerate(arguments, start=1):
        lines += _value_lines(f"  [{number}]", argument, width)
    return lines


def _detail_lines(label: str, value: DetailValue, width: int) -> list[str]:
    label = escape_line(label)
    if isinstance(value, ExactArguments):
        return _arguments_lines(label, value, width)
    if isinstance(value, ExactText):
        return _text_lines(label, value, width)
    if isinstance(value, ExactValue):
        return _value_lines(label, value, width)
    return _hard_wrap(f"{label}: ", list(escape_line(str(value))), width)


def _paragraph(text: str, width: int) -> list[str]:
    """Server-written prose, word-wrapped."""
    return textwrap.wrap(escape_line(text), width, break_on_hyphens=False) or [""]


def render_approval(request: ApprovalRequest, limits: DisplayLimits, *, closing: str = "") -> str:
    """The complete question for ``request``, laid out for a dialog with ``limits``.

    Raises:
        ApprovalTooLargeError: If the complete question does not fit ``limits``. It is
            never shortened instead.
    """
    width = limits.width
    lines = _paragraph(
        f"Claude, through {request.client}, asks to run an action that needs your approval.",
        width,
    )
    lines.append("")
    lines += _hard_wrap("Action: ", list(escape_line(request.operation)), width)
    lines += _hard_wrap(
        "Tool: ", list(escape_line(f"{request.tool} (risk: {request.risk})")), width
    )
    lines += _paragraph("What it means: " + request.consequences, width)
    if request.details:
        lines.append("")
        for label, value in request.details:
            lines += _detail_lines(label, value, width)
    lines.append("")
    if request.scope == "session":
        lines += _paragraph(
            f"Approving also allows further '{request.capability}' actions for up to "
            f"{request.session_minutes:g} minutes, without asking again and without showing "
            "them to you.",
            width,
        )
    lines += _paragraph(
        "This is the complete request: nothing in it has been shortened. It comes from the "
        "AI model, which may have been influenced by files, web pages, or windows it has "
        "seen. Approve only if you want exactly this to happen.",
        width,
    )
    if closing:
        lines += ["", *_paragraph(closing, width)]
    message = "\n".join(lines)
    if len(lines) > limits.max_lines or len(message) > limits.max_chars:
        raise ApprovalTooLargeError(len(lines), len(message), limits)
    return message


class ApprovalChannel(Protocol):
    name: str
    closing: str
    """A last line the channel's dialog needs, such as the question its buttons answer."""

    def limits(self) -> DisplayLimits:
        """How much this channel's dialog can show in full, now."""
        ...

    async def request_approval(self, request: ApprovalRequest, message: str) -> ApprovalDecision:
        """Show ``message`` (``render_approval`` of ``request``) to a human and wait.

        Must never answer ``APPROVED`` on anyone's behalf.
        """
        ...


class UnavailableChannel:
    name = "none"
    closing = ""

    def limits(self) -> DisplayLimits:
        return CLIENT_DIALOG_LIMITS

    async def request_approval(self, request: ApprovalRequest, message: str) -> ApprovalDecision:
        return ApprovalDecision.UNAVAILABLE


# The client's dialog: MCP form elicitation.


class ApprovalForm(BaseModel):
    # Closed: an answer with any other field does not match the form, so it is not approval.
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["approve", "deny"] = Field(
        description="Choose 'approve' to let this action run, or 'deny' to refuse it."
    )


def client_supports_form_elicitation(ctx: Context) -> bool:
    """Whether the connected client declared form-mode elicitation.

    An ``elicitation`` capability with neither ``form`` nor ``url`` is the older spelling of
    form mode. Anything else, including no declared capabilities, is no.
    """
    try:
        capabilities = ctx.client_capabilities
    except ValueError:
        return False
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


class ElicitationChannel:
    """Asks through the client while the call waits (MCP 2025-11-25 and earlier)."""

    name = "client"
    closing = ""

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def limits(self) -> DisplayLimits:
        return CLIENT_DIALOG_LIMITS

    async def request_approval(self, request: ApprovalRequest, message: str) -> ApprovalDecision:
        if not client_supports_form_elicitation(self._ctx):
            return ApprovalDecision.UNAVAILABLE
        try:
            answer = await self._ctx.elicit(message, ApprovalForm)
        except ValueError:
            # Accepted, but without content or with content that is not the form.
            return ApprovalDecision.INVALID
        except Exception as exc:
            logger.warning("elicitation failed: %s", type(exc).__name__)
            return ApprovalDecision.UNAVAILABLE
        if answer.action == "accept":
            data = getattr(answer, "data", None)
            if isinstance(data, ApprovalForm):
                if data.decision == "approve":
                    return ApprovalDecision.APPROVED
                return ApprovalDecision.DENIED
            return ApprovalDecision.INVALID
        if answer.action == "decline":
            return ApprovalDecision.DENIED
        if answer.action == "cancel":
            return ApprovalDecision.CANCELLED
        return ApprovalDecision.INVALID


def decision_from_responses(responses: Mapping[str, object] | None, key: str) -> ApprovalDecision:
    """The human's answer in a 2026-07-28 retry. Anything unclear is not approval."""
    from mcp.types import ElicitResult

    response = responses.get(key) if responses else None
    if not isinstance(response, ElicitResult):
        return ApprovalDecision.INVALID
    return decision_from_elicit_result(response)


def decision_from_elicit_result(response: ElicitResult) -> ApprovalDecision:
    if response.action == "decline":
        return ApprovalDecision.DENIED
    if response.action == "cancel":
        return ApprovalDecision.CANCELLED
    if response.action != "accept" or response.content is None:
        return ApprovalDecision.INVALID
    try:
        form = ApprovalForm.model_validate(response.content, strict=True)
    except ValidationError:
        return ApprovalDecision.INVALID
    return ApprovalDecision.APPROVED if form.decision == "approve" else ApprovalDecision.DENIED


# A dialog on this PC.

DialogAnswer = Literal["yes", "no", "dismissed", "unavailable"]


@dataclass
class DialogHandle:
    """Lets another thread close an open dialog, which then counts as No."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread_id: int | None = None
    _closed: bool = False

    def opened(self, thread_id: int) -> bool:
        """Record the dialog's thread. False if it was closed before it opened."""
        with self._lock:
            self._thread_id = thread_id
            return not self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            thread_id = self._thread_id
        if thread_id is not None:
            _dismiss_dialogs_of_thread(thread_id)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


ShowDialog = Callable[[str, DialogHandle], DialogAnswer]


class DesktopDialogChannel:
    """A Yes/No dialog on this PC's desktop, owned by the server process."""

    name = "desktop_dialog"
    closing = "Allow this action? Yes runs it; No refuses it."

    def __init__(
        self,
        show: ShowDialog | None = None,
        *,
        timeout_seconds: float = 120,
        limits: DisplayLimits | None = None,
    ) -> None:
        self._show = show or show_windows_dialog
        self._timeout = timeout_seconds
        self._limits = limits

    def limits(self) -> DisplayLimits:
        return self._limits if self._limits is not None else desktop_dialog_limits()

    async def request_approval(self, request: ApprovalRequest, message: str) -> ApprovalDecision:
        handle = DialogHandle()
        answer: DialogAnswer = "dismissed"
        timed_out = False
        try:
            with anyio.move_on_after(self._timeout) as scope:
                answer = await anyio.to_thread.run_sync(
                    self._show, message, handle, abandon_on_cancel=True
                )
            timed_out = scope.cancelled_caught
        finally:
            # On timeout or cancellation the dialog is still open in its thread: close it.
            if timed_out or answer == "dismissed":
                handle.close()
        if timed_out:
            return ApprovalDecision.TIMED_OUT
        if answer == "yes" and not handle.closed:
            return ApprovalDecision.APPROVED
        if answer == "unavailable":
            return ApprovalDecision.UNAVAILABLE
        if answer == "no":
            return ApprovalDecision.DENIED
        return ApprovalDecision.CANCELLED


def desktop_dialog_available() -> bool:
    """Whether this process can show a dialog to an interactive user."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # A service or a disconnected session has no visible input desktop.
        desktop = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            return False
        user32.CloseDesktop(desktop)
    except OSError:
        return False
    return True


def limits_for_screen(work_width: int, work_height: int, font_height: int) -> DisplayLimits:
    """Conservative limits for a message box on a work area of this size, in its font.

    A message box grows to fit its text up to the work area and clips what does not fit.
    Assumed, generously: a box at most 5/8 of the work area wide, 200 pixels of title bar,
    icon, buttons, and margins, lines 1.5 times the font height, and every character as
    wide as the font is high. Clamped to 40-90 cells and 12-60 lines.
    """
    font = max(abs(font_height), 8)
    line_height = math.ceil(font * 1.5)
    lines = (work_height - 200) // line_height
    cells = (work_width * 5 // 8 - 120) // font
    width = min(max(cells, 40), 90)
    max_lines = min(max(lines, 12), 60)
    return DisplayLimits(
        "the Windows approval dialog", width=width, max_lines=max_lines, max_chars=width * max_lines
    )


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _MessageBoxW = _user32.MessageBoxW
    _MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    _MessageBoxW.restype = ctypes.c_int
    _EnumThreadWindows = _user32.EnumThreadWindows
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _EnumThreadWindows.argtypes = [wintypes.DWORD, _WNDENUMPROC, wintypes.LPARAM]
    _PostMessageW = _user32.PostMessageW
    _PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _SystemParametersInfoW = _user32.SystemParametersInfoW
    _SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    _SystemParametersInfoW.restype = wintypes.BOOL

    class _LOGFONTW(ctypes.Structure):
        _fields_ = (
            ("lfHeight", wintypes.LONG),
            ("lfWidth", wintypes.LONG),
            ("lfEscapement", wintypes.LONG),
            ("lfOrientation", wintypes.LONG),
            ("lfWeight", wintypes.LONG),
            ("lfItalic", wintypes.BYTE),
            ("lfUnderline", wintypes.BYTE),
            ("lfStrikeOut", wintypes.BYTE),
            ("lfCharSet", wintypes.BYTE),
            ("lfOutPrecision", wintypes.BYTE),
            ("lfClipPrecision", wintypes.BYTE),
            ("lfQuality", wintypes.BYTE),
            ("lfPitchAndFamily", wintypes.BYTE),
            ("lfFaceName", wintypes.WCHAR * 32),
        )

    class _NONCLIENTMETRICSW(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.UINT),
            ("iBorderWidth", ctypes.c_int),
            ("iScrollWidth", ctypes.c_int),
            ("iScrollHeight", ctypes.c_int),
            ("iCaptionWidth", ctypes.c_int),
            ("iCaptionHeight", ctypes.c_int),
            ("lfCaptionFont", _LOGFONTW),
            ("iSmCaptionWidth", ctypes.c_int),
            ("iSmCaptionHeight", ctypes.c_int),
            ("lfSmCaptionFont", _LOGFONTW),
            ("iMenuWidth", ctypes.c_int),
            ("iMenuHeight", ctypes.c_int),
            ("lfMenuFont", _LOGFONTW),
            ("lfStatusFont", _LOGFONTW),
            ("lfMessageFont", _LOGFONTW),
            ("iPaddedBorderWidth", ctypes.c_int),
        )

    _SPI_GETWORKAREA = 0x0030
    _SPI_GETNONCLIENTMETRICS = 0x0029
    _MB_YESNO = 0x4
    _MB_ICONWARNING = 0x30
    _MB_DEFBUTTON2 = 0x100
    _MB_SETFOREGROUND = 0x10000
    _MB_TOPMOST = 0x40000
    _IDYES = 6
    _IDNO = 7
    _WM_COMMAND = 0x0111

    def desktop_dialog_limits() -> DisplayLimits:
        """Limits for the message box on this screen; the defaults if they cannot be read."""
        area = wintypes.RECT()
        metrics = _NONCLIENTMETRICSW()
        metrics.cbSize = ctypes.sizeof(_NONCLIENTMETRICSW)
        if not _SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(area), 0):
            return DEFAULT_DESKTOP_DIALOG_LIMITS
        if not _SystemParametersInfoW(
            _SPI_GETNONCLIENTMETRICS, metrics.cbSize, ctypes.byref(metrics), 0
        ):
            return DEFAULT_DESKTOP_DIALOG_LIMITS
        return limits_for_screen(
            area.right - area.left, area.bottom - area.top, metrics.lfMessageFont.lfHeight
        )

    def show_windows_dialog(text: str, handle: DialogHandle) -> DialogAnswer:
        if not handle.opened(_kernel32.GetCurrentThreadId()):
            return "dismissed"
        result = _MessageBoxW(
            None,
            text,
            APPROVAL_DIALOG_TITLE,
            _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2 | _MB_SETFOREGROUND | _MB_TOPMOST,
        )
        if result == 0:
            return "unavailable"
        if handle.closed:
            return "dismissed"
        if result == _IDYES:
            return "yes"
        if result == _IDNO:
            return "no"
        return "dismissed"

    def _dismiss_dialogs_of_thread(thread_id: int) -> None:
        def answer_no(hwnd: int, _lparam: int) -> bool:
            # The same as clicking No: a dismissed dialog never approves.
            _PostMessageW(hwnd, _WM_COMMAND, _IDNO, 0)
            return True

        _EnumThreadWindows(thread_id, _WNDENUMPROC(answer_no), 0)

else:

    def desktop_dialog_limits() -> DisplayLimits:
        return DEFAULT_DESKTOP_DIALOG_LIMITS

    def show_windows_dialog(text: str, handle: DialogHandle) -> DialogAnswer:
        return "unavailable"

    def _dismiss_dialogs_of_thread(thread_id: int) -> None:
        return None


__all__ = [
    "APPROVAL_DIALOG_TITLE",
    "CLIENT_DIALOG_LIMITS",
    "DEFAULT_DESKTOP_DIALOG_LIMITS",
    "ApprovalChannel",
    "ApprovalDecision",
    "ApprovalForm",
    "ApprovalRequest",
    "ApprovalTooLargeError",
    "DesktopDialogChannel",
    "DetailValue",
    "DialogHandle",
    "DisplayLimits",
    "ElicitationChannel",
    "ExactArguments",
    "ExactPath",
    "ExactText",
    "ExactValue",
    "UnavailableChannel",
    "client_supports_form_elicitation",
    "decision_from_responses",
    "desktop_dialog_available",
    "desktop_dialog_limits",
    "limits_for_screen",
    "render_approval",
]
