from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import CapabilityVerifier
from control_plane.delegation_authorization import (
    ApprovalStatus,
    AuthorizationError,
    DelegationAuthorizer,
    DelegationBudget,
    DelegationContext,
    DelegationDecisionState,
)
from control_plane.mission_planning import DelegationProposal


class DelegationAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.discovery = AgentDiscovery()
        self.discovery.discover_existing()
        self.authorizer = DelegationAuthorizer(self.discovery, CapabilityVerifier(self.root))
        self.proposal = DelegationProposal(
            "mission-1", "task-1", "local-python-static-analysis",
            "Static inspection is required", "A source analysis summary", ("read-local-source",),
        )
        self.context = DelegationContext(
            "mission-1", "task-1", None, ("read-local-source",),
            approval_required=True, risk_summary=("Local source read",),
        )

    def test_valid_proposal_requires_human_approval_and_never_executes(self) -> None:
        result = self.authorizer.evaluate(
            self.proposal, self.context, requested_task_type="inspect-local-python-source"
        )
        self.assertEqual(result.state, DelegationDecisionState.REQUIRES_HUMAN_APPROVAL)
        self.assertTrue(result.required_human_approval)
        self.assertFalse(result.execution_permitted)
        self.assertEqual(result.authorized_permissions, ())

    def test_unsupported_capability_is_blocked(self) -> None:
        proposal = replace(self.proposal, proposed_specialist_capability="unknown-capability")
        result = self.authorizer.evaluate(proposal, self.context, requested_task_type="task")
        self.assertEqual(result.state, DelegationDecisionState.BLOCKED)
        self.assertIn("proposed capability is not registered", result.reasons)

    def test_permission_escalation_and_missing_parent_authority_are_blocked(self) -> None:
        escalated = replace(self.proposal, permission_requirements=("publish-plan-to-in-memory-workspace",))
        result = self.authorizer.evaluate(escalated, self.context, requested_task_type="inspect-local-python-source")
        self.assertEqual(result.state, DelegationDecisionState.BLOCKED)
        self.assertIn("permission escalation beyond parent-authorized scope is forbidden", result.reasons)
        no_authority = replace(self.context, parent_authorized_permissions=())
        denied = self.authorizer.evaluate(self.proposal, no_authority, requested_task_type="inspect-local-python-source")
        self.assertEqual(denied.state, DelegationDecisionState.BLOCKED)

    def test_human_approval_explicit_denial_and_expiration(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect the declared local source", expected_outcome="Static report",
            risks=("Source contents may be sensitive",), now=now,
        )
        denied = self.authorizer.decide(request, approved=False, human_identity="operator", reason="Denied", now=now)
        self.assertEqual(denied.status, ApprovalStatus.DENIED)
        expired_request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect local source", expected_outcome="Static report", risks=(), now=now,
        )
        expired = self.authorizer.decide(
            expired_request, approved=True, human_identity="operator", reason="Late decision",
            now=now + timedelta(minutes=16),
        )
        self.assertEqual(expired.status, ApprovalStatus.EXPIRED)

    def test_approval_replay_cross_task_and_self_approval_are_rejected(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect local source", expected_outcome="Report", risks=(), now=now,
        )
        approved = self.authorizer.decide(request, approved=True, human_identity="operator", reason="Approved", now=now)
        self.assertTrue(self.authorizer.validate_approval_for(
            request, approved, mission_ref="mission-1", task_ref="task-1",
            agent_id="multi-agent.local-recon", capability="local-python-static-analysis", now=now,
        ))
        self.assertFalse(self.authorizer.validate_approval_for(
            request, approved, mission_ref="mission-1", task_ref="task-2",
            agent_id="multi-agent.local-recon", capability="local-python-static-analysis", now=now,
        ))
        with self.assertRaises(AuthorizationError):
            self.authorizer.decide(request, approved=True, human_identity="operator", reason="Replay", now=now)
        pending = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect", expected_outcome="Report", risks=(), now=now,
        )
        with self.assertRaises(AuthorizationError):
            self.authorizer.decide(pending, approved=True, human_identity="multi-agent.local-recon", reason="Self", now=now)

    def test_depth_cycle_and_budget_violations_block(self) -> None:
        depth = replace(self.context, depth=2)
        self.assertEqual(self.authorizer.evaluate(self.proposal, depth, requested_task_type="inspect-local-python-source").state, DelegationDecisionState.BLOCKED)
        cycle = replace(self.context, parent_task_ref="ancestor", ancestor_task_refs=("task-1",))
        self.assertEqual(self.authorizer.evaluate(self.proposal, cycle, requested_task_type="inspect-local-python-source").state, DelegationDecisionState.BLOCKED)
        budget_authorizer = DelegationAuthorizer(
            self.discovery, CapabilityVerifier(self.root),
            DelegationBudget(max_depth=2, max_tasks=1, max_model_calls=0, max_duration_seconds=5, max_cost_microunits=0),
        )
        over_budget = replace(self.context, existing_task_count=1, elapsed_seconds=6)
        result = budget_authorizer.evaluate(self.proposal, over_budget, requested_task_type="inspect-local-python-source")
        self.assertEqual(result.state, DelegationDecisionState.BLOCKED)
        self.assertTrue(result.missing_requirements)

    def test_agent_unavailable_never_becomes_approvable(self) -> None:
        result = self.authorizer.evaluate(self.proposal, self.context, requested_task_type="inspect-local-python-source")
        self.assertNotEqual(result.state, DelegationDecisionState.APPROVABLE)
        self.assertFalse(result.execution_permitted)

    def test_verified_metadata_and_missing_execution_integration_are_separate(self) -> None:
        result = self.authorizer.evaluate(
            self.proposal, replace(self.context, approval_required=False),
            requested_task_type="inspect-local-python-source",
        )
        self.assertEqual(result.state, DelegationDecisionState.APPROVABLE)
        self.assertEqual(result.agent_id, "multi-agent.local-recon")
        self.assertIn("execution integration is unavailable; agent cannot currently be commanded", result.missing_requirements)
        self.assertFalse(result.execution_permitted)
        self.assertEqual(result.authorized_permissions, ())

    def test_uninterpreted_permission_evidence_is_unknown(self) -> None:
        agent = self.discovery.lookup_capability("local-python-static-analysis")[0]
        changed = replace(agent, contract=replace(agent.contract, permissions=("unreviewed-permission",)))
        self.discovery.lookup_capability = lambda _capability: (changed,)
        result = self.authorizer.evaluate(
            self.proposal, self.context, requested_task_type="inspect-local-python-source"
        )
        self.assertEqual(result.state, DelegationDecisionState.UNKNOWN)
        self.assertIn("understood capability and supported task", result.missing_requirements)

    def test_budget_validation_rejects_booleans_and_depth_over_two(self) -> None:
        with self.assertRaises(AuthorizationError):
            DelegationBudget(max_tasks=True)
        with self.assertRaises(AuthorizationError):
            DelegationBudget(max_depth=3)

    def test_approval_expiration_and_malformed_inputs_fail_closed(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect", expected_outcome="Report", risks=(), now=now,
        )
        decision = self.authorizer.decide(
            request, approved=True, human_identity="operator", reason="Approved", now=now,
        )
        self.assertFalse(self.authorizer.validate_approval_for(
            request, decision, mission_ref="mission-1", task_ref="task-1",
            agent_id="multi-agent.local-recon", capability="local-python-static-analysis",
            now=request.expires_at,
        ))
        self.assertFalse(self.authorizer.validate_approval_for(
            request, decision, mission_ref="mission-1", task_ref="task-1",
            agent_id="multi-agent.local-recon", capability="local-python-static-analysis",
            now=datetime(2026, 1, 1),
        ))
        self.assertFalse(self.authorizer.validate_approval_for(
            request, decision, mission_ref="mission-1", task_ref="task-1",
            agent_id="multi-agent.local-recon", capability="local-python-static-analysis",
            now=datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc),
        ))
        with self.assertRaises(AuthorizationError):
            self.authorizer.decide(
                request, approved=1, human_identity="operator", reason="Invalid", now=now,
            )

    def test_approval_request_is_immutable_safe_and_json_serializable(self) -> None:
        request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect local source", expected_outcome="Static report",
            risks=("No execution",), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        payload = json.loads(json.dumps(request.to_dict()))
        self.assertEqual(payload["status"], "PENDING")
        self.assertFalse(payload.get("authorized", False))
        same_request = self.authorizer.create_approval_request(
            self.proposal, self.context, requested_agent="multi-agent.local-recon",
            requested_action="Inspect local source", expected_outcome="Static report",
            risks=("No execution",), now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(request.request_digest, same_request.request_digest)
        with self.assertRaises(AuthorizationError):
            self.authorizer.create_approval_request(
                replace(self.proposal, permission_requirements=("admin",)), self.context,
                requested_agent="multi-agent.local-recon", requested_action="Act",
                expected_outcome="Result", risks=(),
            )

    def test_no_legacy_imports_or_execution_side_effects(self) -> None:
        code = (
            "import sys; from control_plane.agent_discovery import AgentDiscovery; "
            "from control_plane.delegation_authorization import DelegationAuthorizer,DelegationContext; "
            "from control_plane.mission_planning import DelegationProposal; "
            "d=AgentDiscovery(); d.discover_existing(); a=DelegationAuthorizer(d); "
            "p=DelegationProposal('m','t','local-python-static-analysis','why','result',()); "
            "a.evaluate(p,DelegationContext('m','t',None,()),requested_task_type='inspect-local-python-source'); "
            "assert not any(n in sys.modules for n in ('multi_agent_system','conversational_launcher','enterprise_stealth_range'))"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
