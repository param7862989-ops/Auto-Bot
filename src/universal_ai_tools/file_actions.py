"""Tools that change existing files: ``edit_file``, ``create_directory``, and ``move_path``.

Registered only in file mode with ``[filesystem] write = true``, next to ``write_file``.
Every call needs the user's approval (see ``universal_ai_tools.gate``). All filesystem
access goes through ``Workspace``, so the workspace boundary, link refusal, and the policy
(``Operation.MODIFY`` for what is changed, ``Operation.CREATE`` for what is made) apply.

Each tool reports evidence, not intent: ``edit_file`` reads the file back after replacing it
and compares hashes; ``move_path`` checks that the item now at the destination is the one
that was moved and that nothing is left at the source. Nothing here deletes anything.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from universal_ai_tools.approval import ExactPath, ExactText
from universal_ai_tools.filesystem import FilesystemLimits
from universal_ai_tools.tools import STRICT_INPUT, ActionDescription, ToolRisk, ToolSpec
from universal_ai_tools.workspace import PathNotFoundError, Workspace, WorkspaceError

MAX_EDIT_TEXT_CHARS = 200_000
MAX_REPLACEMENTS = 1000

_PATH_HELP = (
    "Path relative to the workspace root, using '/' or '\\' separators. Absolute paths, "
    "drive letters, and '..' are rejected."
)


class EditFileInput(BaseModel):
    model_config = STRICT_INPUT

    path: Annotated[str, Field(description=f"Existing UTF-8 text file to change. {_PATH_HELP}")]
    old_text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_EDIT_TEXT_CHARS,
            description="Exact text to replace, as it appears in the file (whitespace and line "
            "breaks included). Read the file first. If the file uses CRLF line endings, LF in "
            "this text also matches CRLF.",
        ),
    ]
    new_text: Annotated[
        str,
        Field(max_length=MAX_EDIT_TEXT_CHARS, description="Replacement text. May be empty."),
    ]
    expected_replacements: Annotated[
        int,
        Field(
            ge=1,
            le=MAX_REPLACEMENTS,
            description="How many times old_text must occur. The edit is refused unless the "
            "count matches exactly, so an ambiguous match never changes the wrong place.",
        ),
    ] = 1


class EditFileResult(BaseModel):
    path: str
    replacements: int
    bytes_before: int
    bytes_after: int
    sha256_before: str
    sha256_after: str
    verified: bool = Field(
        description="True when the file was read back after the change and matched exactly."
    )


class CreateDirectoryInput(BaseModel):
    model_config = STRICT_INPUT

    path: Annotated[
        str,
        Field(description=f"New directory. Its parent must already exist. {_PATH_HELP}"),
    ]


class CreateDirectoryResult(BaseModel):
    path: str
    verified: bool = Field(description="True when a directory was found at the path afterwards.")


class MovePathInput(BaseModel):
    model_config = STRICT_INPUT

    source: Annotated[str, Field(description=f"Existing file or directory. {_PATH_HELP}")]
    destination: Annotated[
        str,
        Field(
            description="New path. Nothing may exist there yet, and its parent directory must "
            f"exist. {_PATH_HELP}"
        ),
    ]


class MovePathResult(BaseModel):
    source: str
    destination: str
    type: Literal["file", "directory"]
    verified: bool = Field(
        description="True when the moved item was found at the destination and the source "
        "path no longer exists."
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def edit_file(
    workspace: Workspace, arguments: EditFileInput, limits: FilesystemLimits
) -> EditFileResult:
    """Replace ``old_text`` with ``new_text`` exactly ``expected_replacements`` times."""
    counted: dict[str, int] = {}

    def transform(data: bytes) -> bytes:
        if b"\x00" in data:
            raise WorkspaceError(f"not a text file (contains NUL bytes): '{arguments.path}'")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                f"not valid UTF-8 at byte {exc.start}: '{arguments.path}'"
            ) from None
        old, new = arguments.old_text, arguments.new_text
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count != arguments.expected_replacements:
            raise WorkspaceError(
                f"old_text occurs {count} time(s) in '{arguments.path}', expected "
                f"{arguments.expected_replacements}; nothing was changed"
            )
        if "\x00" in new:
            raise WorkspaceError("new_text must not contain NUL characters")
        result = text.replace(old, new).encode("utf-8", "strict")
        if len(result) > limits.max_write_bytes:
            raise WorkspaceError(
                f"the edited file would be {len(result)} bytes; the limit is "
                f"{limits.max_write_bytes} bytes; nothing was changed"
            )
        counted["n"] = count
        return result

    try:
        old, new = workspace.replace_file(
            arguments.path, transform, max_read_bytes=limits.max_read_bytes
        )
    except UnicodeEncodeError:
        raise WorkspaceError("new_text is not valid Unicode; nothing was changed") from None
    target = workspace.resolve(arguments.path)
    readback = workspace.read_bytes(target, limits.max_read_bytes)
    relative = target.relative
    return EditFileResult(
        path=relative,
        replacements=counted["n"],
        bytes_before=len(old),
        bytes_after=len(new),
        sha256_before=_sha256(old),
        sha256_after=_sha256(readback),
        verified=readback == new,
    )


def create_directory(
    workspace: Workspace, arguments: CreateDirectoryInput
) -> CreateDirectoryResult:
    relative = workspace.make_directory(arguments.path)
    found = workspace.resolve(relative)
    return CreateDirectoryResult(path=relative, verified=found.is_dir and not found.is_link)


def move_path(workspace: Workspace, arguments: MovePathInput) -> MovePathResult:
    source, destination, is_directory = workspace.move(arguments.source, arguments.destination)
    found = workspace.resolve(destination)
    try:
        workspace.resolve(source)
        source_gone = False
    except PathNotFoundError:
        source_gone = True
    except WorkspaceError:
        source_gone = False
    return MovePathResult(
        source=source,
        destination=destination,
        type="directory" if is_directory else "file",
        verified=source_gone and found.is_dir == is_directory and not found.is_link,
    )


def _changing(title: str, *, destructive: bool) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=False,
        open_world_hint=False,
    )


def specs(workspace: Workspace, limits: FilesystemLimits) -> list[ToolSpec[Any, Any]]:
    def describe_edit(arguments: EditFileInput) -> ActionDescription:
        times = arguments.expected_replacements
        return ActionDescription(
            operation=f"Edit file '{arguments.path}' ({times} replacement"
            + ("s)" if times > 1 else ")"),
            consequences=f"Every occurrence of the text under 'Replace' (it must occur exactly "
            f"{times} time" + ("s" if times > 1 else "") + ", or nothing changes) becomes the "
            "text under 'With'. If the file uses CRLF line endings and the text is not found "
            "as given, line breaks in both texts are matched and written as CRLF. The change "
            "is atomic and can be undone only by editing it back; there is no automatic "
            "backup.",
            details=(
                ("Path", ExactPath(arguments.path)),
                ("Replace", ExactText(arguments.old_text)),
                ("With", ExactText(arguments.new_text)),
            ),
            audit={
                "path": arguments.path,
                "old_chars": len(arguments.old_text),
                "new_chars": len(arguments.new_text),
                "replacements": times,
            },
        )

    def describe_mkdir(arguments: CreateDirectoryInput) -> ActionDescription:
        return ActionDescription(
            operation=f"Create directory '{arguments.path}'",
            consequences="A new, empty directory is created. Nothing that exists is changed.",
            details=(("Path", ExactPath(arguments.path)),),
            audit={"path": arguments.path},
        )

    def describe_move(arguments: MovePathInput) -> ActionDescription:
        return ActionDescription(
            operation=f"Move '{arguments.source}' to '{arguments.destination}'",
            consequences="The file or directory (with everything in it) gets a new path in the "
            "workspace. Anything that refers to the old path stops finding it. Nothing is "
            "overwritten or deleted.",
            details=(
                ("From", ExactPath(arguments.source)),
                ("To", ExactPath(arguments.destination)),
            ),
            audit={"source": arguments.source, "destination": arguments.destination},
        )

    return [
        ToolSpec(
            name="edit_file",
            title="Edit file",
            description=(
                "Change an existing UTF-8 text file in the workspace by replacing exact text: "
                "old_text must occur exactly expected_replacements times (default 1), or "
                "nothing changes. Read the file first and copy old_text exactly. The new "
                "content replaces the file atomically and is read back to verify it. Needs "
                f"the user's approval. Files over {limits.max_read_bytes} bytes, binary files, "
                "links, hard-linked files, credential files, repository metadata, and excluded "
                "directories are refused. Returns hashes before and after and 'verified'."
            ),
            input_model=EditFileInput,
            output_model=EditFileResult,
            handler=lambda arguments: edit_file(workspace, arguments, limits),
            risk=ToolRisk.MODIFY,
            annotations=_changing("Edit file", destructive=True),
            capability="filesystem",
            describe=describe_edit,
            operation="edit",
        ),
        ToolSpec(
            name="create_directory",
            title="Create directory",
            description=(
                "Create one new directory in the workspace. The parent must exist (create "
                "nested directories one level at a time), and nothing may exist at the path. "
                "Needs the user's approval. Returns the path and 'verified'."
            ),
            input_model=CreateDirectoryInput,
            output_model=CreateDirectoryResult,
            handler=lambda arguments: create_directory(workspace, arguments),
            risk=ToolRisk.CREATE,
            annotations=_changing("Create directory", destructive=False),
            capability="filesystem",
            describe=describe_mkdir,
            operation="create_directory",
        ),
        ToolSpec(
            name="move_path",
            title="Move or rename",
            description=(
                "Move or rename a file or directory within the workspace. Never overwrites: "
                "the destination must not exist, and its parent directory must. Links, "
                "credential files, repository metadata, and excluded directories are refused, "
                "and a directory cannot be moved into itself. Needs the user's approval. "
                "There is no delete tool; to tidy up, move items into a folder."
            ),
            input_model=MovePathInput,
            output_model=MovePathResult,
            handler=lambda arguments: move_path(workspace, arguments),
            risk=ToolRisk.MODIFY,
            annotations=_changing("Move or rename", destructive=True),
            capability="filesystem",
            describe=describe_move,
            operation="move",
        ),
    ]
