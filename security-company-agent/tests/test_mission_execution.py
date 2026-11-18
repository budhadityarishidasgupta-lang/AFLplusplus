from __future__ import annotations

import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))

from brain_core import mission_conversation, mission_execution
from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus


def state_fixture():
    from control_plane.mission_planning import MissionPlan, MissionTask
    mission = mission_conversation.mission_intelligence.define_mission("Plan a harmless synthetic fixture report.")
    task = MissionTask(
        task_id="synthetic.task-01", description="Produce a deterministic synthetic fixture report.",
        required_capabilities=("synthetic-mission-report",), task_type="synthetic-report",
        proposed_agent="synthetic.brain-acceptance", dependencies=(),
        required_permissions=("read-fixture",), required_approvals=("explicit_approval",),
        missing_prerequisites=("Synthetic adapter is an acceptance fixture only.",),
        expected_deliverables=("Capability support assessment",), execution_ready=False)
    plan = MissionPlan(
        mission_id=mission.mission_id, objective=mission.original_objective,
        intended_outcome=mission.intended_outcome, success_criteria=mission.success_criteria,
        tasks=(task,), candidates=(), missing_prerequisites=task.missing_prerequisites,
        required_permissions=("read-fixture",), required_approvals=("explicit_approval",),
        execution_ready=False, status="proposed_execution_unavailable")
    import tempfile
    harness = mission_execution._TRUSTED_ACCEPTANCE_HARNESS
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=Path(tempfile.mkdtemp()) / "fixture.sqlite3",
        registry=CapabilityRegistry(), _acceptance_context=harness)
    state = mission_conversation.MissionConversationState(
        mission, plan, {}, {}, "f" * 64,
        capability_discovery=service.mission_capability_discovery(_acceptance_context=harness))
    state.synthetic_acceptance = True
    return state


def approved(state, task_id, **changes):
    values = dict(mission_id=state.mission.mission_id, task_id=task_id,
                  adapter_id=mission_execution.SYNTHETIC_AGENT_ID,
                  scope=(mission_execution.SYNTHETIC_PERMISSION,),
                  approved_by="human-test", reason="explicit synthetic execution approval")
    values.update(changes)
    return mission_execution.ExecutionApproval(**values)


def make_service(now=None, limits=None, durable_store=None):
    harness = mission_execution._TRUSTED_ACCEPTANCE_HARNESS
    if durable_store is None:
        import tempfile
        from control_plane.execution_durability import SQLiteExecutionStore
        durable_store = SQLiteExecutionStore(Path(tempfile.mkdtemp()) / "execution.sqlite3")
    return mission_execution.MissionExecutionService.synthetic(
        now=now, limits=limits, durable_store=durable_store,
        persist_state=lambda candidate: True, registry=CapabilityRegistry(),
        _acceptance_context=harness)


def test_ordinary_service_context_cannot_register_authorize_or_submit_synthetic_fixture(tmp_path):
    from control_plane.execution_durability import SQLiteExecutionStore
    from control_plane.execution_engine import ExecutionAuthorizationIssuer, ExecutionEngine, ExecutionRequest, ExecutionError

    state = state_fixture()
    task = state.plan.tasks[0]
    with pytest.raises(mission_execution.MissionExecutionError):
        mission_execution.MissionExecutionService.synthetic(
            registry=CapabilityRegistry(),
            _acceptance_context=object())
    registry = CapabilityRegistry()
    registry.register(AgentContract(
        agent_id=mission_execution.SYNTHETIC_AGENT_ID,
        purpose="Fixture metadata forged by ordinary application",
        version="1.0.0", capabilities=(mission_execution.SYNTHETIC_CAPABILITY,),
        supported_tasks=("synthetic-mission-report",),
        inputs={"objective": "text", "success_criteria": "criteria"},
        outputs={"deliverable.synthetic_report": "report"},
        permissions=(mission_execution.SYNTHETIC_PERMISSION,)), CapabilityStatus.VERIFIED)
    from control_plane.agent_discovery import AgentDiscovery
    untrusted_discovery = AgentDiscovery(registry)
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.build_mission(
            "Plan fixture", {"synthetic_acceptance": True, "_fixture_discovery": untrusted_discovery},
            report_fingerprint="f" * 64, conversation_id="ordinary")
    store = SQLiteExecutionStore(tmp_path / "ordinary.sqlite3")
    issuer = ExecutionAuthorizationIssuer()
    engine = ExecutionEngine(registry, issuer, durable_store=store)
    with pytest.raises(mission_execution.MissionExecutionError):
        mission_execution.MissionExecutionService(
            registry=registry, issuer=issuer, engine=engine)
    with pytest.raises(mission_execution.MissionExecutionError):
        mission_execution.MissionExecutionService.synthetic(
            store_path=tmp_path / "denied.sqlite3", registry=registry)
    assert mission_execution.SYNTHETIC_AGENT_ID not in engine._adapters
    forged = state.to_dict()
    forged["synthetic_acceptance"] = True
    forged["plan"]["synthetic_acceptance"] = True
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.state_from_dict(forged)
    with pytest.raises(ExecutionError):
        engine.submit(ExecutionRequest(
            task_id=task.task_id, mission_id=state.mission.mission_id,
            agent_id=mission_execution.SYNTHETIC_AGENT_ID,
            capability=mission_execution.SYNTHETIC_CAPABILITY,
            input_data={"objective": "x", "success_criteria": ["x"]},
            permissions=(mission_execution.SYNTHETIC_PERMISSION,)), None)
    assert store.get_claim(task.task_id) is None
    assert not engine.audit_events


