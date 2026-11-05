import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain_core import conversation, mission_intelligence as mission


def test_mission_classification_separates_chat_questions_research_and_actions():
    assert mission.classify_mission_intent("Hello, how are you?") == "ordinary"
    assert mission.classify_mission_intent("What is a local language model?") == "ordinary"
    assert mission.classify_mission_intent("Research the best local AI model for my laptop and recommend whether I should upgrade.") == "information_gathering"
    assert mission.classify_mission_intent("Help me plan a careful migration of my project.") == "multi_step_mission"
    assert mission.classify_mission_intent("Plan how to send the approved notification.") == "multi_step_mission"
    assert mission.define_mission("Plan how to send the approved notification.").authorization_requirements == ()
    assert mission.classify_mission_intent("Please book a hotel for me") == "external_action"
    assert mission.classify_mission_intent("Book a hotel for my trip.") == "external_action"
    assert mission.classify_mission_intent("What did you learn about the agent?") == "ordinary"
    assert conversation.classify_intent("What is a local language model?").intent == "general"
    assert conversation.classify_intent("Which agents do you know?").intent == "agent_awareness"
    assert conversation.classify_intent("Research the best local AI model for my laptop.").intent == "mission"
    assert conversation.classify_intent("Do not research this topic.").intent != "mission"
    assert conversation.classify_intent("What have you learned about the agent?").intent == "agent_awareness"


def test_mission_extracts_outcome_success_and_hardware_constraints():
    objective = "Research the best local AI model for my laptop and recommend whether I should upgrade."
    definition = mission.define_mission(objective)
    assert definition is not None
    assert "suitable local model" in mission.summarize_mission(definition)
    assert len(definition.success_criteria) >= 3
    assert any("hardware limits" in item for item in definition.missing_information)
    assert "comparative-analysis" in definition.required_capabilities
    assert definition.execution_readiness == "not_assessed"
    assert definition.status == "needs_clarification"
    assert "interpreted_at" in definition.provenance
    assert "hardware limits" in mission.summarize_mission(definition)


def test_ordinary_conversation_does_not_create_a_mission():
    assert mission.define_mission("Hello, how are you?") is None
    assert mission.define_mission("What is the weather?") is None


def test_validation_requires_schema_success_and_safe_fields():
    definition = mission.define_mission("Compare local model options for my laptop")
    raw = definition.to_dict()
    assert mission.validate_mission(raw) == definition
    malformed = copy.deepcopy(raw)
    malformed["execution_readiness"] = True
    with pytest.raises(mission.MissionValidationError):
        mission.validate_mission(malformed)
    malformed = copy.deepcopy(raw)
    malformed["success_criteria"] = []
    with pytest.raises(mission.MissionValidationError):
        mission.validate_mission(malformed)
    malformed = copy.deepcopy(raw)
    malformed["permissions_granted"] = True
    with pytest.raises(mission.MissionValidationError):
        mission.validate_mission(malformed)
    malformed = copy.deepcopy(raw)
    malformed["status"] = "completed"
    with pytest.raises(mission.MissionValidationError):
        mission.validate_mission(malformed)
    malformed = copy.deepcopy(raw)
    malformed["provenance"]["capability_report_fingerprint"] = "not-a-digest"
    with pytest.raises(mission.MissionValidationError):
        mission.validate_mission(malformed)


def test_mission_planner_does_not_write_private_memory_or_call_a_provider(tmp_path, monkeypatch):
    from brain_core import conversation as conversation_module
    import brain_core.mission_intelligence as mission_module
    private_db = tmp_path / "private-memory.sqlite3"
    private_db.write_bytes(b"private-memory-sentinel")
    before = private_db.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("mission planning cannot call providers or modify conversation memory")

    monkeypatch.setattr(conversation_module, "save_message", forbidden)
    monkeypatch.setattr(conversation_module, "save_lesson_state", forbidden)
    monkeypatch.setattr(conversation_module, "delete_session", forbidden)
    monkeypatch.setattr(mission_module, "requests", forbidden, raising=False)
    monkeypatch.setattr(mission_module, "urlopen", forbidden, raising=False)
    definition = mission.define_mission("Research local model choices for my laptop")
    assert definition is not None
    assert private_db.read_bytes() == before


def test_unsupported_capabilities_and_invalid_report_are_not_fabricated():
    report = {
        "schema_version": 1, "agents": [], "unsupported_capabilities": [],
        "execution_verified": False, "permissions_granted": False,
    }
    definition = mission.define_mission("Research and recommend a local AI model for my laptop", capability_report=report)
    assert definition.provenance["capability_report_fingerprint"]
    assert definition.execution_readiness == "blocked"
    assert definition.status == "blocked"
    assert "Current sources have not been gathered or independently verified." in definition.missing_information
    assert any("No declared agent capability" in item for item in definition.missing_information)
    assert any("execution-ready command interface" in item for item in definition.missing_information)
    contradictory = copy.deepcopy(report)
    contradictory["execution_verified"] = True
    rejected = mission.define_mission("Research local model options", capability_report=contradictory)
    assert any("valid verified capability report" in item for item in rejected.missing_information)
    malformed = mission.define_mission("Research local model options", capability_report={"not": "valid"})
    assert any("valid verified capability report" in item for item in malformed.missing_information)


def test_external_actions_require_authorization_and_are_not_marked_executed():
    definition = mission.define_mission("Send an email to my manager asking for a meeting")
    assert definition.authorization_requirements == ("explicit_approval",)
    assert definition.execution_readiness == "blocked"
    assert definition.status == "blocked"
    assert definition.risk_level == "high"
    summary = mission.summarize_mission(definition)
    assert "does not authorize it" in summary
    assert "haven't assigned an agent or started execution" in summary


def test_natural_summary_never_claims_execution_or_agent_assignment():
    definition = mission.define_mission("Research the best local AI model for my laptop and recommend whether I should upgrade")
    summary = mission.summarize_mission(definition)
    assert "I understand your objective:" in summary
    assert "A grounded, evidence-aware result addressing the user's stated objective and limits." not in summary
    assert "no sources have been gathered" in summary
    assert "No current sources have been gathered or independently verified." in summary
    assert "I haven't assigned an agent or started execution" in summary
    assert "mission_id" not in summary
    assert "hardware-compatibility-assessment" not in summary
    assert "should I upgrade" not in summary


def test_negated_external_action_is_not_misclassified_as_a_request():
    assert mission.classify_mission_intent("Research the options; do not purchase anything.") == "information_gathering"
    assert mission.define_mission("Research the options; do not purchase anything.").authorization_requirements == ()
    assert mission.classify_mission_intent("Please do not send an email yet.") == "ordinary"
    assert mission.classify_mission_intent("Do not research this topic.") == "ordinary"


def test_mission_state_isolated_by_conversation_and_not_persisted():
    store = mission.MissionStateStore()
    one = mission.define_mission("Research local model choices for my laptop")
    two = mission.define_mission("Plan a safe project migration")
    store.save("conversation-one", one)
    store.save("conversation-two", two)
    assert store.get("conversation-one") == one
    assert store.get("conversation-two") == two
    assert store.get("unrelated") is None
    assert store.clear("conversation-one") == one
    assert store.get("conversation-one") is None


def test_existing_conversation_compatibility():
    assert conversation.classify_intent("Show my approved lessons.").intent == "memory_recall"
    assert conversation.classify_intent("A friend asked what I learned about them.").intent == "emotional_reasoning"
    assert conversation.classify_intent("Which agents do you know?").intent == "agent_awareness"
    assert conversation.classify_intent("I want to teach you something.").intent == "training"
