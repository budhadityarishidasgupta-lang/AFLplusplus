"""Synthetic-only bridge from Brain mission drafts to orchestration contracts.

The module imports the versioned Agent Orchestration control-plane interfaces
at runtime; it does not duplicate planning, approval, or verification rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import agent_awareness, mission_intelligence


class MissionConversationError(ValueError):
    """Mission integration input or state is invalid."""


@dataclass
class MissionConversationState:
    """Ephemeral mission UI state. No private memory is read or modified."""

    mission: mission_intelligence.MissionDefinition
    plan: Any
    approvals: dict[str, tuple[Any, Any, Any]]
    outcomes: dict[str, Any]
    report_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission": self.mission.to_dict(),
            "plan": self.plan.to_dict(),
            "approvals": {key: {"request": pair[0].to_dict(),
                                 "decision": pair[1].to_dict() if pair[1] else None}
                          for key, pair in self.approvals.items()},
            "outcomes": {key: item.to_dict() for key, item in self.outcomes.items()},
            "report_fingerprint": self.report_fingerprint,
            "execution_performed": False,
        }


def orchestration_path() -> str | None:
    """Locate the separately maintained orchestration worktree, if available."""
    import os
    from pathlib import Path

    configured = os.environ.get("SOVEREIGN_BRAIN_ORCHESTRATION_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path(__file__).resolve().parents[3] / "agent-orchestration",
                       Path(__file__).resolve().parents[2] / "agent-orchestration",
                       Path(__file__).resolve().parents[4] / "agent-orchestration"))
    for candidate in candidates:
        if (candidate / "control_plane" / "mission_planning.py").is_file():
            return str(candidate)
    return None


def _orchestration():
    import importlib
    import sys
    from pathlib import Path

    root = orchestration_path()
    if root is None:
        raise MissionConversationError("Agent Orchestration contracts are unavailable.")
    resolved = str(Path(root).resolve())
    package = sys.modules.get("control_plane")
    if package is not None:
        package_file = getattr(package, "__file__", None)
        if not package_file or not str(Path(package_file).resolve()).startswith(resolved):
            for name in tuple(sys.modules):
                if name == "control_plane" or name.startswith("control_plane."):
                    del sys.modules[name]
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    try:
        modules = [importlib.import_module("control_plane." + name) for name in (
            "agent_contracts", "agent_discovery", "delegation_authorization",
            "execution_engine", "mission_planning", "result_verification")]
    except (ImportError, OSError) as error:
        raise MissionConversationError("Agent Orchestration contracts could not be loaded.") from error
    if any(not str(Path(module.__file__).resolve()).startswith(resolved) for module in modules):
        raise MissionConversationError("Loaded orchestration contracts do not match the configured worktree.")
    return tuple(modules)


def build_mission(objective: str, capability_report: Mapping[str, Any], *, report_fingerprint: str,
                  conversation_id: str):
    """Validate a synthetic capability report and create a Brain-owned draft and control-plane plan."""
    if not isinstance(report_fingerprint, str) or len(report_fingerprint) != 64:
        raise MissionConversationError("A validated capability-report fingerprint is required.")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise MissionConversationError("Conversation identity is required.")
    definition = mission_intelligence.define_mission(objective, capability_report=None)
    if definition is None:
        return None
    (agent_contracts, agent_discovery, _, _, mission_planning, _) = _orchestration()
    registry = agent_contracts.CapabilityRegistry()
    discovery = agent_discovery.AgentDiscovery(registry)
    discovered = discovery.discover_existing()  # static source-contract discovery only
    try:
        report = agent_awareness.read_capability_report(capability_report)
        actual_fingerprint = agent_awareness.report_fingerprint(capability_report)
    except (ValueError, TypeError):
        report = None
        actual_fingerprint = None
    if actual_fingerprint != report_fingerprint:
        raise MissionConversationError("Capability report does not match its supplied fingerprint.")
    if report is not None:
        declared = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]}
        supported = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]
                     if cap["status"] in {"understood", "verified"}}
        missing_by_agent = {
            cap["name"]: tuple(cap["missing"])
            for agent in report["agents"] for cap in agent["capabilities"]
        }
        notes = list(definition.missing_information)
        unresolved = [name for name in definition.required_capabilities if name not in declared]
        if unresolved:
            notes.append("No declared agent capability was found for: " + ", ".join(unresolved) + ".")
        unverified = [name for name in definition.required_capabilities
                      if name in declared and name not in supported]
        if unverified:
            notes.append("Capability status is not verified for: " + ", ".join(unverified) + ".")
        for name in definition.required_capabilities:
            if name in missing_by_agent and missing_by_agent[name]:
                notes.extend("Capability prerequisite: " + item for item in missing_by_agent[name])
        if definition.required_capabilities:
            notes.append("Capability metadata is informational only; no agent is assigned or executed.")
        raw_definition = definition.to_dict()
        raw_definition["missing_information"] = list(dict.fromkeys(notes))
        raw_definition["status"] = "needs_clarification" if notes else "ready_for_review"
        definition = mission_intelligence.validate_mission(raw_definition)
    plan_input = definition.to_dict()
    capability_aliases = {
        "mission-planning": "fuzzing-plan-generation",
        "information-research": "fuzzing-plan-generation",
        "comparative-analysis": "coverage-feedback-scoring",
        "hardware-compatibility-assessment": "local-python-static-analysis",
    }
    required_for_planning = [capability_aliases.get(item, item)
                             for item in plan_input["required_capabilities"]]
    if not required_for_planning:
        required_for_planning = ["fuzzing-plan-generation"]
    plan_input["required_capabilities"] = required_for_planning
    plan_permissions = ()
    plan = mission_planning.MissionPlanner(discovery=discovery, available_permissions=plan_permissions).plan(plan_input)
    # Surface a suitable declared specialist even though planning cannot satisfy
    # permissions or execution readiness. This is explicitly a proposal only.
    if plan.tasks:
        from dataclasses import replace
        candidates = {item.capability: item for item in plan.candidates}
        tasks = []
        for task in plan.tasks:
            candidate = candidates.get(task.required_capabilities[0])
            if candidate is None:
                tasks.append(task)
                continue
            tasks.append(replace(
                task, proposed_agent=candidate.agent_id,
                required_permissions=candidate.required_permissions,
                required_approvals=("explicit_approval",),
                missing_prerequisites=tuple(dict.fromkeys(
                    task.missing_prerequisites + candidate.rejection_reasons)),
                execution_ready=False))
        plan = replace(plan, tasks=tuple(tasks), execution_ready=False,
                       status="proposed_execution_unavailable")
    return MissionConversationState(definition, plan, {}, {}, report_fingerprint)


def state_from_dict(payload: Mapping[str, Any]) -> MissionConversationState:
    """Reconstruct a stored state using the canonical orchestration schemas."""
    if not isinstance(payload, Mapping) or payload.get("execution_performed") is not False:
        raise MissionConversationError("Stored mission state is invalid or claims execution.")
    (agent_contracts, agent_discovery, _, _, mission_planning, _) = _orchestration()
    definition = mission_intelligence.validate_mission(payload.get("mission", {}))
    registry = agent_contracts.CapabilityRegistry()
    discovery = agent_discovery.AgentDiscovery(registry)
    discovery.discover_existing()
    raw_plan = payload.get("plan", {})
    plan_input = definition.to_dict()
    aliases = {
        "mission-planning": "fuzzing-plan-generation",
        "information-research": "fuzzing-plan-generation",
        "comparative-analysis": "coverage-feedback-scoring",
        "hardware-compatibility-assessment": "local-python-static-analysis",
    }
    plan_input["required_capabilities"] = [aliases.get(item, item)
                                            for item in plan_input["required_capabilities"]] or ["fuzzing-plan-generation"]
    plan = mission_planning.MissionPlanner(
        discovery=discovery, available_permissions=()).plan(plan_input)
    if (not isinstance(raw_plan, Mapping)
            or raw_plan.get("mission_ref") != plan.mission_id
            or raw_plan.get("execution_performed") is not False):
        raise MissionConversationError("Stored plan does not match its mission.")
    # Stored task/approval/output fields are intentionally ignored; reconstruct a
    # fresh planning-only view from validated mission data and current static contracts.
    fingerprint = payload.get("report_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise MissionConversationError("Stored capability fingerprint is invalid.")
    return MissionConversationState(definition, plan, {}, {}, fingerprint)


def create_approval_request(state: MissionConversationState, task_id: str, *, now=None):
    """Create an approval request using the orchestration authorizer contract."""
    (_, agent_discovery, authorization, _, mission_planning, _) = _orchestration()
    discovery = agent_discovery.AgentDiscovery()
    discovery.discover_existing()
    if not isinstance(state, MissionConversationState):
        raise MissionConversationError("A valid in-session mission state is required.")
    task = next((task for task in state.plan.tasks if task.task_id == task_id), None)
    if task is None:
        raise MissionConversationError("Approval task does not belong to the current mission.")
    if task.proposed_agent is None:
        raise MissionConversationError("No suitable agent is proposed for this task.")
    if task_id in state.approvals:
        raise MissionConversationError("An approval request already exists for this task.")
    if "explicit_approval" not in task.required_approvals:
        raise MissionConversationError("The current plan does not declare human approval for this task.")
    proposal = mission_planning.DelegationProposal(
        state.mission.mission_id, task.task_id, task.required_capabilities[0],
        "User requested a reviewable task approval.", "; ".join(task.expected_deliverables),
        tuple(task.required_permissions))
    context = authorization.DelegationContext(
        mission_ref=state.mission.mission_id, task_ref=task.task_id,
        parent_task_ref=None, parent_authorized_permissions=tuple(task.required_permissions),
        approval_required=True, risk_summary=(state.mission.risk_level,))
    authorizer = authorization.DelegationAuthorizer(discovery)
    decision = authorizer.evaluate(proposal, context, requested_task_type=task.task_type)
    if decision.state is not authorization.DelegationDecisionState.REQUIRES_HUMAN_APPROVAL:
        raise MissionConversationError(
            "The orchestration contract does not consider this task approvable: "
            + "; ".join(decision.reasons + decision.missing_requirements))
    request = authorizer.create_approval_request(
        proposal, context, requested_agent=task.proposed_agent,
        requested_action=task.description,
        expected_outcome="; ".join(task.expected_deliverables),
        risks=(state.mission.risk_level,), now=now)
    state.approvals[task_id] = (request, None, authorizer)
    return request


def decide_approval(state: MissionConversationState, task_id: str, *, approved: bool,
                    human_identity: str, reason: str, now=None):
    """Record one explicit, task-bound human decision; it grants no execution authority."""
    if not isinstance(state, MissionConversationState):
        raise MissionConversationError("A valid in-session mission state is required.")
    pair = state.approvals.get(task_id)
    if pair is None or pair[1] is not None:
        raise MissionConversationError("No undecided approval request exists for this task.")
    request, _, authorizer = pair
    try:
        decision = authorizer.decide(request, approved=approved, human_identity=human_identity,
                                     reason=reason, now=now)
    except Exception as error:
        raise MissionConversationError("Approval decision was rejected by the authorization contract.") from error
    state.approvals[task_id] = (request, decision, authorizer)
    return decision


def record_synthetic_result(state: MissionConversationState, task_id: str, *, result: Any,
                            evidences: tuple[Any, ...] = ()):
    """Verify only a supplied result record via SB-41; no adapter is invoked."""
    (_, _, _, execution, mission_planning, verification) = _orchestration()
    if not isinstance(state, MissionConversationState):
        raise MissionConversationError("A valid in-session mission state is required.")
    task = next((task for task in state.plan.tasks if task.task_id == task_id), None)
    if task is None:
        raise MissionConversationError("Result task does not belong to the current mission.")
    pair = state.approvals.get(task_id)
    if pair is None or pair[1] is None or pair[1].status.value != "APPROVED":
        raise MissionConversationError("An explicit approval for this exact task is required before accepting results.")
    if not isinstance(result, execution.ExecutionResult):
        raise MissionConversationError("Result must match the orchestration ExecutionResult contract.")
    reference = mission_planning.mission_reference_from_definition(state.mission.to_dict())
    task_input = verification.TaskVerificationInput(
        task=task, result=result, evidences=tuple(evidences),
        agent_capabilities=tuple(task.required_capabilities),
        authorized_permissions=tuple(task.required_permissions),
        required_permissions=tuple(task.required_permissions))
    assessment = verification.ResultVerifier().verify_task(reference, task_input)
    state.outcomes[task_id] = assessment
    return assessment


def describe_state(state: MissionConversationState) -> str:
    """Render a restrained chat summary of plan, availability, permissions, and outcome."""
    mission = state.mission
    plan = state.plan
    lines = [f"Objective: {mission.original_objective}",
             f"Intended outcome: {mission.intended_outcome}",
             "Success criteria: " + "; ".join(mission.success_criteria),
             f"Mission status: {mission.status}; planning status: {plan.status} (planning only)."]
    if state.outcomes:
        completed = sum(item.completed for item in state.outcomes.values())
        lines.append(f"Verified outcomes: {completed} of {len(state.outcomes)} submitted task results validated.")
    for task in plan.tasks:
        agent = task.proposed_agent or "No suitable agent identified"
        lines.append(f"Task {task.task_id}: {task.description} Agent: {agent}. Status: planned, not running. "
                     f"Required permissions: {', '.join(task.required_permissions) or 'none declared'}. "
                     f"Approval: {', '.join(task.required_approvals) or 'not required by this draft'}.")
        if task.missing_prerequisites:
            lines.append("Unavailable or missing: " + "; ".join(task.missing_prerequisites))
        pair = state.approvals.get(task.task_id)
        if pair:
            decision = pair[1]
            lines.append("Approval status: " + (decision.status.value if decision else "PENDING; no implicit approval") +
                         ". Planning approval is separate from execution authorization.")
        outcome = state.outcomes.get(task.task_id)
        if outcome:
            lines.append(f"Verified task status: {outcome.status}; completed={outcome.completed}. "
                         f"Uncertainties: {'; '.join(outcome.uncertainties) or 'none'}")
    lines.append("No task was executed by this planning and review flow.")
    return "\n".join(lines)
