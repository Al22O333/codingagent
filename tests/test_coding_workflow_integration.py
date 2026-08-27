"""Opt-in real-model M1 coding workflow acceptance test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from coding_agent.cli import CLIConfig, build_runtime
from coding_agent.interaction import (
    ClarificationRequest,
    ConfirmationRequest,
    FakeUserInteraction,
    UserInteractionError,
)
from coding_agent.model_client import ModelClient
from coding_agent.openai_client import (
    OpenAICompatibleConfig,
    OpenAICompatibleModelClient,
)
from coding_agent.protocol import (
    ModelRequest,
    ModelResponse,
    ToolOutcome,
    ToolResultMessage,
)
from coding_agent.runtime import RunState
from coding_agent.shell import ShellContent


_API_KEY = os.getenv("CODING_AGENT_TEST_API_KEY")
_MODEL = os.getenv("CODING_AGENT_TEST_MODEL")
_BASE_URL = os.getenv("CODING_AGENT_TEST_BASE_URL")
_HAS_PROVIDER = bool(_API_KEY and _MODEL)


class RecordingModelClient:
    """Test-only recorder around the real concrete ModelClient."""

    def __init__(self, delegate: ModelClient) -> None:
        self._delegate = delegate
        self.requests: list[ModelRequest] = []
        self.responses: list[ModelResponse] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = self._delegate.complete(request)
        self.responses.append(response)
        return response


class DiagnosticInteraction(FakeUserInteraction):
    def confirm(self, request: ConfirmationRequest) -> NoReturn:
        raise UserInteractionError(f"unexpected confirmation: {request!r}")

    def ask(self, request: ClarificationRequest) -> NoReturn:
        raise UserInteractionError(f"unexpected clarification: {request!r}")


@pytest.mark.skipif(
    not _HAS_PROVIDER,
    reason="set CODING_AGENT_TEST_API_KEY and CODING_AGENT_TEST_MODEL",
)
def test_real_model_completes_inspect_edit_verify_workflow(tmp_path: Path) -> None:
    workspace = tmp_path / "sample-project"
    workspace.mkdir()
    (workspace / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        "    \"\"\"Return the sum of two integers.\"\"\"\n"
        "    return left - right\n",
        encoding="utf-8",
    )
    (workspace / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_adds_positive_numbers(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_adds_mixed_sign_numbers(self):\n"
        "        self.assertEqual(add(-2, 1), -1)\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    assert _API_KEY is not None and _MODEL is not None
    real_client = OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model=_MODEL,
            api_key=_API_KEY,
            base_url=_BASE_URL,
        )
    )
    recording_client = RecordingModelClient(real_client)
    runtime = build_runtime(
        CLIConfig(
            workspace=workspace,
            model=_MODEL,
            api_key=_API_KEY,
            base_url=_BASE_URL,
            api_key_environment_name="CODING_AGENT_TEST_API_KEY",
        ),
        model_client=recording_client,
        user_interaction=DiagnosticInteraction(),
    )

    verification_command = f'"{Path(sys.executable)}" -m unittest -v'
    run = runtime.run(
        "Fix the bug in calculator.add so the existing unit tests pass. "
        "Inspect the relevant files first. Make the smallest code change using "
        "the edit_file tool. After editing, use the shell tool with exactly this "
        f"command to verify the result: `{verification_command}`. Do not claim "
        "completion until that command exits successfully."
    )

    assert run.state is RunState.COMPLETED, {
        "termination_reason": run.termination_reason,
        "last_error": repr(run.last_error),
        "tool_calls": [
            (
                call.name,
                dict(call.raw_arguments)
                if hasattr(call.raw_arguments, "items")
                else call.raw_arguments,
            )
            for response in recording_client.responses
            for call in response.tool_calls
        ],
    }
    assert run.final_response is not None and run.final_response.strip()
    assert "return left + right" in (workspace / "calculator.py").read_text(
        encoding="utf-8"
    )

    tool_names = [
        call.name
        for response in recording_client.responses
        for call in response.tool_calls
    ]
    assert "read_file" in tool_names
    assert "edit_file" in tool_names
    assert "shell" in tool_names

    results = [
        result
        for request in recording_client.requests
        for message in request.messages
        if isinstance(message, ToolResultMessage)
        for result in message.results
    ]
    edit_results = [result for result in results if result.tool_name == "edit_file"]
    shell_results = [result for result in results if result.tool_name == "shell"]
    assert any(result.outcome is ToolOutcome.SUCCESS for result in edit_results)
    assert any(
        result.outcome is ToolOutcome.SUCCESS
        and isinstance(result.content, ShellContent)
        and result.content.command == verification_command
        and result.content.exit_code == 0
        and "OK" in result.content.stderr
        for result in shell_results
    ), [
        (result.outcome, repr(result.content), result.error)
        for result in shell_results
    ]


@pytest.mark.skipif(
    not _HAS_PROVIDER,
    reason="set CODING_AGENT_TEST_API_KEY and CODING_AGENT_TEST_MODEL",
)
def test_real_model_solves_natural_language_bug_report(tmp_path: Path) -> None:
    workspace = tmp_path / "natural-bug-fix"
    workspace.mkdir()
    (workspace / "pricing.py").write_text(
        "def discounted_price(price: int, discount: int) -> int:\n"
        "    \"\"\"Return price after subtracting the discount.\"\"\"\n"
        "    return price + discount\n",
        encoding="utf-8",
    )
    (workspace / "test_pricing.py").write_text(
        "import unittest\n\n"
        "from pricing import discounted_price\n\n\n"
        "class PricingTests(unittest.TestCase):\n"
        "    def test_subtracts_discount(self):\n"
        "        self.assertEqual(discounted_price(100, 15), 85)\n\n"
        "    def test_zero_discount(self):\n"
        "        self.assertEqual(discounted_price(42, 0), 42)\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )

    assert _API_KEY is not None and _MODEL is not None
    recording_client = RecordingModelClient(
        OpenAICompatibleModelClient(
            OpenAICompatibleConfig(
                model=_MODEL,
                api_key=_API_KEY,
                base_url=_BASE_URL,
            )
        )
    )
    runtime = build_runtime(
        CLIConfig(
            workspace=workspace,
            model=_MODEL,
            api_key=_API_KEY,
            base_url=_BASE_URL,
            api_key_environment_name="CODING_AGENT_TEST_API_KEY",
        ),
        model_client=recording_client,
        user_interaction=DiagnosticInteraction(),
    )

    run = runtime.run("这个项目有一个测试失败，帮我找到原因并修好。")

    tool_calls = [
        call
        for response in recording_client.responses
        for call in response.tool_calls
    ]
    assert run.state is RunState.COMPLETED, {
        "termination_reason": run.termination_reason,
        "last_error": repr(run.last_error),
        "tool_names": [call.name for call in tool_calls],
    }
    assert run.final_response is not None and run.final_response.strip()
    assert "return price - discount" in (workspace / "pricing.py").read_text(
        encoding="utf-8"
    )
    verification = subprocess.run(
        [sys.executable, "-m", "unittest", "-v"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verification.returncode == 0, verification.stderr

    tool_names = [call.name for call in tool_calls]
    assert any(
        name in {"list_directory", "search_files", "search_text", "read_file"}
        for name in tool_names
    )
    assert "edit_file" in tool_names
    assert "shell" in tool_names
    results = [
        result
        for request in recording_client.requests
        for message in request.messages
        if isinstance(message, ToolResultMessage)
        for result in message.results
    ]
    assert any(
        result.tool_name == "shell"
        and result.outcome is ToolOutcome.SUCCESS
        and isinstance(result.content, ShellContent)
        and result.content.exit_code == 0
        for result in results
    )
    for call in tool_calls:
        arguments = call.raw_arguments
        if not hasattr(arguments, "get"):
            continue
        for key in ("path", "cwd"):
            value = arguments.get(key)
            if not isinstance(value, str):
                continue
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            assert candidate.resolve(strict=False).is_relative_to(workspace.resolve())
