"""Lean terminal-safe persistence for completed Session continuity."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

from .context import CompletedRunContinuity


SESSION_SCHEMA_VERSION = 1
MAX_RETAINED_COMPLETED_RUNS = 1
MAX_CONTINUITY_FIELD_CHARS = 12_000
MAX_SESSION_DOCUMENT_BYTES = 96 * 1024
_TRUNCATION_MARKER = "\n[terminal-safe continuity truncated]\n"
_REDACTION = "<redacted>"


class SessionStoreError(RuntimeError):
    """Deterministic persistence failure safe for CLI normalization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionCheckpoint:
    """Validated terminal-safe Session checkpoint."""

    session_id: str
    workspace_identity: str
    updated_at: str
    continuity: tuple[CompletedRunContinuity, ...]


class SessionStore:
    """Store exact UUID-addressed JSON documents with atomic replacement."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, session_id: str, workspace: Path) -> SessionCheckpoint:
        canonical_id = canonical_session_id(session_id)
        path = self._root / f"{canonical_id}.json"
        if not path.exists():
            raise SessionStoreError("SESSION_NOT_FOUND", "session was not found")
        if path.is_symlink() or not path.is_file():
            raise SessionStoreError(
                "SESSION_INVALID_FILE", "session path is not a regular file"
            )
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise SessionStoreError(
                "SESSION_READ_FAILED", "session could not be read"
            ) from error
        if len(raw) > MAX_SESSION_DOCUMENT_BYTES:
            raise SessionStoreError(
                "SESSION_TOO_LARGE", "session document exceeds its size limit"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionStoreError(
                "SESSION_CORRUPT", "session document is not valid UTF-8 JSON"
            ) from error
        return _parse_checkpoint(
            document, canonical_id, canonical_workspace_identity(workspace)
        )

    def save(
        self,
        *,
        session_id: str,
        workspace: Path,
        continuity: tuple[CompletedRunContinuity, ...],
        runtime_secret_values: tuple[str, ...] = (),
    ) -> SessionCheckpoint:
        canonical_id = canonical_session_id(session_id)
        workspace_identity = canonical_workspace_identity(workspace)
        retained = continuity[-MAX_RETAINED_COMPLETED_RUNS:]
        sanitized = tuple(
            CompletedRunContinuity(
                _bounded_text(_redact(record.task, runtime_secret_values)),
                _bounded_text(_redact(record.final_response, runtime_secret_values)),
            )
            for record in retained
        )
        checkpoint = SessionCheckpoint(
            session_id=canonical_id,
            workspace_identity=workspace_identity,
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            continuity=sanitized,
        )
        document = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": checkpoint.session_id,
            "workspace_identity": checkpoint.workspace_identity,
            "updated_at": checkpoint.updated_at,
            "completed_runs": [
                {"task": record.task, "final_response": record.final_response}
                for record in checkpoint.continuity
            ],
        }
        encoded = (
            json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_SESSION_DOCUMENT_BYTES:
            raise SessionStoreError(
                "SESSION_TOO_LARGE", "session document exceeds its size limit"
            )

        target = self._root / f"{canonical_id}.json"
        temporary = self._root / f".{canonical_id}.{uuid4().hex}.tmp"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise SessionStoreError(
                    "SESSION_INVALID_FILE", "session path must not be a symbolic link"
                )
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, target)
        except SessionStoreError:
            _remove_temporary(temporary)
            raise
        except OSError as error:
            _remove_temporary(temporary)
            raise SessionStoreError(
                "SESSION_WRITE_FAILED",
                "session checkpoint could not be written atomically",
            ) from error
        return checkpoint


def canonical_session_id(value: str) -> str:
    """Return one canonical UUID or reject path-like and ambiguous identifiers."""

    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise SessionStoreError("SESSION_INVALID_ID", "session ID must be a UUID") from error
    canonical = str(parsed)
    if value != canonical:
        raise SessionStoreError(
            "SESSION_INVALID_ID", "session ID must use canonical UUID form"
        )
    return canonical


def canonical_workspace_identity(workspace: Path) -> str:
    """Bind a checkpoint to one existing canonical workspace directory."""

    try:
        resolved = workspace.resolve(strict=True)
    except OSError as error:
        raise SessionStoreError(
            "SESSION_WORKSPACE_INVALID", "workspace cannot be resolved"
        ) from error
    if not resolved.is_dir():
        raise SessionStoreError(
            "SESSION_WORKSPACE_INVALID", "workspace is not a directory"
        )
    return os.path.normcase(str(resolved))


def _parse_checkpoint(
    document: object,
    expected_session_id: str,
    expected_workspace: str,
) -> SessionCheckpoint:
    if not isinstance(document, dict):
        _corrupt("session document must be a JSON object")
    version = document.get("schema_version")
    if type(version) is not int or version != SESSION_SCHEMA_VERSION:
        raise SessionStoreError(
            "SESSION_VERSION_UNSUPPORTED", "session schema version is unsupported"
        )
    session_id = document.get("session_id")
    if session_id != expected_session_id:
        _corrupt("session ID does not match its file name")
    workspace_identity = document.get("workspace_identity")
    if not isinstance(workspace_identity, str):
        _corrupt("workspace identity is missing")
    if os.path.normcase(workspace_identity) != expected_workspace:
        raise SessionStoreError(
            "SESSION_WORKSPACE_MISMATCH",
            "session belongs to a different workspace",
        )
    updated_at = document.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        _corrupt("session timestamp is missing")
    raw_runs = document.get("completed_runs")
    if not isinstance(raw_runs, list) or len(raw_runs) > MAX_RETAINED_COMPLETED_RUNS:
        _corrupt("completed continuity is invalid")
    continuity: list[CompletedRunContinuity] = []
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            _corrupt("completed continuity entry is invalid")
        task = raw_run.get("task")
        final_response = raw_run.get("final_response")
        if (
            not isinstance(task, str)
            or not isinstance(final_response, str)
            or len(task) > MAX_CONTINUITY_FIELD_CHARS
            or len(final_response) > MAX_CONTINUITY_FIELD_CHARS
        ):
            _corrupt("completed continuity text is invalid")
        continuity.append(CompletedRunContinuity(task, final_response))
    return SessionCheckpoint(
        session_id=expected_session_id,
        workspace_identity=workspace_identity,
        updated_at=updated_at,
        continuity=tuple(continuity),
    )


def _bounded_text(value: str) -> str:
    if len(value) <= MAX_CONTINUITY_FIELD_CHARS:
        return value
    remaining = MAX_CONTINUITY_FIELD_CHARS - len(_TRUNCATION_MARKER)
    head = remaining // 2
    tail = remaining - head
    return value[:head] + _TRUNCATION_MARKER + value[-tail:]


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, _REDACTION)
    return redacted


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _corrupt(message: str) -> NoReturn:
    raise SessionStoreError("SESSION_CORRUPT", message)


__all__ = [
    "MAX_CONTINUITY_FIELD_CHARS",
    "MAX_SESSION_DOCUMENT_BYTES",
    "SESSION_SCHEMA_VERSION",
    "SessionCheckpoint",
    "SessionStore",
    "SessionStoreError",
    "canonical_session_id",
    "canonical_workspace_identity",
]
