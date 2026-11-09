"""SQLite-backed single-use authorization and at-most-once admission ledger.

A durable claim is recorded before adapters are invoked. Any claim left between
admission and a durable terminal result after restart is reconciliation-required;
it is never replayed automatically.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.execution_engine import ExecutionAuthorization, ExecutionRequest


class DurableExecutionError(ValueError):
    """Durable execution admission or reconciliation failed closed."""


class SQLiteExecutionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._owner_token = uuid.uuid4().hex
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS authorizations (
                authorization_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                mission_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1))
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS execution_claims (
                task_id TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL UNIQUE REFERENCES authorizations(authorization_id),
                mission_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                capability TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result_json TEXT,
                error_category TEXT,
                error_message TEXT,
                reconciliation TEXT,
                worker_token TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS execution_claims_state_idx ON execution_claims(state)")
        finally:
            db.close()

    def record_authorization(self, authorization: ExecutionAuthorization) -> None:
        if not isinstance(authorization, ExecutionAuthorization):
            raise DurableExecutionError("validated execution authorization required")
        db = self._connect()
        try:
            try:
                db.execute("""INSERT INTO authorizations (
                    authorization_id, task_id, mission_id, agent_id, capability,
                    permissions_json, input_digest, approved_by, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    authorization.authorization_id, authorization.task_id,
                    authorization.mission_id, authorization.agent_id, authorization.capability,
                    _json(authorization.permissions), authorization.input_digest,
                    authorization.approved_by, authorization.issued_at.isoformat(),
                    authorization.expires_at.isoformat(),
                ))
            except sqlite3.IntegrityError:
                row = db.execute("SELECT * FROM authorizations WHERE authorization_id=?",
                                 (authorization.authorization_id,)).fetchone()
                if row is None or not _authorization_matches(row, authorization):
                    raise DurableExecutionError("authorization identity collision") from None
        finally:
            db.close()

    def claim(self, request: ExecutionRequest, authorization: ExecutionAuthorization | None) -> None:
        if not isinstance(request, ExecutionRequest) or not isinstance(authorization, ExecutionAuthorization):
            raise DurableExecutionError("durable claim requires a validated request and authorization")
        if not _authorization_binds(authorization, request):
            raise DurableExecutionError("authorization does not bind to the execution request")
        if datetime.now(timezone.utc) < authorization.issued_at or datetime.now(timezone.utc) >= authorization.expires_at:
            raise DurableExecutionError("authorization is expired or not yet valid")
        now = datetime.now(timezone.utc).isoformat()
        request_digest = _request_digest(request)
        db = self._connect()
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
                auth = db.execute("SELECT * FROM authorizations WHERE authorization_id=?",
                                  (authorization.authorization_id,)).fetchone()
                if auth is None or not _authorization_matches(auth, authorization):
                    raise DurableExecutionError("authorization is not durably registered")
                if auth["consumed"]:
                    raise DurableExecutionError("authorization has already been durably consumed")
                existing = db.execute("SELECT 1 FROM execution_claims WHERE task_id=?", (request.task_id,)).fetchone()
                if existing is not None:
                    raise DurableExecutionError("task already has a durable execution claim")
                if datetime.fromisoformat(auth["expires_at"]) <= datetime.now(timezone.utc):
                    raise DurableExecutionError("authorization is expired")
                db.execute("UPDATE authorizations SET consumed=1 WHERE authorization_id=? AND consumed=0",
                           (authorization.authorization_id,))
                if db.execute("SELECT changes()").fetchone()[0] != 1:
                    raise DurableExecutionError("authorization consumption was not unique")
                db.execute("""INSERT INTO execution_claims (
                    task_id, authorization_id, mission_id, agent_id, capability,
                    request_digest, state, created_at, updated_at, worker_token
                ) VALUES (?, ?, ?, ?, ?, ?, 'CLAIMED', ?, ?, ?)""", (
                    request.task_id, authorization.authorization_id, request.mission_id,
                    request.agent_id, request.capability, request_digest, now, now, self._owner_token,
                ))
                db.execute("COMMIT")
            except Exception:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            db.close()

    def consume_authorization(self, authorization: ExecutionAuthorization,
                              request: ExecutionRequest, now: datetime) -> None:
        if not isinstance(authorization, ExecutionAuthorization) or not isinstance(request, ExecutionRequest):
            raise DurableExecutionError("validated authorization and request required")
        if not _authorization_binds(authorization, request):
            raise DurableExecutionError("authorization identity or task binding mismatch")
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM authorizations WHERE authorization_id=?",
                             (authorization.authorization_id,)).fetchone()
            if row is None or not _authorization_matches(row, authorization):
                raise DurableExecutionError("authorization is not durably registered")
            if row["consumed"] != 1:
                raise DurableExecutionError("authorization was not consumed with an atomic claim")
            claim = db.execute("SELECT authorization_id FROM execution_claims WHERE task_id=?",
                               (request.task_id,)).fetchone()
            if claim is None or claim["authorization_id"] != authorization.authorization_id:
                raise DurableExecutionError("execution claim does not match authorization")
            if now.isoformat() >= row["expires_at"]:
                raise DurableExecutionError("authorization expired before execution")
        finally:
            db.close()

    def transition(self, task_id: str, state: str, *, result: Any = None,
                   error_category: str | None = None, error_message: str | None = None) -> None:
        if not isinstance(task_id, str) or not task_id or not isinstance(state, str):
            raise DurableExecutionError("task and state are required")
        allowed = {"AUTHORIZED", "INVOKING", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT",
                   "RECONCILIATION_REQUIRED"}
        if state not in allowed:
            raise DurableExecutionError("unsupported durable execution state")
        result_json = _json(result) if result is not None else None
        if state == "COMPLETED" and result is None:
            raise DurableExecutionError("completed execution requires a persisted result")
        if state in {"FAILED", "CANCELLED", "TIMED_OUT", "RECONCILIATION_REQUIRED"} and result is not None:
            raise DurableExecutionError("non-success state cannot persist a success result")
        now = datetime.now(timezone.utc).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM execution_claims WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise DurableExecutionError("durable execution claim is missing")
            old_state = row["state"]
            transitions = {
                "CLAIMED": {"AUTHORIZED", "FAILED", "CANCELLED", "RECONCILIATION_REQUIRED"},
                "AUTHORIZED": {"INVOKING", "FAILED", "CANCELLED", "RECONCILIATION_REQUIRED"},
                "INVOKING": {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "RECONCILIATION_REQUIRED"},
                "RECONCILIATION_REQUIRED": {"COMPLETED", "FAILED"},
            }
            if state not in transitions.get(old_state, set()):
                db.execute("ROLLBACK")
                raise DurableExecutionError(f"invalid durable transition {old_state} -> {state}")
            db.execute("""UPDATE execution_claims SET state=?, updated_at=?, result_json=?,
                error_category=?, error_message=?, reconciliation=? WHERE task_id=?""", (
                state, now, result_json, error_category, error_message,
                "manual reconciliation required" if state == "RECONCILIATION_REQUIRED" else None,
                task_id,
            ))
            db.execute("COMMIT")
        finally:
            db.close()

    def get_claim(self, task_id: str) -> dict[str, Any] | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM execution_claims WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            db.close()

    def recover_inflight(self, *, current_owner: bool = False) -> tuple[str, ...]:
        """Mark claims from a previous store owner uncertain; never replay adapter work."""
        uncertain = ("CLAIMED", "AUTHORIZED", "INVOKING")
        changed: list[str] = []
        now = datetime.now(timezone.utc).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT task_id, worker_token FROM execution_claims WHERE state IN (?, ?, ?)", uncertain
            ).fetchall()
            for row in rows:
                if not current_owner and row["worker_token"] == self._owner_token:
                    continue
                db.execute("""UPDATE execution_claims SET state='RECONCILIATION_REQUIRED',
                    updated_at=?, result_json=NULL, reconciliation=? WHERE task_id=?
                    AND state IN ('CLAIMED', 'AUTHORIZED', 'INVOKING')""",
                           (now, "restart found an in-flight claim; execution will not be retried", row["task_id"]))
                if db.execute("SELECT changes()").fetchone()[0] == 1:
                    changed.append(row["task_id"])
            db.execute("COMMIT")
        finally:
            db.close()
        return tuple(changed)

    def reconcile(self, task_id: str, *, outcome: str, evidence: str,
                  result: Any = None, verified: bool = False) -> None:
        if outcome not in {"COMPLETED", "FAILED"} or not isinstance(evidence, str) or not evidence.strip():
            raise DurableExecutionError("manual reconciliation requires a terminal outcome and evidence")
        if verified is not True:
            raise DurableExecutionError("manual reconciliation requires an explicit verified-evidence attestation")
        if outcome == "COMPLETED" and result is None:
            raise DurableExecutionError("successful reconciliation requires verified result evidence")
        if outcome == "FAILED" and result is not None:
            raise DurableExecutionError("failed reconciliation cannot store a success result")
        digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        verified_payload = (
            {"reconciliation_evidence_digest": digest, "verified_result": result}
            if outcome == "COMPLETED" else None
        )
        now = datetime.now(timezone.utc).isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""UPDATE execution_claims SET state=?, updated_at=?,
                result_json=?, error_category=NULL, error_message=NULL, reconciliation=?
                WHERE task_id=? AND state='RECONCILIATION_REQUIRED'""", (
                outcome, now, _json(verified_payload) if verified_payload is not None else None,
                "manual reconciliation evidence sha256:" + digest, task_id,
            ))
            if cursor.rowcount != 1:
                db.execute("ROLLBACK")
                raise DurableExecutionError("claim is not awaiting reconciliation")
            db.execute("COMMIT")
        finally:
            db.close()


def _authorization_matches(row: sqlite3.Row, authorization: ExecutionAuthorization) -> bool:
    return bool(row["authorization_id"] == authorization.authorization_id
                and row["task_id"] == authorization.task_id
                and row["mission_id"] == authorization.mission_id
                and row["agent_id"] == authorization.agent_id
                and row["capability"] == authorization.capability
                and row["permissions_json"] == _json(authorization.permissions)
                and row["input_digest"] == authorization.input_digest
                and row["approved_by"] == authorization.approved_by
                and row["issued_at"] == authorization.issued_at.isoformat()
                and row["expires_at"] == authorization.expires_at.isoformat())


def _authorization_binds(authorization: ExecutionAuthorization, request: ExecutionRequest) -> bool:
    digest = hashlib.sha256(json.dumps(request.input_data, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return bool(authorization.task_id == request.task_id and authorization.mission_id == request.mission_id
                and authorization.agent_id == request.agent_id and authorization.capability == request.capability
                and authorization.input_digest == digest and set(authorization.permissions) == set(request.permissions))


def _request_digest(request: ExecutionRequest) -> str:
    payload = {"task_id": request.task_id, "mission_id": request.mission_id,
               "agent_id": request.agent_id, "capability": request.capability,
               "input": request.input_data, "permissions": request.permissions}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
