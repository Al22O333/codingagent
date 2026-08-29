"""Terminal-safe persistent Session continuity tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from coding_agent.context import CompletedRunContinuity
from coding_agent.session_store import (
    MAX_CONTINUITY_FIELD_CHARS,
    SessionStore,
    SessionStoreError,
)


def _record(task: str = "First task", final: str = "First final") -> CompletedRunContinuity:
    return CompletedRunContinuity(task, final)


def test_completed_continuity_survives_a_new_store_instance(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session_id = str(uuid4())

    SessionStore(root).save(
        session_id=session_id,
        workspace=workspace,
        continuity=(_record(),),
    )
    checkpoint = SessionStore(root).load(session_id, workspace)

    assert checkpoint.session_id == session_id
    assert checkpoint.continuity == (_record(),)
    assert checkpoint.updated_at.endswith("Z")


def test_store_retains_only_latest_bounded_pair_and_redacts_runtime_secret(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session_id = str(uuid4())
    secret = "provider-secret-value"
    huge = "head" + "x" * (MAX_CONTINUITY_FIELD_CHARS * 2) + "tail"

    SessionStore(root).save(
        session_id=session_id,
        workspace=workspace,
        continuity=(
            _record("old", "old final"),
            _record(f"new {secret}", huge + secret),
        ),
        runtime_secret_values=(secret,),
    )
    raw = (root / f"{session_id}.json").read_text(encoding="utf-8")
    checkpoint = SessionStore(root).load(session_id, workspace)

    assert len(checkpoint.continuity) == 1
    assert checkpoint.continuity[0].task == "new <redacted>"
    assert len(checkpoint.continuity[0].final_response) <= MAX_CONTINUITY_FIELD_CHARS
    assert "terminal-safe continuity truncated" in checkpoint.continuity[0].final_response
    assert secret not in raw
    assert "old final" not in raw


@pytest.mark.parametrize(
    "session_id",
    [
        "../escape",
        "not-a-uuid",
        "{00000000-0000-0000-0000-000000000000}",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_invalid_session_id_is_rejected_without_path_access(
    tmp_path: Path,
    session_id: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SessionStoreError) as raised:
        SessionStore(tmp_path / "sessions").load(session_id, workspace)

    assert raised.value.code == "SESSION_INVALID_ID"


def test_missing_corrupt_unknown_version_and_wrong_workspace_are_distinct(
    tmp_path: Path,
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    root = tmp_path / "sessions"
    root.mkdir()
    store = SessionStore(root)

    missing_id = str(uuid4())
    with pytest.raises(SessionStoreError) as missing:
        store.load(missing_id, first_workspace)
    assert missing.value.code == "SESSION_NOT_FOUND"

    corrupt_id = str(uuid4())
    (root / f"{corrupt_id}.json").write_bytes(b"not-json\xff")
    with pytest.raises(SessionStoreError) as corrupt:
        store.load(corrupt_id, first_workspace)
    assert corrupt.value.code == "SESSION_CORRUPT"

    version_id = str(uuid4())
    (root / f"{version_id}.json").write_text(
        json.dumps({"schema_version": 999}), encoding="utf-8"
    )
    with pytest.raises(SessionStoreError) as version:
        store.load(version_id, first_workspace)
    assert version.value.code == "SESSION_VERSION_UNSUPPORTED"

    valid_id = str(uuid4())
    store.save(
        session_id=valid_id,
        workspace=first_workspace,
        continuity=(_record(),),
    )
    with pytest.raises(SessionStoreError) as mismatch:
        store.load(valid_id, second_workspace)
    assert mismatch.value.code == "SESSION_WORKSPACE_MISMATCH"


def test_atomic_replace_failure_preserves_previous_checkpoint_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session_id = str(uuid4())
    store = SessionStore(root)
    store.save(
        session_id=session_id,
        workspace=workspace,
        continuity=(_record("original", "original final"),),
    )
    original = (root / f"{session_id}.json").read_bytes()

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("coding_agent.session_store.os.replace", fail_replace)
    with pytest.raises(SessionStoreError) as raised:
        store.save(
            session_id=session_id,
            workspace=workspace,
            continuity=(_record("new", "new final"),),
        )

    assert raised.value.code == "SESSION_WRITE_FAILED"
    assert (root / f"{session_id}.json").read_bytes() == original
    assert list(root.glob("*.tmp")) == []
    assert list(root.glob(".*.tmp")) == []


def test_session_file_symlink_is_never_followed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    root.mkdir()
    session_id = str(uuid4())
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    link = root / f"{session_id}.json"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks are unavailable: {error}")

    with pytest.raises(SessionStoreError) as raised:
        SessionStore(root).save(
            session_id=session_id,
            workspace=workspace,
            continuity=(_record(),),
        )

    assert raised.value.code == "SESSION_INVALID_FILE"
    assert outside.read_text(encoding="utf-8") == "sentinel"
