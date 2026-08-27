"""CLI composition-root and console interaction tests."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.cli import CLIConfig, ConsoleUserInteraction, build_runtime, load_config
from coding_agent.interaction import (
    ClarificationRequest,
    ClarificationStatus,
    ConfirmationDecision,
    ConfirmationRequest,
    FakeUserInteraction,
    UserInteractionError,
)
from coding_agent.model_client import FakeModelClient
from coding_agent.model_client import FatalProviderError
from coding_agent.protocol import ModelResponse, ToolCall, ToolOutcome
from coding_agent.runtime import RunState
from coding_agent.shell import ShellContent


def _config(workspace: Path) -> CLIConfig:
    return CLIConfig(workspace=workspace, model="test-model", api_key="test-key")


def _python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def test_config_loads_minimal_environment_without_exposing_secret(tmp_path: Path) -> None:
    config = load_config(
        str(tmp_path),
        {
            "CODING_AGENT_MODEL": "provider-model",
            "CODING_AGENT_API_KEY": "secret-value",
            "CODING_AGENT_BASE_URL": "https://provider.invalid/v1",
            "CODING_AGENT_SHELL": "test-shell",
        },
    )
    assert config.workspace == tmp_path
    assert config.model == "provider-model"
    assert config.base_url == "https://provider.invalid/v1"
    assert config.shell_executable == "test-shell"
    assert "secret-value" not in repr(config)

    with pytest.raises(ValueError, match="CODING_AGENT_MODEL"):
        load_config(str(tmp_path), {"CODING_AGENT_API_KEY": "key"})
    with pytest.raises(ValueError, match="CODING_AGENT_API_KEY"):
        load_config(str(tmp_path), {"CODING_AGENT_MODEL": "model"})


def test_default_windows_shell_uses_full_comspec_path(tmp_path: Path) -> None:
    config = load_config(
        str(tmp_path),
        {
            "CODING_AGENT_MODEL": "model",
            "CODING_AGENT_API_KEY": "key",
        },
    )
    if os.name == "nt":
        assert config.shell_executable == os.environ["COMSPEC"]
        assert Path(config.shell_executable).is_absolute()
    else:
        assert config.shell_executable == "/bin/sh"


def test_composition_root_registers_complete_v1_toolset(tmp_path: Path) -> None:
    client = FakeModelClient([ModelResponse(text="Ready.")])
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )
    run = runtime.run("Inspect the workspace")
    assert run.state is RunState.COMPLETED
    assert [spec.name for spec in client.requests[0].tools] == [
        "read_file",
        "list_directory",
        "search_files",
        "search_text",
        "edit_file",
        "create_file",
        "shell",
        "ask_user",
    ]


def test_composed_runtime_executes_real_read_file_tool(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("answer = 42\n", encoding="utf-8")
    call = ToolCall("read", "read_file", {"path": "main.py"})
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="The answer is 42."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )
    run = runtime.run("Read main.py")
    assert run.state is RunState.COMPLETED
    result_message = client.requests[1].messages[-1]
    assert result_message.results[0].outcome is ToolOutcome.SUCCESS  # type: ignore[union-attr]
    assert "answer = 42" in repr(result_message.results[0].content)  # type: ignore[union-attr]


def test_composition_filters_runtime_secrets_but_preserves_ordinary_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "super-secret-test-key"
    monkeypatch.setenv("CODING_AGENT_TEST_API_KEY", secret)
    monkeypatch.setenv("CODING_AGENT_API_KEY", "default-agent-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "default-openai-secret")
    monkeypatch.setenv("CODING_AGENT_NORMAL_TEST_VAR", "hello")
    source = (
        "print([__import__('os').environ.get(name, 'missing') for name in "
        "['CODING_AGENT_TEST_API_KEY', 'CODING_AGENT_API_KEY', "
        "'OPENAI_API_KEY', 'CODING_AGENT_NORMAL_TEST_VAR']])"
    )
    call = ToolCall("shell", "shell", {"command": _python_command(source)})
    client = FakeModelClient(
        [
            ModelResponse(text=None, tool_calls=(call,)),
            ModelResponse(text="Environment checked."),
        ]
    )
    config = CLIConfig(
        workspace=tmp_path,
        model="test-model",
        api_key=secret,
        api_key_environment_name="CODING_AGENT_TEST_API_KEY",
    )

    runtime = build_runtime(
        config,
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )
    run = runtime.run("Check the child process environment")

    assert run.state is RunState.COMPLETED
    result_message = client.requests[1].messages[-1]
    result = result_message.results[0]  # type: ignore[union-attr]
    assert result.outcome is ToolOutcome.SUCCESS
    assert isinstance(result.content, ShellContent)
    assert result.content.stdout.count("missing") == 3
    assert "hello" in result.content.stdout
    assert secret not in result.content.stdout
    assert "default-agent-secret" not in result.content.stdout
    assert "default-openai-secret" not in result.content.stdout


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y\n", ConfirmationDecision.APPROVE),
        ("n\n", ConfirmationDecision.REJECT),
        ("c\n", ConfirmationDecision.CANCEL),
        ("", ConfirmationDecision.CANCEL),
    ],
)
def test_console_permission_decisions(
    answer: str,
    expected: ConfirmationDecision,
) -> None:
    interaction = ConsoleUserInteraction(StringIO(answer), StringIO())
    request = ConfirmationRequest("call", "shell", "shell('x')", "RISK", "risk")
    assert interaction.confirm(request) is expected


def test_console_clarification_answer_and_eof() -> None:
    answered = ConsoleUserInteraction(StringIO("src/main.py\n"), StringIO()).ask(
        ClarificationRequest("ask", "Which file?")
    )
    cancelled = ConsoleUserInteraction(StringIO(""), StringIO()).ask(
        ClarificationRequest("ask", "Which file?")
    )
    assert answered.status is ClarificationStatus.ANSWERED
    assert answered.answer == "src/main.py"
    assert cancelled.status is ClarificationStatus.CANCELLED


def test_console_clarification_io_error_is_not_treated_as_cancellation() -> None:
    class BrokenReader(StringIO):
        def readline(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("terminal unavailable")

    interaction = ConsoleUserInteraction(BrokenReader(), StringIO())

    with pytest.raises(UserInteractionError) as raised:
        interaction.ask(ClarificationRequest("ask", "Which file?"))

    assert isinstance(raised.value.__cause__, OSError)


def test_one_shot_main_starts_runtime_and_prints_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    fake = FakeModelClient([ModelResponse(text="Finished from CLI.")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(
        ["--workspace", str(tmp_path), "inspect", "the", "project"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert cli.STARTUP_MESSAGE in output
    assert f"Workspace: {tmp_path.resolve()}" in output
    assert "Model: test-model" in output
    assert "Agent: running" in output
    assert "Finished from CLI." in output
    assert fake.requests[0].messages[0].text == "inspect the project"  # type: ignore[union-attr]


def test_cli_shows_sanitized_tool_activity_without_arguments_or_secrets(
    tmp_path: Path,
) -> None:
    secret = "super-secret-tool-argument"
    output = StringIO()
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        "edit",
                        "edit_file",
                        {
                            "path": "src/main.py",
                            "old_text": secret,
                            "new_text": "replacement",
                            "expected_count": 1,
                        },
                    ),
                ),
            ),
            ModelResponse(text="Handled."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
        stdout=output,
    )

    cli._run_task(runtime, "Inspect the project", output)

    rendered = output.getvalue()
    assert "[tool] edit_file: src/main.py" in rendered
    assert secret not in rendered
    assert "replacement" not in rendered


def test_cli_provider_failure_is_understandable_and_does_not_leak_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "super-secret-provider-key"
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", secret)
    fake = FakeModelClient([FatalProviderError(f"invalid API key: {secret}")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(["--workspace", str(tmp_path), "fix", "the", "bug"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Provider error:" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
