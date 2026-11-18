from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
for path in (ROOT, REPO):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from brain_core import agent_awareness, conversation, mission_conversation, mission_controller, studio
import brain_studio as app_module
import brain_core.mission_execution_ui as execution_ui


def test_owner_streamlit_runs_three_explicit_gates_and_persists_reload(tmp_path):
    from unittest.mock import patch
    from brain_core import owner_simulation, mission_execution
    from control_plane.agent_contracts import CapabilityRegistry

    database = tmp_path / "brain.sqlite3"
    execution_database = tmp_path / "mission-execution.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    owner_simulation.revoke_owner_authority()
    with patch.object(owner_simulation.sys.stdin, "isatty", return_value=True):
        authority = owner_simulation.acquire_owner_authority(
            environ={"SOVEREIGN_BRAIN_ENV": "local-development",
                     "SOVEREIGN_BRAIN_ENABLE_OWNER_SIMULATION": "1",
                     "STREAMLIT_SERVER_HEADLESS": "false"},
            input_fn=lambda _: "AUTHORIZE LOCAL SYNTHETIC SIMULATION")
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=execution_database, registry=CapabilityRegistry(), _owner_authority=authority)
    assert getattr(authority, "_service", None) is service
    empty_report = {"schema_version": 1, "agents": [], "unsupported_capabilities": [],
                    "execution_verified": False, "permissions_granted": False}
    build_state = mission_conversation.build_mission(
        "Plan a deterministic synthetic test report.", empty_report,
        report_fingerprint=agent_awareness.report_fingerprint(empty_report),
        conversation_id="owner-app-test", owner_simulation_authority=authority)
    assert build_state.plan.tasks[0].proposed_agent == "synthetic.brain-owner-simulation"
    app_module._OWNER_SIMULATION_AUTHORITY = authority
    app_module._OWNER_SIMULATION_SERVICE = service
    mission_conversation.owner_simulation = owner_simulation
    app = AppTest.from_file(str(ROOT / "brain_studio.py"), default_timeout=60)
    raw_report = {"schema_version": 1, "agents": [], "unsupported_capabilities": [],
                  "execution_verified": False, "permissions_granted": False,
                  "capability_states": []}
    with patch.object(app_module, "owner_simulation", owner_simulation, create=True), \
         patch.object(studio, "DATA", tmp_path), \
         patch.object(studio, "DB", database), \
         patch.object(app_module, "_model_configuration", return_value=(None, False, None, None, False)), \
         patch.object(app_module, "_capability_report_path", return_value=tmp_path / "absent-report.json"), \
         patch.object(app_module, "_build_capability_report_offline", return_value=raw_report), \
         patch.object(app_module, "_owner_simulation_authority", return_value=authority), \
         patch.object(app_module, "_owner_simulation_service", return_value=service), \
         patch.object(app_module, "_observe_capability_update_for_chat"), \
         patch.object(app_module, "_render_pending_capability_notice"), \
         patch.object(app_module, "installed_models", return_value=[]), \
         patch.object(app_module, "available_memory_gib", return_value=1.0), \
         patch.dict("sys.modules", {"brain_studio": app_module,
                                    "brain_core.mission_conversation": mission_conversation,
                                    "brain_core.mission_execution_ui": execution_ui,
                                    "brain_core.conversation": conversation}):
        app.run()
        assert not app.exception
        app.session_state["studio_inference"] = "Local model (Ollama)"
        app.chat_input[0].set_value("Plan a deterministic synthetic test report.").run()
        assert not app.exception
        session_id = app.session_state["conversation_id"]
        payload = {"synthetic_acceptance": True, "plan": {"synthetic_acceptance": True}}
        task = None
        for _ in range(3):
            saved = conversation.load_mission_state(session_id, data_dir=tmp_path, db_path=database)
            if saved is not None:
                payload, _ = saved
                if payload.get("plan", {}).get("tasks"):
                    break
            app.chat_input[0].set_value("Plan a deterministic synthetic test report.").run()
            assert not app.exception
        assert payload.get("plan", {}).get("tasks")
        assert payload is not None
        assert payload["synthetic_acceptance"] is True
        state = mission_conversation.state_from_dict(
            payload, capability_discovery=service.mission_capability_discovery(_owner_authority=authority))
        task = state.plan.tasks[0]
        assert task.proposed_agent == "synthetic.brain-owner-simulation"
        assert service.engine.result(task.task_id) is None
        assert service.engine.durable_store.get_claim(task.task_id) is None
        request_key = conversation.load_mission_state(session_id, data_dir=tmp_path, db_path=database)[1]
        repository = mission_controller.ConversationMissionRepository(
            conversation, data_dir=tmp_path, db_path=database,
            synthetic_fixture_discovery=service.mission_capability_discovery(_owner_authority=authority))
        repository.load(session_id)
        app.run()
        assert not app.exception
        review_key = f"sb48-review-{task.task_id}"
        assert any(getattr(item, "key", None) == review_key for item in app)
        app.button(key=review_key).click().run()
        assert not app.exception
        assert any(getattr(item, "key", None) == f"sb48-approve-plan-{task.task_id}" for item in app)
        app.button(key=f"sb48-approve-plan-{task.task_id}").click().run()
        assert not app.exception
        assert any(getattr(item, "key", None) == f"sb48-authorize-{task.task_id}" for item in app)
        assert service.engine.durable_store.get_claim(task.task_id) is None
        app.button(key=f"sb48-authorize-{task.task_id}").click().run()
        assert not app.exception
        assert any(getattr(item, "key", None) == f"sb48-submit-{task.task_id}" for item in app)
        assert service.engine.result(task.task_id) is None
        app.button(key=f"sb48-submit-{task.task_id}").click().run()
        assert not app.exception
        assert service.engine.result(task.task_id) is not None
        assert service.engine.result(task.task_id).state.value == "completed"
        persisted, persisted_key = conversation.load_mission_state(
            session_id, data_dir=tmp_path, db_path=database)
        assert persisted_key == request_key
        reloaded = mission_conversation.state_from_dict(
            persisted, capability_discovery=service.mission_capability_discovery(_owner_authority=authority))
        assert reloaded.plan.tasks[0].proposed_agent == task.proposed_agent
        assert reloaded.execution_records[task.task_id]["status"] == "completed"
        assert service.engine.durable_store.get_claim(task.task_id) is not None
        with pytest.raises(Exception):
            service.submit(state, task.task_id, service._authorizations.get((state.mission.mission_id, task.task_id)))
    owner_simulation.revoke_owner_authority()


