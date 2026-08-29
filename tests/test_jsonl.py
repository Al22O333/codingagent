"""Bounded non-interactive JSONL event-stream tests."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.model_client import FakeModelClient
from coding_agent.protocol import ModelResponse, ToolCall
from coding_agent.runtime import RuntimeEvent


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")


def test_jsonl_projection_redacts_and_excludes_content_bearing_event_facts() -> None:
    secret = "jsonl-projection-secret"
    output = StringIO()
    stream = cli._JsonlEventStream(output, (secret,))

    stream(
        RuntimeEvent(
            "tool_proposed",
            {
                "call_id": "call-1",
                "tool_name": "shell",
                "action": f"print {secret}",
                "diagnostic": f"output {secret}",
                "known_touched_paths": f"private-{secret}.txt",
                "safe_note": f"status {secret}",
            },
        )
    )
    stream.write_result({"lifecycle_state": "COMPLETED"})

    documents = [json.loads(line) for line in output.getvalue().splitlines()]
    facts = documents[0]["event"]["facts"]

    assert [document["sequence"] for document in documents] == [1, 2]
    assert [document["type"] for document in documents] == ["event", "result"]
    assert facts == {
        "call_id": "call-1",
        "tool_name": "shell",
        "safe_note": "status <redacted>",
    }
    assert secret not in output.getvalue()


def test_jsonl_success_streams_safe_events_then_one_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    (tmp_path / "note.txt").write_text(
        "FILE_CONTENT_MUST_NOT_ENTER_EVENTS",
        encoding="utf-8",
    )
    client = FakeModelClient(
        [
            ModelResponse(
                text=None,
                tool_calls=(
                    ToolCall("read", "read_file", {"path": "note.txt"}),
                ),
            ),
            ModelResponse(text="Observed the current file safely."),
        ]
    )
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: client)

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--jsonl",
            "--non-interactive",
            "inspect note.txt",
        ]
    )
    captured = capsys.readouterr()
    documents = [json.loads(line) for line in captured.out.splitlines()]
    events = [document for document in documents if document["type"] == "event"]
    result = documents[-1]

    assert exit_code == 0
    assert captured.err == ""
    assert len(events) >= 5
    assert [document["sequence"] for document in documents] == list(
        range(1, len(documents) + 1)
    )
    assert result["type"] == "result"
    assert result["result"]["lifecycle_state"] == "COMPLETED"
    assert result["result"]["final_response"] == "Observed the current file safely."
    assert events[-1]["event"]["kind"] == "run_terminal"
    assert all(
        "action" not in document["event"]["facts"]
        and "diagnostic" not in document["event"]["facts"]
        and "known_touched_paths" not in document["event"]["facts"]
        for document in events
    )
    assert "FILE_CONTENT_MUST_NOT_ENTER_EVENTS" not in captured.out
    assert "Coding Agent v1" not in captured.out


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        (["--jsonl", "task"], "JSONL_NON_INTERACTIVE_REQUIRED"),
        (
            ["--jsonl", "--non-interactive"],
            "NON_INTERACTIVE_ONE_SHOT_REQUIRED",
        ),
    ],
)
def test_jsonl_usage_failures_are_one_terminal_result_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected_code: str,
) -> None:
    exit_code = cli.main(["--workspace", str(tmp_path), *arguments])
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    document = json.loads(lines[0])

    assert exit_code == 2
    assert len(lines) == 1
    assert captured.err == ""
    assert document["type"] == "result"
    assert document["sequence"] == 1
    assert document["result"]["lifecycle_state"] == "STARTUP_FAILED"
    assert document["result"]["normalized_error"]["code"] == expected_code


def test_jsonl_noninteractive_required_interaction_is_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleModelClient",
        lambda config: FakeModelClient(
            [
                ModelResponse(
                    text=None,
                    tool_calls=(
                        ToolCall(
                            "clarify",
                            "ask_user",
                            {"question": "Which exact format should I use?"},
                        ),
                    ),
                )
            ]
        ),
    )

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--jsonl",
            "--non-interactive",
            "change the format",
        ]
    )
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    result = documents[-1]["result"]

    assert exit_code == 3
    assert documents[-1]["type"] == "result"
    assert result["terminal_reason"] == "CLARIFICATION_REQUIRED"
    assert result["required_interaction"]["kind"] == "CLARIFICATION"
    assert result["model_turns"] == 1
    assert all(
        "question" not in document["event"]["facts"]
        for document in documents[:-1]
    )


def test_jsonl_composes_with_review_and_persistent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch)
    session_directory = tmp_path / "sessions"
    monkeypatch.setenv("CODING_AGENT_SESSION_DIR", str(session_directory))
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleModelClient",
        lambda config: FakeModelClient([ModelResponse(text="Persisted result.")]),
    )

    exit_code = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "--jsonl",
            "--non-interactive",
            "--review",
            "--persist-session",
            "remember this result",
        ]
    )
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    result = documents[-1]["result"]

    assert exit_code == 0
    assert result["session_checkpoint_updated"] is True
    assert result["session_error"] is None
    assert result["review"]["verification_sufficiency"] == "NOT_INFERRED"
    assert (session_directory / f"{result['session_id']}.json").is_file()
