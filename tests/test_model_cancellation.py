"""Real local HTTP cancellation, without provider credentials or API spending."""

from __future__ import annotations

import _thread
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from coding_agent.cli import build_runtime, load_config
from coding_agent.interaction import FakeUserInteraction
from coding_agent.runtime import RunState, TerminationReason


@contextmanager
def _slow_provider(
    partial_body: bool, *, stall_seconds: float = 5,
) -> Iterator[tuple[str, threading.Event, threading.Event, list[object]]]:
    waiting = threading.Event()
    disconnected = threading.Event()
    handler_done = threading.Event()
    requests: list[object] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            slow = len(requests) == 1
            # A cancelled provider response must never dispatch this mutation.
            message = (
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "late-call", "type": "function", "function": {
                        "name": "create_file",
                        "arguments": json.dumps({"path": "late.txt", "content": "late"}),
                    },
                }]}
                if slow else {"role": "assistant", "content": "ready"}
            )
            payload = json.dumps({
                "id": "local-test", "object": "chat.completion", "created": 0,
                "model": "local-test", "choices": [{
                    "index": 0, "message": message,
                    "finish_reason": "tool_calls" if slow else "stop",
                }],
            }).encode()

            def headers() -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()

            try:
                if slow:
                    if partial_body:
                        headers()
                        self.wfile.write(payload[:1])
                        self.wfile.flush()
                    waiting.set()
                    self.connection.settimeout(stall_seconds)
                    try:
                        if self.connection.recv(1) == b"":
                            disconnected.set()
                            return
                    except socket.timeout:
                        pass
                if not (slow and partial_body):
                    headers()
                self.wfile.write(payload[1:] if slow and partial_body else payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()
            finally:
                if slow:
                    handler_done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", waiting, disconnected, requests
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        if waiting.is_set():
            assert handler_done.wait(6), "local HTTP handler leaked"


@pytest.mark.parametrize(
    "partial_body", [False, True], ids=["response-headers", "response-body"],
)
def test_model_wait_cancels_promptly_disconnects_and_recovers_same_session(
    tmp_path: Path, partial_body: bool,
) -> None:
    with _slow_provider(partial_body) as (base_url, waiting, disconnected, requests):
        runtime = build_runtime(
            load_config(str(tmp_path), {
                "CODING_AGENT_MODEL": "local-test",
                "CODING_AGENT_BASE_URL": base_url,
                "CODING_AGENT_API_KEY": "local-placeholder",
            }),
            user_interaction=FakeUserInteraction(),
        )
        stop_interrupt = threading.Event()
        interrupt_at: list[float] = []

        def interrupt_when_waiting() -> None:
            if waiting.wait(5) and not stop_interrupt.wait(0.25):
                interrupt_at.append(time.monotonic())
                _thread.interrupt_main()

        interrupt_thread = threading.Thread(target=interrupt_when_waiting, daemon=True)
        interrupt_thread.start()
        try:
            cancelled = runtime.run("Wait for the slow response")
            returned_at = time.monotonic()
        finally:
            stop_interrupt.set()
            interrupt_thread.join(timeout=1)

        assert interrupt_at, "test did not send an interrupt during HTTP waiting"
        latency = returned_at - interrupt_at[0]
        print(f"HTTP cancellation latency ({partial_body=}): {latency:.3f}s")
        assert latency < 2, "cancellation waited for provider response"
        assert cancelled.state is RunState.CANCELLED
        assert cancelled.termination_reason is TerminationReason.USER_CANCELLATION
        assert cancelled.model_turns == cancelled.tool_call_attempts == 0
        assert cancelled.final_response is None
        assert cancelled.pending_action is None
        assert runtime.completed_run_continuity == ()
        assert disconnected.wait(1), "cancelled HTTP connection stayed open"
        assert not (tmp_path / "late.txt").exists()

        recovered = runtime.run("Reply ready; do not change any files")
        assert recovered.state is RunState.COMPLETED
        assert recovered.final_response == "ready"
        assert recovered.tool_call_attempts == 0
        assert runtime.session.runs == (cancelled, recovered)
        assert len(requests) == 2, "cancellation caused an unintended retry"
        assert not (tmp_path / "late.txt").exists()