def test_ordinary_streamlit_denies_fixture_even_with_forged_session_metadata(tmp_path):
    from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus
    from control_plane.agent_discovery import AgentDiscovery, AgentSource
    registry = CapabilityRegistry()
    registry.register(AgentContract(
        agent_id="synthetic.brain-acceptance", purpose="forged fixture", version="1.0.0",
        capabilities=("synthetic-mission-report",), supported_tasks=("synthetic-mission-report",),
        inputs={"objective": "text", "success_criteria": "criteria"},
        outputs={"deliverable.synthetic_report": "report"}, permissions=("read-fixture",)),
        CapabilityStatus.VERIFIED)
    discovery = AgentDiscovery(registry)
    with pytest.raises(Exception):
        discovery.register_synthetic_source("synthetic.brain-acceptance", AgentSource(
            "synthetic_fixture", "test", ("invoke",), ("test",), (), "none", False))
    database = tmp_path / "brain.sqlite3"
    execution_database = tmp_path / "execution.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    app = AppTest.from_file(str(ROOT / "brain_studio.py"), default_timeout=60)
    raw_report = {"schema_version": 1, "agents": [], "unsupported_capabilities": [],
                  "execution_verified": False, "permissions_granted": False,
                  "capability_states": []}
    fingerprint = agent_awareness.report_fingerprint(raw_report)

    with patch.object(studio, "DATA", tmp_path), \
         patch.object(studio, "DB", database), \
         patch.object(app_module, "_model_configuration", return_value=(None, False, None, None, False)), \
         patch.object(app_module, "_capability_report_path", return_value=tmp_path / "capability-report.json"), \
         patch.object(app_module, "_build_capability_report_offline", return_value=raw_report), \
         patch.object(agent_awareness, "read_capability_report", return_value=raw_report), \
         patch.object(agent_awareness, "report_fingerprint", return_value=fingerprint), \
         patch.object(app_module, "_observe_capability_update_for_chat"), \
         patch.object(app_module, "_render_pending_capability_notice"), \
         patch.object(app_module, "installed_models", return_value=[]), \
         patch.object(app_module, "available_memory_gib", return_value=1.0), \
         patch.dict("sys.modules", {"brain_studio": app_module,
                                    "brain_core.mission_conversation": mission_conversation,
                                    "brain_core.mission_execution_ui": execution_ui,
                                    "brain_core.conversation": conversation}):
        app.run()
        assert not app.exception
        app.session_state["studio_inference"] = "Local model (Ollama)"
        app.session_state["_synthetic_mission_acceptance"] = True
        app.session_state["_mission_conversations"] = {"forged": type("FakeMission", (), {
            "synthetic_acceptance": True, "capability_discovery": object()})()}
        app.session_state["_synthetic_fixture_capability_discovery"] = object()
        app.chat_input[0].set_value("Plan a harmless synthetic fixture report.").run()
        assert not app.exception
        session_id = app.session_state["conversation_id"]
        payload, _ = conversation.load_mission_state(session_id, data_dir=tmp_path, db_path=database)
        assert payload["execution_performed"] is False
        assert payload.get("synthetic_acceptance") is False
        task_id = payload["plan"]["tasks"][0]["task_id"]
        assert f"exec_approve_{task_id}" not in {getattr(item, "key", None) for item in app}
        assert f"exec_submit_{task_id}" not in {getattr(item, "key", None) for item in app}
        assert any("Synthetic acceptance capability is unavailable" in str(item.value)
                   for item in app.info)
        assert not execution_database.exists()

        app.session_state["_mission_execution_service_factory"] = lambda: (_ for _ in ()).throw(
            AssertionError("ordinary UI must not consult injected factory"))
        app.run()
        assert not app.exception
        assert f"exec_approve_{task_id}" not in {getattr(item, "key", None) for item in app}
        assert f"exec_submit_{task_id}" not in {getattr(item, "key", None) for item in app}
        assert not execution_database.exists()
