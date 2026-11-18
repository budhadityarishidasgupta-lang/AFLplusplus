from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
for item in (ROOT, REPO):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from brain_core import agent_awareness, conversation, mission_controller, mission_conversation, mission_execution, studio


def test_repository_round_trip_preserves_execution_intent_and_planning_approval(tmp_path):
    database = tmp_path / "brain.sqlite3"
    execution_database = tmp_path / "mission-execution.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    session_id = conversation.create_session(data_dir=tmp_path, db_path=database)
    capability_report = {
        "schema_version": 1, "agents": [], "unsupported_capabilities": [],
        "execution_verified": False, "permissions_granted": False,
        "synthetic_acceptance": True,
    }
    fingerprint = agent_awareness.report_fingerprint({
        key: value for key, value in capability_report.items() if key != "synthetic_acceptance"})
    from control_plane.agent_contracts import CapabilityRegistry
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=execution_database, registry=CapabilityRegistry(),
        _acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS)
    discovery = service.mission_capability_discovery(
        _acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS)
    capability_report["_fixture_discovery"] = discovery
    state = mission_conversation.build_mission(
        "Plan a harmless synthetic fixture report.", capability_report,
        report_fingerprint=fingerprint, conversation_id=session_id)
    repository = mission_controller.ConversationMissionRepository(
        conversation, data_dir=tmp_path, db_path=database,
        synthetic_fixture_discovery=discovery)
    state, request_key, created = repository.create_once(
        session_id, state, "Plan a harmless synthetic fixture report.")
    assert created is True
    task = state.plan.tasks[0]

    mission_conversation.create_approval_request(state, task.task_id)
    mission_conversation.decide_approval(
        state, task.task_id, approved=True, human_identity="planning-reviewer",
        reason="Planning review only")
    assert repository.persist(session_id, state, request_key) is True

    service.persist_state = lambda candidate: repository.persist(session_id, candidate, request_key)
    approval = mission_execution.ExecutionApproval(
        state.mission.mission_id, task.task_id, mission_execution.SYNTHETIC_AGENT_ID,
        tuple(task.required_permissions), "execution-reviewer", "Separate explicit execution consent")
    authorization = service.authorize(state, task.task_id, approval)

    stored = conversation.load_mission_state(session_id, data_dir=tmp_path, db_path=database)
    assert stored is not None
    payload, stored_key = stored
    assert stored_key == request_key
    assert payload["execution_records"].keys() == {task.task_id}
    assert payload["execution_records"][task.task_id]["status"] == "authorization_issued"
    assert payload["approvals"][task.task_id]["decision"]["status"] == "APPROVED"
    assert execution_database.exists()
    assert service.engine.durable_store.get_claim(task.task_id) is None
    assert authorization.task_id == task.task_id

    reloaded, reloaded_key = repository.load(session_id)
    assert reloaded_key == request_key
    assert reloaded.plan.tasks[0].task_id == task.task_id
    assert reloaded.execution_records.keys() == {task.task_id}
    assert reloaded.execution_records[task.task_id]["status"] == "authorization_issued"
    assert reloaded.approvals[task.task_id][1].status.value == "APPROVED"
    assert service.engine.durable_store.get_claim(task.task_id) is None
