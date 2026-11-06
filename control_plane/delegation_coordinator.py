"""Brain-governed coordination of proposals from synthetic agents.

This module tracks planning and outcome metadata only. It never launches an
agent, creates execution authorizations, or upgrades a delegation decision into
permission to execute.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from control_plane.agent_contracts import CapabilityRegistry
from control_plane.delegation_authorization import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    DelegationAuthorizationDecision,
    DelegationAuthorizer,
    DelegationContext,
    DelegationDecisionState,
)
from control_plane.execution_engine import ExecutionAuthorization, ExecutionRequest
from control_plane.mission_planning import DelegationProposal, MissionReference


class CoordinationError(ValueError):
    """Invalid coordination input or an unsafe state transition."""


class ProposalStatus(str, Enum):
    SUBMITTED = "PROPOSAL_SUBMITTED"
    EVALUATING = "UNDER_EVALUATION"
    APPROVED_FOR_PLANNING = "APPROVED_FOR_PLANNING"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ChildTaskStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ParentTask:
    mission: MissionReference
    task_id: str
    agent_id: str
    capability: str
    authorized_permissions: tuple[str, ...]
    depth: int = 0
    parent_task_id: str | None = None
    ancestor_task_ids: tuple[str, ...] = ()
    max_children: int = 8

    def __post_init__(self) -> None:
        if not isinstance(self.mission, MissionReference):
            raise CoordinationError("mission must be a MissionReference")
        if (not isinstance(self.mission.mission_id, str)
                or not _stable_id(self.mission.mission_id)
                or not isinstance(self.mission.success_criteria, tuple)
                or not self.mission.success_criteria
                or any(not isinstance(item, str) or not item.strip()
                       or any(ord(character) < 32 for character in item)
                       for item in self.mission.success_criteria)):
            raise CoordinationError("mission reference must contain stable identity and safe success criteria")
        for name in ("task_id", "agent_id", "capability"):
            if not _stable_id(getattr(self, name)):
                raise CoordinationError(f"{name} must be a stable identifier")
        if self.parent_task_id is not None and not _stable_id(self.parent_task_id):
            raise CoordinationError("parent_task_id must be a stable identifier")
        if (not isinstance(self.authorized_permissions, tuple)
                or any(not _stable_id(item) for item in self.authorized_permissions)
                or len(set(self.authorized_permissions)) != len(self.authorized_permissions)):
            raise CoordinationError("authorized_permissions must be unique stable identifiers")
        if (not isinstance(self.ancestor_task_ids, tuple)
                or any(not _stable_id(item) for item in self.ancestor_task_ids)
                or len(set(self.ancestor_task_ids)) != len(self.ancestor_task_ids)):
            raise CoordinationError("ancestor_task_ids must be unique stable identifiers")
        if self.task_id in self.ancestor_task_ids:
            raise CoordinationError("parent task cannot be its own ancestor")
        if isinstance(self.depth, bool) or not isinstance(self.depth, int) or not 0 <= self.depth <= 2:
            raise CoordinationError("depth must be an integer in [0, 2]")
        if self.depth != len(self.ancestor_task_ids):
            raise CoordinationError("parent depth must match its ancestry")
        if self.parent_task_id is None and self.ancestor_task_ids:
            raise CoordinationError("root task cannot declare ancestors")
        if self.parent_task_id is not None and (
                not self.ancestor_task_ids or self.ancestor_task_ids[-1] != self.parent_task_id):
            raise CoordinationError("parent ancestry must end with the direct parent")
        if isinstance(self.max_children, bool) or not isinstance(self.max_children, int) or not 1 <= self.max_children <= 100:
            raise CoordinationError("max_children must be in [1, 100]")


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    proposal: DelegationProposal
    originating_agent: str
    requested_task_type: str
    status: ProposalStatus
    decision: DelegationAuthorizationDecision | None = None
    approval_request: ApprovalRequest | None = None
    approval_decision: ApprovalDecision | None = None
    authorization_context: DelegationContext | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal": self.proposal.to_dict(),
            "originating_agent": self.originating_agent,
            "requested_task_type": self.requested_task_type,
            "status": self.status.value,
            "decision": self.decision.to_dict() if self.decision else None,
            "approval_request": self.approval_request.to_dict() if self.approval_request else None,
            "approval_decision": self.approval_decision.to_dict() if self.approval_decision else None,
            "execution_authorized": False,
        }


@dataclass(frozen=True)
class DelegatedTask:
    task_id: str
    parent_task_id: str
    mission_id: str
    agent_id: str
    capability: str
    description: str
    expected_result: str
    dependencies: tuple[str, ...] = ()
    status: ChildTaskStatus = ChildTaskStatus.PLANNED
    result: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not _stable_id(self.task_id) or not _stable_id(self.parent_task_id) or not _stable_id(self.mission_id):
            raise CoordinationError("delegated task identifiers are invalid")
        if not _stable_id(self.agent_id) or not _stable_id(self.capability):
            raise CoordinationError("delegated agent and capability identifiers are invalid")
        if (not isinstance(self.description, str) or not self.description.strip()
                or not isinstance(self.expected_result, str) or not self.expected_result.strip()
                or any(ord(character) < 32 for character in self.description)
                or any(ord(character) < 32 for character in self.expected_result)):
            raise CoordinationError("delegated task description and expected result must be safe text")
        if len(self.description) > 2_000 or len(self.expected_result) > 2_000:
            raise CoordinationError("delegated task description and expected result exceed bounds")
        if (not isinstance(self.dependencies, tuple)
                or any(not _stable_id(item) for item in self.dependencies)
                or len(set(self.dependencies)) != len(self.dependencies)):
            raise CoordinationError("dependencies must be unique stable task identifiers")
        if not isinstance(self.status, ChildTaskStatus):
            raise CoordinationError("child status is invalid")
        if self.task_id in self.dependencies:
            raise CoordinationError("child task cannot depend on itself")
        if self.status is ChildTaskStatus.PLANNED and (self.result is not None or self.error is not None):
            raise CoordinationError("planned task cannot carry an outcome")
        if self.status is ChildTaskStatus.RUNNING and (self.result is not None or self.error is not None):
            raise CoordinationError("running task cannot carry a terminal outcome")
        if self.status is ChildTaskStatus.FAILED and self.result is not None:
            raise CoordinationError("failed task cannot carry a successful result")
        if self.result is not None:
            _bounded_json(self.result)
        if self.status is ChildTaskStatus.SUCCEEDED and self.result is None:
            raise CoordinationError("successful child status requires a recorded result")
        if self.status in {ChildTaskStatus.FAILED, ChildTaskStatus.BLOCKED} and (
                not isinstance(self.error, str) or not self.error.strip()
                or any(ord(character) < 32 for character in self.error)):
            raise CoordinationError("failed or blocked child tasks require a safe error summary")
        if self.status not in {ChildTaskStatus.FAILED, ChildTaskStatus.BLOCKED} and self.error is not None:
            raise CoordinationError("only failed or blocked child tasks may carry an error summary")

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "parent_task_id": self.parent_task_id,
                "mission_id": self.mission_id, "agent_id": self.agent_id,
                "capability": self.capability, "description": self.description,
                "expected_result": self.expected_result, "dependencies": list(self.dependencies),
                "status": self.status.value, "result": self.result, "error": self.error}


@dataclass(frozen=True)
class CoordinationEvent:
    event_id: str
    mission_id: str
    parent_task_id: str
    task_id: str
    status: str
    timestamp: str
    detail: str


@dataclass(frozen=True)
class AggregatedOutcome:
    mission_id: str
    parent_task_id: str
    success_criteria: tuple[str, ...]
    status: str
    child_tasks: tuple[DelegatedTask, ...]
    incomplete_task_ids: tuple[str, ...]
    failed_task_ids: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"mission_id": self.mission_id, "parent_task_id": self.parent_task_id,
                "success_criteria": list(self.success_criteria), "status": self.status,
                "child_tasks": [task.to_dict() for task in self.child_tasks],
                "incomplete_task_ids": list(self.incomplete_task_ids),
                "failed_task_ids": list(self.failed_task_ids),
                "execution_authorized": False}


@dataclass
class _MissionState:
    reference: MissionReference
    parents: dict[str, ParentTask] = field(default_factory=dict)
    proposals: dict[str, ProposalRecord] = field(default_factory=dict)
    proposal_keys: dict[str, str] = field(default_factory=dict)
    children: dict[str, DelegatedTask] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)


class DelegationCoordinator:
    """Coordinate proposals and outcomes while leaving execution authorization separate."""

    def __init__(self, registry: CapabilityRegistry, authorizer: DelegationAuthorizer) -> None:
        if not isinstance(registry, CapabilityRegistry) or not isinstance(authorizer, DelegationAuthorizer):
            raise CoordinationError("a capability registry and delegation authorizer are required")
        if authorizer.discovery.registry is not registry:
            raise CoordinationError("coordinator and authorizer must share the same capability registry")
        self.registry = registry
        self.authorizer = authorizer
        self._missions: dict[str, _MissionState] = {}
        self._events: list[CoordinationEvent] = []
        self._lock = threading.RLock()

    @property
    def events(self) -> tuple[CoordinationEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def register_parent(self, mission: MissionReference, *, task_id: str, agent_id: str,
                        capability: str, authorized_permissions: tuple[str, ...],
                        depth: int = 0, parent_task_id: str | None = None,
                        ancestor_task_ids: tuple[str, ...] = (), max_children: int = 8) -> ParentTask:
        parent = ParentTask(mission, task_id, agent_id, capability, authorized_permissions,
                            depth, parent_task_id, ancestor_task_ids, max_children)
        with self._lock:
            state = self._missions.get(mission.mission_id)
            if state is None:
                state = _MissionState(mission)
                self._missions[mission.mission_id] = state
            elif state.reference != mission:
                raise CoordinationError("mission definition cannot change during coordination")
            if task_id in parent.ancestor_task_ids:
                raise CoordinationError("parent task cannot appear in its own ancestry")
            if parent.parent_task_id is not None:
                if parent.parent_task_id == parent.task_id:
                    raise CoordinationError("parent task cannot reference itself")
                if parent.parent_task_id in state.cancelled:
                    raise CoordinationError("cancelled parent task cannot register descendants")
                registered_parent = state.parents.get(parent.parent_task_id)
                if registered_parent is not None:
                    if parent.depth != registered_parent.depth + 1:
                        raise CoordinationError("delegation depth does not match registered parent")
                    expected_ancestors = registered_parent.ancestor_task_ids + (registered_parent.task_id,)
                    if parent.ancestor_task_ids != expected_ancestors:
                        raise CoordinationError("registered ancestry does not match parent chain")
                    if not set(parent.authorized_permissions).issubset(registered_parent.authorized_permissions):
                        raise CoordinationError("delegated task permission scope exceeds its parent")
                elif (parent.depth == 0 or parent.depth > 2
                      or len(parent.ancestor_task_ids) != parent.depth
                      or parent.ancestor_task_ids[-1] != parent.parent_task_id
                      or any(ancestor not in state.parents
                             for ancestor in parent.ancestor_task_ids[:-1])):
                    raise CoordinationError("unregistered parent requires bounded, registered ancestry")
                if (parent.parent_task_id in parent.ancestor_task_ids[:-1]
                        or any(item in state.parents
                               and item != parent.parent_task_id
                               and state.parents[item].mission.mission_id != mission.mission_id
                               for item in parent.ancestor_task_ids)):
                    raise CoordinationError("parent ancestry contains a cycle or cross-mission task")
            elif parent.depth != 0 or parent.ancestor_task_ids:
                raise CoordinationError("root task cannot declare a parent depth or ancestry")
            existing = state.parents.get(task_id)
            if existing is not None and existing != parent:
                raise CoordinationError("parent task is already registered with different metadata")
            state.parents[task_id] = parent
        return parent

    def submit_proposal(self, proposal: DelegationProposal, *, originating_mission: str,
                        parent_task_id: str, originating_agent: str,
                        requested_task_type: str, context: DelegationContext,
                        execution_authorization: ExecutionAuthorization | None,
                        execution_request: ExecutionRequest | None = None) -> ProposalRecord:
        if not isinstance(proposal, DelegationProposal) or not isinstance(context, DelegationContext):
            raise CoordinationError("validated proposal and delegation context are required")
        if not _stable_id(originating_mission) or not _stable_id(parent_task_id) or not _stable_id(originating_agent):
            raise CoordinationError("proposal origin identifiers are invalid")
        if not _stable_id(requested_task_type):
            raise CoordinationError("requested_task_type must be a stable identifier")
        with self._lock:
            state = self._missions.get(originating_mission)
            parent = state.parents.get(parent_task_id) if state else None
            if parent is None:
                raise CoordinationError("originating parent task is not registered")
            if parent_task_id in state.cancelled:
                raise CoordinationError("cancelled parent tasks cannot propose delegation")
            if parent.depth >= 2:
                raise CoordinationError("maximum delegation depth of 2 exceeded")
            self._validate_origin(parent, proposal, context, originating_agent,
                                  execution_request, execution_authorization)
            if proposal.parent_mission_ref != originating_mission or proposal.parent_task_ref != parent_task_id:
                raise CoordinationError("proposal mission or parent task does not match its origin")
            if context.mission_ref != originating_mission or context.task_ref != parent_task_id:
                raise CoordinationError("authorization context does not match originating task")
            if self._would_create_cycle(state, parent, context.ancestor_task_refs):
                raise CoordinationError("delegation cycle detected in parent ancestry")
            proposal_key = _proposal_digest(proposal, originating_agent, requested_task_type)
            if proposal_key in state.proposal_keys:
                existing = state.proposals[state.proposal_keys[proposal_key]]
                raise CoordinationError("duplicate delegation proposal: " + existing.proposal_id)
            if self._parent_load(state, parent.task_id) >= parent.max_children:
                raise CoordinationError("parent task delegation budget exhausted")
            normalized_context = self._validated_proposal_context(parent, state, context)
            if normalized_context.existing_task_count + 1 > self.authorizer.budget.max_tasks:
                raise CoordinationError("delegation task budget exhausted")
            try:
                self.authorizer.evaluate(
                    proposal, replace(normalized_context, approval_required=True),
                    requested_task_type=requested_task_type)
            except Exception:
                raise CoordinationError("delegation authorization preflight failed closed") from None
            proposal_id = "proposal-" + proposal_key[:24]
            state.proposal_keys[proposal_key] = proposal_id
            record = ProposalRecord(proposal_id, proposal, originating_agent,
                                    requested_task_type, ProposalStatus.SUBMITTED,
                                    authorization_context=replace(normalized_context, approval_required=True))
            state.proposals[proposal_id] = record
            self._emit(state, parent_task_id, parent_task_id, ProposalStatus.SUBMITTED.value,
                       "delegation proposal received")
            self._emit(state, parent_task_id, parent_task_id, ProposalStatus.EVALUATING.value,
                       "proposal evaluated against registered capabilities and parent scope")
            try:
                decision = self.authorizer.evaluate(
                    proposal, replace(normalized_context, approval_required=True),
                    requested_task_type=requested_task_type)
            except Exception:
                state.proposals.pop(proposal_id, None)
                state.proposal_keys.pop(proposal_key, None)
                raise CoordinationError("delegation evaluation failed closed") from None
            status = self._proposal_status(decision)
            record = ProposalRecord(proposal_id, proposal, originating_agent,
                                    requested_task_type, status, decision,
                                    authorization_context=replace(normalized_context, approval_required=True))
            state.proposals[proposal_id] = record
            self._emit(state, parent_task_id, parent_task_id, status.value,
                       "planning decision only; execution authorization remains mandatory")
            return record

    def record_human_approval(self, mission_id: str, proposal_id: str, *,
                              execution_request: ExecutionRequest,
                              execution_authorization: ExecutionAuthorization | None,
                              human_identity: str, approved: bool, reason: str,
                              action: str, risks: tuple[str, ...],
                                      context: DelegationContext) -> ProposalRecord:
        if not isinstance(approved, bool):
            raise CoordinationError("human approval must be an explicit boolean")
        if (not isinstance(human_identity, str) or not _stable_id(human_identity)
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(action, str) or not action.strip()
                or len(reason) > 2_000 or len(action) > 2_000
                or any(ord(character) < 32 for character in reason)
                or any(ord(character) < 32 for character in action)
                or not isinstance(risks, tuple)
                or any(not isinstance(item, str) or not item.strip() for item in risks)):
            raise CoordinationError("explicit human identity, reason, action and risks are required")
        with self._lock:
            state, record, parent = self._get_record(mission_id, proposal_id)
            if record.status is not ProposalStatus.REQUIRES_HUMAN_APPROVAL:
                raise CoordinationError("proposal is not awaiting human approval")
            if not isinstance(execution_request, ExecutionRequest):
                raise CoordinationError("exact parent execution request is required")
            self._validate_execution_binding(parent, execution_request, execution_authorization)
            if (not isinstance(action, str) or not action.strip()
                    or any(ord(character) < 32 for character in action)
                    or any(ord(character) < 32 for character in reason)
                    or any(any(ord(character) < 32 for character in risk) for risk in risks)):
                raise CoordinationError("approval details contain unsafe control characters")
            decision = record.decision
            if decision is None or decision.agent_id is None:
                raise CoordinationError("proposal has no eligible specialist to approve")
            if human_identity == record.originating_agent or human_identity == decision.agent_id:
                raise CoordinationError("agents cannot approve their own or delegated proposals")
            if (not isinstance(risks, tuple)
                    or any(not isinstance(risk, str) or not risk.strip()
                           or any(ord(character) < 32 for character in risk) for risk in risks)):
                raise CoordinationError("approval risks must be safe, non-empty text")
            base_context = record.authorization_context
            if not isinstance(context, DelegationContext):
                raise CoordinationError("approval requires the proposal's bound delegation context")
            if base_context is None:
                raise CoordinationError("proposal authorization context is unavailable")
            if replace(context, approval_required=True) not in {
                    base_context,
                    replace(base_context, existing_task_count=base_context.existing_task_count + 1)}:
                raise CoordinationError("approval context does not match the submitted proposal context")
            proposal_context = replace(
                self._validated_proposal_context(
                    parent, state, base_context, allow_current_proposal=True),
                approval_required=False,
            )
            try:
                refreshed = self.authorizer.evaluate(
                    record.proposal, replace(proposal_context, approval_required=True),
                    requested_task_type=record.requested_task_type,
                )
            except Exception:
                raise CoordinationError("delegation revalidation failed closed") from None
            if refreshed.state is not DelegationDecisionState.REQUIRES_HUMAN_APPROVAL:
                raise CoordinationError("delegation is no longer eligible for human approval")
            if refreshed.agent_id != decision.agent_id:
                raise CoordinationError("selected specialist changed; submit a fresh proposal")
            proposal_context = replace(proposal_context, approval_required=True)
            approval_request = self.authorizer.create_approval_request(
                record.proposal, proposal_context,
                requested_agent=decision.agent_id, requested_action=action,
                expected_outcome=record.proposal.expected_subtask_result, risks=risks,
                requester=parent.agent_id)
            # DelegationAuthorizer itself excludes the proposed agent from approval.
            approval_decision = self.authorizer.decide(
                approval_request, approved=approved, human_identity=human_identity,
                reason=reason)
            if approval_decision.status is ApprovalStatus.APPROVED and self.authorizer.validate_approval_for(
                approval_request, approval_decision, mission_ref=mission_id,
                task_ref=parent.task_id, agent_id=decision.agent_id,
                capability=record.proposal.proposed_specialist_capability):
                status = ProposalStatus.APPROVED_FOR_PLANNING
            elif approval_decision.status is ApprovalStatus.DENIED:
                status = ProposalStatus.BLOCKED
            else:
                status = ProposalStatus.BLOCKED
            updated = ProposalRecord(record.proposal_id, record.proposal,
                record.originating_agent, record.requested_task_type, status, decision,
                approval_request, approval_decision, record.authorization_context)
            state.proposals[proposal_id] = updated
            self._emit(state, parent.task_id, parent.task_id, status.value,
                       "explicit human approval recorded; execution authorization remains separate")
            return updated

    def create_child_task(self, mission_id: str, proposal_id: str, *,
                          execution_request: ExecutionRequest,
                          execution_authorization: ExecutionAuthorization | None,
                          task_id: str, description: str, dependencies: tuple[str, ...] = ()) -> DelegatedTask:
        with self._lock:
            state, record, parent = self._get_record(mission_id, proposal_id)
            if record.status is not ProposalStatus.APPROVED_FOR_PLANNING:
                raise CoordinationError("proposal has not been approved for planning")
            if record.approval_decision is None or record.approval_decision.status is not ApprovalStatus.APPROVED:
                raise CoordinationError("proposal lacks its own explicit human planning approval")
            if not isinstance(record.approval_request, ApprovalRequest) or not self.authorizer.validate_approval_for(
                    record.approval_request, record.approval_decision,
                    mission_ref=mission_id, task_ref=parent.task_id,
                    agent_id=record.decision.agent_id if record.decision else "",
                    capability=record.proposal.proposed_specialist_capability):
                raise CoordinationError("planning approval is invalid or does not match this task")
            self._validate_execution_binding(parent, execution_request, execution_authorization)
            if parent.task_id in state.cancelled:
                raise CoordinationError("cancelled parent cannot create child tasks")
            if record.decision is None or record.decision.execution_permitted:
                raise CoordinationError("proposal decision does not preserve planning-only authority")
            if record.decision is None or record.decision.agent_id is None:
                raise CoordinationError("proposal has no selected specialist")
            if (not _stable_id(task_id) or not isinstance(description, str)
                    or not description.strip() or len(description) > 2_000
                    or any(ord(character) < 32 for character in description)):
                raise CoordinationError("child task identity and description must be bounded safe text")
            if task_id in state.children or task_id in state.parents or task_id == parent.task_id:
                raise CoordinationError("duplicate or cyclic child task")
            if task_id in state.proposals:
                raise CoordinationError("child task identifier cannot reuse a proposal identifier")
            if (execution_request.depth != parent.depth
                    or execution_request.parent_task_id != parent.parent_task_id):
                raise CoordinationError("execution request depth or parent binding changed")
            child_count = sum(1 for child in state.children.values()
                              if child.parent_task_id == parent.task_id)
            if child_count >= parent.max_children:
                raise CoordinationError("parent task child budget exhausted")
            proposal = record.proposal
            requested_permissions = set(proposal.permission_requirements)
            parent_permissions = set(parent.authorized_permissions)
            if not requested_permissions.issubset(parent_permissions):
                raise CoordinationError("delegated child permission scope exceeds parent authority")
            if parent.depth + 1 > 2:
                raise CoordinationError("maximum delegation depth of 2 exceeded")
            if task_id in parent.ancestor_task_ids or task_id == parent.parent_task_id:
                raise CoordinationError("child task would create a delegation cycle")
            if (not isinstance(dependencies, tuple)
                    or any(not _stable_id(item) for item in dependencies)
                    or len(set(dependencies)) != len(dependencies)):
                raise CoordinationError("dependencies must be unique stable task identifiers")
            if any(dep not in state.children or state.children[dep].parent_task_id != parent.task_id
                   for dep in dependencies):
                raise CoordinationError("dependencies must refer to tasks under the same parent")
            if _dependency_cycle(task_id, dependencies, state.children):
                raise CoordinationError("child task dependency cycle detected")
            agent_id = record.decision.agent_id
            child = DelegatedTask(task_id, parent.task_id, mission_id, agent_id,
                record.proposal.proposed_specialist_capability, description.strip(),
                record.proposal.expected_subtask_result, dependencies)
            state.children[task_id] = child
            parent_state = state.parents.get(parent.task_id)
            if parent_state is not None:
                state.parents[task_id] = ParentTask(
                    parent.mission, task_id, agent_id, child.capability,
                    tuple(sorted(requested_permissions)), parent.depth + 1,
                    parent.task_id,
                    parent.ancestor_task_ids + (parent.task_id,),
                    parent.max_children,
                )
            self._emit(state, parent.task_id, task_id, ChildTaskStatus.PLANNED.value,
                       "subtask recorded for planning; it has not been executed")
            return child

    def update_child_status(self, mission_id: str, task_id: str, status: ChildTaskStatus,
                            *, result: Any = None, error: str | None = None) -> DelegatedTask:
        with self._lock:
            if not isinstance(status, ChildTaskStatus):
                raise CoordinationError("child status must be a ChildTaskStatus")
            state = self._missions.get(mission_id)
            child = state.children.get(task_id) if state else None
            if child is None:
                raise CoordinationError("child task is not registered")
            if task_id in state.cancelled:
                raise CoordinationError("cancelled task cannot accept further status updates")
            if status is ChildTaskStatus.SUCCEEDED:
                if result is None:
                    raise CoordinationError("successful child status requires a recorded result")
                _bounded_json(result)
            elif result is not None:
                raise CoordinationError("only successful children may publish a result")
            if status in {ChildTaskStatus.FAILED, ChildTaskStatus.BLOCKED}:
                if not isinstance(error, str) or not error.strip() or any(ord(c) < 32 for c in error):
                    raise CoordinationError("failed or blocked child status requires a safe error summary")
                error = error[:160]
            elif error is not None:
                raise CoordinationError("error details are only accepted for failed children")
            if child.status in {ChildTaskStatus.SUCCEEDED, ChildTaskStatus.FAILED,
                                ChildTaskStatus.CANCELLED, ChildTaskStatus.BLOCKED}:
                raise CoordinationError("terminal child status cannot be changed")
            if status in {ChildTaskStatus.SUCCEEDED, ChildTaskStatus.FAILED} and any(
                    state.children[dependency].status is not ChildTaskStatus.SUCCEEDED
                    for dependency in child.dependencies):
                raise CoordinationError("child dependencies must succeed before terminal completion")
            if child.status is ChildTaskStatus.PLANNED and status not in {
                    ChildTaskStatus.RUNNING, ChildTaskStatus.SUCCEEDED, ChildTaskStatus.FAILED,
                    ChildTaskStatus.CANCELLED, ChildTaskStatus.BLOCKED}:
                raise CoordinationError("planned child may only start or reach a terminal status")
            if child.status is ChildTaskStatus.RUNNING and status is ChildTaskStatus.PLANNED:
                raise CoordinationError("running child cannot return to planned")
            updated = DelegatedTask(child.task_id, child.parent_task_id, child.mission_id,
                child.agent_id, child.capability, child.description, child.expected_result,
                child.dependencies, status, result, error)
            state.children[task_id] = updated
            if status in {ChildTaskStatus.FAILED, ChildTaskStatus.CANCELLED, ChildTaskStatus.BLOCKED}:
                pending = [task_id]
                blocked_ids: set[str] = set()
                while pending:
                    failed_dependency = pending.pop(0)
                    for dependent in tuple(state.children.values()):
                        if (failed_dependency in dependent.dependencies
                                and dependent.task_id not in blocked_ids
                                and dependent.status is ChildTaskStatus.PLANNED):
                            blocked_ids.add(dependent.task_id)
                            pending.append(dependent.task_id)
                            blocked_child = DelegatedTask(
                                dependent.task_id, dependent.parent_task_id, dependent.mission_id,
                                dependent.agent_id, dependent.capability, dependent.description,
                                dependent.expected_result, dependent.dependencies,
                                ChildTaskStatus.BLOCKED,
                                error="dependency task did not succeed",
                            )
                            state.children[dependent.task_id] = blocked_child
                            state.cancelled.add(dependent.task_id)
                            self._emit(state, dependent.parent_task_id, dependent.task_id,
                                       ChildTaskStatus.BLOCKED.value,
                                       "blocked because a dependency did not succeed")
            self._emit(state, child.parent_task_id, task_id, status.value,
                       "child status recorded; no outcome inferred")
            return updated

    def cancel_task(self, mission_id: str, task_id: str) -> tuple[str, ...]:
        with self._lock:
            state = self._missions.get(mission_id)
            if state is None or (task_id not in state.parents and task_id not in state.children):
                raise CoordinationError("task is not registered in this mission")
            if task_id in state.cancelled:
                raise CoordinationError("task is already cancelled")
            queue = [task_id]
            cancelled: list[str] = []
            while queue:
                current = queue.pop(0)
                if current in state.cancelled:
                    continue
                state.cancelled.add(current)
                cancelled.append(current)
                for proposal_id, record in tuple(state.proposals.items()):
                    if record.proposal.parent_task_ref == current and record.status not in {
                            ProposalStatus.BLOCKED, ProposalStatus.CANCELLED}:
                        state.proposals[proposal_id] = replace(record, status=ProposalStatus.CANCELLED)
                        self._emit(state, current, current, ProposalStatus.CANCELLED.value,
                                   "proposal cancelled with parent task")
                for child in sorted(state.children.values(), key=lambda item: item.task_id):
                    if child.parent_task_id == current:
                        queue.append(child.task_id)
                        if child.status not in {ChildTaskStatus.SUCCEEDED, ChildTaskStatus.FAILED,
                                                ChildTaskStatus.CANCELLED, ChildTaskStatus.BLOCKED}:
                            state.children[child.task_id] = DelegatedTask(
                                child.task_id, child.parent_task_id, child.mission_id,
                                child.agent_id, child.capability, child.description,
                                child.expected_result, child.dependencies,
                                ChildTaskStatus.CANCELLED)
                            self._emit(state, current, child.task_id,
                                       ChildTaskStatus.CANCELLED.value,
                                       "cancellation propagated from parent")
            self._emit(state, task_id, task_id, ProposalStatus.CANCELLED.value,
                       "task cancellation recorded")
            return tuple(cancelled)

    def aggregate(self, mission_id: str, parent_task_id: str) -> AggregatedOutcome:
        with self._lock:
            state = self._missions.get(mission_id)
            parent = state.parents.get(parent_task_id) if state else None
            if state is None or parent is None:
                raise CoordinationError("parent task is not registered")
            if parent_task_id in state.cancelled:
                raise CoordinationError("cancelled parent task has no aggregatable active outcome")
            children = tuple(sorted((item for item in state.children.values()
                                     if item.parent_task_id == parent_task_id),
                                    key=lambda item: item.task_id))
            incomplete = tuple(item.task_id for item in children
                               if item.status not in {ChildTaskStatus.SUCCEEDED,
                                                      ChildTaskStatus.FAILED,
                                                      ChildTaskStatus.CANCELLED,
                                                      ChildTaskStatus.BLOCKED})
            failed = tuple(item.task_id for item in children
                           if item.status in {ChildTaskStatus.FAILED,
                                              ChildTaskStatus.BLOCKED,
                                              ChildTaskStatus.CANCELLED})
            if incomplete:
                outcome = "INCOMPLETE"
            elif failed:
                outcome = "FAILED"
            elif not children:
                outcome = "NO_DELEGATED_TASKS"
            else:
                outcome = "CHILD_TASKS_REPORTED_SUCCEEDED"
            return AggregatedOutcome(mission_id, parent_task_id,
                parent.mission.success_criteria, outcome, children, incomplete, failed, False)

    @staticmethod
    def _proposal_status(decision: DelegationAuthorizationDecision) -> ProposalStatus:
        if decision.state is DelegationDecisionState.REQUIRES_HUMAN_APPROVAL:
            return ProposalStatus.REQUIRES_HUMAN_APPROVAL
        if decision.state is DelegationDecisionState.APPROVABLE:
            return ProposalStatus.APPROVED_FOR_PLANNING
        return ProposalStatus.BLOCKED

    @staticmethod
    def _parent_load(state: _MissionState, parent_task_id: str) -> int:
        proposals = sum(1 for record in state.proposals.values()
                       if record.proposal.parent_task_ref == parent_task_id
                       and record.status not in {ProposalStatus.BLOCKED, ProposalStatus.CANCELLED})
        children = sum(1 for child in state.children.values()
                       if child.parent_task_id == parent_task_id)
        return max(proposals, children)

    @staticmethod
    def _would_create_cycle(state: _MissionState, parent: ParentTask,
                            declared_ancestors: tuple[str, ...]) -> bool:
        actual = parent.ancestor_task_ids
        if parent.task_id in actual or len(actual) != parent.depth:
            return True
        if len(set(actual)) != len(actual) or (actual and actual[-1] != parent.parent_task_id):
            return True
        if any(ancestor not in state.parents for ancestor in actual):
            return True
        if parent.parent_task_id is None:
            return bool(actual or declared_ancestors or parent.depth)
        if not actual or actual[-1] != parent.parent_task_id:
            return True
        return tuple(declared_ancestors) != tuple(actual)

    def _validate_origin(self, parent: ParentTask, proposal: DelegationProposal,
                         context: DelegationContext, originating_agent: str,
                         request: ExecutionRequest | None,
                         authorization: ExecutionAuthorization | None) -> None:
        if originating_agent != parent.agent_id:
            raise CoordinationError("proposal agent does not match registered parent agent")
        if proposal.parent_mission_ref != parent.mission.mission_id or proposal.parent_task_ref != parent.task_id:
            raise CoordinationError("proposal mission or parent task mismatch")
        if context.parent_task_ref != parent.parent_task_id:
            raise CoordinationError("delegation context parent relationship mismatch")
        if parent.parent_task_id is not None:
            registered_parent = self._missions[parent.mission.mission_id].parents.get(parent.parent_task_id)
            if registered_parent is not None:
                if parent.depth != registered_parent.depth + 1:
                    raise CoordinationError("delegation depth does not match registered parent")
                expected_ancestors = registered_parent.ancestor_task_ids + (registered_parent.task_id,)
                if parent.ancestor_task_ids != expected_ancestors:
                    raise CoordinationError("delegated parent ancestry does not match its registered parent")
                if not set(parent.authorized_permissions).issubset(registered_parent.authorized_permissions):
                    raise CoordinationError("delegated parent permission scope exceeds its parent")
            elif len(parent.ancestor_task_ids) != parent.depth:
                raise CoordinationError("parent ancestry is incomplete")
        if parent.depth >= 2:
            raise CoordinationError("maximum delegation depth of 2 exceeded")
        if context.depth != parent.depth:
            raise CoordinationError("delegation context depth mismatch")
        expected_ancestors = parent.ancestor_task_ids
        if context.ancestor_task_refs != expected_ancestors:
            raise CoordinationError("delegation context ancestry mismatch")
        if context.parent_authorized_permissions != parent.authorized_permissions:
            raise CoordinationError("delegation context permission scope does not match the parent task")
        if request is not None and request.depth != parent.depth:
            raise CoordinationError("execution request depth does not match registered parent")
        self._validate_execution_binding(parent, request, authorization)

    @staticmethod
    def _validate_execution_binding(parent: ParentTask, request: ExecutionRequest | None,
                                    authorization: ExecutionAuthorization | None) -> None:
        if not isinstance(request, ExecutionRequest) or not isinstance(authorization, ExecutionAuthorization):
            raise CoordinationError("separate execution authorization and exact task request are required")
        if (not isinstance(authorization.issued_at, datetime)
                or not isinstance(authorization.expires_at, datetime)
                or authorization.issued_at.tzinfo is None
                or authorization.expires_at.tzinfo is None):
            raise CoordinationError("execution authorization timestamps must be timezone-aware")
        if not isinstance(authorization.approved_by, str) or not authorization.approved_by.strip():
            raise CoordinationError("execution authorization approver must be identified")
        if authorization.approved_by in {parent.agent_id, request.agent_id}:
            raise CoordinationError("execution authorization cannot be approved by the originating agent")
        if (request.task_id != parent.task_id or request.mission_id != parent.mission.mission_id
                or request.agent_id != parent.agent_id or request.capability != parent.capability
                or request.parent_task_id != parent.parent_task_id
                or request.depth != parent.depth
                or set(request.permissions) != set(parent.authorized_permissions)
                or authorization.task_id != request.task_id
                or authorization.mission_id != request.mission_id
                or authorization.agent_id != request.agent_id
                or authorization.capability != request.capability
                or authorization.input_digest != _input_digest(request.input_data)
                or set(authorization.permissions) != set(request.permissions)
                or len(set(request.permissions)) != len(request.permissions)
                or len(set(authorization.permissions)) != len(authorization.permissions)):
            raise CoordinationError("execution authorization does not match the registered parent task")
        if (authorization.issued_at.utcoffset() is None
                or authorization.expires_at.utcoffset() is None
                or not authorization.issued_at <= datetime.now(timezone.utc) < authorization.expires_at):
            raise CoordinationError("execution authorization is expired or not yet valid")

    def _validated_proposal_context(self, parent: ParentTask, state: _MissionState,
                                    context: DelegationContext,
                                    *, allow_current_proposal: bool = False) -> DelegationContext:
        if (context.mission_ref != parent.mission.mission_id
                or context.task_ref != parent.task_id
                or context.parent_task_ref != parent.parent_task_id
                or context.depth != parent.depth
                or context.ancestor_task_refs != parent.ancestor_task_ids
                or context.parent_authorized_permissions != parent.authorized_permissions):
            raise CoordinationError("authorization context does not match the registered parent")
        existing = sum(
            1 for record in state.proposals.values()
            if record.proposal.parent_task_ref == parent.task_id
            and record.status not in {ProposalStatus.BLOCKED, ProposalStatus.CANCELLED}
        )
        if allow_current_proposal and existing > 0:
            existing -= 1
        if allow_current_proposal:
            valid_counts = {existing, existing + 1}
        else:
            valid_counts = {existing}
        if context.existing_task_count not in valid_counts:
            raise CoordinationError("authorization task count does not match coordinator state")
        try:
            return DelegationContext(
                mission_ref=parent.mission.mission_id,
                task_ref=parent.task_id,
                parent_task_ref=parent.parent_task_id,
                parent_authorized_permissions=parent.authorized_permissions,
                depth=parent.depth,
                existing_task_count=existing,
                model_calls=context.model_calls,
                elapsed_seconds=context.elapsed_seconds,
                cost_microunits=context.cost_microunits,
                ancestor_task_refs=parent.ancestor_task_ids,
                approval_required=True,
                risk_summary=context.risk_summary,
            )
        except Exception:
            raise CoordinationError("authorization context is invalid") from None

    def _authorization_context(self, parent: ParentTask, state: _MissionState,
                               proposal: DelegationProposal) -> DelegationContext:
        existing_task_count = sum(
            1 for record in state.proposals.values()
            if record.proposal.parent_task_ref == parent.task_id
            and record.status not in {ProposalStatus.BLOCKED, ProposalStatus.CANCELLED}
        )
        return DelegationContext(parent.mission.mission_id, parent.task_id,
            parent.parent_task_id, parent.authorized_permissions, depth=parent.depth,
            existing_task_count=existing_task_count,
            ancestor_task_refs=parent.ancestor_task_ids,
            approval_required=False)

    def _get_record(self, mission_id: str, proposal_id: str) -> tuple[_MissionState, ProposalRecord, ParentTask]:
        if not _stable_id(mission_id) or not _stable_id(proposal_id):
            raise CoordinationError("mission or proposal identifier is invalid")
        state = self._missions.get(mission_id)
        record = state.proposals.get(proposal_id) if state else None
        if state is None or record is None:
            raise CoordinationError("delegation proposal is unavailable")
        if record.status is ProposalStatus.CANCELLED:
            raise CoordinationError("cancelled proposal cannot be modified")
        parent = state.parents.get(record.proposal.parent_task_ref)
        if parent is None or record.proposal.parent_mission_ref != mission_id:
            raise CoordinationError("delegation proposal parent binding is invalid")
        if parent.task_id in state.cancelled:
            raise CoordinationError("cancelled parent task cannot modify delegation state")
        return state, record, parent

    def _emit(self, state: _MissionState, parent_task_id: str, task_id: str,
              status: str, detail: str) -> None:
        event_id = hashlib.sha256(f"{state.reference.mission_id}:{len(self._events)}:{task_id}:{status}".encode()).hexdigest()[:24]
        safe_status = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(status))[:64]
        safe_detail = "".join(character if ord(character) >= 32 else " " for character in str(detail))[:160]
        self._events.append(CoordinationEvent(event_id, state.reference.mission_id,
            parent_task_id, task_id, safe_status, datetime.now(timezone.utc).isoformat(), safe_detail))


def _stable_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))


def _proposal_digest(proposal: DelegationProposal, agent_id: str, task_type: str) -> str:
    payload = {"proposal": proposal.to_dict(), "originating_agent": agent_id,
               "requested_task_type": task_type}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _input_digest(value: Mapping[str, Any]) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise CoordinationError("execution input is not valid canonical JSON") from None
    return hashlib.sha256(raw.encode()).hexdigest()


def _bounded_json(value: Any) -> None:
    if not isinstance(value, (Mapping, list, str, int, float, bool)):
        raise CoordinationError("child result must be a JSON-compatible value")
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 65_536:
            raise CoordinationError("child result exceeds the bounded result size")
    except (TypeError, ValueError, RecursionError):
        raise CoordinationError("child result must be bounded JSON data") from None


def _dependency_cycle(task_id: str, dependencies: tuple[str, ...],
                      children: Mapping[str, DelegatedTask]) -> bool:
    if task_id in dependencies:
        return True
    visited: set[str] = set()
    pending = list(dependencies)
    while pending:
        current = pending.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        child = children.get(current)
        if child is not None:
            pending.extend(child.dependencies)
    return False
