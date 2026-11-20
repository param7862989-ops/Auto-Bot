"""The emergency stop and controls by name (UI Automation).

Against the fake desktop from ``test_desktop``, except the last tests, which drive real
Windows controls in a small window this test process creates for itself (no other program
is touched, and no mouse or keyboard input is sent: UI Automation acts on the controls).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from test_desktop import FakeBackend, window
from universal_ai_tools import cli
from universal_ai_tools.config import AppConfig
from universal_ai_tools.desktop.backend import ElementInfo, Rect
from universal_ai_tools.desktop.stop import (
    CORNER_PIXELS,
    STOP_FILE_NAME,
    EmergencyStop,
    in_corner,
)
from universal_ai_tools.desktop.tools import (
    INSPECTION_TTL_SECONDS,
    ClickInput,
    Desktop,
    InputRefused,
    InspectInput,
    InspectResult,
    PressKeysInput,
    TypeTextInput,
    specs,
    typing_arrived,
)
from universal_ai_tools.settings import InputSettings, ScreenSettings, ServiceConfig
from universal_ai_tools.tools import ToolCallError

SCREEN = Rect(0, 0, 2560, 1440)


def element(handle: int, control: str, name: str, *actions: str, **changes: Any) -> ElementInfo:
    base = ElementInfo(
        handle=handle,
        depth=1,
        control_type=control,
        name=name,
        automation_id=f"id{handle}",
        class_name="Generic",
        enabled=True,
        offscreen=False,
        password=False,
        value=None,
        actions=tuple(actions),  # type: ignore[arg-type]
        rect=Rect(10, 10, 110, 40),
        process_id=1001,
    )
    return replace(base, **changes)


def service(state: Path, workspace: Path) -> ServiceConfig:
    return ServiceConfig(
        app=AppConfig(workspace_root=workspace.resolve()),
        source="file",
        config_path=state / "config.toml",
        screen=ScreenSettings(enabled=True),
        input=InputSettings(enabled=True),
    )


@pytest.fixture
def backend() -> FakeBackend:
    fake = FakeBackend(
        windows={1: window(1, "notepad.exe"), 2: window(2, "claude.exe")},
        foreground=1,
        under_point=1,
    )
    fake.elements = {
        1: [
            element(11, "button", "Save", "invoke", "focus"),
            element(12, "edit", "Text editor", "set_value", "focus", value="hello"),
            # A backend that wrongly reports a password field's text: the tools still hide it.
            element(13, "edit", "Password", "set_value", password=True, value="hunter2"),
            element(14, "check box", "Word wrap", "toggle"),
            element(15, "button", "Disabled", "invoke", enabled=False),
            element(16, "text", "Line\u202eevil", depth=2),
        ]
    }
    return fake


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def desktop(backend: FakeBackend, state: Path, tmp_path: Path) -> Desktop:
    return Desktop(backend, service(state, tmp_path))


def desktop_inspection_id(result: object) -> str:
    assert isinstance(result, InspectResult)
    return result.inspection_id


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# The corners.


@pytest.mark.parametrize(
    ("x", "y"),
    [(0, 0), (2559, 0), (0, 1439), (2559, 1439), (CORNER_PIXELS - 1, CORNER_PIXELS - 1)],
)
def test_the_corners_are_the_stop(x: int, y: int) -> None:
    assert in_corner(x, y, SCREEN)


@pytest.mark.parametrize(
    ("x", "y"), [(1280, 720), (0, 720), (1280, 0), (CORNER_PIXELS, 0), (0, CORNER_PIXELS)]
)
def test_edges_and_the_middle_are_not(x: int, y: int) -> None:
    assert not in_corner(x, y, SCREEN)


def test_a_pointer_in_a_corner_stops_all_input_until_resumed(
    backend: FakeBackend, desktop: Desktop, state: Path
) -> None:
    backend.cursor = (0, 0)
    with pytest.raises(InputRefused, match="input is stopped") as refused:
        desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    assert "universal-ai-tools input resume" in str(refused.value)
    assert backend.sent == []
    assert (state / STOP_FILE_NAME).is_file()

    backend.cursor = (1000, 700)  # moving away does not resume
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.type_text(TypeTextInput(window_id=1, text="x"))
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.focus(1)
    assert backend.sent == []


def test_every_server_process_sees_the_stop(
    backend: FakeBackend, state: Path, tmp_path: Path
) -> None:
    first = Desktop(backend, service(state, tmp_path))
    second = Desktop(backend, service(state, tmp_path))
    first.stop.trigger("test")
    with pytest.raises(InputRefused, match="input is stopped"):
        second.press_keys(PressKeysInput(window_id=1, keys=["enter"]))


def test_controls_by_name_obey_the_stop(backend: FakeBackend, desktop: Desktop) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1))
    desktop.stop.trigger("test")
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.act_on_element(1, inspection.inspection_id, "e0", "invoke")
    assert backend.sent == []


def test_typing_stops_when_the_mouse_moves(backend: FakeBackend, desktop: Desktop) -> None:
    backend.move_cursor_after = (5, (1100, 700))
    with pytest.raises(InputRefused, match="the mouse moved"):
        desktop.type_text(TypeTextInput(window_id=1, text="0123456789"))
    assert backend.sent == [("type", "01234")]
    assert desktop.stop.reason() is None  # a nudge stops the typing, not all input


def test_typing_stops_when_an_approval_dialog_opens(backend: FakeBackend, desktop: Desktop) -> None:
    backend.open_dialog_after = 4
    with pytest.raises(InputRefused, match="an approval dialog opened"):
        desktop.type_text(TypeTextInput(window_id=1, text="0123456789"))
    assert backend.sent == [("type", "0123")]


def test_typing_is_read_back_and_confirmed(backend: FakeBackend, desktop: Desktop) -> None:
    backend.field = "already here\r\n"
    result = desktop.type_text(TypeTextInput(window_id=1, text="new line\nsecond"))
    assert "read back and matches" in result.detail


def test_typing_that_arrived_wrong_is_reported_as_a_failure(
    backend: FakeBackend, desktop: Desktop
) -> None:
    """Live: the Windows 11 Notepad turned 'the quick ... 12345.' into 'tttthe 5555.'."""
    backend.field = ""
    backend.garble = True
    with pytest.raises(ToolCallError, match="shows different text than was typed"):
        desktop.type_text(TypeTextInput(window_id=1, text="the quick brown fox"))


def test_a_slow_application_is_given_time(
    backend: FakeBackend, state: Path, tmp_path: Path
) -> None:
    pauses: list[float] = []
    slow = Desktop(backend, service(state, tmp_path), sleep=pauses.append)
    backend.field = ""
    backend.lag_reads = 3
    result = slow.type_text(TypeTextInput(window_id=1, text="abc"))
    assert "read back and matches" in result.detail
    assert len(pauses) == 2


def test_typing_into_an_unreadable_control_says_it_was_not_checked(
    backend: FakeBackend, desktop: Desktop
) -> None:
    result = desktop.type_text(TypeTextInput(window_id=1, text="abc"))
    assert "not checked" in result.detail


@pytest.mark.parametrize(
    ("before", "after", "text", "arrived"),
    [
        ("", "abc", "abc", True),
        ("x\r\n", "x\r\nab\rc", "ab\nc", True),
        ("hello", "hello world", " world", True),
        ("", "abd", "abc", False),
        ("", "abcc", "abc", False),
        ("old", "abc", "abc", False),  # a selection was replaced: cannot be confirmed
        (None, "abc", "abc", False),
        ("", None, "abc", False),
    ],
)
def test_what_counts_as_arrived(
    before: str | None, after: str | None, text: str, arrived: bool
) -> None:
    assert typing_arrived(before, after, text) is arrived


def test_small_pointer_jitter_does_not_stop_typing(backend: FakeBackend, desktop: Desktop) -> None:
    backend.move_cursor_after = (3, (1002, 701))
    desktop.type_text(TypeTextInput(window_id=1, text="0123456789"))
    assert backend.sent == [("type", "0123456789")]


def test_a_corner_during_typing_latches_the_stop(backend: FakeBackend, desktop: Desktop) -> None:
    backend.move_cursor_after = (2, (2559, 1439))
    with pytest.raises(InputRefused, match="emergency stop"):
        desktop.type_text(TypeTextInput(window_id=1, text="0123456789"))
    assert desktop.stop.reason() is not None


def test_clicks_into_a_corner_are_refused(backend: FakeBackend, desktop: Desktop) -> None:
    capture = desktop.capture(None)
    with pytest.raises(InputRefused, match="reserved for the emergency stop"):
        desktop.click(ClickInput(window_id=1, capture_id=capture.capture_id, x=0, y=0))
    assert backend.sent == []
    assert desktop.stop.reason() is None


def test_a_stop_that_cannot_be_written_holds_in_memory(
    backend: FakeBackend, tmp_path: Path
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    stop = EmergencyStop(blocked / STOP_FILE_NAME)
    stop.trigger("test")
    assert stop.reason() == "test"


def test_an_unreadable_stop_file_still_stops(tmp_path: Path) -> None:
    path = tmp_path / STOP_FILE_NAME
    path.write_text("not json", encoding="utf-8")
    assert EmergencyStop(path).reason() == "stopped"


def _cli(*arguments: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(list(arguments))
    code = exit_info.value.code
    return code if isinstance(code, int) else 1


def test_the_cli_stops_and_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], backend: FakeBackend
) -> None:
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    config = tmp_path / "state" / "config.toml"
    config.parent.mkdir()
    config.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")

    assert _cli("input", "--config", str(config), "stop") == 0
    assert (config.parent / STOP_FILE_NAME).is_file()
    desktop = Desktop(backend, service(config.parent, workspace))
    with pytest.raises(InputRefused):
        desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    assert _cli("input", "--config", str(config), "status") == 0
    assert "input is stopped" in capsys.readouterr().out

    assert _cli("input", "--config", str(config), "resume") == 0
    assert not (config.parent / STOP_FILE_NAME).exists()
    desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    assert backend.sent == [("keys", [0x0D])]
    log = (config.parent / "audit.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in log.splitlines()]
    assert events == ["input_stop", "input_resume"]


def test_input_can_be_limited_to_named_programs(tmp_path: Path) -> None:
    from universal_ai_tools.settings import read_config_file

    workspace = tmp_path / "scratch"
    workspace.mkdir()
    config = tmp_path / "state" / "config.toml"
    config.parent.mkdir()
    config.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")

    assert _cli("config", "--config", str(config), "allow-input", "Notepad.EXE") == 0
    assert read_config_file(config).input.allowed_processes == ("notepad.exe",)
    for bad in (["C:\\Windows\\notepad.exe"], ["notepad"], []):
        assert _cli("config", "--config", str(config), "allow-input", *bad) == 2
    assert read_config_file(config).input.allowed_processes == ("notepad.exe",)
    assert _cli("config", "--config", str(config), "allow-input", "--clear") == 0
    assert read_config_file(config).input.allowed_processes == ()


# Controls by name.


def test_inspection_lists_the_controls(backend: FakeBackend, desktop: Desktop) -> None:
    result = desktop.inspect(InspectInput(window_id=1))

    assert [e.element_id for e in result.elements] == ["e0", "e1", "e2", "e3", "e4", "e5"]
    save = result.elements[0]
    assert (save.control_type, save.name, save.actions) == ("button", "Save", ["invoke", "focus"])
    assert result.elements[1].value == "hello"
    assert result.elements[2].password is True
    assert result.elements[2].value is None
    assert "\u202e" not in result.elements[5].name
    assert result.accepts_input is True
    assert result.trust == "untrusted"


def test_inspection_says_when_input_is_refused(backend: FakeBackend, desktop: Desktop) -> None:
    backend.elements[2] = [element(21, "button", "Send", "invoke")]
    result = desktop.inspect(InspectInput(window_id=2))
    assert result.accepts_input is False
    assert result.refusal is not None
    assert "claude.exe" in result.refusal
    with pytest.raises(InputRefused, match=r"claude\.exe"):
        desktop.act_on_element(2, result.inspection_id, "e0", "invoke")
    assert backend.sent == []


def test_inspection_is_limited_and_says_so(backend: FakeBackend, desktop: Desktop) -> None:
    result = desktop.inspect(InspectInput(window_id=1, max_elements=2))
    assert len(result.elements) == 2
    assert result.truncated is True


def test_an_unreadable_window_is_an_error(backend: FakeBackend, desktop: Desktop) -> None:
    def broken(window_id: int, limit: int) -> tuple[list[ElementInfo], bool]:
        raise OSError("the application did not answer")

    backend.inspect_window = broken  # type: ignore[method-assign]
    with pytest.raises(ToolCallError, match="did not answer"):
        desktop.inspect(InspectInput(window_id=1))
    with pytest.raises(ToolCallError, match="does not exist"):
        desktop.inspect(InspectInput(window_id=99))


def test_invoking_and_setting_text(backend: FakeBackend, desktop: Desktop) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id

    pressed = desktop.act_on_element(1, inspection, "e0", "invoke")
    desktop.act_on_element(1, inspection, "e1", "set_value", "new text")
    desktop.act_on_element(1, inspection, "e3", "toggle")

    assert "invoke done on the button 'Save'" in pressed.detail
    assert backend.sent == [
        ("element", (11, "invoke", None)),
        ("element", (12, "set_value", "new text")),
        ("element", (14, "toggle", None)),
    ]


@pytest.mark.parametrize(
    ("element_id", "action", "message"),
    [
        ("e2", "set_value", "password fields are never set"),
        ("e4", "invoke", "disabled"),
        ("e0", "toggle", "does not support 'toggle'"),
        ("e9", "invoke", "no element 'e9'"),
    ],
)
def test_actions_that_are_refused(
    backend: FakeBackend, desktop: Desktop, element_id: str, action: Any, message: str
) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    with pytest.raises(InputRefused, match=message):
        desktop.act_on_element(1, inspection, element_id, action, "x")
    assert backend.sent == []


def test_the_element_is_read_again_before_acting(backend: FakeBackend, desktop: Desktop) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    backend.live[11] = None
    with pytest.raises(InputRefused, match="no longer exists"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    backend.live[11] = element(11, "button", "Save", "invoke", process_id=666)
    with pytest.raises(InputRefused, match="another program"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    backend.live[11] = element(11, "button", "Save", "invoke", enabled=False)
    with pytest.raises(InputRefused, match="disabled"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    backend.live[11] = element(11, "button", "Save")
    with pytest.raises(InputRefused, match="does not support"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    assert backend.sent == []


def test_a_replaced_window_is_refused(backend: FakeBackend, desktop: Desktop) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    # Another program now owns window id 1, and the element reports that program too, so
    # only the window's own check can tell.
    backend.windows[1] = window(1, "notepad.exe", pid=5555)
    backend.live[11] = element(11, "button", "Save", "invoke", process_id=5555)
    with pytest.raises(InputRefused, match="the window now belongs to another program"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    assert backend.sent == []


def test_inspections_expire_and_belong_to_one_window(
    backend: FakeBackend, state: Path, tmp_path: Path
) -> None:
    clock = Clock()
    desktop = Desktop(backend, service(state, tmp_path), clock=clock)
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    backend.windows[3] = window(3, "notepad.exe")
    with pytest.raises(InputRefused, match="is of window 1"):
        desktop.act_on_element(3, inspection, "e0", "invoke")
    with pytest.raises(InputRefused, match="unknown inspection_id"):
        desktop.act_on_element(1, "nope", "e0", "invoke")
    clock.now += INSPECTION_TTL_SECONDS + 1
    with pytest.raises(InputRefused, match="too old"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    assert backend.sent == []


def test_old_inspections_release_their_elements(backend: FakeBackend, desktop: Desktop) -> None:
    for _ in range(6):
        desktop.inspect(InspectInput(window_id=1))
    assert backend.forgotten == [11, 12, 13, 14, 15, 16]


def test_an_unfinished_action_is_reported_as_such(backend: FakeBackend, desktop: Desktop) -> None:
    backend.action_finishes = False
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    result = desktop.act_on_element(1, inspection, "e0", "invoke")
    assert "still handling it" in result.detail


def test_no_action_while_an_approval_dialog_is_open(backend: FakeBackend, desktop: Desktop) -> None:
    inspection = desktop.inspect(InspectInput(window_id=1)).inspection_id
    backend.dialog_open = True
    with pytest.raises(InputRefused, match="approval dialog"):
        desktop.act_on_element(1, inspection, "e0", "invoke")
    assert backend.sent == []


def test_the_approval_names_the_control_and_the_audit_omits_the_text(
    backend: FakeBackend, desktop: Desktop, state: Path, tmp_path: Path
) -> None:
    registered = {spec.name: spec for spec in specs(service(state, tmp_path), backend)}
    inspect = registered["inspect_window"]
    inspection = desktop_inspection_id(inspect.handler(inspect.validate({"window_id": 1})))

    spec = registered["set_element_value"]
    assert spec.describe is not None
    arguments = spec.validate(
        {"window_id": 1, "inspection_id": inspection, "element_id": "e1", "value": "s3cret"}
    )
    described = spec.describe(arguments)
    assert ("Control", "the edit 'Text editor'") in described.details
    assert ("New text", "s3cret") in described.details
    assert "s3cret" not in repr(described.audit)
    assert described.audit["characters"] == 6

    invoke = registered["invoke_element"]
    assert invoke.describe is not None
    pressed = invoke.describe(
        invoke.validate({"window_id": 1, "inspection_id": inspection, "element_id": "e0"})
    )
    assert "Invoke the button 'Save'" in pressed.operation


def test_controls_by_name_need_the_screen(
    backend: FakeBackend, state: Path, tmp_path: Path
) -> None:
    without_screen = replace(service(state, tmp_path), screen=ScreenSettings(enabled=False))
    names = [spec.name for spec in specs(without_screen, backend)]
    assert "invoke_element" not in names
    assert "set_element_value" not in names
    assert "inspect_window" not in names


# Real Windows controls, in a window of this process.


def _control_window() -> Any:
    """A small window with a check box and a text field, run on its own thread."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    wndproc_type = ctypes.WINFUNCTYPE(
        lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = lresult
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, wintypes.LPVOID,
    ]  # fmt: skip
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = lresult
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    # 64-bit handles: without these, ctypes would cut them to 32-bit ints.
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = lresult

    class WndClass(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT), ("lpfnWndProc", wndproc_type), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )  # fmt: skip

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
    user32.RegisterClassW.restype = wintypes.ATOM

    def procedure(hwnd: int, message: int, wparam: int, lparam: int) -> int:
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

    callback = wndproc_type(procedure)
    class_name = f"UATControlTest{time.time_ns()}"
    ready = threading.Event()
    handles: dict[str, int] = {}

    def run() -> None:
        instance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(
            ctypes.byref(
                WndClass(lpfnWndProc=callback, hInstance=instance, lpszClassName=class_name)
            )
        )
        parent = user32.CreateWindowExW(
            0x08000000 | 0x80, class_name, "UAT control test", 0x10000000 | 0x00C00000,
            40, 40, 320, 160, None, None, instance, None,
        )  # fmt: skip
        check = user32.CreateWindowExW(
            0, "BUTTON", "UAT check", 0x40000000 | 0x10000000 | 3, 10, 10, 200, 24,
            parent, None, instance, None,
        )  # fmt: skip
        edit = user32.CreateWindowExW(
            0, "EDIT", "before", 0x40000000 | 0x10000000 | 0x00800000 | 0x80, 10, 44, 280, 24,
            parent, None, instance, None,
        )  # fmt: skip
        handles.update(parent=parent or 0, check=check or 0, edit=edit or 0,
                       thread=kernel32.GetCurrentThreadId())  # fmt: skip
        ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(10)

    def text(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buffer, 256)
        return buffer.value

    def checked() -> bool:
        return bool(user32.SendMessageW(handles["check"], 0x00F0, 0, 0))

    def close() -> None:
        user32.PostMessageW(handles["parent"], 0x0010, 0, 0)
        user32.PostThreadMessageW(handles["thread"], 0x0012, 0, 0)
        thread.join(5)

    return handles, text, checked, close, callback


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows only")
def test_real_controls_are_found_and_used_by_name() -> None:
    from universal_ai_tools.desktop.win32 import Win32Backend

    handles, text, checked, close, _keep = _control_window()
    if not handles.get("parent"):
        pytest.skip("cannot create a window here")
    try:
        backend = Win32Backend()
        try:
            found, truncated = backend.inspect_window(handles["parent"], 50)
        except OSError as exc:
            pytest.skip(f"UI Automation is not available: {exc}")
        by_name = {e.name: e for e in found}
        assert not truncated
        check = by_name["UAT check"]
        assert check.control_type == "check box"
        assert "toggle" in check.actions
        edit = next(e for e in found if e.control_type == "edit")
        assert edit.value == "before"
        assert "set_value" in edit.actions

        assert backend.element_action(check.handle, "toggle", None) is True
        assert backend.element_action(edit.handle, "set_value", "after by name") is True
        deadline = time.monotonic() + 5
        while (not checked() or text(handles["edit"]) != "after by name") and (
            time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert checked()
        assert text(handles["edit"]) == "after by name"

        state = backend.element_state(edit.handle)
        assert state is not None
        assert state.value == "after by name"
        backend.forget_elements([check.handle, edit.handle])
        assert backend.element_state(edit.handle) is None
    finally:
        close()


@pytest.mark.skipif(sys.platform != "win32", reason="the real backend is Windows only")
def test_real_pointer_and_primary_screen() -> None:
    from universal_ai_tools.desktop.win32 import Win32Backend

    backend = Win32Backend()
    screen = backend.primary_screen()
    if screen.width <= 0:
        pytest.skip("no interactive desktop")
    try:
        x, y = backend.cursor_position()
    except OSError:
        pytest.skip("no pointer here")
    assert isinstance(x, int)
    assert isinstance(y, int)


@pytest.mark.skipif(sys.platform != "win32", reason="keyboard layouts are Windows only")
def test_characters_map_to_real_keys_on_this_layout() -> None:
    """Typing sends real keys where the layout has them (live: Unicode key events were
    misread by the Windows 11 Notepad). Reads the layout only; sends nothing."""
    import ctypes

    from universal_ai_tools.desktop.win32 import _layout_keys

    key = _layout_keys()
    if ctypes.WinDLL("user32").GetKeyState(0x14) & 1:
        assert key("a") is None  # Caps Lock: Unicode events, never inverted letters
        return
    lower, upper, digit = key("a"), key("A"), key("5")
    if lower is None:
        pytest.skip("this keyboard layout has no 'a' key")
    assert lower[0] == upper[0] == 0x41  # type: ignore[index]
    assert lower[2] is False
    assert upper[2] is True  # type: ignore[index]
    assert digit is not None
    assert key("\U0001f600") is None  # no key: a Unicode key event instead
