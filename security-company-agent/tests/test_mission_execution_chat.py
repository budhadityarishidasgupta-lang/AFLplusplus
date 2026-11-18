from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from brain_core import agent_awareness, conversation, mission_controller, mission_conversation, mission_execution, studio
import brain_studio as app_module
from streamlit.testing.v1 import AppTest


def test_streamlit_chat_persists_explicit_mission_without_inference_or_execution(tmp_path):
    database = tmp_path / "brain.sqlite3"
    execution_db = tmp_path / "execution.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    app = AppTest.from_file(str(ROOT / "brain_studio.py"), default_timeout=30)
    raw_report = {
        "schema_version": 1,
        "agents": [],
        "unsupported_capabilities": [],
        "execution_verified": False,
        "permissions_granted": False,
    }
    report = agent_awareness.read_capability_report(raw_report)
    fingerprint = agent_awareness.report_fingerprint(raw_report)
    controller_calls = []
    original_init = mission_controller.MissionController.__init__
    original_handle = mission_controller.MissionController.handle
    builders = []
    from control_plane.agent_contracts import CapabilityRegistry
    service = mission_execution.MissionExecutionService.synthetic(
        store_path=execution_db, registry=CapabilityRegistry(),
        _acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS)

    def mission_summarizer(state):
        return mission_conversation.describe_state(state)

    def controller_init(self, *, builder, repository, capability_provider,
                        summarizer=mission_conversation.describe_state):
        builders.append(builder)
        original_init(self, builder=builder, repository=repository,
                      capability_provider=lambda: ({**raw_report, "synthetic_acceptance": True,
                                                    "_fixture_discovery": service.mission_capability_discovery(
                                                        _acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS)},
                                                   fingerprint),
                      summarizer=mission_summarizer)

    def observed_handle(self, request):
        controller_calls.append(request)
        return original_handle(self, request)

    with patch.object(studio, "DATA", tmp_path), \
         patch.object(studio, "DB", database), \
         patch.object(app_module, "conversation", conversation), \
         patch.object(app_module, "mission_controller", mission_controller, create=True), \
         patch.object(app_module, "mission_conversation", mission_conversation, create=True), \
         patch.object(app_module, "_model_configuration", return_value=(None, False, None, None, False)), \
         patch.object(app_module, "_capability_report_path", side_effect=OSError("offline fixture")), \
         patch.object(app_module, "_build_capability_report_offline", return_value=raw_report), \
         patch.object(conversation, "save_mission_state", wraps=conversation.save_mission_state), \
         patch.object(app_module, "_observe_capability_update_for_chat"), \
         patch.object(app_module, "_render_pending_capability_notice"), \
         patch.object(app_module, "installed_models", return_value=[]), \
         patch.object(app_module, "available_memory_gib", return_value=1.0), \
         patch.object(mission_controller.MissionController, "__init__", controller_init), \
         patch.object(mission_controller.MissionController, "handle", observed_handle), \
         patch.object(app_module, "_run_general_answer") as general, \
         patch.object(app_module.trainer, "infer") as infer, \
         patch.object(app_module, "_start_automatic_journey") as automatic, \
         patch.object(app_module, "render_conversation", wraps=app_module.render_conversation), \
         patch.object(app_module.conversation, "classify_intent",
                      wraps=conversation.classify_intent) as classify, \
         patch.dict("sys.modules", {"brain_studio": app_module,
                                    "brain_core.mission_controller": mission_controller,
                                    "brain_core.mission_conversation": mission_conversation,
                                    "brain_core.mission_execution": mission_execution,
                                    "brain_core.conversation": conversation}):
        app.run()
        assert not app.exception
        app.session_state["studio_inference"] = "Local model (Ollama)"
        app.session_state["_mission_execution_service_factory"] = lambda: service
        app.chat_input[0].set_value("Plan a synthetic test.").run()
        assert not app.exception
        conversation_id = app.session_state["conversation_id"]
        persisted = conversation.load_mission_state(conversation_id, data_dir=tmp_path, db_path=database)
        assert persisted is not None
        state = mission_conversation.state_from_dict(
            persisted[0], capability_discovery=service.mission_capability_discovery(
                _acceptance_context=mission_execution._TRUSTED_ACCEPTANCE_HARNESS))
        assert state.mission.original_objective == "Plan a synthetic test."
        assert len(builders) == 1
        assert len(controller_calls) == 1
        assert controller_calls[0].intent == "mission"
        assert controller_calls[0].confidence == "explicit"
        assert controller_calls[0].inference_mode == "Local model (Ollama)"
        assert general.call_count == 0
        assert infer.call_count == 0
        assert service.engine.result(state.plan.tasks[0].task_id) is None
        assert conversation.load_lesson_state(conversation_id, data_dir=tmp_path, db_path=database) is None
        state = app.session_state["_mission_conversations"][conversation_id]
        task = state.plan.tasks[0]
        assert service.engine.result(task.task_id) is None
        assert service.engine.durable_store.get_claim(task.task_id) is None
        assert conversation.load_lesson_state(conversation_id, data_dir=tmp_path, db_path=database) is None

        assert app_module.mission_execution_ui.route_explicit_execution_request(
            "Request synthetic execution for this mission.")
        assert app.session_state["conversation_id"] == conversation_id
        app.chat_input[0].set_value("Request synthetic execution for this mission.").run()
        assert not app.exception
        assert app.session_state.get("_mission_conversations", {}).get(conversation_id) is not None
        state = app.session_state["_mission_conversations"][conversation_id]
        assert app.session_state.get("chat_error") is None
        assert service.engine.result(task.task_id) is None
        assert service.engine.durable_store.get_claim(task.task_id) is None
        messages = conversation.load_messages(conversation_id, data_dir=tmp_path, db_path=database)
        assert any("did not execute any task" in item["content"] for item in messages)
        assert service.engine.result(task.task_id) is None
        assert service.engine.durable_store.get_claim(task.task_id) is None
        assert app.session_state.get("_mission_execution_authorization") is None
        # Route intent only leaves the active lifecycle controls untouched until
        # the user deliberately opens the chat submission's mission review.
        assert app.session_state.get("_mission_execution_authorizations", {}) == {}

        # Ordinary status refresh cannot consult an injected test service, read
        # durable claims, or mutate mission execution projections.
        def forbidden_service_factory():
            raise AssertionError("ordinary status refresh must not construct/use an execution service")
        app.session_state["_mission_execution_service_factory"] = forbidden_service_factory
        before_payload, before_key = conversation.load_mission_state(
            conversation_id, data_dir=tmp_path, db_path=database)
        app.chat_input[0].set_value("Refresh execution status.").run()
        assert not app.exception
        after_payload, after_key = conversation.load_mission_state(
            conversation_id, data_dir=tmp_path, db_path=database)
        assert after_key == before_key
        assert after_payload["execution_records"] == before_payload["execution_records"]
        assert service.engine.result(task.task_id) is None
        assert service.engine.durable_store.get_claim(task.task_id) is None
        assert all(item["message_type"] != "mission_execution_status_refreshed"
                   for item in conversation.load_messages(conversation_id, data_dir=tmp_path, db_path=database))
        assert app.session_state.get("_mission_execution_authorization") is None
        assert app.session_state.get("_mission_execution_authorizations", {}) == {}
        app.session_state["_mission_execution_service_factory"] = None
        assert general.call_count == 0
        assert infer.call_count == 1
        assert not app.exception
        classify.assert_any_call("Plan a synthetic test.")
        automatic.assert_not_called()
