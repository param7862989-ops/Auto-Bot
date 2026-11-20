"""Reading text and tables, waiting for windows and controls, and launching listed programs.

Against the fake desktop, except where marked: the real UI Automation text reader on a
window this test creates, and the real program lookup (nothing is launched by any test).
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from test_desktop import FakeBackend, window
from test_desktop_control import element, service
from universal_ai_tools import cli
from universal_ai_tools.desktop import launch as launcher
from universal_ai_tools.desktop.backend import TableData
from universal_ai_tools.desktop.tools import (
    Desktop,
    InputRefused,
    InspectInput,
    LaunchInput,
    ReadTableInput,
    ReadTextInput,
    WaitInput,
    specs,
)
from universal_ai_tools.settings import InputSettings, ServiceConfig
from universal_ai_tools.tools import ToolCallError


@pytest.fixture
def backend() -> FakeBackend:
    fake = FakeBackend(windows={1: window(1, "notepad.exe")}, foreground=1, under_point=1)
    fake.elements = {
        1: [
            element(11, "document", "Text editor", "set_value", readable=True),
            element(12, "data grid", "Invoices", table=True),
            element(13, "edit", "Password", password=True, readable=True),
            element(14, "button", "Save", "invoke"),
        ]
    }
    fake.texts = {11: "first line\nsecond \x1b[31mline", 13: "hunter2"}
    fake.tables = {
        12: TableData(
            ("Number", "Amount"), (("1", "10.00"), ("2", "\u202e20.00"), ("3", "30.00")), 3, 2
        )
    }
    return fake


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


def with_launchable(config: ServiceConfig, *programs: str) -> ServiceConfig:
    settings = InputSettings(
        enabled=True, allowed_processes=config.input.allowed_processes, launchable=programs
    )
    return replace(config, input=settings)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def desktop(backend: FakeBackend, state: Path, tmp_path: Path, clock: Clock) -> Desktop:
    return Desktop(backend, service(state, tmp_path), clock=clock, sleep=clock.sleep)


def inspection(desktop: Desktop) -> str:
    return desktop.inspect(InspectInput(window_id=1)).inspection_id


# Inspection filters.


def test_inspection_can_be_filtered(desktop: Desktop) -> None:
    by_type = desktop.inspect(InspectInput(window_id=1, control_type="Button"))
    assert [(e.element_id, e.name) for e in by_type.elements] == [("e3", "Save")]
    by_name = desktop.inspect(InspectInput(window_id=1, name_contains="INVOI"))
    assert [(e.element_id, e.table) for e in by_name.elements] == [("e1", True)]
    assert desktop.inspect(InspectInput(window_id=1, name_contains="nothing")).elements == []


def test_filtered_ids_match_the_unfiltered_ones(desktop: Desktop) -> None:
    """An element id means the same element however the inspection was filtered."""
    filtered = desktop.inspect(InspectInput(window_id=1, control_type="button"))
    result = desktop.act_on_element(1, filtered.inspection_id, "e3", "invoke")
    assert "the button 'Save'" in result.detail


# Reading text.


def test_reading_a_document(desktop: Desktop) -> None:
    result = desktop.read_text(
        ReadTextInput(window_id=1, inspection_id=inspection(desktop), element_id="e0")
    )
    assert result.text.startswith("first line\nsecond ")
    assert "\x1b" not in result.text  # control characters are shown escaped
    assert result.trust == "untrusted"
    assert result.truncated is False


def test_reading_is_bounded(desktop: Desktop) -> None:
    result = desktop.read_text(
        ReadTextInput(window_id=1, inspection_id=inspection(desktop), element_id="e0", max_chars=5)
    )
    assert result.text == "first"
    assert result.truncated is True


def test_a_password_field_is_never_read(backend: FakeBackend, desktop: Desktop) -> None:
    with pytest.raises(ToolCallError, match="password"):
        desktop.read_text(
            ReadTextInput(window_id=1, inspection_id=inspection(desktop), element_id="e2")
        )


def test_reading_needs_the_same_live_element(backend: FakeBackend, desktop: Desktop) -> None:
    current = inspection(desktop)
    backend.live[11] = None
    with pytest.raises(ToolCallError, match="no longer exists"):
        desktop.read_text(ReadTextInput(window_id=1, inspection_id=current, element_id="e0"))
    backend.live[11] = element(11, "document", "Text editor", process_id=666)
    with pytest.raises(ToolCallError, match="no longer exists"):
        desktop.read_text(ReadTextInput(window_id=1, inspection_id=current, element_id="e0"))
    backend.live.clear()
    backend.windows[1] = window(1, "notepad.exe", pid=5555)
    with pytest.raises(ToolCallError, match="closed or changed"):
        desktop.read_text(ReadTextInput(window_id=1, inspection_id=current, element_id="e0"))


def test_a_password_field_reported_late_is_not_read(backend: FakeBackend, desktop: Desktop) -> None:
    current = inspection(desktop)
    backend.live[11] = element(11, "document", "Text editor", password=True)
    with pytest.raises(ToolCallError, match="password"):
        desktop.read_text(ReadTextInput(window_id=1, inspection_id=current, element_id="e0"))


# Reading tables.


def test_reading_a_table(desktop: Desktop) -> None:
    result = desktop.read_table(
        ReadTableInput(window_id=1, inspection_id=inspection(desktop), element_id="e1")
    )
    assert result.headers == ["Number", "Amount"]
    assert result.rows[0] == ["1", "10.00"]
    assert "\u202e" not in result.rows[1][1]
    assert (result.total_rows, result.total_columns, result.truncated) == (3, 2, False)


def test_a_table_is_bounded(desktop: Desktop) -> None:
    result = desktop.read_table(
        ReadTableInput(
            window_id=1, inspection_id=inspection(desktop), element_id="e1", max_rows=2,
            max_columns=1,
        )
    )  # fmt: skip
    assert result.rows == [["1"], ["2"]]
    assert result.truncated is True


def test_something_that_is_not_a_table(desktop: Desktop) -> None:
    with pytest.raises(ToolCallError, match="not a table"):
        desktop.read_table(
            ReadTableInput(window_id=1, inspection_id=inspection(desktop), element_id="e3")
        )


# Waiting.


def test_waiting_for_a_window_that_appears(
    backend: FakeBackend, desktop: Desktop, clock: Clock
) -> None:
    def appear(seconds: float) -> None:
        clock.now += seconds
        if clock.now >= 102:
            backend.windows[7] = window(7, "notepad.exe", title="Save As")

    desktop._sleep = appear
    result = desktop.wait_for(WaitInput(title_contains="save as"))
    assert result.found is True
    assert [w.window_id for w in result.windows] == [7]
    assert result.waited_seconds == 2.0


def test_waiting_gives_up_after_the_timeout(desktop: Desktop) -> None:
    result = desktop.wait_for(WaitInput(process="excel.exe", timeout_seconds=3))
    assert result.found is False
    assert result.waited_seconds >= 3


def test_waiting_for_a_control(backend: FakeBackend, desktop: Desktop) -> None:
    result = desktop.wait_for(WaitInput(window_id=1, name_contains="save"))
    assert result.found is True
    assert result.inspection is not None
    assert [e.name for e in result.inspection.elements] == ["Save"]


def test_waiting_needs_something_to_wait_for(desktop: Desktop) -> None:
    with pytest.raises(ToolCallError, match="process or title_contains"):
        desktop.wait_for(WaitInput())
    with pytest.raises(ToolCallError, match="name_contains or control_type"):
        desktop.wait_for(WaitInput(window_id=1))


# Launching.


@pytest.mark.parametrize(
    "name", ["cmd.exe", "powershell.exe", "python.exe", "mshta.exe", "rundll32.exe",
             "regedit.exe", "explorer.exe", "claude.exe", "notepad", "..\\x.exe", "a b/c.exe"],
)  # fmt: skip
def test_dangerous_or_odd_programs_are_never_launched(name: str) -> None:
    assert launcher.refusal(name.casefold()) is not None
    with pytest.raises(launcher.LaunchRefused):
        launcher.resolve(name.casefold())


def test_only_trusted_folders_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    (windows / "System32" / "notepad.exe").write_bytes(b"MZ")
    user = tmp_path / "Users" / "me"
    user.mkdir(parents=True)
    (user / "evil.exe").write_bytes(b"MZ")
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    monkeypatch.setattr(launcher, "_app_paths", lambda name: [user / name])

    assert (
        launcher.resolve("notepad.exe", roots=[windows])
        == (windows / "System32" / "notepad.exe").resolve()
    )
    with pytest.raises(launcher.LaunchRefused, match="not found under"):
        launcher.resolve("evil.exe", roots=[windows])
    # An "App Paths" entry naming a different program, even a trusted one, is not used.
    (windows / "System32" / "other.exe").write_bytes(b"MZ")
    monkeypatch.setattr(launcher, "_app_paths", lambda name: [windows / "System32" / "other.exe"])
    with pytest.raises(launcher.LaunchRefused, match="not found under"):
        launcher.resolve("renamed.exe", roots=[windows])


def test_launching_returns_the_new_window(
    backend: FakeBackend,
    state: Path,
    tmp_path: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Path] = []
    exe = tmp_path / "notepad.exe"
    monkeypatch.setattr(launcher, "resolve", lambda name: exe)

    def start(path: Path) -> None:
        started.append(path)
        backend.windows[9] = window(9, "notepad.exe", title="Untitled - Notepad")

    monkeypatch.setattr(launcher, "start", start)
    config = with_launchable(service(state, tmp_path), "notepad.exe")
    desktop = Desktop(backend, config, clock=clock, sleep=clock.sleep)

    result = desktop.launch(LaunchInput(program="Notepad.exe"))

    assert started == [exe]
    assert [w.window_id for w in result.windows] == [9]
    assert "its new window" in result.detail


def test_only_listed_programs_launch(
    backend: FakeBackend, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []
    monkeypatch.setattr(launcher, "start", started.append)
    desktop = Desktop(backend, with_launchable(service(state, tmp_path), "notepad.exe"))
    with pytest.raises(ToolCallError, match="not in \\[input\\] launchable"):
        desktop.launch(LaunchInput(program="calc.exe"))
    assert started == []


def test_nothing_launches_while_input_is_stopped(
    backend: FakeBackend, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []
    monkeypatch.setattr(launcher, "start", started.append)
    monkeypatch.setattr(launcher, "resolve", lambda name: tmp_path / name)
    desktop = Desktop(backend, with_launchable(service(state, tmp_path), "notepad.exe"))
    desktop.stop.trigger("test")
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.launch(LaunchInput(program="notepad.exe"))
    assert started == []


def test_the_launch_tool_always_asks_and_exists_only_when_listed(
    backend: FakeBackend, state: Path, tmp_path: Path
) -> None:
    assert "launch_app" not in [s.name for s in specs(service(state, tmp_path), backend)]
    config = with_launchable(service(state, tmp_path), "notepad.exe")
    registered = {s.name: s for s in specs(config, backend)}
    spec = registered["launch_app"]
    assert spec.always_ask is True
    assert spec.grant is None
    assert spec.describe is not None
    described = spec.describe(spec.validate({"program": "Notepad.EXE"}))
    assert ("Program", "notepad.exe") in described.details


def _cli(*arguments: str) -> int:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(list(arguments))
    code = exit_info.value.code
    return code if isinstance(code, int) else 1


def test_the_cli_lists_launchable_programs_but_refuses_dangerous_ones(tmp_path: Path) -> None:
    from universal_ai_tools.settings import read_config_file

    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = tmp_path / "state" / "config.toml"
    config.parent.mkdir()
    config.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")
    assert _cli("config", "--config", str(config), "allow-launch", "notepad.exe") == 0
    assert read_config_file(config).input.launchable == ("notepad.exe",)
    assert _cli("config", "--config", str(config), "allow-launch", "cmd.exe") == 2
    assert read_config_file(config).input.launchable == ("notepad.exe",)


# Real Windows: lookup and text reading. Nothing is launched.


@pytest.mark.skipif(sys.platform != "win32", reason="program lookup is Windows only")
def test_the_real_notepad_is_found_in_a_trusted_folder() -> None:
    try:
        path = launcher.resolve("notepad.exe")
    except launcher.LaunchRefused:
        pytest.skip("no notepad.exe on this Windows")
    assert path.name.casefold() == "notepad.exe"
    assert any(path.is_relative_to(root.resolve()) for root in launcher.trusted_roots())


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows only")
def test_real_text_is_read_by_name() -> None:
    from test_desktop_control import _control_window
    from universal_ai_tools.desktop.win32 import Win32Backend

    handles, _text, _checked, close, _keep = _control_window()
    if not handles.get("parent"):
        pytest.skip("cannot create a window here")
    try:
        backend = Win32Backend()
        try:
            found, _ = backend.inspect_window(handles["parent"], 50)
        except OSError as exc:
            pytest.skip(f"UI Automation is not available: {exc}")
        edit = next(e for e in found if e.control_type == "edit")
        assert edit.readable is True
        assert backend.element_text(edit.handle, 100) == "before"
        check = next(e for e in found if e.name == "UAT check")
        with pytest.raises(OSError, match="not a table"):
            backend.element_table(check.handle, 5, 5)
    finally:
        close()
