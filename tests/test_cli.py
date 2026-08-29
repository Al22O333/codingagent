"""CLI composition-root and console interaction tests."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys
import builtins
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
from coding_agent.protocol import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProjectInstructionMessage,
    ToolCall,
    ToolOutcome,
)
from coding_agent.runtime import RunState, RuntimeEvent


def _config(workspace: Path) -> CLIConfig:
    return CLIConfig(
        workspace=workspace,
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key="test-key",
    )


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
    assert not hasattr(config, "shell_executable")
    assert "secret-value" not in repr(config)

    with pytest.raises(ValueError, match="CODING_AGENT_MODEL"):
        load_config(
            str(tmp_path),
            {
                "CODING_AGENT_API_KEY": "key",
                "CODING_AGENT_BASE_URL": "https://provider.invalid/v1",
            },
        )
    with pytest.raises(ValueError, match="CODING_AGENT_API_KEY"):
        load_config(
            str(tmp_path),
            {
                "CODING_AGENT_MODEL": "model",
                "CODING_AGENT_BASE_URL": "https://provider.invalid/v1",
            },
        )


def test_platform_shell_ignores_public_environment_override(tmp_path: Path) -> None:
    config = load_config(
        str(tmp_path),
        {
            "CODING_AGENT_MODEL": "model",
            "CODING_AGENT_API_KEY": "key",
            "CODING_AGENT_BASE_URL": "https://provider.invalid/v1",
            "CODING_AGENT_SHELL": "untrusted-override",
        },
    )
    if os.name == "nt":
        assert cli._platform_shell_executable() == os.environ["COMSPEC"]
        assert Path(cli._platform_shell_executable()).is_absolute()
    else:
        assert cli._platform_shell_executable() == "/bin/sh"
    assert not hasattr(config, "shell_executable")


def test_composition_uses_v1_shell_timeout_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    real_shell_tool = cli.ShellTool

    def recording_shell_tool(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_shell_tool(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "ShellTool", recording_shell_tool)
    build_runtime(
        _config(tmp_path),
        model_client=FakeModelClient([ModelResponse(text="Ready.")]),
        user_interaction=FakeUserInteraction(),
    )

    assert captured["default_timeout_seconds"] == 120
    assert captured["max_timeout_seconds"] == 300


def test_composition_uses_v1_file_and_discovery_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, object]] = {}

    for name in (
        "ReadFileTool",
        "ListDirectoryTool",
        "SearchFilesTool",
        "SearchTextTool",
    ):
        real_constructor = getattr(cli, name)

        def recording_constructor(
            *args: object,
            _name: str = name,
            _constructor: object = real_constructor,
            **kwargs: object,
        ) -> object:
            captured[_name] = dict(kwargs)
            return _constructor(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(cli, name, recording_constructor)

    build_runtime(
        _config(tmp_path),
        model_client=FakeModelClient([ModelResponse(text="Ready.")]),
        user_interaction=FakeUserInteraction(),
    )

    assert captured["ReadFileTool"] == {"max_lines": 400, "max_bytes": 20_000}
    assert captured["ListDirectoryTool"] == {"max_entries": 200}
    assert captured["SearchFilesTool"] == {"max_results": 200}
    assert captured["SearchTextTool"] == {
        "max_matches": 100,
        "max_line_bytes": 4096,
    }


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
        "apply_edits",
        "create_file",
        "create_directory",
        "move_path",
        "delete_path",
        "shell",
        "ask_user",
    ]


def test_composition_loads_root_agents_for_each_run_and_redacts_secret(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Use pytest. Secret=test-key", encoding="utf-8")
    client = FakeModelClient(
        [ModelResponse(text="First."), ModelResponse(text="Second.")]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )

    assert runtime.run("First task").state is RunState.COMPLETED
    agents.write_text("Use unittest.", encoding="utf-8")
    assert runtime.run("Second task").state is RunState.COMPLETED

    first_instruction = client.requests[0].messages[1]
    second_instruction = client.requests[1].messages[1]
    assert isinstance(first_instruction, ProjectInstructionMessage)
    assert isinstance(second_instruction, ProjectInstructionMessage)
    assert "Use pytest." in first_instruction.text
    assert "test-key" not in first_instruction.text
    assert "Use unittest." in second_instruction.text
    assert "Use pytest." not in second_instruction.text


def test_composition_uses_openai_compatible_client_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    fake = FakeModelClient([ModelResponse(text="Ready.")])

    def build_default(config: object) -> FakeModelClient:
        captured.append(config)
        return fake

    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", build_default)

    runtime = build_runtime(
        _config(tmp_path),
        user_interaction=FakeUserInteraction(),
    )

    assert runtime.run("Inspect").state is RunState.COMPLETED
    assert len(captured) == 1
    assert captured[0].model == "test-model"  # type: ignore[attr-defined]
    assert captured[0].base_url == "https://provider.invalid/v1"  # type: ignore[attr-defined]


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
            ModelResponse(text="Environment checked after review."),
        ]
    )
    config = CLIConfig(
        workspace=tmp_path,
        model="test-model",
        base_url="https://provider.invalid/v1",
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
    assert result.content["stdout"].count("missing") == 3  # type: ignore[index]
    assert "hello" in result.content["stdout"]  # type: ignore[index]
    assert secret not in result.content["stdout"]  # type: ignore[index]
    assert "default-agent-secret" not in result.content["stdout"]  # type: ignore[index]
    assert "default-openai-secret" not in result.content["stdout"]  # type: ignore[index]
    assert secret not in repr(client.requests)
    assert "default-agent-secret" not in repr(client.requests)
    assert "default-openai-secret" not in repr(client.requests)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y\n", ConfirmationDecision.APPROVE),
        ("n\n", ConfirmationDecision.REJECT),
        ("\n", ConfirmationDecision.REJECT),
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
    answered_output = StringIO()
    answered = ConsoleUserInteraction(StringIO("src/main.py\n"), answered_output).ask(
        ClarificationRequest("ask", "Which file?")
    )
    cancelled = ConsoleUserInteraction(StringIO(""), StringIO()).ask(
        ClarificationRequest("ask", "Which file?")
    )
    assert answered.status is ClarificationStatus.ANSWERED
    assert answered.answer == "src/main.py"
    assert cancelled.status is ClarificationStatus.CANCELLED
    assert "需要你补充信息：" in answered_output.getvalue()
    assert "需要确认" not in answered_output.getvalue()


def test_console_permission_and_clarification_prompts_are_distinct() -> None:
    permission_output = StringIO()
    interaction = ConsoleUserInteraction(StringIO("\n"), permission_output)
    decision = interaction.confirm(
        ConfirmationRequest("call", "shell", "shell command", "RISK", "risk")
    )

    assert decision is ConfirmationDecision.REJECT
    assert "⚠ 需要确认" in permission_output.getvalue()
    assert "仅授权下面这一次精确操作" in permission_output.getvalue()
    assert "需要你补充信息" not in permission_output.getvalue()


def test_console_permission_command_keeps_head_and_risky_tail_without_tool_repr() -> None:
    output = StringIO()
    command = "python " + "x" * 400 + " && git push origin main"
    interaction = ConsoleUserInteraction(StringIO("n\n"), output)

    interaction.confirm(
        ConfirmationRequest(
            "call",
            "shell",
            command,
            "AMBIGUOUS_COMPLEX_SHELL",
            "risk",
        )
    )

    rendered = output.getvalue()
    assert rendered.count("⚠ 需要确认") == 1
    assert "python" in rendered
    assert "git push origin main" in rendered
    assert "中间已省略" in rendered
    assert "ShellTool(" not in rendered


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
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")
    fake = FakeModelClient([ModelResponse(text="Finished from CLI.")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(
        ["--workspace", str(tmp_path), "inspect", "the", "project"]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert cli.STARTUP_MESSAGE in output
    assert f"工作区  {tmp_path.resolve()}" in output
    assert "模型    test-model" in output
    assert "正在运行…" in output
    assert "◆ 运行结束" in output
    assert "Finished from CLI." in output
    assert fake.requests[0].messages[1].text == "inspect the project"  # type: ignore[union-attr]


def test_json_one_shot_emits_one_stable_document_without_success_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)
    fake = FakeModelClient([ModelResponse(text="Could not prove the task.\n第二行")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(
        ["--workspace", str(tmp_path), "--json", "inspect", "the", "project"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.count("\n") == 1
    document = json.loads(captured.out)
    assert list(document) == [
        "schema_version",
        "lifecycle_state",
        "final_response",
        "terminal_reason",
        "normalized_error",
        "model_turns",
        "tool_attempts",
        "limit_reached",
    ]
    assert document == {
        "schema_version": 1,
        "lifecycle_state": "COMPLETED",
        "final_response": "Could not prove the task.\n第二行",
        "terminal_reason": None,
        "normalized_error": None,
        "model_turns": 1,
        "tool_attempts": 0,
        "limit_reached": None,
    }
    assert "success" not in document
    assert cli.STARTUP_MESSAGE not in captured.out
    assert "正在运行" not in captured.out


def test_json_one_shot_failure_is_normalized_and_redacts_runtime_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "json-provider-secret"
    _configure_main_environment(monkeypatch)
    monkeypatch.setenv("CODING_AGENT_API_KEY", secret)
    fake = FakeModelClient([FatalProviderError(f"invalid key: {secret}")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(["--workspace", str(tmp_path), "--json", "inspect"])

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert exit_code == 1
    assert document["lifecycle_state"] == "FAILED"
    assert document["terminal_reason"] == "PROVIDER_FAILURE"
    assert document["normalized_error"] == {
        "code": "PROVIDER_FAILURE",
        "message": "模型服务请求失败，请检查凭据、服务地址或稍后重试。",
    }
    assert document["final_response"] is None
    assert secret not in captured.out
    assert secret not in captured.err
    assert "invalid key" not in captured.out
    assert "Traceback" not in captured.out


def test_json_one_shot_redacts_secret_from_otherwise_complete_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "json-final-secret"
    _configure_main_environment(monkeypatch)
    monkeypatch.setenv("CODING_AGENT_API_KEY", secret)
    fake = FakeModelClient([ModelResponse(text=f"Result: {secret}")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(["--workspace", str(tmp_path), "--json", "inspect"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["final_response"] == "Result: <redacted>"
    assert secret not in captured.out
    assert secret not in captured.err


def test_json_mode_routes_permission_ui_to_stderr_and_keeps_stdout_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)
    (tmp_path / ".env").write_text("TOKEN=value\n", encoding="utf-8")
    fake = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("read", "read_file", {"path": ".env"}),)
            ),
            ModelResponse(text="The read was rejected."),
        ]
    )
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)
    monkeypatch.setattr(sys, "stdin", StringIO("n\n"))

    exit_code = cli.main(["--workspace", str(tmp_path), "--json", "inspect"])

    captured = capsys.readouterr()
    assert exit_code == 0
    document = json.loads(captured.out)
    assert document["final_response"] == "The read was rejected."
    assert captured.out.count("\n") == 1
    assert "需要确认" not in captured.out
    assert "需要确认" in captured.err


def test_json_cancellation_has_lifecycle_exit_code_without_error_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)

    class InterruptingClient:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        cli, "OpenAICompatibleModelClient", lambda config: InterruptingClient()
    )

    exit_code = cli.main(["--workspace", str(tmp_path), "--json", "inspect"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 130
    assert document["lifecycle_state"] == "CANCELLED"
    assert document["terminal_reason"] == "USER_CANCELLATION"
    assert document["normalized_error"] is None
    assert document["final_response"] is None


def test_json_startup_and_missing_task_failures_remain_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)

    missing_task_exit = cli.main(["--workspace", str(tmp_path), "--json"])
    missing_task = json.loads(capsys.readouterr().out)

    assert missing_task_exit == 2
    assert missing_task["lifecycle_state"] == "STARTUP_FAILED"
    assert missing_task["normalized_error"]["code"] == "JSON_ONE_SHOT_REQUIRED"
    assert missing_task["model_turns"] == 0

    missing_workspace_exit = cli.main(
        ["--workspace", str(tmp_path / "missing"), "--json", "inspect"]
    )
    missing_workspace_capture = capsys.readouterr()
    missing_workspace = json.loads(missing_workspace_capture.out)

    assert missing_workspace_exit == 2
    assert missing_workspace["lifecycle_state"] == "STARTUP_FAILED"
    assert missing_workspace["terminal_reason"] == "STARTUP_FAILURE"
    assert missing_workspace["normalized_error"]["code"] == "STARTUP_FAILURE"
    assert "Traceback" not in missing_workspace_capture.out


def test_invalid_workspace_fails_startup_before_model_client_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)
    constructed = False

    def model_client_must_not_construct(config):  # type: ignore[no-untyped-def]
        nonlocal constructed
        constructed = True
        raise AssertionError("model client constructed for invalid workspace")

    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", model_client_must_not_construct)

    exit_code = cli.main(["--workspace", str(tmp_path / "missing"), "inspect"])

    assert exit_code == 2
    assert constructed is False
    assert "启动失败：" in capsys.readouterr().err


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
    assert "● 修改文件 src/main.py" in rendered
    assert secret not in rendered
    assert "replacement" not in rendered


def test_normal_tool_activity_hides_internal_requested_placeholder() -> None:
    lines = cli._render_event(
        RuntimeEvent(
            "tool_proposed",
            {
                "call_id": "search",
                "tool_name": "search_files",
                "action": "requested",
            },
        ),
        debug=False,
    )

    assert lines == ("● 查找文件",)


def test_activity_writer_groups_by_observable_tool_family_and_resets_per_run() -> None:
    output = StringIO()
    report = cli._event_writer(output)

    report(RuntimeEvent("run_started", {"run_id": "one"}))
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "read-1", "tool_name": "read_file", "action": "a.py"},
        )
    )
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "read-2", "tool_name": "search_text", "action": "token"},
        )
    )
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "edit", "tool_name": "edit_file", "action": "a.py"},
        )
    )
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "test", "tool_name": "shell", "action": "pytest -q"},
        )
    )
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "command", "tool_name": "shell", "action": "git status"},
        )
    )
    report(RuntimeEvent("run_started", {"run_id": "two"}))
    report(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "read-3", "tool_name": "read_file", "action": "b.py"},
        )
    )

    rendered = output.getvalue()
    assert rendered.count("◆ 查看项目") == 2
    assert rendered.count("◆ 修改文件") == 1
    assert rendered.count("◆ 测试与检查") == 1
    assert rendered.count("◆ 执行命令") == 1
    assert "● 运行测试" in rendered
    assert "● 执行本地命令" in rendered


def test_debug_shell_rendering_keeps_bounded_command_tail_and_diagnostic() -> None:
    command = "python " + "x" * 1_200 + " COMMAND_TAIL"
    diagnostic = "OUTPUT_HEAD " + "y" * 2_000 + " OUTPUT_TAIL"
    proposal = cli._render_event(
        RuntimeEvent(
            "tool_proposed",
            {"call_id": "shell", "tool_name": "shell", "action": command},
        ),
        debug=True,
    )
    result = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {
                "call_id": "shell",
                "outcome": "UNSUCCESSFUL_COMMAND",
                "exit_code": 1,
                "diagnostic": diagnostic,
            },
        ),
        debug=True,
        proposed_actions={"shell": ("shell", command)},
    )

    rendered_proposal = "\n".join(proposal)
    rendered_result = "\n".join(result)
    assert "[调试] command=" in rendered_proposal
    assert "COMMAND_TAIL" in rendered_proposal
    assert len(rendered_proposal) < 1_500
    assert "[调试] diagnostic=" in rendered_result
    assert "OUTPUT_HEAD" in rendered_result
    assert "OUTPUT_TAIL" in rendered_result
    assert len(rendered_result) < 3_000


def test_normal_hides_usage_while_debug_shows_only_normalized_usage(
    tmp_path: Path,
) -> None:
    usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    normal_output = StringIO()
    normal_runtime = build_runtime(
        _config(tmp_path),
        model_client=FakeModelClient([ModelResponse("Done.", usage=usage)]),
        user_interaction=FakeUserInteraction(),
        stdout=normal_output,
    )
    cli._run_task(normal_runtime, "Inspect", normal_output)

    debug_output = StringIO()
    debug_config = CLIConfig(
        workspace=tmp_path,
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key="test-key",
        debug=True,
    )
    debug_runtime = build_runtime(
        debug_config,
        model_client=FakeModelClient([ModelResponse("Done.", usage=usage)]),
        user_interaction=FakeUserInteraction(),
        stdout=debug_output,
    )
    cli._run_task(debug_runtime, "Inspect", debug_output)

    assert "input_tokens" not in normal_output.getvalue()
    rendered_debug = debug_output.getvalue()
    assert "input_tokens=10" in rendered_debug
    assert "output_tokens=5" in rendered_debug
    assert "total_tokens=15" in rendered_debug
    assert "raw" not in rendered_debug


def test_shell_rendering_is_bounded_and_redacts_runtime_credential(
    tmp_path: Path,
) -> None:
    secret = "test-key"
    output = StringIO()
    command = _python_command(f"print({secret!r} + 'x' * 10000)")
    call = ToolCall("shell-secret", "shell", {"command": command})
    runtime = build_runtime(
        _config(tmp_path),
        model_client=FakeModelClient(
            [ModelResponse(None, (call,)), ModelResponse("Done.")]
        ),
        user_interaction=FakeUserInteraction(),
        stdout=output,
    )

    cli._run_task(runtime, "Run the check", output)

    rendered = output.getvalue()
    assert "● 执行本地命令" in rendered
    assert secret not in rendered
    assert command not in rendered
    assert len(rendered) < 5_000


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", "test"),
        ("python -m pytest -q", "test"),
        ("ruff check src", "check"),
        ("cargo build", "build"),
        ("npm run build", "build"),
        ("my-pytest-wrapper", None),
        ("echo pytest", None),
        ("pytest -q && git push origin main", None),
        ("python -c \"print('pytest')\"", None),
    ],
)
def test_shell_presentation_classification_is_conservative(
    command: str,
    expected: str | None,
) -> None:
    assert cli._classify_shell_presentation(command) == expected


def test_normal_shell_success_uses_summary_or_bounded_useful_excerpt() -> None:
    test_lines = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {"call_id": "test", "outcome": "SUCCESS", "diagnostic": "8 passed"},
        ),
        debug=False,
        proposed_actions={"test": ("shell", "pytest -q")},
    )
    unknown_lines = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {
                "call_id": "status",
                "outcome": "SUCCESS",
                "diagnostic": "first\nsecond\nthird\nfourth",
            },
        ),
        debug=False,
        proposed_actions={"status": ("shell", "git status --short")},
    )

    assert test_lines == ("  ✓ 8 个测试全部通过",)
    assert unknown_lines == (
        "  ✓ 命令执行成功",
        "    first",
        "    second",
        "    third",
    )


def test_permission_resolution_does_not_render_a_second_confirmation_prompt() -> None:
    lines = cli._render_event(
        RuntimeEvent(
            "permission_resolved",
            {"call_id": "call", "decision": "APPROVE"},
        ),
        debug=False,
    )

    rendered = "\n".join(lines)
    assert "已批准，仅限本次操作" in rendered
    assert "需要确认" not in rendered
    assert "允许执行" not in rendered


def test_completion_audit_has_one_normal_indicator_and_bounded_debug_events() -> None:
    normal = cli._render_event(
        RuntimeEvent("completion_audit_started", {"model_turn": 3}),
        debug=False,
    )
    continued = cli._render_event(
        RuntimeEvent(
            "completion_audit_continued",
            {"model_turn": 4, "tool_call_count": 2},
        ),
        debug=False,
    )
    finished = cli._render_event(
        RuntimeEvent("completion_audit_finished", {"model_turn": 5}),
        debug=True,
    )

    assert normal == ("", "◆ 检查完成情况", "")
    assert continued == ()
    rendered_debug = "\n".join(finished)
    assert "completion_audit_finished" in rendered_debug
    assert "model_turn=5" in rendered_debug
    assert "Candidate" not in rendered_debug


def test_permission_rejection_tool_result_does_not_duplicate_resolution() -> None:
    resolution = cli._render_event(
        RuntimeEvent(
            "permission_resolved",
            {"call_id": "call", "decision": "REJECT"},
        ),
        debug=False,
    )
    tool_result = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {
                "call_id": "call",
                "outcome": "POLICY_REJECTED",
                "error_code": "USER_REJECTED_CONFIRMATION",
            },
        ),
        debug=False,
        proposed_actions={"call": ("shell", "git add version.py")},
    )

    assert resolution == ("  ✗ 已拒绝本次操作",)
    assert tool_result == ()


def test_clarification_success_has_specific_human_result() -> None:
    lines = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {"call_id": "ask", "outcome": "SUCCESS"},
        ),
        debug=False,
        proposed_actions={"ask": ("ask_user", "requested")},
    )

    assert lines == ("  ✓ 已收到补充信息",)


def test_composed_console_permission_has_one_prompt_and_one_resolution(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    output = StringIO()
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("secret", "read_file", {"path": ".env"}),),
            ),
            ModelResponse(text="Inspected."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        stdin=StringIO("y\n"),
        stdout=output,
    )

    cli._run_task(runtime, "Inspect .env", output)

    rendered = output.getvalue()
    assert rendered.count("⚠ 需要确认") == 1
    assert rendered.count("允许执行？") == 1
    assert rendered.count("已批准，仅限本次操作") == 1
    assert "ReadFileTool(" not in rendered


def test_composed_console_rejection_has_one_human_result(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    output = StringIO()
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(ToolCall("secret", "read_file", {"path": ".env"}),),
            ),
            ModelResponse(text="The requested read was rejected."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        stdin=StringIO("n\n"),
        stdout=output,
    )

    cli._run_task(runtime, "Inspect .env", output)

    rendered = output.getvalue()
    assert rendered.count("⚠ 需要确认") == 1
    assert rendered.count("允许执行？") == 1
    assert rendered.count("已拒绝本次操作") == 1
    assert "你已拒绝本次操作" not in rendered


def test_failed_shell_diagnostic_prefers_error_lines_and_stays_bounded() -> None:
    diagnostic = "intro\n" + "x" * 1_500 + "\nerror: compilation failed\ntrailer"
    lines = cli._render_event(
        RuntimeEvent(
            "tool_result",
            {
                "call_id": "build",
                "outcome": "UNSUCCESSFUL_COMMAND",
                "exit_code": 1,
                "diagnostic": diagnostic,
            },
        ),
        debug=False,
        proposed_actions={"build": ("shell", "custom-build")},
    )

    rendered = "\n".join(lines)
    assert "error: compilation failed" in rendered
    assert "x" * 1_000 not in rendered
    assert len(rendered) < 1_000


def test_workspace_change_summary_is_counts_only_in_normal_and_bounded_in_debug() -> None:
    event = RuntimeEvent(
        "workspace_change_summary",
        {
            "awareness_state": "AVAILABLE",
            "pre_existing_count": 1,
            "known_touched_count": 2,
            "new_or_other_count": 1,
            "attribution_uncertain": True,
            "truncated": False,
            "pre_existing_paths": "user-secret-name.txt",
            "known_touched_paths": "a.py | b.py",
            "new_or_other_paths": "other.txt",
        },
    )

    normal = "\n".join(cli._render_event(event, debug=False))
    debug = "\n".join(cli._render_event(event, debug=True))

    assert "运行前已有 1" in normal
    assert "Agent 已触及 2" in normal
    assert "其他新增 1" in normal
    assert "不完全确定" in normal
    assert "user-secret-name.txt" not in normal
    assert "user-secret-name.txt" in debug
    assert len(debug) < 3_000


def test_unavailable_workspace_awareness_is_normal_silent() -> None:
    event = RuntimeEvent(
        "workspace_change_summary",
        {
            "awareness_state": "NOT_GIT",
            "pre_existing_count": 0,
            "known_touched_count": 0,
            "new_or_other_count": 0,
            "attribution_uncertain": True,
        },
    )

    assert cli._render_event(event, debug=False) == ()
    assert "workspace_change_summary" in "\n".join(
        cli._render_event(event, debug=True)
    )


def test_cli_provider_failure_is_understandable_and_does_not_leak_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "super-secret-provider-key"
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", secret)
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")
    fake = FakeModelClient([FatalProviderError(f"invalid API key: {secret}")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    exit_code = cli.main(["--workspace", str(tmp_path), "fix", "the", "bug"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "模型服务请求失败" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def _configure_main_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")


def test_interactive_session_reprompts_after_empty_failed_and_completed_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)
    fake = FakeModelClient(
        [FatalProviderError("temporary failure"), ModelResponse("Recovered final.")]
    )
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)
    answers = iter(["   ", "first task", "second task", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    exit_code = cli.main(["--workspace", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert len(fake.requests) == 2
    assert "模型服务请求失败" in output
    assert "Recovered final." in output
    assert "Task succeeded" not in output


def test_interactive_active_run_cancellation_returns_to_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_main_environment(monkeypatch)
    class InterruptThenRespondClient:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                raise KeyboardInterrupt
            return ModelResponse("Second final.")

    fake = InterruptThenRespondClient()
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)
    answers = iter(["cancel this run", "continue", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    exit_code = cli.main(["--workspace", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert len(fake.requests) == 2
    assert "已取消本次运行" in output
    assert "Second final." in output


@pytest.mark.parametrize("terminal_signal", [EOFError(), KeyboardInterrupt()])
def test_interactive_top_level_eof_or_interrupt_exits_without_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_signal: BaseException,
) -> None:
    _configure_main_environment(monkeypatch)
    fake = FakeModelClient([ModelResponse("unused")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)

    def stop(prompt: str = "") -> str:
        raise terminal_signal

    monkeypatch.setattr(builtins, "input", stop)

    assert cli.main(["--workspace", str(tmp_path)]) == 0
    assert fake.requests == ()


@pytest.mark.parametrize("command", ["/exit", "/QUIT"])
def test_interactive_exit_commands_do_not_start_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    _configure_main_environment(monkeypatch)
    fake = FakeModelClient([ModelResponse("unused")])
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: fake)
    monkeypatch.setattr(builtins, "input", lambda prompt="": command)

    assert cli.main(["--workspace", str(tmp_path)]) == 0
    assert fake.requests == ()
