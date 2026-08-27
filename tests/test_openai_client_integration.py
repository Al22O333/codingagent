"""Opt-in real-provider smoke tests for the OpenAI-compatible client."""

from __future__ import annotations

import os

import pytest

from coding_agent.openai_client import (
    OpenAICompatibleConfig,
    OpenAICompatibleModelClient,
)
from coding_agent.protocol import ModelRequest, ToolKind, ToolSpec, UserMessage


_API_KEY = os.getenv("CODING_AGENT_TEST_API_KEY")
_MODEL = os.getenv("CODING_AGENT_TEST_MODEL")
_BASE_URL = os.getenv("CODING_AGENT_TEST_BASE_URL")
_HAS_PROVIDER = bool(_API_KEY and _MODEL)


def _real_client() -> OpenAICompatibleModelClient:
    assert _API_KEY is not None and _MODEL is not None
    return OpenAICompatibleModelClient(
        OpenAICompatibleConfig(
            model=_MODEL,
            api_key=_API_KEY,
            base_url=_BASE_URL,
        )
    )


@pytest.mark.skipif(
    not _HAS_PROVIDER,
    reason="set CODING_AGENT_TEST_API_KEY and CODING_AGENT_TEST_MODEL",
)
def test_real_provider_no_tool_final_smoke() -> None:
    response = _real_client().complete(
        ModelRequest(messages=(UserMessage("Reply with exactly: ready"),))
    )
    assert response.text is not None and response.text.strip()
    assert response.tool_calls == ()


@pytest.mark.skipif(
    not _HAS_PROVIDER,
    reason="set CODING_AGENT_TEST_API_KEY and CODING_AGENT_TEST_MODEL",
)
def test_real_provider_read_file_tool_call_smoke() -> None:
    read_file = ToolSpec(
        name="read_file",
        description="Read a UTF-8 text file from the local workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        kind=ToolKind.LOCAL,
        capabilities=frozenset(),
    )
    response = _real_client().complete(
        ModelRequest(
            messages=(
                UserMessage(
                    "Use the read_file tool to inspect main.py. Do not answer "
                    "without calling the tool."
                ),
            ),
            tools=(read_file,),
        )
    )
    assert response.tool_calls
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].raw_arguments is not None
