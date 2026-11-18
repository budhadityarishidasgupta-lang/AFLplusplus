from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
for item in (ROOT, REPO):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from brain_core import owner_simulation, mission_execution, mission_conversation, agent_awareness


@pytest.fixture(autouse=True)
def clear_process_authority():
    owner_simulation.revoke_owner_authority()
    yield
    owner_simulation.revoke_owner_authority()


def _env():
    return {"SOVEREIGN_BRAIN_ENV": "local-development",
            "SOVEREIGN_BRAIN_ENABLE_OWNER_SIMULATION": "1",
            "STREAMLIT_SERVER_HEADLESS": "false"}


def _state(service, authority):
    discovery = service.mission_capability_discovery(_owner_authority=authority)
    report = {"schema_version": 1, "agents": [], "unsupported_capabilities": [],
              "execution_verified": False, "permissions_granted": False, "capability_states": []}
    fingerprint = agent_awareness.report_fingerprint(report)
    state = mission_conversation.build_mission(
        "Research how AI can improve an HR process.", report,
        report_fingerprint=fingerprint, conversation_id="owner-conversation",
        owner_simulation_authority=authority)
    return state, discovery


def test_authority_requires_enabled_local_tty_and_exact_terminal_confirmation():
    with patch("brain_core.owner_simulation.sys.stdin") as stdin:
        stdin.isatty.return_value = False
        with pytest.raises(owner_simulation.OwnerSimulationError):
            owner_simulation.acquire_owner_authority(environ=_env(), input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
        stdin.isatty.return_value = True
        with pytest.raises(owner_simulation.OwnerSimulationError):
            owner_simulation.acquire_owner_authority(environ={"SOVEREIGN_BRAIN_ENV": "production"}, input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
        with pytest.raises(owner_simulation.OwnerSimulationError):
            owner_simulation.acquire_owner_authority(environ=_env(), input_fn=lambda _: "yes")
        authority = owner_simulation.acquire_owner_authority(
            environ=_env(), input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
    assert owner_simulation.validate_owner_authority(authority, "synthetic.brain-owner-simulation")
    assert not owner_simulation.validate_owner_authority(authority, "synthetic.brain-acceptance")
    assert not owner_simulation.validate_owner_authority(object(), "synthetic.brain-owner-simulation")


def test_owner_simulation_uses_distinct_scoped_adapter_and_executes_once(tmp_path):
    with patch("brain_core.owner_simulation.sys.stdin") as stdin:
        stdin.isatty.return_value = True
        authority = owner_simulation.acquire_owner_authority(
            environ=_env(), input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
    from control_plane.agent_contracts import CapabilityRegistry
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=tmp_path / "execution.sqlite3", registry=CapabilityRegistry(),
        _owner_authority=authority)
    state, discovery = _state(service, authority)
    task = state.plan.tasks[0]
    assert task.proposed_agent == "synthetic.brain-owner-simulation"
    assert tuple(service.engine._adapters) == ("synthetic.brain-owner-simulation",)
    assert {item.contract.agent_id for item in service.registry.list_agents()} == {"synthetic.brain-owner-simulation"}
    assert mission_conversation.create_approval_request(state, task.task_id)
    mission_conversation.decide_approval(state, task.task_id, approved=True,
                                         human_identity="local-owner", reason="reviewed")
    from brain_core.mission_execution import ExecutionApproval
    approval = ExecutionApproval(state.mission.mission_id, task.task_id,
                                 "synthetic.brain-owner-simulation", ("read-fixture",),
                                 "local-owner", "separate execution consent")
    authorized = service.authorize(state, task.task_id, approval,
                                   persist_claim=lambda _: True)
    assert service.engine.durable_store.get_claim(task.task_id) is None
    result = service.submit(state, task.task_id, authorized, persist_state=lambda _: True)
    assert result.state.value == "completed"
    assert result.agent_id == "synthetic.brain-owner-simulation"
    with pytest.raises(mission_execution.MissionExecutionError):
        service.submit(state, task.task_id, authorized, persist_state=lambda _: True)
    assert service.engine.durable_store.get_claim(task.task_id)["state"] == "COMPLETED"
    assert "no external research" in result.output["Capability support assessment"]["limitations"]


def test_owner_authority_revocation_fails_closed_at_service_and_control_plane(tmp_path):
    with patch("brain_core.owner_simulation.sys.stdin") as stdin:
        stdin.isatty.return_value = True
        authority = owner_simulation.acquire_owner_authority(
            environ=_env(), input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
    from control_plane.agent_contracts import CapabilityRegistry
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=tmp_path / "execution.sqlite3", registry=CapabilityRegistry(),
        _owner_authority=authority)
    state, _ = _state(service, authority)
    task = state.plan.tasks[0]
    mission_conversation.create_approval_request(state, task.task_id)
    mission_conversation.decide_approval(state, task.task_id, approved=True,
                                         human_identity="local-owner", reason="reviewed")
    owner_simulation.revoke_owner_authority()
    from brain_core.mission_execution import ExecutionApproval
    approval = ExecutionApproval(state.mission.mission_id, task.task_id,
                                 "synthetic.brain-owner-simulation", ("read-fixture",),
                                 "local-owner", "separate execution consent")
    with pytest.raises(mission_execution.MissionExecutionError):
        service.authorize(state, task.task_id, approval, persist_claim=lambda _: True)


def test_test_adapter_cannot_be_used_as_owner_authority():
    with pytest.raises(mission_execution.MissionExecutionError):
        mission_execution.MissionExecutionService.synthetic(
            registry=object(), _owner_authority=object())


def test_forged_persisted_owner_flags_do_not_create_authority(tmp_path):
    from brain_core import conversation, studio, mission_controller, mission_intelligence
    db = tmp_path / "brain.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=db)
    session = conversation.create_session(data_dir=tmp_path, db_path=db)
    definition = mission_intelligence.define_mission("Research how AI can improve an HR process.")
    payload = {"mission": definition.to_dict(), "plan": {"synthetic_acceptance": True,
               "execution_performed": False, "mission_ref": definition.mission_id, "tasks": []},
               "synthetic_acceptance": True, "execution_performed": False,
               "report_fingerprint": "a" * 64, "approvals": {}, "outcomes": {},
               "execution_records": {}, "execution_intents": {}}
    conversation.save_mission_state(session, payload, "forged", data_dir=tmp_path, db_path=db)
    repository = mission_controller.ConversationMissionRepository(
        conversation, data_dir=tmp_path, db_path=db)
    loaded = repository.load(session)
    assert loaded is not None
    assert loaded[0].synthetic_acceptance is False
    with pytest.raises(Exception):
        mission_conversation.state_from_dict(payload)


def test_direct_service_call_without_process_issued_authority_is_denied():
    with pytest.raises(mission_execution.MissionExecutionError):
        mission_execution.MissionExecutionService.synthetic(
            registry=object(), _owner_authority=owner_simulation.OwnerSimulationAuthority(
                frozenset({"synthetic.brain-owner-simulation"}), object()))
