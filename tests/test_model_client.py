"""Unit tests for the Step 3 ModelClient seam and fake."""

import pytest

from coding_agent.model_client import (
    FakeModelClient,
    FakeModelExhaustedError,
    ModelClient,
)
from coding_agent.protocol import (
    ModelRequest,
    ModelResponse,
    ToolCall,
    UserMessage,
)


def make_request(text: str = "test task") -> ModelRequest:
    return ModelRequest(messages=(UserMessage(text),))


def test_fake_model_client_returns_deterministic_final_response() -> None:
    response = ModelResponse(text="Finished.")
    client = FakeModelClient([response])
    request = make_request()

    assert isinstance(client, ModelClient)
    assert client.complete(request) is response
    assert client.requests == (request,)
    assert client.remaining_events == 0


def test_fake_model_client_returns_deterministic_tool_call_response() -> None:
    tool_call = ToolCall(
        call_id="call-1",
        name="read_file",
        raw_arguments={"path": "main.py"},
    )
    response = ModelResponse(text=None, tool_calls=(tool_call,))
    client = FakeModelClient([response])

    returned = client.complete(make_request("read main.py"))

    assert returned.tool_calls == (tool_call,)
    assert returned.text is None


def test_fake_model_client_consumes_events_in_order() -> None:
    first = ModelResponse(text="first")
    second = ModelResponse(text="second")
    client = FakeModelClient([first, second])

    assert client.complete(make_request("one")) is first
    assert client.complete(make_request("two")) is second
    assert [request.messages[0].text for request in client.requests] == [  # type: ignore[union-attr]
        "one",
        "two",
    ]


def test_fake_model_client_can_inject_an_exception_event() -> None:
    failure = RuntimeError("provider unavailable")
    client = FakeModelClient([failure])

    with pytest.raises(RuntimeError, match="provider unavailable") as captured:
        client.complete(make_request())

    assert captured.value is failure
    assert len(client.requests) == 1


def test_fake_model_client_fails_clearly_when_sequence_is_exhausted() -> None:
    client = FakeModelClient([])

    with pytest.raises(FakeModelExhaustedError, match="no configured event"):
        client.complete(make_request())
