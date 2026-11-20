"""Input grants, the banner, and the stop hotkey.

Grants are tested on their own, through ``Desktop``, and through the real gate and MCP server
with a fake desktop and a fake approval dialog. The banner window and the hotkey are tested on
real Windows at the end, without sending any input.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from service_support import Dialog, dialog_server_kwargs, service
from test_desktop import FakeBackend, window
from universal_ai_tools import cli
from universal_ai_tools.desktop import tools as desktop_tools
from universal_ai_tools.desktop.grants import (
    GRANT_TOOLS,
    REVOKED_FILE_NAME,
    GrantStore,
    revoke_all,
    revoked_file,
)
from universal_ai_tools.desktop.stop import STOP_FILE_NAME
from universal_ai_tools.desktop.tools import (
    STOP_HOTKEY,
    Desktop,
    GrantInput,
    InputRefused,
    PressKeysInput,
    specs,
)
from universal_ai_tools.server import create_server
from universal_ai_tools.settings import ServiceConfig
from universal_ai_tools.tools import ToolCallError

INPUT = {
    "enabled": True,
    "allowed_processes": ["notepad.exe", "excel.exe"],
    "grants": True,
    "max_grant_minutes": 30,
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.wall = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def config(state: Path, workspace: Path, **input_changes: Any) -> ServiceConfig:
    return service(
        state, workspace, {"screen": {"enabled": True}, "input": {**INPUT, **input_changes}}
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(
        windows={
            1: window(1, "notepad.exe"),
            2: window(2, "chrome.exe"),
            3: window(3, "excel.exe"),
            4: window(4, "claude.exe"),
        },
        foreground=1,
        under_point=1,
    )


def grant(desktop: Desktop, programs: list[str], minutes: int = 10) -> str:
    return desktop.request_grant(
        GrantInput(programs=programs, minutes=minutes, purpose="fill in the form")
    ).grant_id


# The store.


def test_a_grant_covers_its_programs_and_the_input_tools_only(tmp_path: Path) -> None:
    store = GrantStore(tmp_path / REVOKED_FILE_NAME)
    made = store.add(["notepad.exe"], 5, "x")
    assert store.covering("type_text", "notepad.exe") == made
    assert store.covering("type_text", "excel.exe") is None
    assert store.covering("capture_screen", "notepad.exe") is None
    assert store.covering("request_input_grant", "notepad.exe") is None
    assert store.covering("type_text", "") is None
    assert "request_input_grant" not in GRANT_TOOLS


def test_a_grant_expires(tmp_path: Path) -> None:
    clock = Clock()
    store = GrantStore(tmp_path / REVOKED_FILE_NAME, clock=clock, wall=clock.time)
    store.add(["notepad.exe"], 2, "x")
    clock.advance(119)
    assert store.covering("type_text", "notepad.exe") is not None
    clock.advance(2)
    assert store.covering("type_text", "notepad.exe") is None
    assert store.active() == []


def test_revoking_voids_older_grants_in_every_process(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / REVOKED_FILE_NAME
    first = GrantStore(path, clock=clock, wall=clock.time)
    second = GrantStore(path, clock=clock, wall=clock.time)
    first.add(["notepad.exe"], 10, "x")
    second.add(["notepad.exe"], 10, "x")
    clock.advance(1)
    revoke_all(path, wall=clock.time)
    assert first.active() == []
    assert second.active() == []
    clock.advance(1)
    second.add(["notepad.exe"], 10, "x")  # a new grant after the revocation counts
    assert len(second.active()) == 1


def test_an_unreadable_revocation_voids_everything(tmp_path: Path) -> None:
    path = tmp_path / REVOKED_FILE_NAME
    store = GrantStore(path)
    store.add(["notepad.exe"], 10, "x")
    path.write_text("not a time", encoding="utf-8")
    assert store.active() == []


def test_revoke_all_clears_this_process_even_without_a_file(tmp_path: Path) -> None:
    store = GrantStore(None)
    store.add(["notepad.exe"], 10, "x")
    store.revoke_all()
    assert store.active() == []


# Through Desktop.


@pytest.fixture
def desktop(backend: FakeBackend, state: Path, workspace: Path) -> Desktop:
    return Desktop(backend, config(state, workspace))


def test_a_grant_covers_allowed_programs_only(backend: FakeBackend, desktop: Desktop) -> None:
    grant_id = grant(desktop, ["Notepad.EXE"])
    assert desktop.grant_for("type_text", 1) == grant_id
    assert desktop.grant_for("type_text", 3) is None  # excel.exe: allowed, but not granted
    assert desktop.grant_for("type_text", 2) is None
    assert desktop.grant_for("type_text", 99) is None


@pytest.mark.parametrize(
    ("programs", "minutes", "message"),
    [
        (["chrome.exe"], 5, "not granted: chrome.exe"),
        (["notepad.exe", "claude.exe"], 5, "not granted: claude.exe"),
        (["notepad.exe"], 31, "at most 30 minutes"),
    ],
)
def test_grants_beyond_the_configuration_are_refused(
    desktop: Desktop, programs: list[str], minutes: int, message: str
) -> None:
    with pytest.raises(ToolCallError, match=message):
        grant(desktop, programs, minutes)
    assert desktop.grants.active() == []


def test_grants_need_a_list_of_allowed_programs(
    backend: FakeBackend, state: Path, workspace: Path
) -> None:
    open_desktop = Desktop(backend, config(state, workspace, allowed_processes=[]))
    with pytest.raises(ToolCallError, match="allowed_processes"):
        grant(open_desktop, ["notepad.exe"])


def test_a_protected_window_is_never_covered(
    backend: FakeBackend, state: Path, workspace: Path
) -> None:
    lenient = Desktop(
        backend, config(state, workspace, allowed_processes=["notepad.exe", "claude.exe"])
    )
    with pytest.raises(ToolCallError, match=r"claude\.exe"):
        grant(lenient, ["claude.exe"])
    lenient.grants.add(["claude.exe"], 10, "forced past the request checks")
    assert lenient.grant_for("type_text", 4) is None


def test_the_hotkey_stops_input_and_ends_grants(
    backend: FakeBackend, desktop: Desktop, state: Path
) -> None:
    grant(desktop, ["notepad.exe"])
    assert backend.hotkey is not None
    backend.hotkey()
    assert desktop.grant_for("type_text", 1) is None
    assert (state / STOP_FILE_NAME).is_file()
    assert (state / REVOKED_FILE_NAME).is_file()
    with pytest.raises(InputRefused, match="input is stopped"):
        desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    with pytest.raises(InputRefused, match="input is stopped"):
        grant(desktop, ["notepad.exe"])


def test_a_stop_alone_suspends_every_grant(desktop: Desktop) -> None:
    """A stop written without revoking (an older version, or by hand) still blocks grants."""
    grant(desktop, ["notepad.exe"])
    desktop.stop.trigger("test")
    assert desktop.grants.active() != []
    assert desktop.grant_for("type_text", 1) is None


def test_input_stop_from_the_cli_revokes_grants(state: Path, workspace: Path) -> None:
    path = state / "config.toml"
    state.mkdir(parents=True, exist_ok=True)
    path.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["input", "--config", str(path), "stop"])
    assert (state / REVOKED_FILE_NAME).is_file()


def test_the_corner_ends_grants_too(backend: FakeBackend, desktop: Desktop) -> None:
    grant(desktop, ["notepad.exe"])
    backend.cursor = (0, 0)
    with pytest.raises(InputRefused):
        desktop.press_keys(PressKeysInput(window_id=1, keys=["enter"]))
    assert desktop.grants.active() == []


def test_the_banner_shows_only_while_a_grant_is_active(
    backend: FakeBackend, desktop: Desktop
) -> None:
    assert backend.banner is not None
    assert backend.banner() is None
    grant(desktop, ["notepad.exe", "excel.exe"], minutes=12)
    text = backend.banner()
    assert text is not None
    assert "excel.exe, notepad.exe" in text
    assert "12 min left" in text
    assert STOP_HOTKEY in text
    desktop.grants.revoke_all()
    assert backend.banner() is None


def test_no_banner_or_hotkey_without_input(
    backend: FakeBackend, state: Path, workspace: Path
) -> None:
    Desktop(backend, service(state, workspace, {"screen": {"enabled": True}}))
    assert backend.banner is None
    assert backend.hotkey is None


def test_the_grant_tool_exists_only_when_enabled(
    backend: FakeBackend, state: Path, workspace: Path
) -> None:
    names = [spec.name for spec in specs(config(state, workspace, grants=False), backend)]
    assert "request_input_grant" not in names
    registered = {spec.name: spec for spec in specs(config(state, workspace), backend)}
    assert registered["request_input_grant"].always_ask is True
    assert registered["request_input_grant"].grant is None
    assert registered["type_text"].grant is not None
    assert registered["capture_screen"].grant is None
    off = {spec.name: spec for spec in specs(config(state, workspace, grants=False), backend)}
    assert off["type_text"].grant is None


# Through the real gate and MCP server.


@pytest.fixture
def fake_desktop(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> FakeBackend:
    monkeypatch.setattr(desktop_tools, "default_backend", lambda: backend)
    return backend


def _records(state: Path) -> list[dict[str, Any]]:
    log = (state / "audit.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in log.splitlines()]


@pytest.mark.anyio
async def test_one_approval_covers_many_actions(
    state: Path, workspace: Path, fake_desktop: FakeBackend
) -> None:
    dialog = Dialog("yes")
    server = create_server(config(state, workspace), **dialog_server_kwargs(dialog))
    async with Client(server) as client:
        granted = await client.call_tool(
            "request_input_grant",
            {"programs": ["notepad.exe"], "minutes": 10, "purpose": "type the letter"},
        )
        typed = [
            await client.call_tool("type_text", {"window_id": 1, "text": f"line {n}"})
            for n in range(3)
        ]
        other = await client.call_tool("type_text", {"window_id": 3, "text": "excel"})

    assert granted.is_error is False
    assert all(result.is_error is False for result in typed)
    assert other.is_error is False
    # Asked twice: the grant, and excel.exe, which the grant does not cover.
    assert len(dialog.asked) == 2
    assert "notepad.exe for 10 minutes" in dialog.asked[0]
    assert "type the letter" in dialog.asked[0]
    assert STOP_HOTKEY in dialog.asked[0]
    assert "excel" in dialog.asked[1]
    assert fake_desktop.sent == [
        ("type", "line 0"),
        ("type", "line 1"),
        ("type", "line 2"),
        ("type", "excel"),
    ]
    started = [r for r in _records(state) if r["event"] == "started" and r["tool"] == "type_text"]
    channels = [r["approval"]["channel"] for r in started]
    assert channels == ["grant", "grant", "grant", "desktop_dialog"]
    grant_id = json.loads(granted.content[0].text)["grant_id"]  # type: ignore[union-attr]
    assert {r["approval"].get("grant") for r in started[:3]} == {grant_id}


@pytest.mark.anyio
async def test_the_grant_request_itself_is_always_asked(
    state: Path, workspace: Path, fake_desktop: FakeBackend
) -> None:
    dialog = Dialog("yes")
    server = create_server(
        config(state, workspace, approval="once_per_session"), **dialog_server_kwargs(dialog)
    )
    request = {"programs": ["notepad.exe"], "minutes": 5, "purpose": "x"}
    async with Client(server) as client:
        await client.call_tool("request_input_grant", request)
        await client.call_tool("request_input_grant", request)
        dialog.answer = "no"
        denied = await client.call_tool("request_input_grant", request)
    assert len(dialog.asked) == 3
    assert denied.is_error is True


@pytest.mark.anyio
async def test_a_denied_grant_grants_nothing(
    state: Path, workspace: Path, fake_desktop: FakeBackend
) -> None:
    dialog = Dialog("no")
    server = create_server(config(state, workspace), **dialog_server_kwargs(dialog))
    async with Client(server) as client:
        await client.call_tool(
            "request_input_grant", {"programs": ["notepad.exe"], "minutes": 5, "purpose": "x"}
        )
        typed = await client.call_tool("type_text", {"window_id": 1, "text": "hi"})
    assert typed.is_error is True
    assert len(dialog.asked) == 2
    assert fake_desktop.sent == []


@pytest.mark.anyio
async def test_revoking_brings_the_questions_back(
    state: Path, workspace: Path, fake_desktop: FakeBackend
) -> None:
    dialog = Dialog("yes")
    server = create_server(config(state, workspace), **dialog_server_kwargs(dialog))
    async with Client(server) as client:
        await client.call_tool(
            "request_input_grant", {"programs": ["notepad.exe"], "minutes": 5, "purpose": "x"}
        )
        await client.call_tool("type_text", {"window_id": 1, "text": "a"})
        time.sleep(0.01)
        revoke_all(revoked_file(state))
        dialog.answer = "no"
        refused = await client.call_tool("type_text", {"window_id": 1, "text": "b"})
    assert refused.is_error is True
    assert len(dialog.asked) == 2
    assert fake_desktop.sent == [("type", "a")]


@pytest.mark.anyio
async def test_a_covered_action_still_obeys_every_input_rule(
    state: Path, workspace: Path, fake_desktop: FakeBackend
) -> None:
    dialog = Dialog("yes")
    server = create_server(config(state, workspace), **dialog_server_kwargs(dialog))
    async with Client(server) as client:
        await client.call_tool(
            "request_input_grant", {"programs": ["notepad.exe"], "minutes": 5, "purpose": "x"}
        )
        fake_desktop.dialog_open = True
        blocked = await client.call_tool("type_text", {"window_id": 1, "text": "a"})
    assert blocked.is_error is True
    assert fake_desktop.sent == []


def test_the_cli_revokes_grants(
    state: Path, workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state / "config.toml"
    state.mkdir(parents=True, exist_ok=True)
    path.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["grants", "--config", str(path), "revoke"])
    assert exit_info.value.code == 0
    assert (state / REVOKED_FILE_NAME).is_file()
    assert "revoked" in capsys.readouterr().out


def test_grants_can_be_enabled_from_the_cli(state: Path, workspace: Path) -> None:
    from universal_ai_tools.settings import read_config_file

    path = state / "config.toml"
    state.mkdir(parents=True, exist_ok=True)
    path.write_text(f"version = 1\nworkspace = {json.dumps(str(workspace))}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["config", "--config", str(path), "enable", "grants"])
    loaded = read_config_file(path)
    assert loaded.input.grants is True
    assert loaded.input.enabled is False  # grants alone do not turn input on
    assert loaded.input.max_grant_minutes == 30


# The real banner and hotkey (Windows): no input is sent.


@pytest.mark.skipif(sys.platform != "win32", reason="the overlay is Windows only")
def test_the_real_banner_shows_hides_and_lets_clicks_through() -> None:
    import ctypes
    from ctypes import wintypes

    from universal_ai_tools.desktop.overlay import EX_STYLE, HOTKEY_ID, Overlay

    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    text: list[str | None] = [None]
    pressed: list[bool] = []
    overlay = Overlay(lambda: text[0], lambda: pressed.append(True), interval=0.1)
    try:
        if not overlay.hwnd:
            pytest.skip("no interactive desktop")

        def settle(want: bool) -> bool:
            deadline = time.monotonic() + 3
            while overlay.visible != want and time.monotonic() < deadline:
                time.sleep(0.05)
            return overlay.visible

        assert settle(False) is False
        text[0] = "Claude is controlling notepad.exe (test)"
        assert settle(True) is True
        style = user32.GetWindowLongPtrW(overlay.hwnd, -20)  # GWL_EXSTYLE
        assert style & EX_STYLE == EX_STYLE
        rect = wintypes.RECT()
        user32.GetWindowRect(overlay.hwnd, ctypes.byref(rect))
        middle = wintypes.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        assert (user32.WindowFromPoint(middle) or 0) != overlay.hwnd  # clicks pass through

        user32.PostMessageW(overlay.hwnd, 0x0312, HOTKEY_ID, 0)  # as if the hotkey was pressed
        deadline = time.monotonic() + 3
        while not pressed and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pressed == [True]
        assert isinstance(overlay.hotkey_registered, bool)

        text[0] = None
        assert settle(False) is False
    finally:
        overlay.close()
