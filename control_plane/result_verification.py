"""Deterministic, evidence-based verification of synthetic task results.

This module assesses recorded results only. It never executes, retries, or
reassigns work, and agent-reported success is not treated as evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from control_plane.delegation_coordinator import ChildTaskStatus, DelegatedTask
from control_plane.execution_engine import ExecutionResult, ExecutionState
from control_plane.mission_planning import MissionReference, MissionTask


class VerificationError(ValueError):
    """Invalid mission verification input or contract."""


class MissionOutcomeStatus(str, Enum):
    COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    NEEDS_MORE_WORK = "NEEDS_MORE_WORK"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    mission_id: str
    task_id: str
    claim: str
    source: str
    verified: bool
    content: Any
    provenance: str

    def __post_init__(self) -> None:
        for name in ("evidence_id", "mission_id", "task_id"):
            if not _stable_id(getattr(self, name)):
                raise VerificationError(f"{name} must be a stable identifier")
        for name in ("claim", "source", "provenance"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or _unsafe_text(value):
                raise VerificationError(f"{name} must be non-empty safe text")
        if not isinstance(self.verified, bool):
            raise VerificationError("verified must be a boolean")
        _canonical_json(self.content)

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "mission_id": self.mission_id,
                "task_id": self.task_id, "claim": self.claim, "source": self.source,
                "verified": self.verified, "content": self.content,
                "provenance": self.provenance}


@dataclass(frozen=True)
class TaskVerificationInput:
    task: MissionTask | DelegatedTask
    result: ExecutionResult | None
    evidences: tuple[Evidence, ...] = ()
    dependencies: tuple[str, ...] = ()
    expected_schema: Mapping[str, Any] | None = None
    agent_capabilities: tuple[str, ...] = ()
    authorized_permissions: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CriterionAssessment:
    criterion: str
    satisfied: bool
    evidence_ids: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {"criterion": self.criterion, "satisfied": self.satisfied,
                "evidence_ids": list(self.evidence_ids), "rationale": self.rationale}


@dataclass(frozen=True)
class TaskAssessment:
    task_id: str
    status: str
    completed: bool
    verified_deliverables: tuple[str, ...]
    criteria: tuple[CriterionAssessment, ...]
    evidence_ids: tuple[str, ...]
    uncertainties: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status, "completed": self.completed,
                "verified_deliverables": list(self.verified_deliverables),
                "criteria": [item.to_dict() for item in self.criteria],
                "evidence_ids": list(self.evidence_ids), "uncertainties": list(self.uncertainties)}


@dataclass(frozen=True)
class MissionOutcome:
    mission_ref: str
    status: MissionOutcomeStatus
    verified_deliverables: tuple[str, ...]
    success_criteria: tuple[CriterionAssessment, ...]
    completed_tasks: tuple[str, ...]
    incomplete_tasks: tuple[str, ...]
    failed_tasks: tuple[str, ...]
    evidence_references: tuple[str, ...]
    uncertainties: tuple[str, ...]
    limitations: tuple[str, ...]
    recommendations: tuple[str, ...]

    @property
    def completion_status(self) -> str:
        return self.status.value

    def to_dict(self) -> dict[str, Any]:
        return {"mission_ref": self.mission_ref, "status": self.status.value,
                "completion_status": self.completion_status,
                "verified_deliverables": list(self.verified_deliverables),
                "success_criteria": [item.to_dict() for item in self.success_criteria],
                "completed_tasks": list(self.completed_tasks),
                "incomplete_tasks": list(self.incomplete_tasks),
                "failed_tasks": list(self.failed_tasks),
                "evidence_references": list(self.evidence_references),
                "uncertainties": list(self.uncertainties), "limitations": list(self.limitations),
                "recommendations": list(self.recommendations)}

    def brain_summary(self) -> dict[str, Any]:
        """Return concise user-facing language without internal IDs or diagnostics."""
        total = len(self.success_criteria)
        satisfied = sum(item.satisfied for item in self.success_criteria)
        if self.status is MissionOutcomeStatus.COMPLETED_VERIFIED:
            text = "I received the agent results and verified all required outcomes. This mission is complete."
        elif self.status is MissionOutcomeStatus.PARTIALLY_COMPLETED:
            text = f"I received the agent results and verified {satisfied} of {total} required outcomes. The remaining requirement lacks supporting evidence or validation."
        elif self.status is MissionOutcomeStatus.BLOCKED:
            text = "I could not verify the required outcomes because prerequisite work is blocked. The mission is not complete."
        elif self.status is MissionOutcomeStatus.FAILED:
            text = "The recorded work failed, so I cannot consider this mission complete."
        elif self.status is MissionOutcomeStatus.UNKNOWN:
            text = "There is not enough validated result evidence to determine whether this mission is complete."
        else:
            text = f"I verified {satisfied} of {total} required outcomes. More validated work or evidence is needed before completion."
        return {"status": self.status.value, "message": text,
                "verified_criteria": satisfied, "required_criteria": total,
                "follow_up_recommended": bool(self.recommendations)}


class ResultVerifier:
    """Pure deterministic result validator and mission assessor."""

    def verify_task(self, mission: MissionReference, item: TaskVerificationInput) -> TaskAssessment:
        if not isinstance(mission, MissionReference) or not isinstance(item, TaskVerificationInput):
            raise VerificationError("validated mission and task verification input are required")
        task_id = _task_id(item.task)
        expected_mission = mission.mission_id
        uncertainties: list[str] = []
        if not isinstance(item.evidences, tuple):
            raise VerificationError("evidences must be a tuple")
        evidence = item.evidences
        if any(not isinstance(entry, Evidence) for entry in evidence):
            raise VerificationError("evidences must contain validated Evidence records")
        evidence_ids = tuple(sorted({entry.evidence_id for entry in evidence}))
        duplicate_evidence = len(evidence_ids) != len(evidence)
        if duplicate_evidence:
            uncertainties.append("duplicate evidence identifiers")
        if any(entry.mission_id != expected_mission or entry.task_id != task_id for entry in evidence):
            uncertainties.append("evidence mission or task binding mismatch")
        valid_evidence = tuple(entry for entry in evidence
                               if entry.mission_id == expected_mission and entry.task_id == task_id
                               and entry.verified)
        contradictory_claims = {entry.claim.removeprefix("not:").removeprefix("contradicts:")
                                for entry in valid_evidence if entry.claim.startswith(("not:", "contradicts:"))}
        result = item.result
        status = "MISSING"
        output: Any = None
        if item.result is not None and not isinstance(item.result, ExecutionResult):
            status = "INVALID_RESULT"
            uncertainties.append("result does not match the execution result contract")
        if (not isinstance(item.agent_capabilities, tuple)
                or any(not _stable_id(value) for value in item.agent_capabilities)
                or not isinstance(item.authorized_permissions, tuple)
                or any(not _stable_id(value) for value in item.authorized_permissions)
                or not isinstance(item.required_permissions, tuple)
                or any(not _stable_id(value) for value in item.required_permissions)
                or not isinstance(item.dependencies, tuple)
                or any(not _stable_id(value) for value in item.dependencies)):
            raise VerificationError("capabilities, permissions, and dependencies must be identifier tuples")
        if item.expected_schema is not None and not isinstance(item.expected_schema, Mapping):
            raise VerificationError("expected output schema must be a mapping")
        if not isinstance(item.task, (MissionTask, DelegatedTask)):
            raise VerificationError("task must use a supported mission or delegated task contract")
        if isinstance(item.task, MissionTask) and item.task.proposed_agent is None:
            status = "BLOCKED"
            uncertainties.append("task has no assigned agent")
        if isinstance(item.task, MissionTask) and item.task.proposed_agent is not None:
            if not item.agent_capabilities or not set(item.task.required_capabilities).issubset(item.agent_capabilities):
                status = "OUT_OF_SCOPE"
                uncertainties.append("agent capability does not cover the assigned task")
        if isinstance(item.task, DelegatedTask):
            if item.task.status is not ChildTaskStatus.SUCCEEDED:
                status = item.task.status.value
                uncertainties.append("delegated task status is not successful")
            if item.task.mission_id != expected_mission:
                status = "REFERENCE_MISMATCH"
                uncertainties.append("delegated task mission reference mismatch")
            if (not item.agent_capabilities
                    or item.task.capability not in item.agent_capabilities):
                status = "OUT_OF_SCOPE"
                uncertainties.append("agent capability does not cover the delegated task")
        if isinstance(result, ExecutionResult):
            if result.mission_id != expected_mission or result.task_id != task_id:
                status = "REFERENCE_MISMATCH"
                uncertainties.append("result mission or task reference mismatch")
            elif result.state is ExecutionState.TIMED_OUT:
                status = "TIMED_OUT"
                uncertainties.append("execution timed out")
            elif result.state is ExecutionState.FAILED:
                status = "FAILED"
                uncertainties.append("execution reported failure")
            elif result.state is not ExecutionState.COMPLETED:
                status = result.state.value.upper()
                uncertainties.append("execution did not complete")
            elif result.error_category is not None or result.error_message is not None:
                status = "FAILED"
                uncertainties.append("completed execution result contains error details")
            else:
                status = "COMPLETED"
                output = result.output
                try:
                    _canonical_json(output)
                    _validate_schema(output, item.expected_schema)
                except VerificationError as error:
                    status = "INVALID_RESULT"
                    uncertainties.append(str(error))
                declared = tuple(getattr(item.task, "expected_deliverables", ()))
                absent = tuple(deliverable for deliverable in declared
                               if not _has_deliverable(output, deliverable, valid_evidence))
                if absent:
                        uncertainties.append("missing deliverables: " + ", ".join(absent))
                required_capabilities = (getattr(item.task, "required_capabilities", ())
                                         or ((item.task.capability,) if isinstance(item.task, DelegatedTask) else ()))
                if not item.agent_capabilities or not set(required_capabilities).issubset(item.agent_capabilities):
                    status = "OUT_OF_SCOPE"
                    uncertainties.append("agent capability does not cover the task")
                if not set(item.required_permissions).issubset(item.authorized_permissions):
                    status = "OUT_OF_SCOPE"
                    uncertainties.append("required task permissions exceed authorized scope")
                if item.dependencies:
                    uncertainties.append("dependency completion was not established")
                    status = "DEPENDENCY_BLOCKED"
        if result is None:
            uncertainties.append("no execution result was recorded")
        elif not isinstance(result, ExecutionResult):
            uncertainties.append("no valid execution result was recorded")
        # Evidence claims count only when evidence is marked verified, bound to this task,
        # and the expected criterion/deliverable is directly represented in its claim.
        criteria: list[CriterionAssessment] = []
        for criterion in mission.success_criteria:
            matches = tuple(entry for entry in valid_evidence if entry.claim == criterion
                            and _output_supports(output, criterion))
            contradictions = _contradictions(valid_evidence, criterion)
            if contradictions:
                uncertainties.append("contradictory evidence for a success criterion")
            criteria.append(CriterionAssessment(criterion, bool(matches) and not contradictions,
                tuple(entry.evidence_id for entry in matches) if not contradictions else (),
                "supported by bound, verified evidence" if matches and not contradictions
                else "no validated supporting evidence" if not contradictions
                else "validated evidence is contradictory"))
        deliverables = tuple(getattr(item.task, "expected_deliverables", ()))
        verified_deliverables = tuple(name for name in deliverables
            if _has_deliverable(output, name, valid_evidence)
            and not _contradictions(valid_evidence, name))
        completed = (status == "COMPLETED" and not uncertainties
                     and not duplicate_evidence
                     and not contradictory_claims
                     and (not isinstance(item.task, DelegatedTask)
                          or item.task.status is ChildTaskStatus.SUCCEEDED)
                     and all(assessment.satisfied for assessment in criteria)
                     and len(verified_deliverables) == len(deliverables))
        return TaskAssessment(task_id, status, completed, verified_deliverables, tuple(criteria),
                              evidence_ids, tuple(_dedupe(uncertainties)))

    def assess_mission(self, mission: MissionReference, tasks: Sequence[TaskVerificationInput]) -> MissionOutcome:
        if not isinstance(mission, MissionReference):
            raise VerificationError("mission must be a validated MissionReference")
        if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
            raise VerificationError("tasks must be a sequence of task verification inputs")
        assessments = tuple(self.verify_task(mission, item) for item in tasks)
        mission_task_ids = {_task_id(item.task) for item in tasks if isinstance(item.task, MissionTask)}
        by_id = {_task_id(item.task): assessment for item, assessment in zip(tasks, assessments)}
        if len(by_id) != len(assessments):
            raise VerificationError("task identifiers must be unique")
        # Declared dependencies are satisfied only by tasks verified in this assessment.
        updated: list[TaskAssessment] = []
        for item, assessment in zip(tasks, assessments):
            declared_dependencies = tuple(item.dependencies) + tuple(
                dependency for dependency in getattr(item.task, "dependencies", ())
                if dependency in mission_task_ids)
            if any(dependency not in by_id or not by_id[dependency].completed
                   for dependency in declared_dependencies):
                assessment = TaskAssessment(
                    assessment.task_id, "DEPENDENCY_BLOCKED", False, (), assessment.criteria,
                    assessment.evidence_ids,
                    _dedupe(assessment.uncertainties + ("dependency task is missing or not verified",)))
            updated.append(assessment)
        assessments = tuple(updated)
        by_id = {assessment.task_id: assessment for assessment in assessments}
        criteria: list[CriterionAssessment] = []
        for criterion in mission.success_criteria:
            supporting = tuple(sorted({evidence_id for assessment in assessments
                                       for entry in assessment.criteria
                                       if entry.criterion == criterion and entry.satisfied
                                       for evidence_id in entry.evidence_ids}))
            criteria.append(CriterionAssessment(criterion, bool(supporting), supporting,
                "supported by validated task evidence" if supporting else "no validated supporting evidence"))
        completed_tasks = tuple(sorted(item.task_id for item in assessments if item.completed))
        failed_tasks = tuple(sorted(item.task_id for item in assessments if item.status == "FAILED"))
        blocked_tasks = tuple(item for item in assessments if item.status in {"DEPENDENCY_BLOCKED", "OUT_OF_SCOPE", "BLOCKED"})
        incomplete_tasks = tuple(sorted(item.task_id for item in assessments if not item.completed))
        all_criteria = bool(criteria) and all(item.satisfied for item in criteria)
        task_count = len(tasks)
        if all_criteria and task_count and not incomplete_tasks:
            status = MissionOutcomeStatus.COMPLETED_VERIFIED
        elif blocked_tasks or any(item.status == "BLOCKED" for item in assessments):
            status = MissionOutcomeStatus.BLOCKED
        elif failed_tasks and not completed_tasks:
            status = MissionOutcomeStatus.FAILED
        elif completed_tasks and (incomplete_tasks or not all_criteria):
            status = MissionOutcomeStatus.PARTIALLY_COMPLETED
        elif not task_count:
            status = MissionOutcomeStatus.UNKNOWN
        else:
            status = MissionOutcomeStatus.NEEDS_MORE_WORK
        recs: list[str] = []
        if any("evidence" in uncertainty or "deliverables" in uncertainty for assessment in assessments for uncertainty in assessment.uncertainties):
            recs.append("Recheck or obtain supporting evidence for incomplete requirements.")
        if failed_tasks or incomplete_tasks:
            recs.append("Review task assignment and propose a revised bounded task plan.")
        if status in {MissionOutcomeStatus.BLOCKED, MissionOutcomeStatus.UNKNOWN}:
            recs.append("Escalate unresolved prerequisites or decisions to the Brain.")
        if status is MissionOutcomeStatus.FAILED:
            recs.append("Review the failure and present any recovery proposal to the Brain for approval.")
        if status is MissionOutcomeStatus.NEEDS_MORE_WORK:
            recs.append("Consider whether additional specialist research should be proposed for Brain approval.")
        uncertainties = tuple(_dedupe(value for assessment in assessments for value in assessment.uncertainties))
        evidence_refs = tuple(sorted({reference for assessment in assessments for reference in assessment.evidence_ids}))
        deliverables = tuple(sorted({value for assessment in assessments for value in assessment.verified_deliverables}))
        limitations = (
            "Assessment uses supplied structured results and evidence only; no external source verification is performed.",
            "Structural validation does not authenticate evidence provenance or establish independent verification.",
        )
        return MissionOutcome(mission.mission_id, status, deliverables, tuple(criteria), completed_tasks,
            incomplete_tasks, failed_tasks, evidence_refs, uncertainties, limitations, tuple(recs))


def _task_id(task: MissionTask | DelegatedTask) -> str:
    value = getattr(task, "task_id", None)
    if not _stable_id(value):
        raise VerificationError("task must have a stable task identifier")
    return value


def _validate_schema(output: Any, schema: Mapping[str, Any] | None) -> None:
    if schema is None:
        return
    if not isinstance(schema, Mapping):
        raise VerificationError("expected output schema must be a mapping")
    if schema.get("type", "object") != "object":
        raise VerificationError("only object output schemas are supported")
    if not isinstance(output, Mapping):
        raise VerificationError("result output must be an object")
    required = schema.get("required", ())
    properties = schema.get("properties", {})
    if not isinstance(required, (tuple, list)) or not isinstance(properties, Mapping):
        raise VerificationError("expected output schema is invalid")
    missing = tuple(key for key in required if not isinstance(key, str) or key not in output)
    if missing:
        raise VerificationError("result is missing required schema fields")
    if schema.get("additionalProperties", True) is False and set(output) - set(properties):
        raise VerificationError("result contains unexpected schema fields")
    for key, definition in properties.items():
        if key in output and isinstance(definition, Mapping) and "type" in definition:
            expected = definition["type"]
            if not _matches_type(output[key], expected):
                raise VerificationError("result field does not match expected schema")


def _matches_type(value: Any, expected: Any) -> bool:
    mapping = {"object": Mapping, "array": (list, tuple), "string": str,
               "integer": int, "number": (int, float), "boolean": bool, "null": type(None)}
    choices = expected if isinstance(expected, (tuple, list)) else (expected,)
    return any(kind in mapping and isinstance(value, mapping[kind])
               and not (kind in {"integer", "number"} and isinstance(value, bool)) for kind in choices)


def _has_deliverable(output: Any, name: str, evidence: Sequence[Evidence]) -> bool:
    if isinstance(output, Mapping) and name in output:
        return output[name] is not None and output[name] != ""
    return any(entry.claim == name and entry.verified for entry in evidence)


def _output_supports(output: Any, criterion: str) -> bool:
    if isinstance(output, Mapping):
        return criterion in output or any(value == criterion for value in output.values())
    return False


def _contradictions(evidence: Sequence[Evidence], claim: str) -> bool:
    matching = [entry for entry in evidence if entry.claim in {claim, f"not:{claim}", f"contradicts:{claim}"}]
    positives = any(entry.claim == claim and entry.verified for entry in matching)
    negatives = any(entry.claim in {f"not:{claim}", f"contradicts:{claim}"} and entry.verified for entry in matching)
    return positives and negatives


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise VerificationError("result or evidence content must be bounded JSON-compatible data") from None


def _stable_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is not None


def _unsafe_text(value: str) -> bool:
    return len(value) > 2000 or any(ord(character) < 32 for character in value)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
