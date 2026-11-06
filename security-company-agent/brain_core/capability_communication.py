"""Durable, read-only communication of validated capability updates."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from brain_core.agent_awareness import CapabilityReportError, capability_change, read_capability_report
from control_plane.capability_changes import PublicationState
from control_plane.capability_manifest import ManifestError, validate_manifest
from control_plane.runtime_capabilities import RuntimeState

_SCHEMA_VERSION = 1
_REVISION = re.compile(r"[0-9a-f]{40,64}\Z")


class CapabilityEvidenceError(ValueError):
    """Capability evidence is invalid, untrusted, or inconsistent."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityEvidenceError(f"{label} must be an object")
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise CapabilityEvidenceError(f"{label} is not safe JSON") from None


def _revision(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise CapabilityEvidenceError(f"{label} must be a full Git revision")


def validate_manifest_evidence(value: Any) -> dict[str, Any]:
    try:
        manifest = validate_manifest(value)
    except (ManifestError, TypeError, ValueError) as error:
        raise CapabilityEvidenceError("invalid capability manifest: " + str(error)) from None
    if manifest["execution_authorized"] or manifest["runtime_available"]:
        raise CapabilityEvidenceError("manifest cannot grant authorization or assert runtime availability")
    return manifest


def validate_change_evidence(value: Any) -> dict[str, Any]:
    raw = _mapping(value, "change evidence")
    required = {"from_revision", "to_revision", "publication_state", "changes", "uncertainties", "runtime_availability_claimed"}
    if set(raw) != required:
        raise CapabilityEvidenceError("change evidence has missing or unexpected fields")
    _revision(raw["from_revision"], "source revision")
    _revision(raw["to_revision"], "target revision")
    if (raw["from_revision"] == raw["to_revision"] or raw["runtime_availability_claimed"] is not False
            or not isinstance(raw["runtime_availability_claimed"], bool)):
        raise CapabilityEvidenceError("change evidence must compare revisions and cannot claim runtime availability")
    valid_states = {state.value for state in PublicationState}
    states = raw["publication_state"]
    if not isinstance(states, list) or any(not isinstance(item, str) or item not in valid_states for item in states):
        raise CapabilityEvidenceError("invalid publication state")
    if any(item in {"RUNTIME_AVAILABLE", "EXECUTION_AUTHORIZED"} for item in states):
        raise CapabilityEvidenceError("publication cannot imply runtime availability or authorization")
    if (len(set(states)) != len(states) or ("CODE_PUSHED" in states and "CODE_COMMITTED" not in states)
            or ("CAPABILITY_DISCOVERED" in states and "CODE_COMMITTED" not in states)):
        raise CapabilityEvidenceError("inconsistent publication state")
    if not isinstance(raw["changes"], list) or not isinstance(raw["uncertainties"], list):
        raise CapabilityEvidenceError("invalid change evidence lists")
    if len(raw["changes"]) > 4096 or len(raw["uncertainties"]) > 4096:
        raise CapabilityEvidenceError("change evidence exceeds bounded record limits")
    if (any(not isinstance(item, str) for item in raw["uncertainties"])
            or len(set(raw["uncertainties"])) != len(raw["uncertainties"])
            or any(not item.strip() or len(item) > 2000 or any(ord(char) < 32 for char in item)
                   for item in raw["uncertainties"])):
        raise CapabilityEvidenceError("invalid uncertainty")
    fields = {"change_id", "agent_id", "category", "item", "before", "after", "from_revision", "to_revision", "evidence", "uncertainty", "publication_state", "runtime_available"}
    for change in raw["changes"]:
        if (not isinstance(change, Mapping) or set(change) != fields
                or not isinstance(change["runtime_available"], bool) or change["runtime_available"] is not False):
            raise CapabilityEvidenceError("invalid change record")
        _revision(change["from_revision"], "change source revision")
        _revision(change["to_revision"], "change target revision")
        if change["from_revision"] != raw["from_revision"] or change["to_revision"] != raw["to_revision"]:
            raise CapabilityEvidenceError("change record revisions do not match summary")
        for key in ("change_id", "agent_id", "category", "item"):
            if not isinstance(change[key], str) or not change[key].strip() or len(change[key]) > 256:
                raise CapabilityEvidenceError("invalid change record text")
        for key in ("evidence", "uncertainty"):
            if (not isinstance(change[key], list) or len(change[key]) > 256
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 2000
                           or any(ord(char) < 32 for char in item) for item in change[key])):
                raise CapabilityEvidenceError("invalid change record evidence")
        if not isinstance(change["publication_state"], list) or any(
                not isinstance(state, str) or state not in valid_states for state in change["publication_state"]):
            raise CapabilityEvidenceError("invalid change record publication state")
        if (any(state in {"RUNTIME_AVAILABLE", "EXECUTION_AUTHORIZED"} for state in change["publication_state"])
                or ("CODE_PUSHED" in change["publication_state"] and "CODE_COMMITTED" not in change["publication_state"])
                or ("CAPABILITY_DISCOVERED" in change["publication_state"]
                    and "CODE_COMMITTED" not in change["publication_state"])):
            raise CapabilityEvidenceError("change record cannot claim an inconsistent publication state")
        if len(set(change["publication_state"])) != len(change["publication_state"]):
            raise CapabilityEvidenceError("duplicate change record publication state")
        if any(state not in states for state in change["publication_state"]):
            raise CapabilityEvidenceError("change record contains unsupported summary publication state")
        if (any(not isinstance(item, str) for item in change["uncertainty"])
                or len(set(change["uncertainty"])) != len(change["uncertainty"])):
            raise CapabilityEvidenceError("duplicate or invalid change record uncertainty")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 2000
               or any(ord(char) < 32 for char in item) for item in change["uncertainty"]):
            raise CapabilityEvidenceError("invalid change record uncertainty")
        try:
            if (not isinstance(change["category"], str) or not change["category"].strip()
                    or len(change["category"]) > 128
                    or any(ord(char) < 32 for char in change["category"])
                    or not isinstance(change["item"], str) or not change["item"].strip()
                    or len(change["item"]) > 256
                    or any(ord(char) < 32 for char in change["item"])
                    or not isinstance(change["change_id"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", change["change_id"]) is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", change["agent_id"]) is None
                    or not isinstance(change["before"], (str, int, float, bool, list, dict, type(None)))
                    or not isinstance(change["after"], (str, int, float, bool, list, dict, type(None)))
                    or any(isinstance(item, float) and not math.isfinite(item)
                           for item in (change["before"], change["after"]))):
                raise ValueError("invalid change record value")
            encoded_values = json.dumps([change["before"], change["after"]], sort_keys=True, allow_nan=False)
            if len(encoded_values) > 1_048_576:
                raise ValueError("change record values exceed limit")
            if (not change["evidence"] or sum(len(item) for item in (*change["evidence"], *change["uncertainty"])) > 131_072
                    or any(len(item) > 2000 for item in (*change["evidence"], *change["uncertainty"]))):
                raise ValueError("change record evidence exceeds limit")
            expected = hashlib.sha256(json.dumps(
                [change[key] for key in ("agent_id", "category", "item", "before", "after", "from_revision", "to_revision")],
                sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, RecursionError):
            raise CapabilityEvidenceError("change record values are not safe JSON") from None
        if change["change_id"] != expected:
            raise CapabilityEvidenceError("change identifier does not match its content")
    if len({item["change_id"] for item in raw["changes"]}) != len(raw["changes"]):
        raise CapabilityEvidenceError("duplicate change identifiers")
    return raw


def validate_runtime_evidence(value: Any) -> dict[str, Any]:
    raw = _mapping(value, "runtime evidence")
    if set(raw) != {"assessments", "summary", "execution_authorized", "runtime_available_claimed"}:
        raise CapabilityEvidenceError("invalid runtime report fields")
    if (raw["execution_authorized"] is not False or not isinstance(raw["execution_authorized"], bool)
            or not isinstance(raw["assessments"], list) or len(raw["assessments"]) > 4096
            or not isinstance(raw["summary"], str) or len(raw["summary"]) > 4000
            or any(ord(char) < 32 and char not in "\n\t" for char in raw["summary"])):
        raise CapabilityEvidenceError("invalid runtime report or authorization claim")
    fields = {"agent_id", "capability", "state", "published", "installed", "loaded", "verified", "execution_eligible", "required_permissions", "runtime_permissions", "reasons", "verification_required", "published_revision", "installed_revision", "software_revision", "execution_authorized"}
    expected_claim = False
    for item in raw["assessments"]:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise CapabilityEvidenceError("invalid runtime assessment")
        if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 128
               or any(ord(char) < 32 for char in item[key])
               for key in ("agent_id", "capability")):
            raise CapabilityEvidenceError("invalid runtime identity or capability")
        if item["state"] not in {state.value for state in RuntimeState} or item["execution_authorized"] is not False:
            raise CapabilityEvidenceError("invalid runtime state, identity, or authorization claim")
        if not isinstance(item["execution_authorized"], bool):
            raise CapabilityEvidenceError("runtime authorization field must be boolean")
        for key in ("published", "installed", "loaded", "verified", "execution_eligible"):
            if type(item[key]) is not bool:
                raise CapabilityEvidenceError("runtime flags must be booleans")
        available = item["state"] == RuntimeState.VERIFIED_AVAILABLE.value
        if available and item["runtime_permissions"] != item["required_permissions"]:
            raise CapabilityEvidenceError("verified runtime permissions conflict with the capability contract")
        if (item["verified"] or item["execution_eligible"]) and not available:
            raise CapabilityEvidenceError("runtime flags conflict with state")
        expected_claim |= available
        for key in ("required_permissions", "runtime_permissions", "reasons", "verification_required"):
            if (not isinstance(item[key], list) or len(item[key]) > 256
                    or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 2000
                           or any(ord(char) < 32 for char in entry) for entry in item[key])):
                raise CapabilityEvidenceError("invalid runtime assessment details")
        for key in ("published_revision", "installed_revision", "software_revision"):
            if item[key] is not None:
                _revision(item[key], key)
        if item["published"] != (item["published_revision"] is not None):
            raise CapabilityEvidenceError("published flag conflicts with publication revision")
        if not item["published"] and item["state"] != RuntimeState.UNKNOWN.value:
            raise CapabilityEvidenceError("unpublished capability cannot have a deployable runtime state")
        if available and not (item["published"] and item["installed"] and item["loaded"]):
            raise CapabilityEvidenceError("verified runtime is missing publication, installation, or loading evidence")
        if item["installed"] != (item["installed_revision"] is not None):
            raise CapabilityEvidenceError("installed flag conflicts with installation revision")
        if item["loaded"] and not item["installed"]:
            raise CapabilityEvidenceError("loaded runtime is not installed")
        if not item["installed"] and item["state"] not in {
                RuntimeState.UNKNOWN.value, RuntimeState.NOT_DEPLOYED.value}:
            raise CapabilityEvidenceError("uninstalled runtime has an inconsistent state")
        if item["verified"] != available or item["execution_eligible"] != available:
            raise CapabilityEvidenceError("runtime verification flags conflict with state")
        if item["loaded"] != (item["state"] in {RuntimeState.LOADED.value, RuntimeState.VERIFIED_AVAILABLE.value}):
            raise CapabilityEvidenceError("loaded flag conflicts with state")
        if item["loaded"] and not item["installed"]:
            raise CapabilityEvidenceError("loaded runtime is not installed")
    if type(raw["runtime_available_claimed"]) is not bool or raw["runtime_available_claimed"] != expected_claim:
        raise CapabilityEvidenceError("runtime availability summary conflicts with assessments")
    summary = raw["summary"].casefold()
    if any(term in summary for term in ("permission granted", "authorized to execute", "execution authorized")):
        raise CapabilityEvidenceError("runtime summary cannot assert execution authorization")
    return raw


