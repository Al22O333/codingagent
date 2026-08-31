"""Content-free runtime failure diagnostics across human and machine output."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from coding_agent import cli
from coding_agent.context import ContextManager
from coding_agent.interaction import FakeUserInteraction
from coding_agent.model_client import FakeModelClient
from coding_agent.openai_client import OpenAICompatibleConfig, OpenAICompatibleModelClient
from coding_agent.policy import PolicyEngine
from coding_agent.protocol import ModelResponse, ToolCall
from coding_agent.runtime import AgentRuntime, RunState, RuntimeLimits, TerminationReason
from coding_agent.tooling import ToolRegistry


def _config(workspace: Path, *, debug: bool = False) -> cli.CLIConfig:
    return cli.CLIConfig(
        workspace=workspace,
        model="local-test",
        base_url="https://provider.invalid/v1",
        api_key="configured-secret",
        debug=debug,
    )


def test_real_sdk_bad_json_has_safe_diagnostic_and_same_session_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "configured-secret"
    private_body = b'{"private":"unconfigured-private-payload"'
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=private_body,
        )

    def sdk(**kwargs: object) -> AsyncOpenAI:
        return AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )

    monkeypatch.setattr("coding_agent.openai_client.AsyncOpenAI", sdk)
    provider = OpenAICompatibleModelClient(OpenAICompatibleConfig(
        model="local-test", base_url="https://provider.invalid/v1", api_key=secret,
    ))

    class RecoveringClient:
        first = True

        def complete(self, request):
            if self.first:
                self.first = False
                return provider.complete(request)
            return ModelResponse("ready")

    events = []
    runtime = cli.build_runtime(
        _config(tmp_path), model_client=RecoveringClient(),
        user_interaction=FakeUserInteraction(), event_observer=events.append,
    )
    run = runtime.run("private-user-task")

    assert run.termination_reason is TerminationReason.RUNTIME_FAILURE
    assert isinstance(run.last_error, json.JSONDecodeError)
    assert len(requests) == 1  # This change must not introduce a retry policy.
    diagnostic = run.failure_diagnostic
    assert diagnostic is not None
    assert diagnostic.code == "MODEL_RESPONSE_JSON_INVALID"
    assert diagnostic.error_type == "JSONDecodeError"
    assert diagnostic.phase == "model_request"
    assert diagnostic.context_chars > 0
    assert diagnostic.context_limit == 80_000
    assert diagnostic.reasoning_chars == 0
    assert "coding_agent.openai_client" in diagnostic.trace
    assert str(tmp_path) not in diagnostic.trace
    assert "model_request" in repr(events)
    for sensitive in (secret, "unconfigured-private-payload", "private-user-task"):
        assert sensitive not in repr(diagnostic)
        assert sensitive not in repr(events)

    recovered = runtime.run("continue")
    assert recovered.state is RunState.COMPLETED
    assert recovered.failure_diagnostic is None
    assert recovered.final_response == "ready"


def test_context_overflow_records_failed_size_including_hidden_reasoning() -> None:
    context = ContextManager(max_context_chars=8_000)
    reasoning = "private-provider-reasoning-" * 400
    client = FakeModelClient([
        ModelResponse(None, (ToolCall("probe", "missing_tool", {}),),
                      provider_reasoning_content=reasoning),
        ModelResponse("must not be used"),
    ])
    runtime = AgentRuntime(
        client, context, ToolRegistry(), RuntimeLimits(5, 5, 30, 0, 2),
        policy_engine=PolicyEngine(), user_interaction=FakeUserInteraction(),
    )

    run = runtime.run("inspect")

    assert run.termination_reason is TerminationReason.RUNTIME_FAILURE
    assert len(client.requests) == 1
    diagnostic = run.failure_diagnostic
    assert diagnostic is not None
    assert diagnostic.code == "CONTEXT_LIMIT_EXCEEDED"
    assert diagnostic.error_type == "ContextLimitError"
    assert diagnostic.phase == "context_build"
    assert diagnostic.context_chars > diagnostic.context_limit == 8_000
    assert diagnostic.reasoning_chars == len(reasoning)
    assert "private-provider-reasoning" not in repr(diagnostic)
    assert context.last_model_context_size is None  # Cleared at the Run boundary.
    recovered = runtime.run("new task")
    assert recovered.state is RunState.COMPLETED
    assert recovered.failure_diagnostic is None


@pytest.mark.parametrize("debug", [False, True])
def test_human_failure_has_actionable_safe_details(tmp_path: Path, debug: bool) -> None:
    output = StringIO()
    config = _config(tmp_path, debug=debug)
    runtime = cli.build_runtime(
        config,
        model_client=FakeModelClient([json.JSONDecodeError(
            "configured-secret arbitrary-private-message", "private-json-document", 0,
        )]),
        user_interaction=FakeUserInteraction(), stdout=output,
    )

    assert cli._run_task(runtime, "inspect", output, config=config) == 1

    rendered = output.getvalue()
    assert "JSONDecodeError" in rendered
    assert "model_request" in rendered
    assert "MODEL_RESPONSE_JSON_INVALID" in rendered
    assert "80000" in rendered
    assert ("coding_agent.runtime" in rendered) is debug
    for sensitive in (
        "configured-secret", "arbitrary-private-message", "private-json-document",
    ):
        assert sensitive not in rendered


@pytest.mark.parametrize("mode", ["--json", "--jsonl"])
def test_machine_failure_diagnostic_is_structured_and_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], mode: str,
) -> None:
    monkeypatch.setenv("CODING_AGENT_MODEL", "local-test")
    monkeypatch.setenv("CODING_AGENT_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("CODING_AGENT_API_KEY", "configured-secret")
    monkeypatch.setattr(cli, "OpenAICompatibleModelClient", lambda config: FakeModelClient([
        RuntimeError("unconfigured-sensitive-text configured-secret"),
    ]))

    assert cli.main([
        "--workspace", str(tmp_path), "--non-interactive", mode, "inspect",
    ]) == 1

    output = capsys.readouterr()
    documents = [json.loads(line) for line in output.out.splitlines()]
    result = documents[-1]["result"] if mode == "--jsonl" else documents[0]
    assert result["terminal_reason"] == "RUNTIME_FAILURE"
    diagnostic = result["normalized_error"]["diagnostic"]
    assert diagnostic["code"] == "UNEXPECTED_RUNTIME_ERROR"
    assert diagnostic["error_type"] == "RuntimeError"
    assert diagnostic["phase"] == "model_request"
    assert "unconfigured-sensitive-text" not in output.out + output.err
    assert "configured-secret" not in output.out + output.err
    if mode == "--jsonl":
        failures = [item for item in documents if item.get("event", {}).get("kind") == "runtime_failure"]
        assert len(failures) == 1


def test_unknown_exception_str_is_never_evaluated(tmp_path: Path) -> None:
    class BrokenError(RuntimeError):
        def __str__(self) -> str:
            raise AssertionError("must not inspect arbitrary exception messages")

    output = StringIO()
    config = _config(tmp_path, debug=True)
    runtime = cli.build_runtime(
        config, model_client=FakeModelClient([BrokenError()]), stdout=output,
        user_interaction=FakeUserInteraction(),
    )

    assert cli._run_task(runtime, "inspect", output, config=config) == 1
    assert "BrokenError" in output.getvalue()
    assert "UNEXPECTED_RUNTIME_ERROR" in output.getvalue()


def test_diagnostic_failure_and_observer_failure_cannot_mask_original_error() -> None:
    class BrokenDiagnosticContext(ContextManager):
        @property
        def last_model_context_size(self):
            raise ValueError("private diagnostic error")

    def broken_observer(event):
        raise ValueError("private observer error")

    original = RuntimeError("private original error")
    context = BrokenDiagnosticContext()
    runtime = AgentRuntime(
        FakeModelClient([original, ModelResponse("ready")]), context, ToolRegistry(),
        RuntimeLimits(5, 5, 30, 0, 2), policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(), observer=broken_observer,
    )

    run = runtime.run("inspect")

    assert run.state is RunState.FAILED
    assert run.last_error is original
    assert run.pending_action is None
    assert run.failure_diagnostic.error_type == "Exception"
    assert "private" not in repr(run.failure_diagnostic)
    assert runtime.run("continue").state is RunState.COMPLETED


def test_diagnostic_omits_source_paths_locals_and_untrusted_exception_names() -> None:
    secret = "name-secret"
    namespace = {"__name__": "coding_agent.diagnostic_test"}
    source = (
        "def complete(self, request):\n"
        "    private_local = 'unconfigured-private-local'\n"
        "    raise RuntimeError(private_local)\n"
    )
    exec(compile(source, "private-source-path/name-secret.py", "exec"), namespace)
    client = type("DiagnosticClient", (), {"complete": namespace["complete"]})()
    runtime = AgentRuntime(
        client, ContextManager(), ToolRegistry(), RuntimeLimits(5, 5, 30, 0, 2),
        policy_engine=PolicyEngine(), user_interaction=FakeUserInteraction(),
        runtime_secret_values=(secret,),
    )

    run = runtime.run("inspect")

    diagnostic = run.failure_diagnostic
    assert "coding_agent.diagnostic_test.complete" in diagnostic.trace
    for sensitive in ("private-source-path", secret, "unconfigured-private-local"):
        assert sensitive not in repr(diagnostic)
    unsafe_error = type("malicious\nsecret-name", (RuntimeError,), {})()
    runtime = AgentRuntime(
        FakeModelClient([unsafe_error]), ContextManager(), ToolRegistry(),
        RuntimeLimits(5, 5, 30, 0, 2), policy_engine=PolicyEngine(),
        user_interaction=FakeUserInteraction(),
    )
    assert runtime.run("inspect").failure_diagnostic.error_type == "Exception"
