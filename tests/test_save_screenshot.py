"""Saving screenshots as new PNG files in the workspace, for documents and reports."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from mcp import Client

from service_support import Dialog, dialog_server_kwargs, service
from test_desktop import FakeBackend, window
from universal_ai_tools.desktop import tools as desktop_tools
from universal_ai_tools.desktop.tools import Desktop, SaveScreenshotInput, specs
from universal_ai_tools.server import create_server
from universal_ai_tools.tools import ToolCallError, ToolInputError
from universal_ai_tools.workspace import Workspace, WorkspaceError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    (path / "shots").mkdir(parents=True)
    return path


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(windows={1: window(1, "notepad.exe")}, foreground=1, under_point=1)


@pytest.fixture
def desktop(backend: FakeBackend, workspace: Path, tmp_path: Path) -> Desktop:
    config = service(tmp_path / "state", workspace, {"screen": {"enabled": True}})
    return Desktop(backend, config, workspace=Workspace(workspace))


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(PNG_SIGNATURE)
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_the_whole_screen_is_saved_at_full_size(desktop: Desktop, workspace: Path) -> None:
    result = desktop.save_screenshot(SaveScreenshotInput(path="shots/screen.png"))
    saved = (workspace / "shots" / "screen.png").read_bytes()
    assert png_size(saved) == (2560, 1440)  # not scaled down like capture_screen's
    assert (result.path, result.width, result.size_bytes) == ("shots/screen.png", 2560, len(saved))


def test_one_window_is_saved(desktop: Desktop, workspace: Path) -> None:
    result = desktop.save_screenshot(SaveScreenshotInput(path="shots/notepad.png", window_id=1))
    assert png_size((workspace / "shots" / "notepad.png").read_bytes()) == (800, 600)
    assert result.window_title == "notepad.exe window"


def test_an_existing_file_is_never_replaced(desktop: Desktop, workspace: Path) -> None:
    (workspace / "shots" / "keep.png").write_bytes(b"mine")
    with pytest.raises(WorkspaceError):
        desktop.save_screenshot(SaveScreenshotInput(path="shots/keep.png"))
    assert (workspace / "shots" / "keep.png").read_bytes() == b"mine"


@pytest.mark.parametrize("path", ["../outside.png", ".git/x.png", "missing/x.png", "C:/x.png"])
def test_only_new_files_inside_the_workspace(desktop: Desktop, workspace: Path, path: str) -> None:
    with pytest.raises(WorkspaceError):
        desktop.save_screenshot(SaveScreenshotInput(path=path))
    assert not (workspace.parent / "outside.png").exists()


def test_only_png_names(backend: FakeBackend, workspace: Path, tmp_path: Path) -> None:
    config = service(tmp_path / "state", workspace, {"screen": {"enabled": True}})
    spec = {s.name: s for s in specs(config, backend, workspace=Workspace(workspace))}[
        "save_screenshot"
    ]
    for bad in ("shots/x.txt", "shots/x.png.exe", "x.pn"):
        with pytest.raises(ToolInputError):
            spec.validate({"path": bad})
    assert spec.validate({"path": "shots/A.PNG"}).path == "shots/A.PNG"


def test_no_workspace_no_saving(backend: FakeBackend, workspace: Path, tmp_path: Path) -> None:
    config = service(tmp_path / "state", workspace, {"screen": {"enabled": True}})
    assert "save_screenshot" not in [s.name for s in specs(config, backend)]
    with pytest.raises(ToolCallError, match="only in file mode"):
        Desktop(backend, config).save_screenshot(SaveScreenshotInput(path="shots/x.png"))


@pytest.mark.anyio
async def test_saving_is_approved_separately_from_looking(
    workspace: Path, tmp_path: Path, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_tools, "default_backend", lambda: backend)
    state = tmp_path / "state"
    dialog = Dialog("yes")
    server = create_server(
        service(state, workspace, {"screen": {"enabled": True}}), **dialog_server_kwargs(dialog)
    )
    async with Client(server) as client:
        await client.call_tool("capture_screen", {})
        first = await client.call_tool("save_screenshot", {"path": "shots/one.png"})
        second = await client.call_tool("save_screenshot", {"path": "shots/two.png"})

    assert first.is_error is False
    assert second.is_error is False
    # Looking at the screen, then saving: two questions; the second save is in the session.
    assert len(dialog.asked) == 2
    assert "shots/one.png" in dialog.asked[1]
    assert "new file in the workspace" in dialog.asked[1]
    records = [
        json.loads(line)
        for line in (state / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    saves = [r for r in records if r.get("tool") == "save_screenshot" and r["event"] == "finished"]
    assert [r["target"]["path"] for r in saves] == ["shots/one.png", "shots/two.png"]
    assert all(r["risk"] == "create" for r in saves)
