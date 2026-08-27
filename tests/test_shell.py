"""Tests for the bounded local Shell Tool."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent.protocol import ToolError, ToolOutcome
from coding_agent.shell import ShellArguments, ShellBackend, ShellContent, ShellTool
from coding_agent.workspace import ResolvedPath, WorkspacePathResolver


def _backend() -> ShellBackend:
    if os.name == "nt":
        return ShellBackend(
            executable=os.environ.get("COMSPEC", "cmd.exe"),
        )
    return ShellBackend(executable="/bin/sh")


def _python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _tool(
    workspace: Path,
    *,
    backend: ShellBackend | None = None,
    max_stdout_bytes: int = 1024,
    max_stderr_bytes: int = 1024,
    excluded_environment_names: frozenset[str] = frozenset(),
) -> ShellTool:
    return ShellTool(
        WorkspacePathResolver(workspace),
        backend or _backend(),
        default_timeout_seconds=5,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        excluded_environment_names=excluded_environment_names,
    )


def _execute(tool: ShellTool, arguments: ShellArguments):
    prepared = tool.prepare(arguments)
    assert isinstance(prepared, ResolvedPath)
    return tool.execute(arguments, prepared)


def test_exit_zero_captures_stdout_and_resolved_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    child.mkdir(parents=True)
    tool = _tool(workspace)
    arguments = ShellArguments(
        command=_python_command("print('hello')"),
        cwd="child",
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.SUCCESS
    assert result.error is None
    assert isinstance(result.content, ShellContent)
    assert result.content.exit_code == 0
    expected_stdout = "hello\r\n" if os.name == "nt" else "hello\n"
    assert result.content.stdout == expected_stdout
    assert result.content.stderr == ""
    assert result.content.cwd == "child"


def test_nonzero_exit_is_unsuccessful_command_with_stderr(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace)
    arguments = ShellArguments(
        command=_python_command(
            "import sys; print('failure', file=sys.stderr); raise SystemExit(7)"
        )
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.UNSUCCESSFUL_COMMAND
    assert result.error is None
    assert isinstance(result.content, ShellContent)
    assert result.content.exit_code == 7
    assert "failure" in result.content.stderr


def test_timeout_is_operation_failure_and_captures_partial_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace)
    arguments = ShellArguments(
        command=_python_command(
            "import time; print('started', flush=True); time.sleep(10)"
        ),
        timeout_seconds=1,
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "COMMAND_TIMEOUT"
    assert isinstance(result.content, ShellContent)
    assert "started" in result.content.stdout


def test_stdout_and_stderr_are_independently_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace, max_stdout_bytes=16, max_stderr_bytes=12)
    arguments = ShellArguments(
        command=_python_command(
            "import sys; print('o' * 100); print('e' * 100, file=sys.stderr)"
        )
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ShellContent)
    assert len(result.content.stdout.encode("utf-8")) <= 16
    assert len(result.content.stderr.encode("utf-8")) <= 12
    assert result.content.stdout_truncated is True
    assert result.content.stderr_truncated is True


def test_missing_and_non_directory_cwd_fail_during_preparation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    tool = _tool(workspace)

    missing = tool.prepare(ShellArguments(command="unused", cwd="missing"))
    file_target = tool.prepare(ShellArguments(command="unused", cwd="file.txt"))

    assert isinstance(missing, ToolError)
    assert missing.code == "CWD_NOT_FOUND"
    assert isinstance(file_target, ToolError)
    assert file_target.code == "CWD_NOT_DIRECTORY"


def test_process_launch_failure_is_operation_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(
        workspace,
        backend=ShellBackend(
            executable=str(workspace / "missing-shell-executable"),
        ),
    )

    result = _execute(tool, ShellArguments(command="unused"))

    assert result.outcome is ToolOutcome.OPERATION_FAILURE
    assert result.error is not None
    assert result.error.code == "PROCESS_START_FAILED"


def test_noninteractive_stdin_reaches_eof(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace)
    arguments = ShellArguments(
        command=_python_command("import sys; print(repr(sys.stdin.read()))")
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ShellContent)
    assert "''" in result.content.stdout


def test_configured_environment_name_is_filtered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODING_AGENT_TEST_SECRET", "must-not-leak")
    tool = _tool(
        workspace,
        excluded_environment_names=frozenset({"coding_agent_test_secret"}),
    )
    arguments = ShellArguments(
        command=_python_command(
            "import os; print(os.environ.get('CODING_AGENT_TEST_SECRET', 'missing'))"
        )
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ShellContent)
    assert "missing" in result.content.stdout
    assert "must-not-leak" not in result.content.stdout


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_timeout_seconds", 0),
        ("max_stdout_bytes", 0),
        ("max_stderr_bytes", 0),
    ],
)
def test_shell_tool_rejects_unbounded_configuration(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kwargs = {
        "default_timeout_seconds": 1,
        "max_stdout_bytes": 1,
        "max_stderr_bytes": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        ShellTool(WorkspacePathResolver(workspace), _backend(), **kwargs)
