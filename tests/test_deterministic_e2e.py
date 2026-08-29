"""Deterministic full-path Coding Agent acceptance tests."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from coding_agent.cli import CLIConfig, build_runtime
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import (
    ModelResponse,
    ToolCall,
    ToolOutcome,
    ToolResultMessage,
)
from coding_agent.runtime import RunState, TerminationReason


def _config(workspace: Path) -> CLIConfig:
    return CLIConfig(
        workspace=workspace,
        model="deterministic-test-model",
        base_url="https://provider.invalid/v1",
        api_key="deterministic-test-key",
    )


def _command(arguments: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _call(number: int, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        text=None,
        tool_calls=(ToolCall(f"call-{number}", name, arguments),),
    )


def _observed_results(client: FakeModelClient):  # type: ignore[no-untyped-def]
    results = []
    seen_call_ids: set[str] = set()
    for request in client.requests:
        for message in request.messages:
            if not isinstance(message, ToolResultMessage):
                continue
            for result in message.results:
                if result.call_id not in seen_call_ids:
                    results.append(result)
                    seen_call_ids.add(result.call_id)
    return results


def test_agent_repairs_code_after_failed_verification_then_observes_pass(
    tmp_path: Path,
) -> None:
    source = (
        "def sign(number: int) -> str:\n"
        "    if number >= 0:\n"
        "        return \"positive\"\n"
        "    return \"negative\"\n"
    )
    final_source = (
        "def sign(number: int) -> str:\n"
        "    if number > 0:\n"
        "        return \"positive\"\n"
        "    if number == 0:\n"
        "        return \"zero\"\n"
        "    return \"negative\"\n"
    )
    (tmp_path / "calculator.py").write_text(source, encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import sign\n\n"
        "def test_sign_values():\n"
        "    assert sign(2) == \"positive\"\n"
        "    assert sign(0) == \"zero\"\n"
        "    assert sign(-2) == \"negative\"\n",
        encoding="utf-8",
    )
    verify = _command([sys.executable, "-m", "pytest", "-q"])
    client = FakeModelClient(
        [
            _call(1, "search_files", {"pattern": "*.py"}),
            _call(2, "search_text", {"query": "def sign", "path": "."}),
            _call(3, "read_file", {"path": "calculator.py"}),
            _call(
                4,
                "edit_file",
                {
                    "path": "calculator.py",
                    "old_text": "number >= 0",
                    "new_text": "number > 0",
                    "expected_count": 1,
                },
            ),
            _call(5, "shell", {"command": verify}),
            _call(
                6,
                "edit_file",
                {
                    "path": "calculator.py",
                    "old_text": '    return "negative"',
                    "new_text": (
                        '    if number == 0:\n'
                        '        return "zero"\n'
                        '    return "negative"'
                    ),
                    "expected_count": 1,
                },
            ),
            _call(7, "shell", {"command": verify}),
            ModelResponse(
                "Updated sign handling after the first verification exposed the "
                "zero case. The full test command now passes."
            ),
            ModelResponse(
                "Self-audit confirmed the positive, zero, and negative cases are "
                "implemented and the full test command passes."
            ),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Fix sign() so positive, zero, and negative values are correct")

    assert run.state is RunState.COMPLETED
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == final_source
    results = _observed_results(client)
    assert [result.tool_name for result in results] == [
        "search_files",
        "search_text",
        "read_file",
        "edit_file",
        "shell",
        "edit_file",
        "shell",
    ]
    shell_results = [result for result in results if result.tool_name == "shell"]
    assert [result.outcome for result in shell_results] == [
        ToolOutcome.UNSUCCESSFUL_COMMAND,
        ToolOutcome.SUCCESS,
    ]
    assert shell_results[0].content["exit_code"] != 0  # type: ignore[index]
    assert shell_results[1].content["exit_code"] == 0  # type: ignore[index]
    assert "passes" in (run.final_response or "")


def test_interrupted_run_does_not_poison_same_session_real_tool_loop(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text("value = 42\n", encoding="utf-8")
    read_call = ToolCall("read-main", "read_file", {"path": "main.py"})
    client = FakeModelClient(
        [
            KeyboardInterrupt(),
            ModelResponse(None, (read_call,)),
            ModelResponse("Recovered and read main.py."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )

    interrupted = runtime.run("Interrupt this run")
    recovered = runtime.run("Read main.py")

    assert interrupted.state is RunState.CANCELLED
    assert interrupted.termination_reason is TerminationReason.USER_CANCELLATION
    assert recovered.state is RunState.COMPLETED
    assert runtime.session.runs == (interrupted, recovered)
    assert len(client.requests) == 3
    assert all(
        "Interrupt this run" not in repr(message)
        for message in client.requests[1].messages
    )
    recovered_results = _observed_results(client)
    assert recovered_results[-1].tool_name == "read_file"
    assert recovered_results[-1].outcome is ToolOutcome.SUCCESS
    assert recovered_results[-1].content["content"] == "1 | value = 42"  # type: ignore[index]
