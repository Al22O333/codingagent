"""Centralized v1 configuration contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.config import AgentConfig, load_agent_config


BASE_ENV = {
    "CODING_AGENT_MODEL": "environment-model",
    "CODING_AGENT_BASE_URL": "https://environment.invalid/v1",
    "CODING_AGENT_API_KEY": "super-secret-test-key",
}


def test_defaults_and_cli_environment_precedence(tmp_path: Path) -> None:
    config = load_agent_config(
        str(tmp_path),
        {
            **BASE_ENV,
            "CODING_AGENT_MAX_MODEL_TURNS": "30",
            "CODING_AGENT_MAX_CONTEXT_CHARS": "90000",
            "CODING_AGENT_DEBUG": "false",
        },
        model="cli-model",
        base_url="https://cli.invalid/v1",
        max_model_turns=31,
        debug=True,
    )

    assert config.model == "cli-model"
    assert config.base_url == "https://cli.invalid/v1"
    assert config.max_model_turns == 31
    assert config.max_tool_call_attempts == 64
    assert config.max_active_run_duration_seconds == 900
    assert config.max_context_chars == 90_000
    assert config.debug is True


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"CODING_AGENT_BASE_URL": "x", "CODING_AGENT_API_KEY": "k"}, "MODEL"),
        ({"CODING_AGENT_MODEL": "m", "CODING_AGENT_API_KEY": "k"}, "BASE_URL"),
        ({"CODING_AGENT_MODEL": "m", "CODING_AGENT_BASE_URL": "x"}, "API_KEY"),
    ],
)
def test_required_configuration_has_no_builtin_fallback(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        load_agent_config(str(tmp_path), environment)


@pytest.mark.parametrize(
    ("field_name", "lower", "upper"),
    [
        ("max_model_turns", 1, 64),
        ("max_tool_call_attempts", 1, 256),
        ("max_active_run_duration_seconds", 1, 3_600),
        ("max_context_chars", 8_000, 256_000),
    ],
)
def test_public_budget_ranges_are_finite(
    tmp_path: Path,
    field_name: str,
    lower: int,
    upper: int,
) -> None:
    base = {
        "workspace": tmp_path,
        "model": "model",
        "base_url": "https://provider.invalid/v1",
        "api_key": "key",
    }
    assert getattr(AgentConfig(**base, **{field_name: lower}), field_name) == lower
    assert getattr(AgentConfig(**base, **{field_name: upper}), field_name) == upper
    with pytest.raises(ValueError, match=field_name):
        AgentConfig(**base, **{field_name: lower - 1})
    with pytest.raises(ValueError, match=field_name):
        AgentConfig(**base, **{field_name: upper + 1})


def test_secret_is_not_in_configuration_representation(tmp_path: Path) -> None:
    config = load_agent_config(str(tmp_path), BASE_ENV)

    assert "super-secret-test-key" not in repr(config)
    assert "environment-model" in repr(config)
