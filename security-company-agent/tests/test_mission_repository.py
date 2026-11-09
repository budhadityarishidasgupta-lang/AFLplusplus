from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent / "agent-orchestration"))

from brain_core import conversation


def test_mission_state_storage_is_conversation_bound_and_insert_once(tmp_path):
    db_path = tmp_path / "brain.sqlite3"
    first_id = conversation.create_session(data_dir=tmp_path, db_path=db_path)
    second_id = conversation.create_session(data_dir=tmp_path, db_path=db_path)
    payload = {"mission": {"original_objective": "synthetic"}}
    first = conversation.save_mission_state(first_id, payload, "request-1",
                                             data_dir=tmp_path, db_path=db_path)
    replay = conversation.save_mission_state(first_id, {"ignored": True}, "request-1",
                                              data_dir=tmp_path, db_path=db_path)
    collision = conversation.save_mission_state(first_id, {"ignored": True}, "request-2",
                                                 data_dir=tmp_path, db_path=db_path)
    assert first == (payload, "request-1", True)
    assert replay == (payload, "request-1", True)
    assert collision == (payload, "request-1", False)
    assert conversation.load_mission_state(second_id, data_dir=tmp_path, db_path=db_path) is None
