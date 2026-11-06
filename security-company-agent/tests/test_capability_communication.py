import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain_core import agent_awareness, capability_communication as communication
from test_agent_awareness import valid_report
from control_plane.capability_changes import CapabilityChangeDiscovery, GitRevisionEvidence

REV_A = "a" * 40
REV_B = "b" * 40


def change_summary():
    return CapabilityChangeDiscovery().compare(
        [], [], from_git=GitRevisionEvidence(REV_A, committed=True),
        to_git=GitRevisionEvidence(REV_B, committed=True, remote_verified=True, remote_revision=REV_B),
    ).to_dict()


def test_valid_manifest_and_runtime_contracts_are_importable_and_offline():
    from control_plane.capability_manifest import validate_manifest
    from control_plane.runtime_capabilities import RuntimeCapabilityVerifier

    assert callable(validate_manifest)
    assert callable(RuntimeCapabilityVerifier().verify)


def test_change_evidence_validates_publication_without_claiming_deployment():
    evidence = change_summary()
    assert communication.validate_change_evidence(evidence)["runtime_availability_claimed"] is False
    evidence["publication_state"].append("RUNTIME_AVAILABLE")
    with pytest.raises(communication.CapabilityEvidenceError):
        communication.validate_change_evidence(evidence)


def test_invalid_untrusted_evidence_is_rejected():
    evidence = change_summary()
    evidence["to_revision"] = "push means ready"
    with pytest.raises(communication.CapabilityEvidenceError):
        communication.validate_change_evidence(evidence)
    with pytest.raises(agent_awareness.CapabilityReportError):
        communication.meaningful_change(None, {"schema_version": 1, "agents": []})


def test_published_change_does_not_claim_installed_or_ready():
    report = normalize_test_report(valid_report())
    prior = json.loads(json.dumps(report))
    changed = remove_agent(report)
    summary = change_summary()
    record = {
        "change_id": "", "agent_id": "specialist.agent", "category": "manifest_added", "item": "manifest",
        "before": None, "after": {}, "from_revision": REV_A, "to_revision": REV_B,
        "evidence": ["validated"], "uncertainty": [], "publication_state": [], "runtime_available": False,
    }
    import hashlib
    material = [record["agent_id"], record["category"], record["item"], record["before"], record["after"], REV_A, REV_B]
    record["change_id"] = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    summary["changes"].append(record)
    message = communication.meaningful_change(prior, changed, change_evidence=summary)
    assert "published" in message
    assert "installed" in message
    assert "command" in message


def test_natural_report_is_clean_and_does_not_leak_registry_metadata():
    report = normalize_test_report(valid_report())
    changed = remove_agent(report)
    message = communication.meaningful_change(report, changed)
    assert "agent" in message.lower()
    assert "execution integration" in message
    assert "sha256" not in message.lower()
    assert "change_id" not in message


def remove_agent(report):
    changed = json.loads(json.dumps(report))
    removed = changed["agents"].pop()
    if "capability_states" in changed:
        changed["capability_states"] = [state for state in changed["capability_states"]
                                        if state["agent"] != removed["identity"]]
    changed["capability_states"] = [
        {"agent": state["agent"], "capability": state["capability"], "state": state["state"],
         "description": state["description"], "readiness": state["readiness"], "reason": state["reason"]}
        for state in changed["capability_states"]]
    return changed


def normalize_test_report(report):
    normalized = json.loads(json.dumps(report))
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("unsupported_capabilities", [])
    normalized.setdefault("execution_verified", False)
    normalized.setdefault("permissions_granted", False)
    for agent in normalized["agents"]:
        agent.setdefault("purpose", "Specialist capability metadata")
    normalized["capability_states"] = [
        {"agent": state["agent"], "capability": state["capability"], "state": state["state"],
         "description": state["description"], "readiness": state["readiness"], "reason": state["reason"]}
        for state in normalized.get("capability_states", [])]
    return normalized


def test_store_deduplicates_and_persists_across_restarts(tmp_path):
    report = normalize_test_report(valid_report())
    changed = remove_agent(report)
    path = tmp_path / "operations" / "capabilities.sqlite3"
    first_store = communication.CapabilityUpdateStore(path)
    first = first_store.observe(report, changed)
    assert first and first["status"] == "observed"
    assert first_store.observe(report, changed)["change_key"] == first["change_key"]
    restarted = communication.CapabilityUpdateStore(path)
    assert restarted.pending()["change_key"] == first["change_key"]
    assert restarted.latest()["message"] == first["message"]


def test_persisted_baseline_first_report_no_change_and_restart(tmp_path):
    path = tmp_path / "operations.sqlite3"
    report = normalize_test_report(valid_report())
    first = communication.CapabilityUpdateStore(path)
    assert first.observe_latest(report) is None
    assert {agent["identity"] for agent in first.baseline()["agents"]} == {
        agent["identity"] for agent in report["agents"]}
    assert first.observe_latest(report) is None
    changed = remove_agent(report)
    restarted = communication.CapabilityUpdateStore(path)
    notice = restarted.observe_latest(changed)
    assert notice and notice["status"] == "observed"
    assert restarted.pending()["change_key"] == notice["change_key"]


def test_runtime_evidence_survives_baseline_updates(tmp_path):
    from control_plane.runtime_capabilities import RuntimeCapabilityVerifier

    report = agent_awareness.read_capability_report(valid_report())
    runtime = RuntimeCapabilityVerifier().verify([], [], [], running_revision=REV_A).to_dict()
    store = communication.CapabilityUpdateStore(tmp_path / "runtime.sqlite3")
    assert store.observe_latest({**report, "_runtime_evidence": runtime}) is None
    baseline = store._read_baseline(store._connect())
    assert baseline["_runtime_evidence"] == runtime
    assert store.observe_latest(report) is None
    updated = store._read_baseline(store._connect())
    assert updated["_runtime_evidence"] == runtime


