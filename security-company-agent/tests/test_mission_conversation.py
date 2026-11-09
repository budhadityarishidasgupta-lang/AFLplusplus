"""Offline synthetic integration tests for unified mission conversation."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))

from brain_core import agent_awareness, conversation, mission_conversation

ORCHESTRATION = Path(REPO).parent / "agent-orchestration"


def make_state(objective="Research the best local AI model for my laptop."):
    from control_plane.agent_discovery import AgentDiscovery
    from control_plane.capability_verification import verification_report

    raw = verification_report(AgentDiscovery(), workspace=ORCHESTRATION)
    raw["unsupported_capabilities"] = []
    raw.pop("schema_version", None)
    raw["schema_version"] = 1
    raw.pop("execution_note", None)
    raw.pop("capability_states", None)
    raw.pop("schema_version", None)
    raw["schema_version"] = 1
    raw["agents"] = [
        {key: value for key, value in agent.items() if key in {
            "identity", "purpose", "version", "verification_status", "evidence",
            "missing_requirements", "execution_readiness", "last_verified_at",
            "uncertainty_reason", "capabilities"}}
        for agent in raw["agents"]
    ]
    for agent in raw["agents"]:
        agent["purpose"] = {
            "multi-agent.local-recon": "Inspect local Python source and create a fuzzing plan.",
            "multi-agent.evolutionary-exploit-dev": "Generate and mutate payloads in a local simulated workflow.",
            "enterprise-range.system-architect": "Coordinate bounded campaign state and approval gates.",
        }[agent["identity"]]
        agent["execution_readiness"]["ready"] = False
        agent["execution_readiness"]["reason"] = "Execution integration unavailable."
        agent["capabilities"] = [
            {key: value for key, value in capability.items() if key in {
                "capability", "status", "evidence", "missing_requirements", "execution_readiness",
                "required_permissions", "last_verified_at", "uncertainty_reason", "description"}}
            for capability in agent["capabilities"]
        ]
        for capability in agent["capabilities"]:
            capability["execution_readiness"]["ready"] = False
            capability["execution_readiness"]["reason"] = "Execution integration unavailable."
    fingerprint = agent_awareness.report_fingerprint(raw)
    return mission_conversation.build_mission(
        objective, raw, report_fingerprint=fingerprint, conversation_id="synthetic-chat")


def test_ordinary_conversation_is_not_routed_as_mission():
    assert conversation.classify_intent("How are you?").intent == "general"
    assert conversation.classify_intent("What is a local AI model?").intent != "mission"
    assert conversation.classify_intent("Which agent can help me accomplish this?").intent != "mission"


def test_mission_plan_and_availability_are_presented_without_execution():
    state = make_state("Plan a local fuzzing campaign for this synthetic target.")
    rendered = mission_conversation.describe_state(state)
    assert "Objective:" in rendered and "Success criteria:" in rendered
    assert "planned, not running" in rendered
    assert "Required permissions:" in rendered
    assert "No task was executed" in rendered
    assert state.to_dict()["execution_performed"] is False
    assert state.plan.status in {"proposed", "proposed_execution_unavailable"}


def test_approval_is_explicit_task_bound_and_replay_protected():
    state = make_state("Plan a local fuzzing campaign for this synthetic target.")
    task = state.plan.tasks[0]
    assert task.proposed_agent is not None
    request = mission_conversation.create_approval_request(
        state, task.task_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert state.approvals[task.task_id][1] is None
    assert request.task_ref == task.task_id
    assert request.mission_ref == state.mission.mission_id
    decision = mission_conversation.decide_approval(
        state, task.task_id, approved=True, human_identity="synthetic-user", reason="reviewed",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert decision.status.value == "APPROVED"
    assert state.to_dict()["execution_performed"] is False
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.decide_approval(
            state, task.task_id, approved=True, human_identity="synthetic-user", reason="replay",
            now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc))


def test_rejection_does_not_accept_results():
    state = make_state("Plan a local fuzzing campaign for this synthetic target.")
    task = state.plan.tasks[0]
    assert task.proposed_agent is not None
    mission_conversation.create_approval_request(
        state, task.task_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    decision = mission_conversation.decide_approval(
        state, task.task_id, approved=False, human_identity="synthetic-user", reason="declined",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert decision.status.value == "DENIED"
    with pytest.raises(mission_conversation.MissionConversationError):
        mission_conversation.record_synthetic_result(state, task.task_id, result=object())


def test_unverified_result_never_claims_completion():
    state = make_state("Plan a local fuzzing campaign for this synthetic target.")
    task = state.plan.tasks[0]
    assert task.proposed_agent is not None
    execution = mission_conversation._orchestration()[3]
    mission_conversation.create_approval_request(
        state, task.task_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    mission_conversation.decide_approval(
        state, task.task_id, approved=True, human_identity="synthetic-user", reason="test fixture",
        now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    result = execution.ExecutionResult(
        task.task_id, state.mission.mission_id, task.proposed_agent,
        execution.ExecutionState.COMPLETED, output={})
    assessment = mission_conversation.record_synthetic_result(state, task.task_id, result=result)
    assert not assessment.completed
    assert assessment.uncertainties
    assert not any(item.completed for item in state.outcomes.values())


def test_capability_announcement_is_preserved_and_memory_separate():
    from brain_core.capability_communication import CapabilityUpdateStore
    assert callable(CapabilityUpdateStore.pending_all)
    assert "personality" not in mission_conversation.MissionConversationState.__annotations__
    assert "memory" not in mission_conversation.MissionConversationState.__annotations__


def test_orchestration_uses_verified_external_worktree_without_execution():
    modules = mission_conversation._orchestration()
    assert all(Path(module.__file__).resolve().is_relative_to(ORCHESTRATION.resolve())
               for module in modules)
    source = (ORCHESTRATION / "control_plane" / "execution_engine.py").read_text(encoding="utf-8")
    assert "only structurally valid synthetic adapters" in source
    assert "legacy specialists" in source
    assert conversation.classify_intent("Which agents are unavailable?").intent == "agent_awareness"
