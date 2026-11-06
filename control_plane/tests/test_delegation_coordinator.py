from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from control_plane.agent_contracts import CapabilityRegistry
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import CapabilityVerifier
from control_plane.delegation_authorization import (
    AuthorizationError, DelegationAuthorizer, DelegationBudget, DelegationContext,
    DelegationDecisionState,
)
from control_plane.delegation_coordinator import (
    ChildTaskStatus, CoordinationError, DelegationCoordinator, ProposalStatus,
)
from control_plane.execution_engine import ExecutionAuthorizationIssuer, ExecutionRequest
from control_plane.mission_planning import DelegationProposal, MissionReference


class DelegationCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.registry = CapabilityRegistry()
        self.discovery = AgentDiscovery(self.registry)
        self.discovery.discover_existing()
        self.authorizer = DelegationAuthorizer(self.discovery, CapabilityVerifier(self.root))
        self.coordinator = DelegationCoordinator(self.registry, self.authorizer)
        self.mission = MissionReference("mission-1", "Review source", "A grounded review",
            ("identify risks", "report evidence"), ("local-python-static-analysis",))
        self.parent = self.coordinator.register_parent(self.mission, task_id="task-parent",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",))
        self.request = ExecutionRequest("task-parent", "mission-1", "synthetic.coordinator",
            "coordinate", {"objective": "Review source"}, ("read-local-source",))
        self.issuer = ExecutionAuthorizationIssuer()
        self.authorization = self.issuer.authorize(self.request, approved_by="brain-orchestrator",
            permissions=self.request.permissions)
        self.proposal = DelegationProposal("mission-1", "task-parent",
            "local-python-static-analysis", "Need a bounded static review",
            "Static analysis summary", ("read-local-source",))
        self.context = DelegationContext("mission-1", "task-parent", None,
            ("read-local-source",), approval_required=True,
            risk_summary=("Source contents may be sensitive",))

    def submit(self, proposal=None, *, context=None, agent="synthetic.coordinator",
               request=None, authorization=None):
        return self.coordinator.submit_proposal(proposal or self.proposal,
            originating_mission="mission-1", parent_task_id="task-parent",
            originating_agent=agent, requested_task_type="inspect-local-python-source",
            context=context or self.context,
            execution_request=request if request is not None else self.request,
            execution_authorization=authorization if authorization is not None else self.authorization)

    def approve_for_planning(self):
        record = self.submit()
        self.assertEqual(record.status, ProposalStatus.REQUIRES_HUMAN_APPROVAL)
        return self.coordinator.record_human_approval("mission-1", record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=True, reason="Approved for planning only",
            action="Create a bounded subtask plan", risks=("No execution authorization is implied",),
            context=record.authorization_context)

    def test_valid_proposal_requires_human_approval_then_planning_only(self) -> None:
        record = self.submit()
        self.assertEqual(record.status, ProposalStatus.REQUIRES_HUMAN_APPROVAL)
        self.assertEqual(record.decision.state, DelegationDecisionState.REQUIRES_HUMAN_APPROVAL)
        self.assertFalse(record.to_dict()["execution_authorized"])
        approved = self.coordinator.record_human_approval("mission-1", record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=True, reason="Proceed with planning",
            action="Plan source review", risks=("No source modification",),
            context=record.authorization_context)
        self.assertEqual(approved.status, ProposalStatus.APPROVED_FOR_PLANNING)
        self.assertEqual(approved.approval_request.risks, ("No source modification",))
        self.assertFalse(approved.to_dict()["execution_authorized"])
        child = self.coordinator.create_child_task("mission-1", approved.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            task_id="approved-child", description="Approved planning only")
        self.assertEqual(child.status, ChildTaskStatus.PLANNED)
        self.assertFalse(approved.to_dict()["execution_authorized"])
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", approved.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="approved-child", description="Duplicate child")
        self.assertEqual(len(self.coordinator.aggregate("mission-1", "task-parent").child_tasks), 1)

    def test_proposal_requires_exact_parent_binding_and_execution_authorization(self) -> None:
        with self.assertRaises(CoordinationError):
            self.submit(agent="another.synthetic.agent")
        pending = self.submit()
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", pending.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="pending-child", description="Pending cannot create a child")
        with self.assertRaises(CoordinationError):
            self.coordinator.submit_proposal(self.proposal,
                originating_mission="mission-1", parent_task_id="task-parent",
                originating_agent="synthetic.coordinator",
                requested_task_type="inspect-local-python-source", context=self.context,
                execution_request=None, execution_authorization=None)
        other_request = replace(self.request, task_id="other-task")
        with self.assertRaises(CoordinationError):
            self.submit(request=other_request)
        context = replace(self.context, task_ref="other-task")
        with self.assertRaises(CoordinationError):
            self.submit(context=context)

    def test_unsupported_capability_permission_escalation_duplicate_and_cycle(self) -> None:
        unsupported = self.submit(replace(self.proposal,
            proposed_specialist_capability="unsupported.capability"))
        self.assertEqual(unsupported.status, ProposalStatus.BLOCKED)
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", unsupported.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="blocked-child", description="Blocked proposal cannot create a child")
        escalated = self.submit(replace(self.proposal, permission_requirements=("admin",)))
        self.assertEqual(escalated.status, ProposalStatus.BLOCKED)
        valid = self.submit()
        with self.assertRaises(CoordinationError):
            self.submit()
        self.coordinator.register_parent(self.mission, task_id="task-cycle-grand",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",))
        cyclic_parent = self.coordinator.register_parent(self.mission, task_id="task-cycle-parent",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",), parent_task_id="task-cycle-grand",
            ancestor_task_ids=("task-cycle-grand",), depth=1)
        cyclic_request = ExecutionRequest("task-cycle-parent", "mission-1",
            "synthetic.coordinator", "coordinate", {"objective": "Review source"},
            ("read-local-source",), parent_task_id="task-cycle-grand")
        cyclic_auth = self.issuer.authorize(cyclic_request, approved_by="brain",
            permissions=cyclic_request.permissions)
        cyclic_proposal = replace(self.proposal, parent_task_ref="task-cycle-parent")
        cyclic_context = DelegationContext("mission-1", "task-cycle-parent", "task-cycle-grand",
            ("read-local-source", "admin"), depth=1,
            ancestor_task_refs=("task-cycle-grand",))
        with self.assertRaises(CoordinationError):
            self.coordinator.submit_proposal(cyclic_proposal,
                originating_mission="mission-1", parent_task_id="task-cycle-parent",
                originating_agent=cyclic_parent.agent_id,
                requested_task_type="inspect-local-python-source", context=cyclic_context,
                execution_request=cyclic_request, execution_authorization=cyclic_auth)
        self.assertEqual(valid.status, ProposalStatus.REQUIRES_HUMAN_APPROVAL)

    def test_denial_duplicate_approval_and_cancel(self) -> None:
        record = self.submit()
        denied = self.coordinator.record_human_approval("mission-1", record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=False, reason="Not approved",
            action="Plan source review", risks=("Unapproved",), context=record.authorization_context)
        self.assertEqual(denied.status, ProposalStatus.BLOCKED)
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", denied.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="denied-child", description="Denied proposal cannot create a child")
        planning_record = self.submit(replace(self.proposal, reason="Separate cancellation planning proposal"))
        approved = self.coordinator.record_human_approval("mission-1", planning_record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=True, reason="Plan", action="Plan cancellation",
            risks=("Fixture only",), context=planning_record.authorization_context)
        cancelled = self.coordinator.cancel_task("mission-1", "task-parent")
        self.assertEqual(cancelled, ("task-parent",))
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", approved.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="task-cancelled-child", description="Must remain unplanned")
        with self.assertRaises(CoordinationError):
            self.coordinator.create_child_task("mission-1", approved.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                task_id="cancelled-child", description="Cancelled proposal cannot create a child")
        with self.assertRaises(CoordinationError):
            self.submit()

    def test_child_tracking_dependencies_aggregation_and_cancellation(self) -> None:
        approved = self.approve_for_planning()
        child = self.coordinator.create_child_task("mission-1", approved.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            task_id="task-child-1", description="Produce a static analysis summary")
        self.assertEqual(child.status, ChildTaskStatus.PLANNED)
        with self.assertRaises(CoordinationError):
            self.coordinator.update_child_status("mission-1", child.task_id,
                ChildTaskStatus.SUCCEEDED)
        pending = self.coordinator.aggregate("mission-1", "task-parent")
        self.assertEqual(pending.status, "INCOMPLETE")
        self.assertEqual(pending.incomplete_task_ids, (child.task_id,))
        self.coordinator.update_child_status("mission-1", child.task_id,
            ChildTaskStatus.RUNNING)
        completed = self.coordinator.update_child_status("mission-1", child.task_id,
            ChildTaskStatus.SUCCEEDED, result={"summary": "fixture evidence only"})
        self.assertEqual(completed.status, ChildTaskStatus.SUCCEEDED)
        with self.assertRaises(CoordinationError):
            self.coordinator.update_child_status("mission-1", child.task_id,
                ChildTaskStatus.FAILED, error="cannot rewrite terminal outcome")
        outcome = self.coordinator.aggregate("mission-1", "task-parent")
        self.assertEqual(outcome.status, "CHILD_TASKS_REPORTED_SUCCEEDED")
        self.assertEqual(outcome.success_criteria, self.mission.success_criteria)
        self.assertFalse(outcome.execution_authorized)
        child2_proposal = replace(self.proposal, reason="Plan a follow-up task")
        followup_record = self.submit(child2_proposal,
            context=replace(self.context, existing_task_count=1))
        followup = self.coordinator.record_human_approval("mission-1", followup_record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=True, reason="Plan follow-up",
            action="Plan child follow-up", risks=("No execution implied",),
            context=replace(followup_record.authorization_context, existing_task_count=1))
        second_child = self.coordinator.create_child_task("mission-1", followup.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            task_id="task-child-2", description="Follow-up report",
            dependencies=("task-child-1",))
        self.assertEqual(second_child.dependencies, ("task-child-1",))
        cancelled = self.coordinator.cancel_task("mission-1", "task-parent")
        self.assertEqual(cancelled, ("task-parent", "task-child-1", "task-child-2"))
        with self.assertRaises(CoordinationError):
            self.coordinator.update_child_status("mission-1", "task-child-2",
                ChildTaskStatus.SUCCEEDED, result={"invented": True})

    def test_depth_limit_budget_no_self_approval_or_legacy_execution(self) -> None:
        self.coordinator.register_parent(self.mission, task_id="task-root",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",))
        middle = self.coordinator.register_parent(self.mission, task_id="task-middle",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",), parent_task_id="task-root",
            ancestor_task_ids=("task-root",), depth=1)
        deep_parent = self.coordinator.register_parent(self.mission, task_id="task-deep",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",), depth=2,
            parent_task_id="task-middle", ancestor_task_ids=("task-root", "task-middle"))
        deep_request = ExecutionRequest("task-deep", "mission-1", "synthetic.coordinator",
            "coordinate", {"objective": "Review source"}, ("read-local-source",),
            parent_task_id="task-middle")
        deep_auth = self.issuer.authorize(deep_request, approved_by="brain-orchestrator",
            permissions=deep_request.permissions)
        deep_context = DelegationContext("mission-1", "task-deep", "task-middle",
            ("read-local-source",), depth=2,
            ancestor_task_refs=("task-root", "task-middle"), approval_required=True)
        deep_proposal = replace(self.proposal, parent_task_ref="task-deep")
        with self.assertRaises(CoordinationError):
            self.coordinator.submit_proposal(deep_proposal,
                originating_mission="mission-1", parent_task_id="task-deep",
                originating_agent=deep_parent.agent_id,
                requested_task_type="inspect-local-python-source", context=deep_context,
                execution_request=deep_request, execution_authorization=deep_auth)
        with self.assertRaises(CoordinationError):
            self.submit(context=replace(self.context, existing_task_count=99))
        pending = self.submit()
        with self.assertRaises(CoordinationError):
            self.coordinator.record_human_approval("mission-1", pending.proposal_id,
                execution_request=self.request, execution_authorization=self.authorization,
                human_identity="synthetic.coordinator", approved=True, reason="Self approval",
                action="Plan", risks=(), context=pending.authorization_context)
        plan_record = self.submit(replace(self.proposal, reason="Plan a separate bounded source review"),
            context=replace(self.context, existing_task_count=1))
        approved = self.coordinator.record_human_approval("mission-1", plan_record.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            human_identity="human-operator", approved=True, reason="Plan only",
            action="Create plan", risks=("Execution remains separately gated",),
            context=plan_record.authorization_context)
        child = self.coordinator.create_child_task("mission-1", approved.proposal_id,
            execution_request=self.request, execution_authorization=self.authorization,
            task_id="task-child", description="Plan only")
        self.assertEqual(child.status, ChildTaskStatus.PLANNED)
        self.assertFalse(approved.to_dict()["execution_authorized"])

    def test_budget_exhaustion_and_failed_dependency_are_deterministic(self) -> None:
        limited_authorizer = DelegationAuthorizer(
            self.discovery, CapabilityVerifier(self.root),
            budget=DelegationBudget(max_tasks=3),
        )
        limited = DelegationCoordinator(self.registry, limited_authorizer)
        limited.register_parent(self.mission, task_id="limited-parent",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",))
        parent = limited.register_parent(self.mission, task_id="limited-parent-wide",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",), parent_task_id="limited-parent",
            ancestor_task_ids=("limited-parent",), depth=1, max_children=2)
        request = ExecutionRequest("limited-parent-wide", "mission-1", parent.agent_id,
            parent.capability, {"objective": "Review source"}, parent.authorized_permissions,
            parent_task_id="limited-parent", depth=1)
        authorization = self.issuer.authorize(request, approved_by="brain-orchestrator",
            permissions=request.permissions)
        proposal = replace(self.proposal, parent_task_ref="limited-parent-wide", permission_requirements=("read-local-source",))
        context = DelegationContext("mission-1", "limited-parent-wide", "limited-parent",
            parent.authorized_permissions, depth=1, existing_task_count=0,
            ancestor_task_refs=("limited-parent",),
            approval_required=True, risk_summary=("bounded fixture",))
        record = limited.submit_proposal(proposal, originating_mission="mission-1",
            parent_task_id="limited-parent-wide", originating_agent=parent.agent_id,
            requested_task_type="inspect-local-python-source", context=context,
            execution_request=request, execution_authorization=authorization)
        with self.assertRaises(CoordinationError):
            limited.submit_proposal(replace(proposal, reason="second proposal", parent_task_ref="limited-parent-wide"),
                originating_mission="mission-1", parent_task_id="limited-parent-wide",
                originating_agent=parent.agent_id,
                requested_task_type="inspect-local-python-source", context=replace(context, existing_task_count=3),
                execution_request=request, execution_authorization=authorization)
        approved = limited.record_human_approval("mission-1", record.proposal_id,
            execution_request=request, execution_authorization=authorization,
            human_identity="human-operator", approved=True, reason="Plan", action="Plan one child",
            risks=("fixture only",), context=record.authorization_context)
        first = limited.create_child_task("mission-1", approved.proposal_id,
            execution_request=request, execution_authorization=authorization,
            task_id="limited-child-1", description="First child")
        # A failure is retained as failure, and dependents are deterministically blocked.
        extra_proposal = replace(proposal, reason="second proposal", permission_requirements=())
        extra_record = limited.submit_proposal(extra_proposal,
            originating_mission="mission-1", parent_task_id="limited-parent-wide",
            originating_agent=parent.agent_id,
            requested_task_type="inspect-local-python-source", context=replace(context, existing_task_count=1),
            execution_request=request, execution_authorization=authorization)
        extra_approved = limited.record_human_approval("mission-1", extra_record.proposal_id,
            execution_request=request, execution_authorization=authorization,
            human_identity="human-operator", approved=True, reason="Plan", action="Plan dependent",
            risks=("fixture only",), context=extra_record.authorization_context)
        dependent = limited.create_child_task("mission-1", extra_approved.proposal_id,
            execution_request=request, execution_authorization=authorization,
            task_id="limited-child-2", description="Dependent", dependencies=(first.task_id,))
        limited.update_child_status("mission-1", first.task_id, ChildTaskStatus.RUNNING)
        limited.update_child_status("mission-1", first.task_id, ChildTaskStatus.FAILED,
            error="fixture failure")
        blocked = limited.aggregate("mission-1", "limited-parent-wide")
        self.assertEqual(blocked.status, "FAILED")
        self.assertEqual(blocked.failed_task_ids, ("limited-child-1", "limited-child-2"))
        self.assertEqual(blocked.child_tasks[1].status, ChildTaskStatus.BLOCKED)
        transitive_parent = limited.register_parent(self.mission, task_id="limited-transitive-parent",
            agent_id="synthetic.coordinator", capability="coordinate",
            authorized_permissions=("read-local-source",))
        transitive_request = ExecutionRequest("limited-transitive-parent", "mission-1",
            transitive_parent.agent_id, transitive_parent.capability,
            {"objective": "Review source"}, transitive_parent.authorized_permissions)
        transitive_auth = self.issuer.authorize(transitive_request,
            approved_by="brain-orchestrator", permissions=transitive_request.permissions)
        transitive_proposal = replace(extra_proposal,
            parent_task_ref=transitive_parent.task_id, reason="Plan a transitive dependent")
        transitive_context = DelegationContext("mission-1", transitive_parent.task_id,
            None, transitive_parent.authorized_permissions)
        transitive_record = limited.submit_proposal(transitive_proposal,
            originating_mission="mission-1", parent_task_id=transitive_parent.task_id,
            originating_agent=transitive_parent.agent_id,
            requested_task_type="inspect-local-python-source", context=transitive_context,
            execution_request=transitive_request, execution_authorization=transitive_auth)
        transitive_approved = limited.record_human_approval("mission-1", transitive_record.proposal_id,
            execution_request=transitive_request, execution_authorization=transitive_auth,
            human_identity="human-operator", approved=True, reason="Plan", action="Plan transitive",
            risks=("fixture only",), context=transitive_record.authorization_context)
        transitive_child = limited.create_child_task("mission-1", transitive_approved.proposal_id,
            execution_request=transitive_request, execution_authorization=transitive_auth,
            task_id="limited-transitive-child", description="Transitive child")
        limited.update_child_status("mission-1", transitive_child.task_id, ChildTaskStatus.RUNNING)
        limited.update_child_status("mission-1", transitive_child.task_id, ChildTaskStatus.FAILED,
            error="fixture failure")
        with self.assertRaises(CoordinationError):
            limited.update_child_status("mission-1", transitive_child.task_id,
                ChildTaskStatus.SUCCEEDED, result={"invented": True})
        self.assertEqual(limited.aggregate("mission-1", transitive_parent.task_id).failed_task_ids,
            (transitive_child.task_id,))


if __name__ == "__main__":
    unittest.main()
