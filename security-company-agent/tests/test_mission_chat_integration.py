"""Exercise the production chat input path with mission routing dependencies patched offline."""
from __future__ import annotations

import sys
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ORCHESTRATION = ROOT.parent.parent / "agent-orchestration"
if str(ORCHESTRATION) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATION))

from brain_core import mission_controller, mission_conversation, studio, conversation
import brain_studio as app_module
from streamlit.testing.v1 import AppTest


def test_streamlit_chat_path_calls_mission_controller(tmp_path):
    database = tmp_path / "brain.sqlite3"
    studio.initialize_store(data_dir=tmp_path, db_path=database)
    app = AppTest.from_file(str(ROOT / "brain_studio.py"), default_timeout=30)
    report = {
        "schema_version": 1, "agents": [], "unsupported_capabilities": [],
        "execution_verified": False, "permissions_granted": False,
    }
    fingerprint = "f" * 64
    build_calls = []
    controller_calls = []
    original_init = mission_controller.MissionController.__init__
    original_handle = mission_controller.MissionController.handle

    def offline_init(self, *, builder, repository, capability_provider, summarizer=mission_conversation.describe_state):
        def build_with_offline_contract(objective, _ignored_report, **kwargs):
            return build(objective, report, report_fingerprint=fingerprint,
                         conversation_id=kwargs["conversation_id"])
        original_init(self, builder=build_with_offline_contract, repository=repository,
                      capability_provider=lambda: (report, fingerprint), summarizer=summarizer)

    def observed_handle(self, request):
        controller_calls.append(request)
        return original_handle(self, request)

    def build(objective, supplied_report, *, report_fingerprint, conversation_id):
        build_calls.append(objective)
        assert supplied_report == report
        assert report_fingerprint == fingerprint
        definition = mission_conversation.mission_intelligence.define_mission(objective)
        _, discovery_module, _, _, planning_module, _ = mission_conversation._orchestration()
        discovery = discovery_module.AgentDiscovery()
        discovery.discover_existing()
        planning_input = definition.to_dict()
        planning_input["required_capabilities"] = ["fuzzing-plan-generation"]
        plan = planning_module.MissionPlanner(discovery=discovery, available_permissions=()).plan(planning_input)
        return mission_conversation.MissionConversationState(definition, plan, {}, {}, fingerprint)

    test_patches = [
        patch.object(studio, "DATA", tmp_path),
        patch.object(studio, "DB", database),
        patch.object(app_module, "mission_controller", mission_controller, create=True),
        patch.object(app_module, "mission_conversation", mission_conversation, create=True),
        patch.object(app_module, "conversation", conversation),
        patch("brain_studio.studio.DATA", tmp_path),
        patch("brain_studio.studio.DB", database),
        patch("brain_studio.studio.initialize_store", wraps=studio.initialize_store),
        patch("brain_studio.conversation.load_mission_state", wraps=conversation.load_mission_state),
        patch("brain_studio._model_configuration", return_value=(None, False, None, None, False)),
        patch("brain_studio._capability_report_path", side_effect=OSError("offline")),
        patch("brain_studio._build_capability_report_offline", return_value=report),
        patch("brain_studio.mission_controller", mission_controller, create=True),
        patch("brain_studio.mission_conversation", mission_conversation, create=True),
        patch.object(mission_controller, "ConversationMissionRepository", wraps=mission_controller.ConversationMissionRepository),
        patch.object(mission_controller.MissionController, "__init__", offline_init),
        patch.object(mission_controller.MissionController, "handle", observed_handle),
        patch("brain_studio._observe_capability_update_for_chat"),
        patch("brain_studio._render_pending_capability_notice"),
        patch("brain_studio.conversation.load_lesson_state", return_value=None),
        patch("brain_studio.installed_models", return_value=[]),
        patch("brain_studio.available_memory_gib", return_value=1.0),
        patch("brain_studio._run_general_answer"),
        patch("brain_studio.trainer.infer"),
        patch.object(app_module.conversation, "classify_intent", wraps=conversation.classify_intent),
        patch.dict("sys.modules", {"brain_studio": app_module,
                                   "brain_core.mission_controller": mission_controller,
                                   "brain_core.mission_conversation": mission_conversation,
                                   "brain_core.conversation": conversation}),
    ]
    with ExitStack() as stack:
        mocks = [stack.enter_context(item) for item in test_patches]
        mission_controller_mock = mocks[2]
        init_store = mocks[7]
        repository_factory = mocks[14]
        general, infer = mocks[22], mocks[23]
        classify = mocks[24]
        app.run()
        assert not app.exception
        app.session_state["studio_inference"] = "Automatic (Local first)"
        app.chat_input[0].set_value("Plan a synthetic test.").run()
        assert not app.exception
        assert app_module.studio is studio
        assert app_module.conversation is conversation

    assert len(build_calls) == 1
    assert build_calls == ["Plan a synthetic test."]
    classify.assert_called_once_with("Plan a synthetic test.")
    assert init_store.call_args.kwargs == {"data_dir": tmp_path, "db_path": database}
    assert len(controller_calls) == 1
    request = controller_calls[0]
    assert controller_calls[0].intent == 'mission'
    assert request.text == "Plan a synthetic test."
    assert request.conversation_id == app.session_state["conversation_id"]
    assert controller_calls[0].inference_mode == "Automatic (Local first)"
    assert app.session_state.get("chat_error") is None
    general.assert_not_called()
    infer.assert_not_called()
    persisted = conversation.load_mission_state(
        request.conversation_id, data_dir=tmp_path, db_path=database)
    assert persisted is not None
    assert persisted[0]["mission"]["original_objective"] == "Plan a synthetic test."
    assert any(message["message_type"] == "mission"
               and "Plan a synthetic test." in message["content"]
               for message in conversation.load_messages(
                   request.conversation_id, data_dir=tmp_path, db_path=database))
