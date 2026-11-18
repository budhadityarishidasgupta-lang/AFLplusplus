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

from brain_core import agent_awareness, conversation, mission_conversation, studio
import brain_studio as app_module
import brain_core.mission_execution_ui as execution_ui


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
