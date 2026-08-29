"""Tests for the bounded local Shell Tool."""

from __future__ import annotations

import _thread
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coding_agent.protocol import ToolError, ToolOutcome
from coding_agent.shell import (
    ShellArguments,
    ShellBackend,
    ShellContent,
    ShellRiskAction,
    ShellTool,
    _decode_shell_output,
    classify_shell_surface,
)
from coding_agent.tooling import PreparedToolCall
from coding_agent.workspace import WorkspacePathResolver


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
    default_timeout_seconds: int = 5,
    max_timeout_seconds: int = 30,
    max_stdout_bytes: int = 1024,
    max_stderr_bytes: int = 1024,
    excluded_environment_names: frozenset[str] = frozenset(),
) -> ShellTool:
    return ShellTool(
        WorkspacePathResolver(workspace),
        backend or _backend(),
        default_timeout_seconds=default_timeout_seconds,
        max_timeout_seconds=max_timeout_seconds,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
        excluded_environment_names=excluded_environment_names,
    )


def _execute(tool: ShellTool, arguments: ShellArguments):
    prepared = tool.prepare("shell", arguments)
    assert isinstance(prepared, PreparedToolCall)
    return tool.execute(prepared)


def test_shell_spec_exposes_current_backend_and_compatibility_guidance(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(workspace, backend=ShellBackend("test-shell"))

    description = tool.spec.description

    assert "current shell backend is test-shell" in description
    assert "compatible with that backend and platform" in description


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


def test_shell_output_decode_falls_back_after_invalid_utf8() -> None:
    message = "此时不应有 <<。"

    decoded = _decode_shell_output(
        message.encode("gbk"),
        ("utf-8", "gbk"),
    )

    assert decoded == message
    assert "\ufffd" not in decoded


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd encoding regression")
def test_windows_cmd_native_error_is_not_mojibake(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _execute(
        _tool(workspace),
        ShellArguments(command="python - <<'EOF'"),
    )

    assert result.outcome is ToolOutcome.UNSUCCESSFUL_COMMAND
    assert isinstance(result.content, ShellContent)
    diagnostic = result.content.stderr or result.content.stdout
    assert "\ufffd" not in diagnostic
    assert "<<" in diagnostic


def test_quoted_interpreter_code_is_not_shell_compound_syntax() -> None:
    facts = classify_shell_surface('python -c "value=1; print(value)"')

    assert facts.has_compound_syntax is False
    assert facts.has_unknown_segment is False
    assert facts.recognized_actions == frozenset()


def test_echo_between_read_only_commands_is_a_known_safe_segment() -> None:
    facts = classify_shell_surface(
        'git status --short && echo "--- cached diff ---" && git diff --cached'
    )

    assert facts.has_compound_syntax is True
    assert facts.has_unknown_segment is False
    assert facts.recognized_actions == frozenset()


def test_exact_stderr_to_stdout_merge_is_not_ambiguous_composition() -> None:
    facts = classify_shell_surface("python -m unittest -v 2>&1")

    assert facts.has_compound_syntax is False
    assert facts.has_unknown_segment is False
    assert facts.recognized_actions == frozenset()


@pytest.mark.parametrize(
    "command",
    [
        "python diagnostics.py 2>&1 | tail -n 5",
        "python diagnostics.py > diagnostics.log 2>&1",
        "python diagnostics.py 1>&2",
    ],
)
def test_stream_merge_does_not_hide_other_ambiguous_composition(
    command: str,
) -> None:
    facts = classify_shell_surface(command)

    assert facts.has_compound_syntax is True
    assert facts.has_unknown_segment is True


def test_quote_awareness_keeps_external_compound_risk_visible() -> None:
    facts = classify_shell_surface(
        'python -c "value=1; print(value)" && git push origin main'
    )

    assert facts.has_compound_syntax is True
    assert ShellRiskAction.GIT_MUTATION in facts.recognized_actions
    assert ShellRiskAction.GIT_REMOTE_WRITE in facts.recognized_actions
    assert ShellRiskAction.NETWORK_ACCESS in facts.recognized_actions


def test_nested_shell_compound_content_remains_ambiguous() -> None:
    facts = classify_shell_surface('cmd /c "echo ready && git push origin main"')

    assert facts.has_compound_syntax is True
    assert facts.has_unknown_segment is True


def test_dynamic_substitution_inside_double_quotes_remains_ambiguous() -> None:
    facts = classify_shell_surface('python -c "print($(whoami))"')

    assert facts.has_compound_syntax is True
    assert facts.has_unknown_segment is True


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


@pytest.mark.skipif(os.name != "nt", reason="Windows Ctrl+C responsiveness regression")
def test_windows_long_command_is_promptly_interruptible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(
        workspace,
        default_timeout_seconds=30,
        max_timeout_seconds=30,
    )
    arguments = ShellArguments(
        command=_python_command("import time; time.sleep(20)"),
    )
    interrupt = threading.Timer(0.25, _thread.interrupt_main)
    started_at = time.monotonic()
    interrupt.start()

    try:
        with pytest.raises(KeyboardInterrupt):
            _execute(tool, arguments)
    finally:
        interrupt.cancel()
        interrupt.join(timeout=1)

    assert time.monotonic() - started_at < 5


def test_timeout_schema_and_preparation_enforce_absolute_and_configured_caps(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = _tool(
        workspace,
        default_timeout_seconds=5,
        max_timeout_seconds=10,
    )

    default = tool.prepare("default", ShellArguments(command="pytest"))
    small = tool.prepare(
        "small",
        ShellArguments(command="pytest", timeout_seconds=3),
    )
    above_configured = tool.prepare(
        "above-configured",
        ShellArguments(command="pytest", timeout_seconds=11),
    )

    assert isinstance(default, PreparedToolCall)
    assert default.operation_facts.effective_timeout_seconds == 5  # type: ignore[union-attr]
    assert isinstance(small, PreparedToolCall)
    assert small.operation_facts.effective_timeout_seconds == 3  # type: ignore[union-attr]
    assert isinstance(above_configured, ToolError)
    assert above_configured.code == "TIMEOUT_EXCEEDS_MAXIMUM"
    assert above_configured.details == {
        "requested_timeout_seconds": 11,
        "maximum_timeout_seconds": 10,
    }
    with pytest.raises(ValueError):
        ShellArguments(command="pytest", timeout_seconds=0)
    with pytest.raises(ValueError):
        ShellArguments(command="pytest", timeout_seconds=301)

    timeout_schema = ShellArguments.model_json_schema()["properties"][
        "timeout_seconds"
    ]["anyOf"][0]
    assert timeout_schema["minimum"] == 1
    assert timeout_schema["maximum"] == 300


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


def test_truncated_capture_preserves_head_and_tail_markers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capture_limit = 64 * 1024
    tool = _tool(
        workspace,
        max_stdout_bytes=capture_limit,
        max_stderr_bytes=capture_limit,
    )
    arguments = ShellArguments(
        command=_python_command(
            "import sys; "
            "sys.stdout.write('HEAD_MARKER' + 'x' * 100000 + 'TAIL_MARKER'); "
            "sys.stderr.write('ERR_HEAD' + 'y' * 100000 + 'ERR_TAIL')"
        )
    )

    result = _execute(tool, arguments)

    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ShellContent)
    assert len(result.content.stdout.encode("utf-8")) <= capture_limit
    assert len(result.content.stderr.encode("utf-8")) <= capture_limit
    assert result.content.stdout.startswith("HEAD_MARKER")
    assert result.content.stdout.endswith("TAIL_MARKER")
    assert result.content.stderr.startswith("ERR_HEAD")
    assert result.content.stderr.endswith("ERR_TAIL")
    assert result.content.stdout_truncated is True
    assert result.content.stderr_truncated is True


def test_missing_and_non_directory_cwd_fail_during_preparation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    tool = _tool(workspace)

    missing = tool.prepare(
        "missing",
        ShellArguments(command="unused", cwd="missing"),
    )
    file_target = tool.prepare(
        "file-target",
        ShellArguments(command="unused", cwd="file.txt"),
    )

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
        ("max_timeout_seconds", 0),
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
        "max_timeout_seconds": 2,
        "max_stdout_bytes": 1,
        "max_stderr_bytes": 1,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        ShellTool(WorkspacePathResolver(workspace), _backend(), **kwargs)


def test_shell_tool_rejects_default_timeout_above_maximum(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="must not exceed"):
        ShellTool(
            WorkspacePathResolver(workspace),
            _backend(),
            default_timeout_seconds=6,
            max_timeout_seconds=5,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
        )


def test_shell_tool_rejects_configured_maximum_above_absolute_cap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="must not exceed 300"):
        ShellTool(
            WorkspacePathResolver(workspace),
            _backend(),
            default_timeout_seconds=120,
            max_timeout_seconds=301,
            max_stdout_bytes=1,
            max_stderr_bytes=1,
        )
