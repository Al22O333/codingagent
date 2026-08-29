"""Factual run-review evidence tests."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.cli import CLIConfig, build_runtime
from coding_agent.interaction import ConfirmationDecision, FakeUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import ModelResponse, ToolCall, ToolOutcome
from coding_agent.runtime import (
    AgentRun,
    CommandExecutionEvidence,
    RunState,
)
from coding_agent.workspace_awareness import (
    WorkspaceAwarenessState,
    WorkspaceChangeFacts,
)


def _python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _config(workspace: Path, *, secret: str = "test-key") -> CLIConfig:
    return CLIConfig(
        workspace=workspace,
        model="test-model",
        base_url="https://provider.invalid/v1",
        api_key=secret,
    )


def test_runtime_records_only_bounded_secret_safe_shell_execution_facts(
    tmp_path: Path,
) -> None:
    secret = "review-runtime-secret"
    failed = _python_command("raise SystemExit(7)")
    successful = _python_command(f"print({secret!r})")
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall("failed", "shell", {"command": failed}),
                    ToolCall("success", "shell", {"command": successful}),
                ),
            ),
            ModelResponse(text="Candidate."),
            ModelResponse(text="Audited final."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path, secret=secret),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Run both checks and report their observed outcomes.")

    assert run.state is RunState.COMPLETED
    assert [item.outcome for item in run.command_execution_evidence] == [
        ToolOutcome.UNSUCCESSFUL_COMMAND,
        ToolOutcome.SUCCESS,
    ]
    assert [item.exit_code for item in run.command_execution_evidence] == [7, 0]
    assert all(item.cwd == "." for item in run.command_execution_evidence)
    assert secret not in repr(run.command_execution_evidence)
    assert "<redacted>" in run.command_execution_evidence[1].command
    assert not hasattr(run.command_execution_evidence[0], "stdout")
    assert not hasattr(run.command_execution_evidence[0], "stderr")


def test_permission_rejected_shell_is_not_execution_evidence(tmp_path: Path) -> None:
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        "commit",
                        "shell",
                        {"command": "git commit -m blocked"},
                    ),
                ),
            ),
            ModelResponse(text="Candidate after rejection."),
            ModelResponse(text="Audited final after rejection."),
        ]
    )
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction([ConfirmationDecision.REJECT]),
    )

    run = runtime.run("Try the requested commit.")

    assert run.state is RunState.COMPLETED
    assert run.command_execution_evidence == []
    assert run.command_evidence_truncated is False


def test_interrupted_shell_is_recorded_without_changing_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall(
                        "interrupted",
                        "shell",
                        {"command": _python_command("print('never returned')")},
                    ),
                ),
            )
        ]
    )

    def interrupt(self, prepared):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr("coding_agent.shell.ShellTool.execute", interrupt)
    runtime = build_runtime(
        _config(tmp_path),
        model_client=client,
        user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("Run the command.")

    assert run.state is RunState.CANCELLED
    assert len(run.command_execution_evidence) == 1
    evidence = run.command_execution_evidence[0]
    assert evidence.interrupted is True
    assert evidence.outcome is None
    assert evidence.error_code == "USER_CANCELLATION"
    review = cli._run_json_document(
        run,
        secret_values=(),
        include_review=True,
    )["review"]
    assert review["command_evidence"][0]["outcome"] == "INTERRUPTED"


def test_review_projection_is_opt_in_bounded_and_does_not_infer_verification() -> None:
    secret = "projection-secret"
    paths = tuple(f"changed/{index:02d}-{secret}.py" for index in range(60))
    run = AgentRun("run", "task", state=RunState.COMPLETED, final_response="done")
    run.workspace_change_facts = WorkspaceChangeFacts(
        awareness_state=WorkspaceAwarenessState.AVAILABLE,
        known_agent_touched_paths=paths,
        attribution_uncertain=True,
    )
    run.command_execution_evidence.append(
        CommandExecutionEvidence(
            command=f"python -m pytest {secret}",
            cwd=".",
            outcome=ToolOutcome.SUCCESS,
            exit_code=0,
        )
    )
    run.command_evidence_truncated = True

    default = cli._run_json_document(run, secret_values=(secret,))
    reviewed = cli._run_json_document(
        run,
        secret_values=(secret,),
        include_review=True,
    )
    serialized = json.dumps(reviewed, ensure_ascii=False)

    assert "review" not in default
    assert secret not in serialized
    assert len(
        reviewed["review"]["workspace_changes"]["known_agent_touched_paths"]
    ) == 50
    assert reviewed["review"]["workspace_changes"]["paths_truncated"] is True
    assert reviewed["review"]["command_evidence"] == [
        {
            "command": "python -m pytest <redacted>",
            "cwd": ".",
            "outcome": "SUCCESS",
            "exit_code": 0,
            "error_code": None,
            "presentation_category": "test",
        }
    ]
    assert reviewed["review"]["command_evidence_truncated"] is True
    assert reviewed["review"]["verification_sufficiency"] == "NOT_INFERRED"


def test_human_review_quotes_control_characters_and_states_evidence_boundary() -> None:
    run = AgentRun("run", "task", state=RunState.FAILED)
    run.workspace_change_facts = WorkspaceChangeFacts(
        awareness_state=WorkspaceAwarenessState.AVAILABLE,
        known_agent_touched_paths=("safe.py",),
        attribution_uncertain=False,
    )
    run.command_execution_evidence.append(
        CommandExecutionEvidence(
            command="python -m pytest\nsecond-line",
            cwd=".",
            outcome=ToolOutcome.UNSUCCESSFUL_COMMAND,
            exit_code=1,
        )
    )
    output = StringIO()

    cli._write_run_review(run, output, secret_values=())
    rendered = output.getvalue()

    assert '"safe.py"' in rendered
    assert "pytest\\nsecond-line" in rendered
    assert "不自动证明验证充分性" in rendered
    assert "\nsecond-line" not in rendered


def test_json_review_flag_is_wired_without_changing_default_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleModelClient",
        lambda config: FakeModelClient([ModelResponse(text="Read-only result.")]),
    )

    exit_code = cli.main(
        ["--workspace", str(tmp_path), "--json", "--review", "inspect"]
    )
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert document["review"]["command_evidence"] == []
    assert document["review"]["verification_sufficiency"] == "NOT_INFERRED"
    assert document["review"]["workspace_changes"]["awareness_state"] == "UNAVAILABLE"
