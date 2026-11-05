"""Deterministic, non-executing agent selection and mission task planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from control_plane.agent_contracts import CapabilityNotFoundError, CapabilityStatus, CapabilityRegistry
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import AgentVerification, CapabilityVerifier


class PlanningError(ValueError):
    """Mission planning input or plan invariants are invalid."""


@dataclass(frozen=True)
class MissionReference:
    mission_id: str
    objective: str
    intended_outcome: str
    success_criteria: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    authorization_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCandidate:
    agent_id: str
    capability: str
    task_type: str
    verification_status: CapabilityStatus
    execution_ready: bool
    required_permissions: tuple[str, ...]
    available_permissions: tuple[str, ...]
    selected: bool
    score: int
    rationale: tuple[str, ...]
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capability": self.capability,
            "task_type": self.task_type,
            "verification_status": self.verification_status.value,
            "execution_ready": self.execution_ready,
            "required_permissions": list(self.required_permissions),
            "available_permissions": list(self.available_permissions),
            "selected": self.selected,
            "score": self.score,
            "rationale": list(self.rationale),
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True)
class MissionTask:
    task_id: str
    description: str
    required_capabilities: tuple[str, ...]
    task_type: str
    proposed_agent: str | None
    dependencies: tuple[str, ...]
    required_permissions: tuple[str, ...]
    required_approvals: tuple[str, ...]
    missing_prerequisites: tuple[str, ...]
    expected_deliverables: tuple[str, ...]
    execution_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
            "task_type": self.task_type,
            "proposed_agent": self.proposed_agent,
            "dependencies": list(self.dependencies),
            "required_permissions": list(self.required_permissions),
            "required_approvals": list(self.required_approvals),
            "missing_prerequisites": list(self.missing_prerequisites),
            "expected_deliverables": list(self.expected_deliverables),
            "execution_readiness": self.execution_ready,
        }


@dataclass(frozen=True)
class MissionPlan:
    mission_id: str
    objective: str
    intended_outcome: str
    success_criteria: tuple[str, ...]
    tasks: tuple[MissionTask, ...]
    candidates: tuple[AgentCandidate, ...]
    missing_prerequisites: tuple[str, ...]
    required_permissions: tuple[str, ...]
    required_approvals: tuple[str, ...]
    execution_ready: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_ref": self.mission_id,
            "objective": self.objective,
            "intended_outcome": self.intended_outcome,
            "success_criteria": list(self.success_criteria),
            "ordered_tasks": [task.to_dict() for task in self.tasks],
            "agent_candidates": [candidate.to_dict() for candidate in self.candidates],
            "missing_prerequisites": list(self.missing_prerequisites),
            "required_permissions": list(self.required_permissions),
            "required_approvals": list(self.required_approvals),
            "execution_readiness": {
                "ready": self.execution_ready,
                "reason": "One or more assigned agents lack command-capable execution integration or required permissions."
                if not self.execution_ready
                else "All planned tasks have separately verified execution prerequisites.",
            },
            "status": self.status,
            "execution_performed": False,
        }


@dataclass(frozen=True)
class DelegationProposal:
    parent_mission_ref: str
    parent_task_ref: str
    proposed_specialist_capability: str
    reason: str
    expected_subtask_result: str
    permission_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        for label in ("parent_mission_ref", "parent_task_ref", "proposed_specialist_capability"):
            if not _stable_id(getattr(self, label)):
                raise PlanningError(f"{label} must be a stable non-empty identifier")
        for label in ("reason", "expected_subtask_result"):
            if not isinstance(getattr(self, label), str) or not getattr(self, label).strip():
                raise PlanningError(f"{label} must be non-empty")
        if not isinstance(self.permission_requirements, tuple) or any(not _stable_id(value) for value in self.permission_requirements):
            raise PlanningError("permission_requirements must be a tuple of stable identifiers")

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_mission_ref": self.parent_mission_ref,
            "parent_task_ref": self.parent_task_ref,
            "proposed_specialist_capability": self.proposed_specialist_capability,
            "reason": self.reason,
            "expected_subtask_result": self.expected_subtask_result,
            "permission_requirements": list(self.permission_requirements),
            "authorized": False,
            "executed": False,
        }


def _stable_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))


def mission_reference_from_definition(definition: Any) -> MissionReference:
    """Adapt an already validated Brain MissionDefinition by its public fields.

    The Brain worktree is never imported. Accepting its JSON representation
    avoids duplicate mission schemas and preserves cross-worktree isolation.
    """
    raw = definition.to_dict() if hasattr(definition, "to_dict") else definition
    required = {
        "mission_id", "original_objective", "intended_outcome", "success_criteria",
        "required_capabilities", "constraints", "dependencies", "authorization_requirements",
        "schema_version", "status", "execution_readiness",
    }
    if not isinstance(raw, Mapping) or not required.issubset(raw):
        raise PlanningError("mission input does not match the validated MissionDefinition contract")
    if raw.get("schema_version") != 1:
        raise PlanningError("unsupported MissionDefinition schema_version")
    for field in ("success_criteria", "required_capabilities", "constraints", "dependencies", "authorization_requirements"):
        if not isinstance(raw[field], (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in raw[field]):
            raise PlanningError(f"MissionDefinition {field} must be a sequence of non-empty strings")
    if not _stable_id(raw["mission_id"]):
        raise PlanningError("MissionDefinition mission_id is invalid")
    if not raw["success_criteria"]:
        raise PlanningError("MissionDefinition must contain success criteria")
    return MissionReference(
        mission_id=raw["mission_id"],
        objective=_text(raw["original_objective"], "original_objective"),
        intended_outcome=_text(raw["intended_outcome"], "intended_outcome"),
        success_criteria=tuple(raw["success_criteria"]),
        required_capabilities=tuple(raw["required_capabilities"]),
        constraints=tuple(raw["constraints"]),
        dependencies=tuple(raw["dependencies"]),
        authorization_requirements=tuple(raw["authorization_requirements"]),
    )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningError(f"{label} must be non-empty text")
    return value.strip()


class AgentSelector:
    """Rank matching discovered agents with a documented deterministic score."""

    def __init__(
        self,
        discovery: AgentDiscovery,
        verifier: CapabilityVerifier | None = None,
        available_permissions: tuple[str, ...] = (),
    ) -> None:
        self.discovery = discovery
        self.verifier = verifier or CapabilityVerifier()
        self.available_permissions = frozenset(available_permissions)
        self._verification: dict[str, AgentVerification] = {}

    def select(self, capability: str, task_type: str) -> tuple[AgentCandidate, ...]:
        if not _stable_id(capability) or not _stable_id(task_type):
            raise PlanningError("capability and task_type must be stable identifiers")
        candidates = []
        try:
            agents = self.discovery.lookup_capability(capability)
        except CapabilityNotFoundError:
            return ()
        for agent in agents:
            verification = self._verification.setdefault(agent.contract.agent_id, self.verifier.verify(agent))
            tasks_match = task_type in agent.contract.supported_tasks
            permissions_missing = tuple(sorted(set(agent.contract.permissions) - self.available_permissions))
            reasons = []
            if not tasks_match:
                reasons.append("requested task type is not declared as supported")
            if verification.status is CapabilityStatus.UNKNOWN:
                reasons.append("capability evidence or required metadata is unknown")
            if verification.status is CapabilityStatus.UNAVAILABLE:
                reasons.append("agent integration is unavailable")
            if not verification.execution_ready:
                reasons.append("execution integration is not ready")
            if permissions_missing:
                reasons.append("required permissions are not available: " + ", ".join(permissions_missing))
            selected = tasks_match and verification.status in {CapabilityStatus.UNDERSTOOD, CapabilityStatus.VERIFIED} and not permissions_missing and verification.execution_ready
            rationale = [
                "declared capability matches requested capability",
                "deterministic ranking: verification status, task match, readiness, then agent ID",
            ]
            score = (100 if verification.status is CapabilityStatus.VERIFIED else 50) + (20 if tasks_match else 0) + (10 if verification.execution_ready else 0) - (5 * len(permissions_missing))
            candidates.append(
                AgentCandidate(
                    agent_id=agent.contract.agent_id,
                    capability=capability,
                    task_type=task_type,
                    verification_status=verification.status,
                    execution_ready=verification.execution_ready and not permissions_missing,
                    required_permissions=agent.contract.permissions,
                    available_permissions=tuple(sorted(self.available_permissions)),
                    selected=selected,
                    score=score,
                    rationale=tuple(rationale),
                    rejection_reasons=tuple(reasons),
                )
            )
        return tuple(sorted(candidates, key=lambda item: (-item.score, item.agent_id)))


class MissionPlanner:
    """Generate explainable plans from MissionDefinition-compatible inputs."""

    def __init__(
        self,
        discovery: AgentDiscovery | None = None,
        verifier: CapabilityVerifier | None = None,
        available_permissions: tuple[str, ...] = (),
    ) -> None:
        self.discovery = discovery or AgentDiscovery(CapabilityRegistry())
        self.verifier = verifier or CapabilityVerifier()
        self.selector = AgentSelector(self.discovery, self.verifier, available_permissions)
        self.available_permissions = frozenset(available_permissions)

    def plan(self, mission: Any) -> MissionPlan:
        reference = mission_reference_from_definition(mission)
        self.discovery.discover_existing()
        capabilities = reference.required_capabilities
        if not capabilities:
            capabilities = ("mission-planning",)
        tasks: list[MissionTask] = []
        candidates_all: list[AgentCandidate] = []
        prerequisites: set[str] = set(reference.constraints)
        approval_requirements = set(reference.authorization_requirements)
        for index, capability in enumerate(capabilities, start=1):
            match = self._task_for_capability(capability)
            if match is None:
                task_type = "unsupported-task"
                expected = ("Capability support assessment",)
                capability_matches: tuple[AgentCandidate, ...] = ()
                prerequisites.add(f"No registered agent declares required capability: {capability}.")
            else:
                task_type, expected = match
                capability_matches = self.selector.select(capability, task_type)
                candidates_all.extend(capability_matches)
                if not capability_matches:
                    prerequisites.add(f"No registered agent provides required capability: {capability}.")
            eligible = next((candidate for candidate in capability_matches if candidate.selected), None)
            if eligible is None and capability_matches:
                reasons = sorted({reason for candidate in capability_matches for reason in candidate.rejection_reasons})
                prerequisites.update(reasons)
            required_permissions = eligible.required_permissions if eligible else ()
            missing_permissions = set(required_permissions) - self.available_permissions
            prerequisites.update(f"Required permission unavailable: {permission}." for permission in missing_permissions)
            prerequisites.add("No execution integration is available for the selected existing agents.")
            tasks.append(
                MissionTask(
                    task_id=f"{reference.mission_id}.task-{index:02d}",
                    description=f"Address required capability: {capability.replace('-', ' ')}.",
                    required_capabilities=(capability,),
                    task_type=task_type,
                    proposed_agent=eligible.agent_id if eligible else None,
                    dependencies=(f"{reference.mission_id}.task-{index-1:02d}",) if index > 1 else reference.dependencies,
                    required_permissions=tuple(sorted(required_permissions)),
                    required_approvals=reference.authorization_requirements,
                    missing_prerequisites=tuple(sorted(prerequisites)),
                    expected_deliverables=expected,
                    execution_ready=bool(eligible and eligible.execution_ready and not missing_permissions),
                )
            )
        ready = bool(tasks) and all(task.execution_ready for task in tasks) and not prerequisites
        status = "proposed" if ready else "proposed_execution_unavailable"
        return MissionPlan(
            mission_id=reference.mission_id,
            objective=reference.objective,
            intended_outcome=reference.intended_outcome,
            success_criteria=reference.success_criteria,
            tasks=tuple(tasks),
            candidates=tuple(sorted(candidates_all, key=lambda item: (-item.score, item.agent_id, item.capability))),
            missing_prerequisites=tuple(sorted(prerequisites)),
            required_permissions=tuple(sorted({permission for task in tasks for permission in task.required_permissions})),
            required_approvals=tuple(sorted(approval_requirements)),
            execution_ready=ready,
            status=status,
        )

    @staticmethod
    def _task_for_capability(capability: str) -> tuple[str, tuple[str, ...]] | None:
        mapping = {
            "local-python-static-analysis": ("inspect-local-python-source", ("Static source analysis summary",)),
            "fuzzing-plan-generation": ("form-local-fuzzing-plan", ("Structured local fuzzing plan",)),
            "seed-pool-generation": ("initialize-byte-seeds", ("Seed payload set",)),
            "genetic-payload-mutation": ("mutate-byte-payload", ("Mutated byte payload",)),
            "coverage-feedback-scoring": ("score-target-feedback", ("Feedback score",)),
            "bounded-campaign-preflight": ("prepare-campaign", ("Campaign preflight metadata",)),
            "attempt-budget-enforcement": ("enforce-attempt-budget", ("Budget enforcement result",)),
            "boundary-approval-gating": ("request-manual-approval", ("Approval gate status",)),
        }
        return mapping.get(capability)


def propose_delegation(
    parent_mission_ref: str,
    parent_task_ref: str,
    proposed_specialist_capability: str,
    reason: str,
    expected_subtask_result: str,
    permission_requirements: tuple[str, ...] = (),
) -> DelegationProposal:
    """Validate proposal metadata only; no authorization or execution occurs."""
    return DelegationProposal(
        parent_mission_ref,
        parent_task_ref,
        proposed_specialist_capability,
        reason,
        expected_subtask_result,
        permission_requirements,
    )
