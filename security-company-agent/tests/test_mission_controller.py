from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ORCHESTRATION = ROOT.parent.parent / "agent-orchestration"
sys.path.insert(0, str(ORCHESTRATION))

from brain_core import mission_controller


class MemoryRepository:
    def __init__(self):
        self.states = {}
        self.created = 0

    def load(self, conversation_id):
        return self.states.get(conversation_id)

    def create_once(self, conversation_id, state, request_key):
        if conversation_id not in self.states:
            self.states[conversation_id] = (state, request_key)
            self.created += 1
            return state, request_key, True
        stored, key = self.states[conversation_id]
        return stored, key, False


def make_controller(builder=None):
    repository = MemoryRepository()
    state = SimpleNamespace(mission=SimpleNamespace(original_objective="Plan a local test."))
    builder = builder or Mock(return_value=state)
    provider = Mock(return_value=({"synthetic": True}, "f" * 64))
    controller = mission_controller.MissionController(
        builder=builder, repository=repository, capability_provider=provider,
        summarizer=lambda current: "Summary: " + current.mission.original_objective)
    return controller, repository, builder, provider


def test_creates_once_binds_session_and_returns_summary():
    controller, repository, builder, provider = make_controller()
    request = mission_controller.MissionRequest("Plan a local test.", "chat-1")
    first = controller.handle(request)
    second = controller.handle(request)
    assert first.handled and first.created and first.summary == "Summary: Plan a local test."
    assert second.handled and not second.created and second.reason == "duplicate"
    assert repository.created == 1
    assert set(repository.states) == {"chat-1"}
    assert builder.call_count == 1
    assert provider.call_count == 1
    assert builder.call_args.kwargs["conversation_id"] == "chat-1"


def test_ordinary_conversation_bypasses_builder_and_provider():
    controller, _, builder, provider = make_controller()
    result = controller.handle(mission_controller.MissionRequest(
        "Hello", "chat-1", intent="general", confidence="explicit"))
    assert not result.handled and result.reason == "not_applicable"
    builder.assert_not_called()
    provider.assert_not_called()


def test_existing_conversation_binding_prevents_second_mission():
    controller, repository, builder, provider = make_controller()
    controller.handle(mission_controller.MissionRequest("Plan a local test.", "chat-1"))
    result = controller.handle(mission_controller.MissionRequest("Plan a different test.", "chat-1"))
    assert not result.handled and result.reason == "conversation_already_bound"
    assert repository.created == 1
    assert builder.call_count == 1
    assert provider.call_count == 1


def test_offline_mode_does_not_infer_or_build():
    controller, _, builder, provider = make_controller()
    result = controller.handle(mission_controller.MissionRequest(
        "Plan a local test.", "chat-1", inference_mode="Offline demonstration"))
    assert result.handled and result.reason == "unavailable_offline"
    builder.assert_not_called()
    provider.assert_not_called()


def test_no_agent_execution_or_approval_dependency():
    controller, _, builder, _ = make_controller()
    controller.handle(mission_controller.MissionRequest("Plan a local test.", "chat-1"))
    assert not hasattr(builder.return_value, "execute")
    assert not hasattr(builder.return_value, "approve")
