from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_studio as app_module
from brain_core import conversation


def test_mission_request_is_summarized_and_stored_only_in_session():
    from test_studio_ui import StudioUITests
    fixture = StudioUITests(methodName="test_ui_readiness_inspection_does_not_initialize_runtime_files")
    fixture.setUp()
    try:
        app = fixture.app.run()
        session_id = app.session_state["conversation_id"]
        state = app_module.st.session_state
        state["conversation_id"] = session_id
        state["studio_inference"] = "Automatic (Local first)"
        objective = "Research the best local AI model for my laptop and recommend whether I should upgrade."
        with patch.object(app_module, "_capability_report_path", side_effect=OSError("offline")), \
             patch.object(app_module, "_run_general_answer") as general, \
             patch.object(app_module, "_start_automatic_journey") as automatic:
            app_module._handle_message(objective, session_id, [], {"identity_name": "Synthetic"}, None, None)
        general.assert_not_called()
        automatic.assert_not_called()
        mission_store = state["_mission_state_store"]
        definition = mission_store.get(session_id)
        assert definition is not None
        assert mission_store.get("another-conversation") is None
        rows = conversation.load_messages(session_id, data_dir=fixture.paths["data_dir"],
                                         db_path=fixture.paths["db_path"])
        assert rows[-1]["role"] == "assistant"
        assert "I understand your objective:" in rows[-1]["content"]
        assert "haven't assigned an agent or started execution" in rows[-1]["content"]
        assert "mission_id" not in rows[-1]["content"]
    finally:
        fixture.tearDown()
