"""Tests for atomic one-file exact multi-editing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.edit_file import (
    ApplyEditsArguments,
    ApplyEditsContent,
    ApplyEditsTool,
    AtomicEditArguments,
)
from coding_agent.protocol import ToolCapability, ToolKind, ToolOutcome
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


def _arguments(
    path: str,
    *edits: tuple[str, str, int],
) -> ApplyEditsArguments:
    return ApplyEditsArguments(
        path=path,
        edits=tuple(
            AtomicEditArguments(
                old_text=old_text,
                new_text=new_text,
                expected_count=expected_count,
            )
            for old_text, new_text, expected_count in edits
        ),
    )


def _execute(
    tool: ApplyEditsTool,
    arguments: ApplyEditsArguments,
):
    prepared = tool.prepare("apply", arguments)
    assert isinstance(prepared, PreparedToolCall)
    return tool.execute(prepared)


def test_tool_spec_is_local_file_mutation() -> None:
    workspace = Path.cwd()
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    assert tool.spec.name == "apply_edits"
    assert tool.spec.kind is ToolKind.LOCAL
    assert tool.spec.capabilities == frozenset({ToolCapability.FILE_MUTATION})


def test_three_separated_edits_commit_once_with_bounded_summary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "service.py"
    original = b"FIRST = 1\nkeep = True\nMIDDLE = 2\nkeep2 = True\nLAST = 3\n"
    target.write_bytes(original)
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    result = _execute(
        tool,
        _arguments(
            "service.py",
            ("FIRST = 1", "FIRST = 10", 1),
            ("MIDDLE = 2", "MIDDLE = 20", 1),
            ("LAST = 3", "LAST = 30", 1),
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.error is None
    assert target.read_bytes() == original.replace(b" = 1", b" = 10").replace(
        b" = 2", b" = 20"
    ).replace(b" = 3", b" = 30")
    assert isinstance(result.content, ApplyEditsContent)
    assert result.content.path == "service.py"
    assert result.content.edit_count == 3
    assert result.content.replacement_count == 3
    assert result.content.bytes_before == len(original)
    assert result.content.bytes_after == len(target.read_bytes())


def test_every_edit_uses_same_execution_time_original_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "values.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    result = _execute(
        tool,
        _arguments(
            "values.txt",
            ("alpha", "beta", 1),
            ("beta", "gamma", 1),
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert target.read_text(encoding="utf-8") == "beta gamma\n"


@pytest.mark.parametrize(
    ("edits", "error_code", "error_details"),
    [
        (
            (("missing", "new", 1), ("second", "changed", 1)),
            "EDIT_TARGET_NOT_FOUND",
            {"edit_index": 0, "expected_count": 1, "actual_count": 0},
        ),
        (
            (("item", "changed", 1), ("second", "changed", 1)),
            "EDIT_MATCH_COUNT_MISMATCH",
            {"edit_index": 0, "expected_count": 1, "actual_count": 2},
        ),
        (
            (("abc", "first", 1), ("bc", "second", 1)),
            "EDIT_OVERLAP",
            {"first_edit_index": 0, "second_edit_index": 1},
        ),
    ],
)
def test_validation_or_overlap_failure_performs_zero_writes(
    tmp_path: Path,
    edits: tuple[tuple[str, str, int], ...],
    error_code: str,
    error_details: dict[str, int],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "values.txt"
    original = b"abc item item second\n"
    target.write_bytes(original)
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    result = _execute(tool, _arguments("values.txt", *edits))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == error_code
    assert result.error.details == {"path": "values.txt", **error_details}
    assert target.read_bytes() == original


def test_expected_count_replaces_every_non_overlapping_original_match(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "values.txt"
    target.write_text("item item item\nend\n", encoding="utf-8")
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    result = _execute(
        tool,
        _arguments(
            "values.txt",
            ("item", "changed", 3),
            ("end", "done", 1),
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ApplyEditsContent)
    assert result.content.replacement_count == 4
    assert target.read_text(encoding="utf-8") == "changed changed changed\ndone\n"


def test_lf_observations_preserve_consistent_crlf_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "service.py"
    target.write_bytes(b"def first():\r\n    return 1\r\n\r\ndef second():\r\n    return 2\r\n")
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    result = _execute(
        tool,
        _arguments(
            "service.py",
            ("def first():\n    return 1", "def first():\n    return 10", 1),
            ("def second():\n    return 2", "def second():\n    return 20", 1),
        ),
    )

    assert result.outcome is ToolOutcome.SUCCESS
    assert target.read_bytes() == (
        b"def first():\r\n    return 10\r\n\r\n"
        b"def second():\r\n    return 20\r\n"
    )


def test_atomic_install_failure_preserves_original_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "values.txt"
    original = b"first second\n"
    target.write_bytes(original)
    tool = ApplyEditsTool(WorkspacePathResolver(workspace))

    def fail_replace(source, destination) -> None:
        raise PermissionError("simulated replacement failure")

    monkeypatch.setattr("coding_agent.edit_file.os.replace", fail_replace)
    result = _execute(
        tool,
        _arguments(
            "values.txt",
            ("first", "one", 1),
            ("second", "two", 1),
        ),
    )

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "EDIT_WRITE_FAILED"
    assert target.read_bytes() == original
    assert list(workspace.glob(".values.txt.*.tmp")) == []


def test_arguments_are_strict_bounded_and_nonempty() -> None:
    valid_edit = {"old_text": "old", "new_text": "new"}
    validated = ApplyEditsArguments.model_validate(
        {"path": "file.txt", "edits": [valid_edit]}
    )
    assert isinstance(validated.edits, tuple)
    assert validated.edits[0].expected_count == 1
    edits_schema = ApplyEditsArguments.model_json_schema()["properties"]["edits"]
    assert edits_schema["minItems"] == 1
    assert edits_schema["maxItems"] == 32

    invalid_payloads = (
        {"path": "file.txt", "edits": []},
        {"path": "file.txt", "edits": [valid_edit] * 33},
        {
            "path": "file.txt",
            "edits": [{"old_text": "", "new_text": "new"}],
        },
        {
            "path": "file.txt",
            "edits": [valid_edit],
            "unexpected": True,
        },
    )

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            ApplyEditsArguments.model_validate(payload)