def test_persisted_synthetic_markers_do_not_recreate_fixture_capability():
    state = state_fixture()
    payload = state.to_dict()
    payload["synthetic_acceptance"] = True
    payload["plan"]["synthetic_acceptance"] = True
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.state_from_dict(payload)


def test_unknown_synthetic_specialist_cannot_be_authorized_or_submitted():
    state = state_fixture()
    task = state.plan.tasks[0]
    state.plan = replace(state.plan, tasks=(replace(task, proposed_agent="unknown.specialist"),))
    service = make_service()
    with pytest.raises(mission_execution.MissionExecutionError):
        service.authorize(state, task.task_id, approved(state, task.task_id))
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, None)
    assert service.engine.durable_store.get_claim(task.task_id) is None
    assert not service.engine.audit_events


def test_successful_approved_synthetic_lifecycle_verifies_actual_execution(tmp_path):
    from brain_core import conversation, studio
    database = tmp_path / "brain.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    session_id = conversation.create_session(data_dir=tmp_path, db_path=database)
    state = state_fixture()
    service = make_service()
    task = state.plan.tasks[0]
    planning_approval = object()
    with pytest.raises(mission_execution.MissionExecutionError):
        service.authorize(state, task.task_id, planning_approval, persist_claim=lambda candidate: False)
    state.execution_records.clear()
    persist = lambda candidate: conversation.save_mission_state(
        session_id, candidate.to_dict(), "request", data_dir=tmp_path, db_path=database)[2]
    service.persist_state = persist
    authorization = service.authorize(
        state, task.task_id, approved(state, task.task_id), persist_claim=persist)
    claim = conversation.load_mission_state(session_id, data_dir=tmp_path, db_path=database)
    result = service.submit(state, task.task_id, authorization, persist_state=persist)
    outcome = service.verify(state, task.task_id, result)
    assert result.state.value == "completed"
    assert service.engine.durable_store.get_claim(task.task_id)["state"] == "COMPLETED"
    assert outcome.status.value == "COMPLETED_VERIFIED"
    assert state.execution_records[task.task_id]["status"] == "completed"
    assert state.execution_records[task.task_id]["external_authentication"] == "unsupported"
    assert state.outcomes[task.task_id].status.value == "COMPLETED_VERIFIED"
    assert service.engine.result(task.task_id) is not None


def test_denial_and_missing_mismatched_and_replayed_authorization_fail_closed(tmp_path):
    from brain_core import conversation, studio
    db_path = tmp_path / "brain.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=db_path)
    conversation_id = conversation.create_session(data_dir=tmp_path, db_path=db_path)
    state = state_fixture()
    task = state.plan.tasks[0]
    service = make_service()
    service.deny(state, task.task_id)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, None)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    state = state_fixture()
    task = state.plan.tasks[0]
    service = make_service()
    persist = lambda candidate: conversation.save_mission_state(
        conversation_id, candidate.to_dict(), "claim test", data_dir=tmp_path, db_path=db_path)[2]
    service.persist_state = persist
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=persist)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, object())
    result = service.submit(state, task.task_id, auth, persist_state=persist)
    assert result.state.value == "completed"
    service.verify(state, task.task_id, result)
    conversation.save_mission_state(conversation_id, state.to_dict(), "claim test", data_dir=tmp_path, db_path=db_path)
    # Replaying a consumed authorization fails closed; the engine remains idempotent too.
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, auth)
    assert len(service.engine.audit_events) == 4


def test_authorization_rejects_wrong_mission_task_adapter_and_scope():
    state = state_fixture()
    task = state.plan.tasks[0]
    service = make_service()
    for mismatch in (
        {"mission_id": "other-mission"}, {"adapter_id": "legacy.security-agent"},
        {"scope": ("network-write",)},
    ):
        with pytest.raises(mission_execution.MissionExecutionError):
            service.authorize(state, task.task_id, approved(state, task.task_id, **mismatch), persist_claim=lambda candidate: False)
    assert not service.engine.audit_events


