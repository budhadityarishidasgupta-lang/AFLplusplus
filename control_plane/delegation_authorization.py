"""Fail-closed authorization decisions for proposed delegation.

This module is authorization infrastructure only. It never launches work,
changes permissions, imports legacy agents, or treats APPROVABLE as approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from control_plane.agent_contracts import CapabilityNotFoundError, CapabilityStatus
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import CapabilityVerifier
from control_plane.mission_planning import DelegationProposal


class AuthorizationError(ValueError):
    """Authorization request/decision contract is invalid."""


class DelegationDecisionState(str, Enum):
    APPROVABLE = "APPROVABLE"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class DelegationBudget:
    max_depth: int = 2
    max_tasks: int = 8
    max_model_calls: int = 0
    max_duration_seconds: int = 300
    max_cost_microunits: int = 0

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_tasks", "max_model_calls", "max_duration_seconds", "max_cost_microunits"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuthorizationError(f"{name} must be a non-negative integer")
        if self.max_depth > 2:
            raise AuthorizationError("maximum delegation depth cannot exceed 2")
        if self.max_tasks == 0 or self.max_duration_seconds == 0:
            raise AuthorizationError("task and time budgets must be positive")


@dataclass(frozen=True)
class DelegationContext:
    mission_ref: str
    task_ref: str
    parent_task_ref: str | None
    parent_authorized_permissions: tuple[str, ...]
    depth: int = 0
    existing_task_count: int = 0
    model_calls: int = 0
    elapsed_seconds: int = 0
    cost_microunits: int = 0
    ancestor_task_refs: tuple[str, ...] = ()
    approval_required: bool = False
    risk_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegationAuthorizationDecision:
    state: DelegationDecisionState
    mission_ref: str
    task_ref: str
    agent_id: str | None
    capability: str
    reasons: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    required_human_approval: bool
    authorized_permissions: tuple[str, ...]
    execution_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "mission_ref": self.mission_ref,
            "task_ref": self.task_ref,
            "agent_id": self.agent_id,
            "capability": self.capability,
            "reasons": list(self.reasons),
            "missing_requirements": list(self.missing_requirements),
            "required_human_approval": self.required_human_approval,
            "authorized_permissions": list(self.authorized_permissions),
            "execution_permitted": False,
            "meaning": "APPROVABLE is eligibility for consideration only; it never authorizes execution.",
        }


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    mission_ref: str
    task_ref: str
    proposed_agent: str
    capability: str
    requested_action: str
    expected_outcome: str
    requested_permissions: tuple[str, ...]
    authorized_scope: tuple[str, ...]
    risks: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    request_digest: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requester: str = "orchestration"

    def __post_init__(self) -> None:
        for name in ("request_id", "mission_ref", "task_ref", "proposed_agent"):
            _require_id(getattr(self, name), name)
        if not isinstance(self.capability, str) or not self.capability.strip() or any(ord(character) < 32 for character in self.capability):
            raise AuthorizationError("capability must be non-empty safe text")
        for name in ("requested_action", "expected_outcome", "requester"):
            _require_text(getattr(self, name), name)
        _require_texts(self.requested_permissions, "requested_permissions")
        _require_texts(self.authorized_scope, "authorized_scope")
        _require_texts(self.risks, "risks", identifiers=False)
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() is None
        ):
            raise AuthorizationError("created_at must be timezone-aware")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.created_at
        ):
            raise AuthorizationError("expires_at must be after created_at and timezone-aware")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_digest):
            raise AuthorizationError("request_digest must be lowercase SHA-256")
        if not isinstance(self.status, ApprovalStatus) or self.status is not ApprovalStatus.PENDING:
            raise AuthorizationError("new approval requests must start pending")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "mission_ref": self.mission_ref,
            "task_ref": self.task_ref,
            "proposed_agent": self.proposed_agent,
            "capability": self.capability,
            "requested_action": self.requested_action,
            "expected_outcome": self.expected_outcome,
            "requested_permissions": list(self.requested_permissions),
            "authorized_scope": list(self.authorized_scope),
            "risks": list(self.risks),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "request_digest": self.request_digest,
            "status": self.status.value,
            "requester": self.requester,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    request_digest: str
    mission_ref: str
    task_ref: str
    status: ApprovalStatus
    human_identity: str
    decided_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _require_id(self.request_id, "request_id")
        _require_id(self.mission_ref, "mission_ref")
        _require_id(self.task_ref, "task_ref")
        _require_text(self.human_identity, "human_identity")
        _require_text(self.reason, "reason")
        if not isinstance(self.status, ApprovalStatus) or self.status not in {
            ApprovalStatus.APPROVED, ApprovalStatus.DENIED, ApprovalStatus.EXPIRED,
        }:
            raise AuthorizationError("decision must explicitly approve, deny, or expire")
        if (
            not isinstance(self.decided_at, datetime)
            or self.decided_at.tzinfo is None
            or self.decided_at.utcoffset() is None
        ):
            raise AuthorizationError("decided_at must be timezone-aware")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_digest):
            raise AuthorizationError("request_digest must be lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "mission_ref": self.mission_ref,
            "task_ref": self.task_ref,
            "status": self.status.value,
            "human_identity": self.human_identity,
            "decided_at": self.decided_at.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DelegationDecisionRecord:
    request: ApprovalRequest
    decision: ApprovalDecision


class DelegationAuthorizer:
    """Evaluate proposal metadata against parent authority and static readiness."""

    def __init__(
        self,
        discovery: AgentDiscovery,
        verifier: CapabilityVerifier | None = None,
        budget: DelegationBudget | None = None,
    ) -> None:
        if not isinstance(discovery, AgentDiscovery):
            raise AuthorizationError("discovery must be an AgentDiscovery")
        if verifier is not None and not isinstance(verifier, CapabilityVerifier):
            raise AuthorizationError("verifier must be a CapabilityVerifier")
        if budget is not None and not isinstance(budget, DelegationBudget):
            raise AuthorizationError("budget must be a DelegationBudget")
        self.discovery = discovery
        self.discovery.discover_existing()
        self.verifier = verifier or CapabilityVerifier()
        self.budget = budget or DelegationBudget()
        self._decisions: dict[str, DelegationDecisionRecord] = {}

    def evaluate(
        self,
        proposal: DelegationProposal,
        context: DelegationContext,
        *,
        requested_task_type: str,
    ) -> DelegationAuthorizationDecision:
        if not isinstance(context, DelegationContext):
            raise AuthorizationError("context must be a validated DelegationContext")
        if not isinstance(proposal, DelegationProposal):
            raise AuthorizationError("proposal must be a validated DelegationProposal")
        reasons: list[str] = []
        missing: list[str] = []
        if (
            not _stable_identifier(context.mission_ref)
            or not _stable_identifier(context.task_ref)
            or context.parent_task_ref is not None and not _stable_identifier(context.parent_task_ref)
            or not isinstance(context.parent_authorized_permissions, tuple)
            or any(not _stable_identifier(value) for value in context.parent_authorized_permissions)
            or len(context.parent_authorized_permissions) != len(set(context.parent_authorized_permissions))
            or any(isinstance(getattr(context, name), bool) or not isinstance(getattr(context, name), int) or getattr(context, name) < 0
                   for name in ("depth", "existing_task_count", "model_calls", "elapsed_seconds", "cost_microunits"))
            or not isinstance(context.ancestor_task_refs, tuple)
            or any(not _stable_identifier(value) for value in context.ancestor_task_refs)
            or not isinstance(context.approval_required, bool)
            or not isinstance(context.risk_summary, tuple)
            or any(not isinstance(value, str) or not value.strip() for value in context.risk_summary)
        ):
            raise AuthorizationError("delegation context contains invalid or unsafe fields")
        if proposal.parent_mission_ref != context.mission_ref or proposal.parent_task_ref != context.task_ref:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, None, ("proposal parent references do not match the authorized mission/task",), (), False)
        if context.parent_task_ref and proposal.parent_task_ref in context.ancestor_task_refs:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, None, ("delegation cycle detected in parent ancestry",), (), False)
        if not isinstance(requested_task_type, str) or not self._stable_task(requested_task_type):
            return self._decision(DelegationDecisionState.UNKNOWN, context, proposal, None, ("requested task type is not a stable identifier",), ("valid task type",), False)
        if context.depth + 1 > self.budget.max_depth or context.depth + 1 > 2:
            missing.append("maximum delegation depth of 2 exceeded")
        if context.existing_task_count + 1 > self.budget.max_tasks:
            missing.append("task budget exceeded")
        if context.model_calls > self.budget.max_model_calls:
            missing.append("model-call budget exceeded")
        if context.elapsed_seconds > self.budget.max_duration_seconds:
            missing.append("time budget exceeded")
        if context.cost_microunits > self.budget.max_cost_microunits:
            missing.append("cost budget exceeded")
        if missing:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, None, ("delegation limits must be satisfied before consideration",), tuple(sorted(missing)), False)

        try:
            agents = self.discovery.lookup_capability(proposal.proposed_specialist_capability)
        except CapabilityNotFoundError:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, None, ("proposed capability is not registered",), ("registered specialist capability",), False)

        task_matches = tuple(agent for agent in agents if requested_task_type in agent.contract.supported_tasks)
        if not task_matches:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, agents[0].contract.agent_id, ("requested task type is not declared by the specialist",), ("supported task type",), False)
        # Keep source-backed candidates for permission checks even when not executable.
        eligible = [
            (agent, self.verifier.verify(agent))
            for agent in task_matches
            if self.verifier.verify(agent).status is not CapabilityStatus.UNKNOWN
        ]
        if not eligible:
            return self._decision(DelegationDecisionState.UNKNOWN, context, proposal, None, ("no registered agent has sufficient verified evidence for the requested task",), ("understood capability and supported task",), False)
        agent, verification = sorted(eligible, key=lambda pair: pair[0].contract.agent_id)[0]

        declared_permissions = set(agent.contract.permissions)
        requested_permissions = set(proposal.permission_requirements)
        parent_permissions = set(context.parent_authorized_permissions)
        undeclared_permissions = requested_permissions - declared_permissions
        if undeclared_permissions:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, agent.contract.agent_id, ("proposal requests permissions not declared by the specialist",), tuple(sorted(undeclared_permissions)), False)
        escalated_permissions = requested_permissions - parent_permissions
        if escalated_permissions:
            return self._decision(DelegationDecisionState.BLOCKED, context, proposal, agent.contract.agent_id, ("permission escalation beyond parent-authorized scope is forbidden",), tuple(sorted(escalated_permissions)), False)
        if context.approval_required:
            reasons.append("parent context requires additional human approval")
        if not requested_permissions:
            reasons.append("no additional permissions requested")
        state = DelegationDecisionState.REQUIRES_HUMAN_APPROVAL if context.approval_required else DelegationDecisionState.APPROVABLE
        missing_execution = tuple(
            requirement for requirement in verification.missing_requirements
            if requirement.startswith("execution integration is unavailable")
        )
        if not verification.execution_ready:
            reasons.append("specialist metadata is valid but execution integration is unavailable")
        return self._decision(
            state, context, proposal, agent.contract.agent_id, tuple(reasons), missing_execution,
            context.approval_required,
        )

    def create_approval_request(
        self,
        proposal: DelegationProposal,
        context: DelegationContext,
        *,
        requested_agent: str,
        requested_action: str,
        expected_outcome: str,
        risks: tuple[str, ...],
        now: datetime | None = None,
        validity: timedelta = timedelta(minutes=15),
        requester: str = "orchestration",
    ) -> ApprovalRequest:
        if not isinstance(proposal, DelegationProposal) or not isinstance(context, DelegationContext):
            raise AuthorizationError("proposal and context must be validated contracts")
        if (
            not _stable_identifier(context.mission_ref)
            or not _stable_identifier(context.task_ref)
            or not isinstance(context.parent_authorized_permissions, tuple)
            or any(not _stable_identifier(value) for value in context.parent_authorized_permissions)
            or len(context.parent_authorized_permissions) != len(set(context.parent_authorized_permissions))
        ):
            raise AuthorizationError("delegation context contains invalid approval-binding fields")
        now = now or datetime.now(timezone.utc)
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or not isinstance(validity, timedelta)
            or validity <= timedelta(0)
            or validity > timedelta(hours=24)
        ):
            raise AuthorizationError("approval validity must be positive, at most 24 hours, and timezone-aware")
        if not isinstance(requested_agent, str) or not _stable_identifier(requested_agent):
            raise AuthorizationError("requested_agent must be a stable identifier")
        _require_text(requested_action, "requested_action")
        _require_text(expected_outcome, "expected_outcome")
        _require_text(requester, "requester")
        if not isinstance(risks, tuple):
            raise AuthorizationError("risks must be a tuple of safe text")
        _require_texts(risks, "risks", identifiers=False)
        requested = tuple(sorted(set(proposal.permission_requirements)))
        scope = tuple(sorted(set(context.parent_authorized_permissions)))
        if not set(requested).issubset(scope):
            raise AuthorizationError("approval request cannot expand parent-authorized permissions")
        data = {
            "mission_ref": context.mission_ref,
            "task_ref": context.task_ref,
            "agent": requested_agent,
            "capability": proposal.proposed_specialist_capability,
            "requested_action": requested_action,
            "expected_outcome": expected_outcome,
            "permissions": requested,
            "scope": scope,
            "risks": tuple(sorted(set(risks))),
            "expires_at": (now + validity).isoformat(),
        }
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        request_id = "approval-" + digest[:24]
        if request_id in self._decisions:
            raise AuthorizationError("approval request identifier has already been decided")
        return ApprovalRequest(
            request_id=request_id,
            mission_ref=context.mission_ref,
            task_ref=context.task_ref,
            proposed_agent=requested_agent,
            capability=proposal.proposed_specialist_capability,
            requested_action=requested_action,
            expected_outcome=expected_outcome,
            requested_permissions=requested,
            authorized_scope=scope,
            risks=tuple(sorted(set(risks))),
            created_at=now,
            expires_at=now + validity,
            request_digest=digest,
            requester=requester,
        )

    def decide(
        self,
        request: ApprovalRequest,
        *,
        approved: bool,
        human_identity: str,
        reason: str,
        now: datetime | None = None,
    ) -> ApprovalDecision:
        if not isinstance(approved, bool):
            raise AuthorizationError("approval must be an explicit boolean decision")
        if not isinstance(reason, str):
            raise AuthorizationError("decision reason must be safe text")
        _require_text(reason, "reason")
        now = now or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise AuthorizationError("decision time must be timezone-aware")
        if not isinstance(request, ApprovalRequest):
            raise AuthorizationError("request must be a validated approval request")
        if (
            not isinstance(human_identity, str)
            or not human_identity.strip()
            or human_identity == request.proposed_agent
        ):
            raise AuthorizationError("approval must come from an identified human, never the proposed agent")
        prior = self._decisions.get(request.request_id)
        if prior is not None:
            raise AuthorizationError("approval request has already been decided; replay is forbidden")
        if now >= request.expires_at:
            status = ApprovalStatus.EXPIRED
            decision_reason = "approval request expired before an explicit decision"
        elif approved:
            status = ApprovalStatus.APPROVED
            decision_reason = reason
        else:
            status = ApprovalStatus.DENIED
            decision_reason = reason
        decision = ApprovalDecision(
            request_id=request.request_id,
            request_digest=request.request_digest,
            mission_ref=request.mission_ref,
            task_ref=request.task_ref,
            status=status,
            human_identity=human_identity,
            decided_at=now,
            reason=decision_reason,
        )
        self._decisions[request.request_id] = DelegationDecisionRecord(request, decision)
        return decision

    def validate_approval_for(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        *,
        mission_ref: str,
        task_ref: str,
        agent_id: str,
        capability: str,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        if (
            not isinstance(request, ApprovalRequest)
            or not isinstance(decision, ApprovalDecision)
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return False
        return bool(
            decision.status is ApprovalStatus.APPROVED
            and decision.request_id == request.request_id
            and decision.request_digest == request.request_digest
            and decision.mission_ref == mission_ref == request.mission_ref
            and decision.task_ref == task_ref == request.task_ref
            and request.proposed_agent == agent_id
            and request.capability == capability
            and request.status is ApprovalStatus.PENDING
            and request.created_at <= now < request.expires_at
        )

    @staticmethod
    def _stable_task(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))

    @staticmethod
    def _decision(
        state: DelegationDecisionState,
        context: DelegationContext,
        proposal: DelegationProposal,
        agent_id: str | None,
        reasons: tuple[str, ...],
        missing: tuple[str, ...],
        approval: bool,
    ) -> DelegationAuthorizationDecision:
        return DelegationAuthorizationDecision(
            state=state,
            mission_ref=context.mission_ref,
            task_ref=context.task_ref,
            agent_id=agent_id,
            capability=proposal.proposed_specialist_capability,
            reasons=reasons,
            missing_requirements=missing,
            required_human_approval=approval,
            authorized_permissions=(),
            execution_permitted=False,
        )


def _stable_identifier(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))


def _require_id(value: str, label: str) -> None:
    if not _stable_identifier(value):
        raise AuthorizationError(f"{label} must be a stable identifier")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(ord(character) < 32 for character in value):
        raise AuthorizationError(f"{label} must be non-empty safe text")


def _require_texts(values: tuple[str, ...], label: str, *, identifiers: bool = True) -> None:
    if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
        raise AuthorizationError(f"{label} must be a tuple of strings")
    if len(values) != len(set(values)):
        raise AuthorizationError(f"{label} must be a unique tuple")
    for value in values:
        if identifiers:
            _require_id(value, f"{label} item")
        else:
            _require_text(value, f"{label} item")