def _validated_report(value: Any) -> dict[str, Any]:
    """Validate only the SB-34 schema; persist its original validated envelope."""
    if not isinstance(value, Mapping):
        raise CapabilityReportError("Unsupported capability report.")
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise CapabilityReportError("Capability report is not safe JSON.") from None
    if len(encoded) > 1_048_576:
        raise CapabilityReportError("Capability report exceeds bounded document size.")
    if set(value) - {"schema_version", "agents", "unsupported_capabilities", "execution_verified", "permissions_granted", "execution_note", "capability_states"}:
        raise CapabilityReportError("Unexpected capability report fields.")
    report = read_capability_report(value)
    try:
        accepted = json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise CapabilityReportError("Capability report is not safe JSON.") from None
    report["_accepted_report"] = accepted
    return report


def _comparison_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """Use the raw validated schema when available, otherwise its normalized SB-34 view."""
    return dict(value.get("_accepted_report", value))


def _comparison_view(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate SB-34 inputs and retain their normalized form for legacy callers."""
    if not isinstance(value, Mapping):
        raise CapabilityReportError("Unsupported capability report.")
    try:
        report = _validated_report(value)
    except CapabilityReportError:
        if "schema_version" in value or "execution_verified" in value:
            raise
        normalized = read_capability_report(_normalize_legacy_report(dict(value)))
        normalized["_accepted_report"] = dict(value)
        return normalized
    return report


def _normalize_legacy_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert normalized test/API views to the canonical SB-34 envelope."""
    agents = []
    for source in raw.get("agents", []):
        if not isinstance(source, Mapping):
            raise CapabilityReportError("Invalid agent record.")
        caps = []
        for cap in source.get("capabilities", []):
            if not isinstance(cap, Mapping):
                raise CapabilityReportError("Invalid capability record.")
            caps.append({
                "capability": cap.get("name"), "status": cap.get("status", "unknown"),
                "evidence": list(cap.get("evidence", [])), "missing_requirements": list(cap.get("missing", [])),
                "execution_readiness": {"ready": False, "reason": cap.get("readiness_reason") or "No verified execution integration."},
                "required_permissions": list(cap.get("required_permissions", [])),
                "last_verified_at": cap.get("last_verified_at"), "uncertainty_reason": cap.get("uncertainty"),
                "description": cap.get("description", cap.get("name", "Declared capability")),
            })
        agents.append({
            "identity": source.get("identity"), "version": source.get("version", "unknown"),
            "verification_status": source.get("status", "unknown"), "evidence": list(source.get("evidence", [])),
            "missing_requirements": list(source.get("missing", [])),
            "execution_readiness": {"ready": False, "reason": source.get("readiness_reason") or "No verified execution integration."},
            "last_verified_at": source.get("last_verified_at"), "uncertainty_reason": source.get("uncertainty"),
            "capabilities": caps, "purpose": source.get("purpose", "Specialist capability metadata"),
        })
    states = []
    for state in raw.get("capability_states", []):
        if not isinstance(state, Mapping):
            raise CapabilityReportError("Invalid capability state record.")
        states.append({"agent": state.get("agent"), "capability": state.get("capability"),
                       "state": state.get("state"), "description": state.get("description", "Declared capability"),
                       "readiness": False, "reason": state.get("reason")})
    return {"schema_version": 1, "agents": agents, "unsupported_capabilities": [],
            "execution_verified": False, "permissions_granted": False, "capability_states": states}


def _natural_change(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> str | None:
    if previous is None:
        return None
    return capability_change(_normalize_legacy_report(_comparison_view(previous)),
                             _normalize_legacy_report(_comparison_view(current)))


def meaningful_change(previous: Mapping[str, Any] | None, current: Mapping[str, Any], *,
                      change_evidence: Mapping[str, Any] | None = None,
                      runtime_evidence: Mapping[str, Any] | None = None) -> str | None:
    if not isinstance(current, Mapping) or (previous is not None and not isinstance(previous, Mapping)):
        raise CapabilityReportError("Unsupported capability report.")
    now = current if "_accepted_report" in current else _comparison_view(current)
    old = (previous if previous is None or "_accepted_report" in previous
           else _comparison_view(previous))
    summary = validate_change_evidence(change_evidence) if change_evidence is not None else None
    raw_runtime = runtime_evidence if runtime_evidence is not None else current.get("_runtime_evidence")
    runtime = validate_runtime_evidence(raw_runtime) if raw_runtime is not None else None
    prior_runtime_raw = old.get("_runtime_evidence") if old else None
    prior_runtime = validate_runtime_evidence(prior_runtime_raw) if prior_runtime_raw is not None else None
    message = _natural_change(old, now)
    if summary and summary["changes"]:
        message = (message + " " if message else "") + "I detected a change in published capability information. Publication does not establish installation or execution readiness."
        if "CODE_PUSHED" in summary["publication_state"]:
            message += " The matching code revision is published, but that does not mean it is installed or available to command."
    current_states = {(item["agent_id"], item["capability"]): item["state"] for item in runtime["assessments"]} if runtime else {}
    previous_states = {(item["agent_id"], item["capability"]): item["state"] for item in prior_runtime["assessments"]} if prior_runtime else {}
    if runtime is not None and current_states != previous_states:
        available = sum(state == RuntimeState.VERIFIED_AVAILABLE.value for state in current_states.values())
        message = (message + " " if message else "") + f"Local checks show {available} capabilities verified available; this does not grant permission to execute."
    if message and "verified available" not in message:
        message += " I understand the declared capabilities, but cannot command an agent without independently verified execution integration."
    return message or None


def observe_capability_update(report: str | Path | Mapping[str, Any], *, data_dir: str | Path,
                              store: "CapabilityUpdateStore | None" = None) -> dict[str, Any] | None:
    """Validate and persist one chat-triggered report observation without polling."""
    accepted_report = read_capability_report(report)
    updates = store or CapabilityUpdateStore(store_path(data_dir))
    return updates.observe_latest(accepted_report)


class CapabilityUpdateStore:
    """Versioned SQLite store for the latest accepted report and durable notices."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("CREATE TABLE IF NOT EXISTS capability_updates ("
                               "change_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
                               "status TEXT NOT NULL CHECK(status IN ('observed','communicated','acknowledged')), "
                               "message TEXT NOT NULL, evidence_json TEXT NOT NULL, "
                               "observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, communicated_at TEXT, acknowledged_at TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS capability_baseline ("
                               "singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL, "
                               "report_json TEXT NOT NULL, runtime_json TEXT)")
            connection.execute("PRAGMA user_version=1")
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _key(report: Mapping[str, Any], change: Mapping[str, Any] | None,
             runtime: Mapping[str, Any] | None) -> str:
        payload = json.dumps({"report": report, "change": change, "runtime": runtime},
                             sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_baseline(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute("SELECT schema_version, report_json, runtime_json FROM capability_baseline WHERE singleton=1").fetchone()
        if row is None:
            return None
        try:
            if row["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("unsupported baseline schema")
            report = _comparison_view(json.loads(row["report_json"]))
            if row["runtime_json"] is not None:
                report["_runtime_evidence"] = validate_runtime_evidence(json.loads(row["runtime_json"]))
            return report
        except (CapabilityReportError, CapabilityEvidenceError, ValueError, TypeError, json.JSONDecodeError, RecursionError) as error:
            raise CapabilityEvidenceError("persisted capability baseline is invalid; refusing to infer a change") from error

    def baseline(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = self._read_baseline(connection)
            if row is None:
                return None
            public = {key: value for key, value in row.items() if key not in {"_accepted_report", "_runtime_evidence"}}
            return public

    def observe_latest(self, current: Mapping[str, Any], *,
                       change_evidence: Mapping[str, Any] | None = None,
                       runtime_evidence: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        if not isinstance(current, Mapping):
            raise CapabilityReportError("Unsupported capability report.")
        report = _comparison_view(current)
        if "_runtime_evidence" in current:
            runtime_evidence = current["_runtime_evidence"]
        elif runtime_evidence is None and "_runtime_evidence" in report:
            runtime_evidence = report["_runtime_evidence"]
        change = validate_change_evidence(change_evidence) if change_evidence is not None else None
        runtime = validate_runtime_evidence(runtime_evidence) if runtime_evidence is not None else None
        current_snapshot = dict(report)
        if runtime is not None:
            current_snapshot["_runtime_evidence"] = runtime
        elif "_runtime_evidence" in report:
            current_snapshot["_runtime_evidence"] = report["_runtime_evidence"]
        with self._connection() as connection:
            previous = self._read_baseline(connection)
            if runtime is None and previous is not None and "_runtime_evidence" in previous:
                current_snapshot["_runtime_evidence"] = previous["_runtime_evidence"]
            message = meaningful_change(previous, current_snapshot, change_evidence=change, runtime_evidence=runtime) if previous is not None else None
            notice = None
            if message:
                key = self._key(report["_accepted_report"], change, runtime)
                evidence = json.dumps({"schema_version": _SCHEMA_VERSION, "report": report["_accepted_report"],
                                       "change": change, "runtime": runtime}, sort_keys=True, ensure_ascii=False)
                connection.execute("INSERT OR IGNORE INTO capability_updates "
                                   "(change_key,schema_version,status,message,evidence_json) VALUES (?,?,'observed',?,?)",
                                   (key, _SCHEMA_VERSION, message, evidence))
                row = connection.execute("SELECT change_key,status,message FROM capability_updates WHERE change_key=?", (key,)).fetchone()
                notice = {"change_key": row["change_key"], "status": row["status"], "message": row["message"]}
            connection.execute("INSERT INTO capability_baseline(singleton,schema_version,report_json,runtime_json) "
                               "VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version, "
                               "report_json=excluded.report_json,runtime_json=excluded.runtime_json",
                               (_SCHEMA_VERSION, json.dumps(report["_accepted_report"], sort_keys=True),
                                json.dumps(current_snapshot["_runtime_evidence"], sort_keys=True) if "_runtime_evidence" in current_snapshot else None))
        return notice

    def observe(self, previous: Mapping[str, Any] | None, current: Mapping[str, Any], *,
                change_evidence: Mapping[str, Any] | None = None,
                runtime_evidence: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        if not isinstance(current, Mapping) or (previous is not None and not isinstance(previous, Mapping)):
            raise CapabilityReportError("Unsupported capability report.")
        report = _comparison_view(current)
        if previous is not None and "_accepted_report" not in previous:
            previous = _comparison_view(previous)
        change = validate_change_evidence(change_evidence) if change_evidence is not None else None
        runtime = validate_runtime_evidence(runtime_evidence) if runtime_evidence is not None else None
        message = meaningful_change(previous, report, change_evidence=change, runtime_evidence=runtime)
        if not message:
            return None
        key = self._key(report["_accepted_report"], change, runtime)
        evidence = json.dumps({"schema_version": _SCHEMA_VERSION, "report": report["_accepted_report"],
                               "change": change, "runtime": runtime}, sort_keys=True, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO capability_updates "
                               "(change_key,schema_version,status,message,evidence_json) VALUES (?,?,'observed',?,?)",
                               (key, _SCHEMA_VERSION, message, evidence))
            row = connection.execute("SELECT change_key,status,message,evidence_json FROM capability_updates WHERE change_key=?", (key,)).fetchone()
        return {"change_key": row["change_key"], "status": row["status"], "message": row["message"],
                "evidence_json": row["evidence_json"]}

    def latest(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT change_key,status,message,evidence_json FROM capability_updates ORDER BY observed_at DESC,change_key DESC LIMIT 1").fetchone()
        return None if row is None else {"change_key": row["change_key"], "status": row["status"], "message": row["message"],
                                         "evidence_json": row["evidence_json"]}

    def pending_all(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT change_key,status,message,evidence_json FROM capability_updates WHERE status='observed' ORDER BY observed_at ASC,change_key ASC").fetchall()
        return [{"change_key": row["change_key"], "status": row["status"], "message": row["message"],
                 "evidence_json": row["evidence_json"]} for row in rows]

    def pending(self) -> dict[str, Any] | None:
        updates = self.pending_all()
        return updates[0] if updates else None

    def communicated(self, change_key: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("UPDATE capability_updates SET status='communicated',communicated_at=CURRENT_TIMESTAMP WHERE change_key=? AND status='observed'", (change_key,))
            return cursor.rowcount == 1

    def acknowledge(self, change_key: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("UPDATE capability_updates SET status='acknowledged',acknowledged_at=CURRENT_TIMESTAMP WHERE change_key=? AND status IN ('observed','communicated')", (change_key,))
            return cursor.rowcount == 1


def store_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "capability_updates.sqlite3"