def test_expired_control_plane_authorization_is_rejected_and_not_retried():
    current = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    service = make_service(now=lambda: current[0])
    state = state_fixture()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    current[0] += timedelta(minutes=6)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, auth)
    assert service.engine.durable_store.get_claim(task.task_id) is None
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, auth)
    assert not any(event.state.value == "running" for event in service.engine.audit_events)


def test_concurrent_duplicate_submissions_execute_fixture_once():
    entered = threading.Event()
    release = threading.Event()

    class BlockingFixture(mission_execution.SyntheticMissionAdapter):
        calls = 0
        def invoke(self, capability, input_data, cancel_event):
            self.calls += 1
            entered.set()
            release.wait(2)
            return super().invoke(capability, input_data, cancel_event)

    service = make_service()
    adapter = BlockingFixture()
    service.engine._adapters[adapter.agent_id] = adapter
    state = state_fixture()
    task = state.plan.tasks[0]
    service.persist_state = lambda candidate: True
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.submit, state, task.task_id, auth)
        assert entered.wait(2)
        second = pool.submit(service.submit, state, task.task_id, auth)
        release.set()
        result = first.result(timeout=3)
        with pytest.raises(mission_execution.MissionExecutionError):
            second.result(timeout=3)
    assert result.state.value == "completed"
    assert adapter.calls == 1
    assert len([event for event in service.engine.audit_events
                if event.state.value == "running"]) == 1


