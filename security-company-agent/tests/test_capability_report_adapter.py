"""Direct contract tests for orchestration-to-SB-34 report adaptation."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ORCHESTRATION = ROOT.parent.parent / "agent-orchestration"
if str(ORCHESTRATION) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATION))

from brain_core import agent_awareness
from capability_report_fixtures import valid_report
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import verification_report


def test_synthetic_sb34_fixture_passes_strict_validator():
    fixture = valid_report()
    assert agent_awareness.validate_capability_report(fixture)["capability_states"]
    assert fixture["permissions_granted"] is False


def test_adapter_output_passes_strict_sb34_validator():
    source = verification_report(AgentDiscovery(), workspace=ROOT.parent.parent)
    adapted = agent_awareness.adapt_verification_report(source)
    assert adapted["schema_version"] == 1
    validated = agent_awareness.validate_capability_report(adapted)
    assert validated["capability_states"]
    expected_states = {(item["agent"], item["capability"]): item["state"]
                       for item in adapted["capability_states"]}
    normalized_states = {(agent["identity"], capability["name"]): capability["status"]
                         for agent in validated["agents"] for capability in agent["capabilities"]}
    assert normalized_states == expected_states
    assert all(not agent["ready"] for agent in validated["agents"])
    adapted_permissions = {
        (agent["identity"], capability["capability"]): capability["required_permissions"]
        for agent in adapted["agents"] for capability in agent["capabilities"]
    }
    normalized_permissions = {
        (agent["identity"], capability["name"]): capability["required_permissions"]
        for agent in validated["agents"] for capability in agent["capabilities"]
    }
    assert normalized_permissions.keys() == adapted_permissions.keys()
    assert all(tuple(normalized_permissions[key]) == tuple(adapted_permissions[key])
               for key in normalized_permissions)
    assert adapted["permissions_granted"] is False
    assert all(not capability["ready"]
               for agent in validated["agents"] for capability in agent["capabilities"])
