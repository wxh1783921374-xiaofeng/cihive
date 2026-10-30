from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from cihive.models import AssignmentKind, RunState, RunStatus, WorkerAssignment, utc_now
from cihive.redaction import truncate_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    signature TEXT,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_signature
ON runs(repo, head_sha, signature, status);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    capability_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assignments_run ON assignments(run_id, kind);
"""


class RunStore:
    def __init__(self, database_path: Path | str, data_dir: Path | str) -> None:
        self.database_path = Path(database_path)
        self.data_dir = Path(data_dir)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "traces").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(SCHEMA)

    def receive_delivery(self, delivery_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id, received_at) VALUES (?, ?)",
                (delivery_id, utc_now().isoformat()),
            )
            return cursor.rowcount == 1

    def create_run(self, state: RunState) -> None:
        payload = state.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(id, repo, head_sha, signature, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.run.id,
                    state.run.repo,
                    state.run.head_sha,
                    state.failure.signature.fingerprint if state.failure else None,
                    state.status.value,
                    payload,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                ),
            )

    def get_run(self, run_id: str) -> RunState:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return RunState.model_validate_json(row["payload"])

    def latest_run(self) -> RunState | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return RunState.model_validate_json(row["payload"]) if row else None

    def save_run(self, state: RunState) -> None:
        state.updated_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET signature = ?, status = ?, payload = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state.failure.signature.fingerprint if state.failure else None,
                    state.status.value,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                    state.run.id,
                ),
            )

    def find_matching_run(
        self,
        repo: str,
        head_sha: str,
        signature: str,
        exclude_run_id: str,
    ) -> RunState | None:
        acceptable = (
            RunStatus.WAITING.value,
            RunStatus.SUCCEEDED.value,
            RunStatus.NO_CANDIDATE.value,
            RunStatus.PROCESSING.value,
        )
        placeholders = ",".join("?" for _ in acceptable)
        sql = (
            "SELECT payload FROM runs "
            "WHERE repo = ? AND head_sha = ? AND signature = ? AND id != ? "
            f"AND status IN ({placeholders}) "
            "ORDER BY created_at ASC LIMIT 1"
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                sql,
                (repo, head_sha, signature, exclude_run_id, *acceptable),
            ).fetchone()
        return RunState.model_validate_json(row["payload"]) if row else None

    def create_assignment(self, assignment: WorkerAssignment, capability_token: str) -> None:
        capability_hash = sha256_text(capability_token)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assignments(
                    id, run_id, kind, capability_sha256, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment.id,
                    assignment.run_id,
                    assignment.kind.value,
                    capability_hash,
                    assignment.model_dump_json(),
                    assignment.created_at.isoformat(),
                    assignment.updated_at.isoformat(),
                ),
            )

    def get_assignment(self, assignment_id: str) -> tuple[WorkerAssignment, str]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload, capability_sha256 FROM assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"assignment not found: {assignment_id}")
        return WorkerAssignment.model_validate_json(row["payload"]), row["capability_sha256"]

    def save_assignment(self, assignment: WorkerAssignment) -> None:
        assignment.updated_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE assignments SET payload = ?, updated_at = ? WHERE id = ?",
                (assignment.model_dump_json(), assignment.updated_at.isoformat(), assignment.id),
            )

    def list_assignments(
        self,
        run_id: str,
        kind: AssignmentKind | None = None,
    ) -> list[WorkerAssignment]:
        sql = "SELECT payload FROM assignments WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if kind is not None:
            sql += " AND kind = ?"
            parameters.append(kind.value)
        sql += " ORDER BY created_at ASC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [WorkerAssignment.model_validate_json(row["payload"]) for row in rows]

    def append_trace(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        trace_path = self.data_dir / "traces" / f"{run_id}.jsonl"
        record = {
            "at": utc_now().isoformat(),
            "event": event,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock, trace_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def expire_stale_runs(self, default_timeout_seconds: int) -> list[str]:
        now = utc_now()
        expired_ids: list[str] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM runs WHERE status IN (?, ?)",
                (RunStatus.PROCESSING.value, RunStatus.WAITING.value),
            ).fetchall()
            for row in rows:
                state = RunState.model_validate_json(row["payload"])
                timeout = (
                    state.repository_config.run_timeout_seconds
                    if state.repository_config
                    else default_timeout_seconds
                )
                if state.updated_at >= now - timedelta(seconds=timeout):
                    continue
                state.status = RunStatus.FAILED
                state.error = f"run exceeded its {timeout}-second timeout"
                state.updated_at = now
                connection.execute(
                    "UPDATE runs SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
                    (
                        state.status.value,
                        state.model_dump_json(),
                        now.isoformat(),
                        state.run.id,
                    ),
                )
                expired_ids.append(state.run.id)
        for run_id in expired_ids:
            self.append_trace(run_id, "timed_out", {"reason": "worker deadline exceeded"})
        return expired_ids

    def write_artifact(self, run_id: str, name: str, content: str) -> Path:
        safe_name = "".join(char for char in name if char.isalnum() or char in "._-")
        if not safe_name:
            safe_name = "artifact.txt"
        directory = self.data_dir / "artifacts" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / safe_name
        path.write_text(truncate_text(content, 2_000_000), encoding="utf-8")
        return path


def sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