def test_authorization_is_not_restored_from_persisted_execution_record(tmp_path):
    from brain_core import conversation, studio
    studio.initialize_store(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    session_id = conversation.create_session(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    state = state_fixture()
    task = state.plan.tasks[0]
    service = make_service()
    persist = lambda candidate: conversation.save_mission_state(
        session_id, candidate.to_dict(), "same request", data_dir=tmp_path,
        db_path=tmp_path / "brain.sqlite3")[2]
    service.persist_state = persist
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=persist)
    conversation.save_mission_state(session_id, state.to_dict(), "same request",
                                    data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    restored_payload, _ = conversation.load_mission_state(
        session_id, data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    assert restored_payload['execution_records'][task.task_id]['status'] == 'authorization_issued'
    restored = mission_conversation.state_from_dict(
        restored_payload, capability_discovery=service.mission_capability_discovery(_acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS))
    assert restored.execution_records[task.task_id]['status'] == 'authorization_issued'
    # An authorization intent without a durable claim is ambiguous and cannot replay.
    from control_plane.execution_durability import SQLiteExecutionStore
    restarted_store = SQLiteExecutionStore(service.engine.durable_store.path)
    assert restarted_store.recover_inflight() == ()
    new_service = make_service(durable_store=restarted_store)
    with pytest.raises(mission_execution.MissionExecutionError):
        new_service.submit(restored, task.task_id, auth)
    assert not new_service.engine.audit_events
    assert restarted_store.get_claim(task.task_id) is None
    replay_payload = {**restored_payload,
        'execution_records': {task.task_id: {'task_id': task.task_id, 'status': 'authorized'}},
        'execution_performed': True}
    conversation.save_mission_state(session_id, replay_payload, 'same request', data_dir=tmp_path,
        db_path=tmp_path / 'brain.sqlite3')
    latest_payload, _ = conversation.load_mission_state(
        session_id, data_dir=tmp_path, db_path=tmp_path / 'brain.sqlite3')
    assert latest_payload['execution_records'][task.task_id]['status'] == 'authorization_issued'
    with pytest.raises(mission_execution.MissionExecutionError):
        new_service.submit(restored, task.task_id, auth)
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.state_from_dict({**restored_payload,
            "execution_performed": True,
            "execution_records": {task.task_id: {"task_id": task.task_id,
                "status": "authorized", "authorization_id": "replayed-token", "outcome": {"status": "COMPLETED_VERIFIED"}}}})


def test_failure_is_never_serialized_as_verified_success_and_evidence_must_bind_task():
    class BlockingFailure(mission_execution.SyntheticMissionAdapter):
        def invoke(self, capability, input_data, cancel_event):
            raise RuntimeError("interrupted fixture")

    service = make_service()
    failure_adapter = BlockingFailure()
    service.engine._adapters[failure_adapter.agent_id] = failure_adapter
    state = state_fixture()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = service.submit(state, task.task_id, auth)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.verify(state, task.task_id, result)
    restored = mission_conversation.state_from_dict(
        state.to_dict(), capability_discovery=service.mission_capability_discovery(_acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS))
    assert result.state.value == "failed"
    assert state.outcomes.get(task.task_id) is None
    assert restored.execution_records[task.task_id]["status"] == "failed"
    assert "outcome" not in restored.execution_records[task.task_id]
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(restored, task.task_id, service._authorizations.get((state.mission.mission_id, task.task_id)))
    with pytest.raises(mission_execution.MissionExecutionError):
        service.verify(state, task.task_id, result)

    successful = make_service()
    state = state_fixture()
    task = state.plan.tasks[0]
    auth = successful.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = successful.submit(state, task.task_id, auth)
    from control_plane.result_verification import Evidence
    successful.evidence_factory = lambda state, task, result: (Evidence(
        "wrong-binding", state.mission.mission_id, "other.task", "claim",
        "fixture", True, {}, "execution-engine-result"),)
    with pytest.raises(mission_execution.MissionExecutionError):
        successful.verify(state, task.task_id, result)
    assert task.task_id not in state.outcomes


def test_duplicate_authorization_and_actual_result_binding():
    state = state_fixture()
    task = state.plan.tasks[0]
    service = make_service()
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: False)
    result = service.submit(state, task.task_id, auth)
    with pytest.raises(mission_execution.MissionExecutionError):
        service.verify(state, task.task_id, replace(result, output={"self_report": "success"}))
    outcome = service.verify(state, task.task_id, result)
    assert outcome.status.value == "COMPLETED_VERIFIED"


def test_failure_timeout_and_incomplete_evidence_do_not_claim_success():
    class FailureAdapter(mission_execution.SyntheticMissionAdapter):
        def invoke(self, capability, input_data, cancel_event):
            raise RuntimeError("fixture failure")

    from control_plane.execution_engine import ExecutionEngine, ExecutionLimits
    service = make_service(limits=ExecutionLimits(max_duration_seconds=0.01))
    service.engine = ExecutionEngine(service.registry, service.issuer, limits=ExecutionLimits(max_duration_seconds=0.01), durable_store=service.engine.durable_store)
    failure_adapter = FailureAdapter()
    service.engine._adapters[failure_adapter.agent_id] = failure_adapter
    state = state_fixture()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = service.submit(state, task.task_id, auth)
    assert result.state.value == "failed"
    with pytest.raises(mission_execution.MissionExecutionError):
        service.verify(state, task.task_id, result)
    assert task.task_id not in state.outcomes

    state = state_fixture()
    service = make_service()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = service.submit(state, task.task_id, auth)
    from control_plane.result_verification import Evidence
    service.evidence_factory = lambda state, task, result: tuple(
        Evidence(f"ev-{task.task_id}-{index}", state.mission.mission_id, task.task_id,
                 criterion, "deterministic fixture", True,
                 {"fixture_result": result.output}, "execution-engine-result")
        for index, criterion in enumerate(state.mission.success_criteria))
    incomplete = dict(result.output)
    incomplete["criterion_evidence"] = {}
    from dataclasses import replace as dc_replace
    actual = dc_replace(result, output=incomplete)
    service._results[(state.mission.mission_id, task.task_id)] = actual
    with pytest.raises(mission_execution.MissionExecutionError):
        service.verify(state, task.task_id, actual)
    assert task.task_id not in state.outcomes


def test_synthetic_registry_never_registers_discovered_specialists_or_changes_memory(tmp_path):
    service = make_service()
    registered = {item.contract.agent_id for item in service.registry.list_agents()}
    assert registered == {mission_execution.SYNTHETIC_AGENT_ID}
    assert service.engine._adapters.keys() == {mission_execution.SYNTHETIC_AGENT_ID}

    from brain_core import conversation, studio
    studio.initialize_store(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    conversation.create_session(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    before = conversation.load_lesson_state(
        conversation.list_sessions(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")[0]["conversation_id"],
        data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    state = state_fixture()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = service.submit(state, task.task_id, auth)
    service.verify(state, task.task_id, result)
    after = conversation.load_lesson_state(
        conversation.list_sessions(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")[0]["conversation_id"],
        data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")
    assert before == after is None
    assert len(conversation.load_messages(
        conversation.list_sessions(data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")[0]["conversation_id"],
        data_dir=tmp_path, db_path=tmp_path / "brain.sqlite3")) == 0


def test_state_serialization_keeps_execution_outcome_out_of_private_memory():
    state = state_fixture()
    service = make_service()
    task = state.plan.tasks[0]
    auth = service.authorize(state, task.task_id, approved(state, task.task_id), persist_claim=lambda candidate: True)
    result = service.submit(state, task.task_id, auth)
    service.verify(state, task.task_id, result)
    payload = state.to_dict()
    restored = mission_conversation.state_from_dict(
        payload, capability_discovery=service.mission_capability_discovery(_acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS))
    assert restored.execution_records[task.task_id]["status"] == "completed"
    assert "lesson" not in str(restored.to_dict()).casefold()
