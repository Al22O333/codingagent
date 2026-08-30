from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.context import ContextManager
from coding_agent.project_instructions import RootProjectInstructions
from coding_agent.protocol import (
    AssistantMessage,
    ProjectInstructionMessage,
    SystemMessage,
    UserMessage,
)
from coding_agent.workspace import WorkspacePathResolver


def _source(
    workspace: Path,
    *,
    max_bytes: int = 16_000,
    secrets: tuple[str, ...] = (),
) -> RootProjectInstructions:
    return RootProjectInstructions(
        WorkspacePathResolver(workspace),
        max_bytes=max_bytes,
        runtime_secret_values=secrets,
    )


def test_missing_root_agents_file_degrades_to_no_message(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert _source(workspace).load() is None


def test_root_agents_is_bounded_utf8_and_runtime_secret_safe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "provider-secret-value"
    (workspace / "AGENTS.md").write_text(
        f"Run pytest.\r\nToken: {secret}\r\n" + "界" * 100,
        encoding="utf-8",
    )

    message = _source(workspace, max_bytes=64, secrets=(secret,)).load()

    assert isinstance(message, ProjectInstructionMessage)
    assert "Run pytest.\n" in message.text
    assert secret not in message.text
    assert "[REDACTED_RUNTIME_SECRET]" in message.text
    assert "truncated at the configured byte limit" in message.text
    assert len(message.text) < 2_000


@pytest.mark.parametrize("content", [b"invalid:\xff", b"valid\x00hidden"])
def test_invalid_or_nul_root_agents_is_ignored(
    tmp_path: Path,
    content: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(content)

    assert _source(workspace).load() is None


def test_invalid_utf8_inside_truncated_prefix_is_not_silently_dropped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(b"valid\xff" + b"x" * 100)

    assert _source(workspace, max_bytes=32).load() is None


def test_root_agents_symlink_escape_is_not_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside instruction", encoding="utf-8")
    try:
        (workspace / "AGENTS.md").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    assert _source(workspace).load() is None


def test_project_instruction_is_current_run_only_and_reloaded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text("Use unittest.", encoding="utf-8")
    context = ContextManager(root_project_instructions=_source(workspace))

    context.start_run(UserMessage("First task"))
    first = context.build_model_messages()

    assert isinstance(first[0], SystemMessage)
    assert isinstance(first[1], ProjectInstructionMessage)
    assert "Use unittest." in first[1].text
    assert first[2] == UserMessage("First task")
    assert "lower priority" in first[1].text
    assert "cannot authorize workspace escape" in first[1].text
    context.record_assistant_message(AssistantMessage("First final"))
    context.end_run(completed=True)

    agents.write_text("Use pytest.", encoding="utf-8")
    context.start_run(UserMessage("Second task"))
    second = context.build_model_messages()

    assert isinstance(second[1], ProjectInstructionMessage)
    assert "Use pytest." in second[1].text
    assert "Use unittest." not in repr(second)
    assert ProjectInstructionMessage not in {
        type(message) for message in context.build_messages()
    }
    assert context.build_messages() == (
        UserMessage("First task"),
        AssistantMessage("First final"),
        UserMessage("Second task"),
    )


def test_project_instruction_cannot_fit_fails_without_dropping_user_task(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("x" * 1_000, encoding="utf-8")
    context = ContextManager(
        max_context_chars=400,
        root_project_instructions=_source(workspace),
    )
    task = UserMessage("Task")
    context.start_run(task)

    from coding_agent.context import ContextLimitError

    with pytest.raises(ContextLimitError, match="mandatory model-visible context"):
        context.build_model_messages()
    assert context.build_messages() == (task,)