def test_persisted_baseline_corruption_fails_safely(tmp_path):
    path = tmp_path / "operations.sqlite3"
    store = communication.CapabilityUpdateStore(path)
    assert store.observe_latest(valid_report()) is None
    with store._connection() as connection:
        connection.execute("UPDATE capability_baseline SET report_json='not json' WHERE singleton=1")
    with pytest.raises(communication.CapabilityEvidenceError):
        communication.CapabilityUpdateStore(path).observe_latest(remove_agent(valid_report()))
    assert store.pending() is None


def test_multiple_pending_notices_are_ordered_and_delivery_transition_is_explicit(tmp_path):
    path = tmp_path / "operations.sqlite3"
    store = communication.CapabilityUpdateStore(path)
    base = normalize_test_report(valid_report())
    first_report = remove_agent(base)
    store.observe(base, first_report)
    second_report = remove_agent(base)
    second_report["agents"][0]["purpose"] += " Updated description."
    store.observe(base, second_report)
    store.observe(base, second_report)
    pending = store.pending_all()
    assert len(pending) == 2
    first, second = pending
    assert store.communicated(first["change_key"])
    assert not store.communicated(first["change_key"])
    assert store.pending_all() == [second]


def test_store_connections_are_closed(tmp_path):
    store = communication.CapabilityUpdateStore(tmp_path / "close.sqlite3")
    report = normalize_test_report(valid_report())
    store.observe_latest(report)
    with store._connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        raw_connection = connection
    with pytest.raises(Exception):
        raw_connection.execute("SELECT 1")


def test_failed_notice_presentation_keeps_observed_notice(tmp_path):
    store = communication.CapabilityUpdateStore(tmp_path / "delivery.sqlite3")
    base = normalize_test_report(valid_report())
    changed = remove_agent(base)
    notice = store.observe(base, changed)
    assert notice and store.pending()["change_key"] == notice["change_key"]
    # Presentation has not succeeded, so no communicated transition occurs.
    assert store.pending()["status"] == "observed"


def test_nonempty_history_does_not_block_notice_delivery_boundary(tmp_path):
    from unittest.mock import Mock

    store = communication.CapabilityUpdateStore(tmp_path / "history.sqlite3")
    base = normalize_test_report(valid_report())
    notice = store.observe(base, remove_agent(base))
    history = [{"role": "user", "content": "Earlier question"}]
    presenter = Mock()
    presenter.info(notice["message"])
    assert history
    assert presenter.info.call_count == 1
    assert store.communicated(notice["change_key"])
    assert store.pending_all() == []



def test_communication_and_acknowledgement_are_explicit(tmp_path):
    report = normalize_test_report(valid_report())
    changed = remove_agent(report)
    store = communication.CapabilityUpdateStore(tmp_path / "capability.sqlite3")
    update = store.observe(report, changed)
    assert store.communicated(update["change_key"])
    assert store.latest()["status"] == "communicated"
    assert store.acknowledge(update["change_key"])
    assert store.pending() is None
    assert not store.acknowledge(update["change_key"])


def test_runtime_report_distinguishes_published_from_unavailable():
    from control_plane.runtime_capabilities import RuntimeCapabilityVerifier
    runtime_report = RuntimeCapabilityVerifier().verify([], [], [], running_revision=REV_A).to_dict()
    assert communication.validate_runtime_evidence(runtime_report)["runtime_available_claimed"] is False
    report = {"assessments": [{
        "agent_id": "agent.one", "capability": "do-task", "state": "NOT_DEPLOYED",
        "published": True, "installed": False, "loaded": False, "verified": False,
        "execution_eligible": False, "required_permissions": [], "runtime_permissions": [],
        "reasons": ["not installed"], "verification_required": [], "published_revision": REV_A,
        "installed_revision": None, "software_revision": None, "execution_authorized": False,
    }], "summary": "Published, not installed", "execution_authorized": False,
        "runtime_available_claimed": False}
    assert communication.validate_runtime_evidence(report)["assessments"][0]["state"] == "NOT_DEPLOYED"
    report["runtime_available_claimed"] = True
    with pytest.raises(communication.CapabilityEvidenceError):
        communication.validate_runtime_evidence(report)


def test_missing_agent_is_reported_and_existing_chat_routes_unchanged():
    report = normalize_test_report(valid_report())
    missing = agent_awareness.answer_agent_question("Which agents or capabilities are missing?", report)
    assert missing is not None
    from brain_core import conversation
    assert conversation.classify_intent("Hello, how are you?").intent == "general"
    assert conversation.classify_intent("What agents do you know?").intent == "agent_awareness"
    assert conversation.classify_intent("Show my approved lessons.").intent == "memory_recall"


def test_no_execution_permission_or_personality_lesson_mutation(tmp_path):
    personality = ROOT / "brain_core" / "personality.json"
    lessons = ROOT / "data" / "lessons.json"
    before_personality = personality.read_bytes() if personality.exists() else None
    before_lessons = lessons.read_bytes() if lessons.exists() else None
    report = normalize_test_report(valid_report())
    changed = remove_agent(report)
    store = communication.CapabilityUpdateStore(tmp_path / "capabilities.sqlite3")
    update = store.observe(report, changed)
    assert update
    assert before_personality == (personality.read_bytes() if personality.exists() else None)
    assert before_lessons == (lessons.read_bytes() if lessons.exists() else None)
    assert store.acknowledge(update["change_key"])
