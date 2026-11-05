from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import CapabilityVerifier
from control_plane.mission_planning import (
    AgentSelector,
    DelegationProposal,
    MissionPlanner,
    PlanningError,
    mission_reference_from_definition,
    propose_delegation,
)


class MissionPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.discovery = AgentDiscovery()
        self.discovery.discover_existing()
        self.verifier = CapabilityVerifier(self.root)
        self.mission = {
            "mission_id": "mission-sb36",
            "original_objective": "Prepare and assess local fuzzing plan",
            "intended_outcome": "A reviewable plan with source evidence",
            "success_criteria": ["Plan covers static review", "Limitations are stated"],
            "constraints": ["No execution"],
            "required_capabilities": ["local-python-static-analysis", "fuzzing-plan-generation"],
            "missing_information": [],
            "dependencies": [],
            "risk_level": "low",
            "authorization_requirements": ["user_confirmation"],
            "execution_readiness": "not_ready",
            "status": "draft",
            "provenance": {"source": "deterministic_interpretation"},
            "schema_version": 1,
        }

    def test_correct_capability_and_task_matching(self) -> None:
        candidates = AgentSelector(self.discovery, self.verifier).select(
            "local-python-static-analysis", "inspect-local-python-source"
        )
        self.assertEqual([candidate.agent_id for candidate in candidates], ["multi-agent.local-recon"])
        self.assertFalse(candidates[0].selected)
        self.assertIn("execution integration is not ready", candidates[0].rejection_reasons)
        self.assertFalse(candidates[0].execution_ready)

    def test_unsupported_capability_and_unknown_task_rejected(self) -> None:
        selector = AgentSelector(self.discovery, self.verifier)
        self.assertEqual(selector.select("unsupported-capability", "unsupported-task"), ())
        rejected = selector.select("local-python-static-analysis", "unknown-task")
        self.assertFalse(rejected[0].selected)
        self.assertIn("requested task type is not declared as supported", rejected[0].rejection_reasons)

    def test_unavailable_and_permission_missing_are_explained(self) -> None:
        selector = AgentSelector(self.discovery, self.verifier)
        candidate = selector.select("local-python-static-analysis", "inspect-local-python-source")[0]
        self.assertIn("execution integration is not ready", candidate.rejection_reasons)
        self.assertTrue(candidate.required_permissions)
        self.assertTrue(candidate.rejection_reasons)

    def test_ranking_is_deterministic(self) -> None:
        selector = AgentSelector(self.discovery, self.verifier)
        first = selector.select("local-python-static-analysis", "inspect-local-python-source")
        second = selector.select("local-python-static-analysis", "inspect-local-python-source")
        self.assertEqual([(item.agent_id, item.score, item.selected) for item in first], [(item.agent_id, item.score, item.selected) for item in second])

    def test_multi_task_plan_order_dependencies_and_execution_readiness(self) -> None:
        plan = MissionPlanner(self.discovery, self.verifier).plan(self.mission)
        self.assertEqual(len(plan.tasks), 2)
        self.assertEqual(plan.tasks[0].dependencies, ())
        self.assertEqual(plan.tasks[1].dependencies, ("mission-sb36.task-01",))
        self.assertFalse(plan.execution_ready)
        self.assertEqual(plan.status, "proposed_execution_unavailable")
        self.assertTrue(all(not task.execution_ready for task in plan.tasks))
        self.assertIn("No execution integration is available for the selected existing agents.", plan.missing_prerequisites)

    def test_mission_contract_compatibility(self) -> None:
        reference = mission_reference_from_definition(self.mission)
        self.assertEqual(reference.mission_id, "mission-sb36")
        self.assertEqual(reference.required_capabilities, tuple(self.mission["required_capabilities"]))
        invalid = dict(self.mission, schema_version=99)
        with self.assertRaises(PlanningError):
            mission_reference_from_definition(invalid)

    def test_delegation_proposal_schema_is_validated_without_authority(self) -> None:
        proposal = propose_delegation(
            "mission-sb36", "mission-sb36.task-01", "local-python-static-analysis",
            "Specialist static inspection is needed", "A static analysis summary", (),
        )
        encoded = json.loads(json.dumps(proposal.to_dict()))
        self.assertFalse(encoded["authorized"])
        self.assertFalse(encoded["executed"])
        with self.assertRaises(PlanningError):
            DelegationProposal("", "task", "capability", "reason", "result", ())

    def test_plan_json_and_no_legacy_agent_execution(self) -> None:
        names = ("multi_agent_system", "conversational_launcher", "enterprise_stealth_range")
        code = (
            "import sys,json; from control_plane.mission_planning import MissionPlanner; "
            f"mission={self.mission!r}; plan=MissionPlanner().plan(mission); json.dumps(plan.to_dict()); "
            f"assert not any(name in sys.modules for name in {names!r})"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        plan = MissionPlanner(self.discovery, self.verifier).plan(self.mission)
        json.dumps(plan.to_dict(), sort_keys=True)
        self.assertFalse(plan.to_dict()["execution_performed"])

    def test_no_permissions_are_granted_by_planning(self) -> None:
        plan = MissionPlanner(self.discovery, self.verifier).plan(self.mission)
        self.assertEqual(plan.required_approvals, ("user_confirmation",))
        self.assertFalse(plan.execution_ready)
        self.assertTrue(all(not candidate.execution_ready for candidate in plan.candidates))


if __name__ == "__main__":
    unittest.main()
