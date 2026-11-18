"""Synthetic-only bridge from Brain mission drafts to orchestration contracts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from . import agent_awareness, mission_intelligence


class MissionConversationError(ValueError):
    """Mission integration input or state is invalid."""


def _trusted_acceptance_token():
    from .mission_execution import _TRUSTED_ACCEPTANCE_HARNESS
    return _TRUSTED_ACCEPTANCE_HARNESS


@dataclass
class MissionConversationState:
    mission: mission_intelligence.MissionDefinition
    plan: Any
    approvals: dict[str, tuple[Any, Any, Any]]
    outcomes: dict[str, Any]
    report_fingerprint: str
    capability_discovery: Any = None

    def to_dict(self) -> dict[str, Any]:
        records = {key: {name: value for name, value in record.items()
                         if name not in {"outcome", "task_assessment", "authorization_ref",
                                         "structural_task_assessment", "structural_assessment",
                                         "structural_uncertainties", "external_authentication"}}
                   for key, record in (getattr(self, "execution_records", {}) or {}).items()}
        synthetic = bool(getattr(self, "synthetic_acceptance", False))
        intents = ({key: dict(value) for key, value in self.execution_intents.items()}
                   if hasattr(self, "execution_intents") else {})
        return {"mission": self.mission.to_dict(),
                "plan": {**self.plan.to_dict(), "mission_ref": self.plan.mission_id,
                         "mission_id": self.mission.mission_id,
                         "tasks": [task.to_dict() for task in self.plan.tasks],
                         "execution_performed": False, "synthetic_acceptance": synthetic},
                "approvals": {key: {"request": pair[0].to_dict(),
                                     "decision": pair[1].to_dict() if pair[1] else None}
                              for key, pair in self.approvals.items()},
                "outcomes": {}, "report_fingerprint": self.report_fingerprint,
                "synthetic_acceptance": synthetic, "execution_records": records,
                "execution_intents": intents, "execution_performed": False}


def orchestration_path() -> str | None:
    from pathlib import Path
    candidate = Path(__file__).resolve().parents[2] / "control_plane"
    required = ("agent_contracts.py", "agent_discovery.py", "delegation_authorization.py",
                "execution_durability.py", "execution_engine.py", "mission_planning.py",
                "result_verification.py")
    return str(candidate.parent) if all((candidate / name).is_file() for name in required) else None


def _orchestration():
    import importlib
    import sys
    from pathlib import Path
    root = orchestration_path()
    if root is None:
        raise MissionConversationError("Agent Orchestration contracts are unavailable.")
    resolved = str(Path(root).resolve())
    package_root = str(Path(resolved) / "control_plane")
    package = sys.modules.get("control_plane")
    if package is not None:
        package_file = getattr(package, "__file__", None)
        if not package_file or not str(Path(package_file).resolve()).startswith(package_root):
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
    if any(not str(Path(module.__file__).resolve()).startswith(package_root) for module in modules):
        raise MissionConversationError("Loaded orchestration contracts do not match the configured worktree.")
    return tuple(modules)


def _register_synthetic_planner(planner, discovery):
    from . import mission_execution
    (_, _, _, _, mission_planning, _) = _orchestration()
    planner.discovery = discovery
    planner.available_permissions = frozenset((mission_execution.SYNTHETIC_PERMISSION,))
    planner.selector = mission_planning.AgentSelector(discovery, planner.verifier,
                                                       (mission_execution.SYNTHETIC_PERMISSION,))
    planner.register_synthetic_capability(
        agent_id=mission_execution.SYNTHETIC_AGENT_ID,
        capability=mission_execution.SYNTHETIC_CAPABILITY,
        task_type="synthetic-mission-report", permission=mission_execution.SYNTHETIC_PERMISSION,
        expected_deliverable="Capability support assessment")


def _validate_fingerprint(capability_report, supplied):
    raw = dict(capability_report)
    for key in ("synthetic_acceptance", "_synthetic_acceptance", "_fixture_discovery"):
        raw.pop(key, None)
    try:
        agent_awareness.read_capability_report(raw)
        actual = agent_awareness.report_fingerprint(raw)
    except (ValueError, TypeError):
        actual = None
    if actual != supplied:
        raise MissionConversationError("Capability report does not match its supplied fingerprint.")


def build_mission(objective: str, capability_report: Mapping[str, Any], *, report_fingerprint: str,
                  conversation_id: str):
    if not isinstance(report_fingerprint, str) or len(report_fingerprint) != 64:
        raise MissionConversationError("A validated capability-report fingerprint is required.")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise MissionConversationError("Conversation identity is required.")
    definition = mission_intelligence.define_mission(objective, capability_report=None)
    if definition is None:
        return None
    (agent_contracts, agent_discovery, _, _, mission_planning, _) = _orchestration()
    synthetic_marker = (isinstance(capability_report, Mapping) and
                        (capability_report.get("synthetic_acceptance") is True or
                         capability_report.get("_synthetic_acceptance") is True))
    synthetic = False
    discovery = agent_discovery.AgentDiscovery(agent_contracts.CapabilityRegistry())
    if synthetic_marker:
        discovery = capability_report.get("_fixture_discovery")
        if not isinstance(discovery, agent_discovery.AgentDiscovery):
            raise MissionConversationError("Synthetic acceptance requires explicit trusted discovery injection.")
        if not callable(getattr(discovery, "_trusted_acceptance_context", None)):
            raise MissionConversationError("Synthetic acceptance discovery is not trusted harness context.")
        try:
            if discovery._trusted_acceptance_context() is not _trusted_acceptance_token():
                raise MissionConversationError("Synthetic acceptance discovery is not trusted harness context.")
        except Exception as error:
            raise MissionConversationError("Synthetic acceptance discovery is not trusted harness context.") from error
        synthetic = True
    discovery.discover_existing()
    _validate_fingerprint(capability_report, report_fingerprint)
    if synthetic:
        from . import mission_execution
        definition = mission_intelligence.define_mission(objective, capability_report=None)
        plan_input = definition.to_dict()
        plan_input["required_capabilities"] = [mission_execution.SYNTHETIC_CAPABILITY]
        planner = mission_planning.MissionPlanner(discovery, (mission_execution.SYNTHETIC_PERMISSION,))
        _register_synthetic_planner(planner, discovery)
    else:
        report = agent_awareness.read_capability_report(capability_report)
        declared = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]}
        supported = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]
                     if cap["status"] in {"understood", "verified"}}
        missing_by_agent = {cap["name"]: tuple(cap["missing"])
                            for agent in report["agents"] for cap in agent["capabilities"]}
        notes = list(definition.missing_information)
        unresolved = [name for name in definition.required_capabilities if name not in declared]
        if unresolved:
            notes.append("No declared agent capability was found for: " + ", ".join(unresolved) + ".")
        unverified = [name for name in definition.required_capabilities if name in declared and name not in supported]
        if unverified:
            notes.append("Capability status is not verified for: " + ", ".join(unverified) + ".")
        for name in definition.required_capabilities:
            notes.extend("Capability prerequisite: " + item for item in missing_by_agent.get(name, ()))
        if definition.required_capabilities:
            notes.append("Capability metadata is informational only; no agent is assigned or executed.")
        raw = definition.to_dict()
        raw["missing_information"] = list(dict.fromkeys(notes))
        raw["status"] = "needs_clarification" if notes else "ready_for_review"
        definition = mission_intelligence.validate_mission(raw)
        plan_input = definition.to_dict()
        aliases = {"mission-planning": "fuzzing-plan-generation", "information-research": "fuzzing-plan-generation",
                   "comparative-analysis": "coverage-feedback-scoring",
                   "hardware-compatibility-assessment": "local-python-static-analysis"}
        plan_input["required_capabilities"] = [aliases.get(item, item)
                                               for item in plan_input["required_capabilities"]] or ["fuzzing-plan-generation"]
        planner = mission_planning.MissionPlanner(discovery, ())
    if not synthetic:
        discovery.discover_existing()
    plan = planner.plan(plan_input)
    if not synthetic and plan.tasks:
        from dataclasses import replace
        candidates = {item.capability: item for item in plan.candidates}
        tasks = []
        for task in plan.tasks:
            candidate = candidates.get(task.required_capabilities[0])
            if candidate is None:
                tasks.append(task)
                continue
            tasks.append(replace(task, proposed_agent=candidate.agent_id,
                                 required_permissions=candidate.required_permissions,
                                 required_approvals=("explicit_approval",),
                                 missing_prerequisites=tuple(dict.fromkeys(
                                     task.missing_prerequisites + candidate.rejection_reasons)),
                                 execution_ready=False))
        plan = replace(plan, tasks=tuple(tasks), execution_ready=False,
                       status="proposed_execution_unavailable")
    state = MissionConversationState(definition, plan, {}, {}, report_fingerprint, discovery)
    state.synthetic_acceptance = synthetic
    return state


def state_from_dict(payload: Mapping[str, Any], *, capability_discovery=None):
    if not isinstance(payload, Mapping):
        raise MissionConversationError("Stored mission state is invalid.")
    raw_records, raw_intents, raw_approvals = (payload.get("execution_records", {}),
                                               payload.get("execution_intents", {}), payload.get("approvals", {}))
    if not all(isinstance(value, Mapping) for value in (raw_records, raw_intents, raw_approvals)):
        raise MissionConversationError("Stored mission execution or approval state is invalid.")
    (agent_contracts, agent_discovery, _, _, mission_planning, _) = _orchestration()
    definition = mission_intelligence.validate_mission(payload.get("mission", {}))
    raw_plan = payload.get("plan", {})
    synthetic = (payload.get("synthetic_acceptance") is True or
                 isinstance(raw_plan, Mapping) and raw_plan.get("synthetic_acceptance") is True)
    discovery = agent_discovery.AgentDiscovery(agent_contracts.CapabilityRegistry())
    if synthetic:
        if not isinstance(capability_discovery, agent_discovery.AgentDiscovery):
            raise MissionConversationError("Synthetic reload requires trusted acceptance-harness discovery.")
        service = getattr(capability_discovery, "_acceptance_service", None)
        if (service is None or not callable(getattr(service, "mission_capability_discovery", None))
                or getattr(service, "_MissionExecutionService__trusted_acceptance_context", None)
                is not _trusted_acceptance_token()):
            raise MissionConversationError("Synthetic reload requires trusted acceptance-harness discovery.")
        trusted = getattr(capability_discovery, "_trusted_acceptance_context", None)
        if not callable(trusted):
            raise MissionConversationError("Synthetic reload discovery is not trusted harness context.")
        try:
            if trusted() is not _trusted_acceptance_token():
                raise MissionConversationError("Synthetic reload discovery is not trusted harness context.")
        except Exception as error:
            raise MissionConversationError("Synthetic reload discovery is not trusted harness context.") from error
        discovery = capability_discovery
    else:
        discovery.discover_existing()
    plan_input = definition.to_dict()
    if synthetic:
        from . import mission_execution
        plan_input["required_capabilities"] = [mission_execution.SYNTHETIC_CAPABILITY]
        planner = mission_planning.MissionPlanner(discovery, (mission_execution.SYNTHETIC_PERMISSION,))
        _register_synthetic_planner(planner, discovery)
    else:
        aliases = {"mission-planning": "fuzzing-plan-generation", "information-research": "fuzzing-plan-generation",
                   "comparative-analysis": "coverage-feedback-scoring",
                   "hardware-compatibility-assessment": "local-python-static-analysis"}
        plan_input["required_capabilities"] = [aliases.get(item, item)
                                               for item in plan_input["required_capabilities"]] or ["fuzzing-plan-generation"]
        planner = mission_planning.MissionPlanner(discovery, ())
    plan = planner.plan(plan_input)
    if synthetic:
        stored_tasks = raw_plan.get("tasks") or raw_plan.get("ordered_tasks", [])
        ids = {item.get("task_id", "").rsplit(".", 1)[-1]: item.get("task_id")
               for item in stored_tasks if isinstance(item, Mapping)}
        from dataclasses import replace
        plan = replace(plan, mission_id=definition.mission_id,
                       tasks=tuple(replace(task, task_id=ids.get(task.task_id.rsplit(".", 1)[-1], task.task_id))
                                   for task in plan.tasks))
    else:
        from dataclasses import replace
        plan = replace(plan, mission_id=definition.mission_id,
                       tasks=tuple(replace(task, task_id=task.task_id.replace(
                           plan.mission_id, definition.mission_id, 1)) for task in plan.tasks))
    if (not isinstance(raw_plan, Mapping) or raw_plan.get("execution_performed") is not False
            or (not synthetic and raw_plan.get("mission_ref", raw_plan.get("mission_id", plan.mission_id)) != plan.mission_id)):
        raise MissionConversationError("Stored plan does not match its mission.")
    fingerprint = payload.get("report_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise MissionConversationError("Stored capability fingerprint is invalid.")
    state = MissionConversationState(definition, plan, {}, {}, fingerprint, discovery if synthetic else None)
    state.synthetic_acceptance = synthetic
    valid_ids = {task.task_id for task in plan.tasks}
    if set(raw_approvals) - valid_ids or set(raw_records) - valid_ids or set(raw_intents) - valid_ids:
        raise MissionConversationError("Stored mission state contains task references outside the reconstructed plan.")
    allowed = {"planned", "blocked", "authorized", "authorization_issued", "running", "completed",
               "failed", "cancelled", "timed_out", "denied", "interrupted"}
    for key, value in raw_records.items():
        if (not isinstance(value, Mapping) or value.get("task_id") != key or value.get("status") not in allowed
                or any(field in value for field in ("authorization_ref", "outcome", "task_assessment",
                                                     "structural_task_assessment", "structural_assessment"))):
            raise MissionConversationError("Stored execution state contains unsafe authority or outcome claims.")
    state.execution_records = {key: dict(value) for key, value in raw_records.items()}
    state.execution_intents = {key: dict(value) for key, value in raw_intents.items()}
    if raw_approvals:
        from control_plane.delegation_authorization import ApprovalDecision, ApprovalRequest, ApprovalStatus, DelegationAuthorizer
        authorizer = DelegationAuthorizer(discovery)
        for task_id, item in raw_approvals.items():
            try:
                req = item["request"]
                request = ApprovalRequest(
                    request_id=req["request_id"], mission_ref=req["mission_ref"], task_ref=req["task_ref"],
                    proposed_agent=req["proposed_agent"], capability=req["capability"],
                    requested_action=req["requested_action"], expected_outcome=req["expected_outcome"],
                    requested_permissions=tuple(req["requested_permissions"]), authorized_scope=tuple(req["authorized_scope"]),
                    risks=tuple(req["risks"]), created_at=datetime.fromisoformat(req["created_at"]),
                    expires_at=datetime.fromisoformat(req["expires_at"]), request_digest=req["request_digest"])
                dec = item.get("decision")
                decision = (ApprovalDecision(
                    request_id=dec["request_id"], request_digest=dec["request_digest"], mission_ref=dec["mission_ref"],
                    task_ref=dec["task_ref"], status=ApprovalStatus(dec["status"]),
                    human_identity=dec["human_identity"], decided_at=datetime.fromisoformat(dec["decided_at"]),
                    reason=dec["reason"]) if dec is not None else None)
                state.approvals[task_id] = (request, decision, authorizer)
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                raise MissionConversationError("Stored planning approval is invalid.") from error
    return state


def create_approval_request(state: MissionConversationState, task_id: str, *, now=None):
    (_, agent_discovery, authorization, _, mission_planning, _) = _orchestration()
    if not isinstance(state, MissionConversationState):
        raise MissionConversationError("A valid in-session mission state is required.")
    task = next((item for item in state.plan.tasks if item.task_id == task_id), None)
    if task is None or task.proposed_agent is None:
        raise MissionConversationError("Approval task does not belong to this mission or has no proposed agent.")
    if task_id in state.approvals or "explicit_approval" not in task.required_approvals:
        raise MissionConversationError("Task approval is duplicate or not declared.")
    discovery = state.capability_discovery
    if not isinstance(discovery, agent_discovery.AgentDiscovery):
        raise MissionConversationError("Approval requires the verified discovery used to construct this plan.")
    try:
        registration = discovery.registry.discover(task.proposed_agent)
    except Exception as error:
        raise MissionConversationError("Proposed specialist is not registered in the authoritative registry.") from error
    if task.required_capabilities[0] not in registration.contract.capabilities:
        raise MissionConversationError("Proposed specialist does not declare the task capability.")
    proposal = mission_planning.DelegationProposal(
        state.mission.mission_id, task.task_id, task.required_capabilities[0],
        "User requested a reviewable task approval.", "; ".join(task.expected_deliverables), tuple(task.required_permissions))
    context = authorization.DelegationContext(
        mission_ref=state.mission.mission_id, task_ref=task.task_id, parent_task_ref=None,
        parent_authorized_permissions=tuple(task.required_permissions), approval_required=True,
        risk_summary=(state.mission.risk_level,))
    authorizer = authorization.DelegationAuthorizer(discovery)
    decision = authorizer.evaluate(proposal, context, requested_task_type=task.task_type)
    if decision.state is not authorization.DelegationDecisionState.REQUIRES_HUMAN_APPROVAL:
        raise MissionConversationError("Task is not approvable: " + "; ".join(decision.reasons + decision.missing_requirements))
    request = authorizer.create_approval_request(
        proposal, context, requested_agent=task.proposed_agent, requested_action=task.description,
        expected_outcome="; ".join(task.expected_deliverables), risks=(state.mission.risk_level,), now=now)
    state.approvals[task_id] = (request, None, authorizer)
    return request


def decide_approval(state: MissionConversationState, task_id: str, *, approved: bool,
                    human_identity: str, reason: str, now=None):
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
        raise MissionConversationError("Planning approval decision was rejected.") from error
    state.approvals[task_id] = (request, decision, authorizer)
    return decision


def describe_state(state: MissionConversationState) -> str:
    if not isinstance(state, MissionConversationState):
        raise MissionConversationError("A valid mission state is required.")
    details = [f"Objective: {state.mission.original_objective}",
               f"Status: mission planned ({state.plan.status})",
               "Success criteria: " + "; ".join(state.mission.success_criteria),
               "No task was executed. No external action was taken."]
    for task in state.plan.tasks:
        details.append(f"Task {task.task_id}: {task.description}; proposed agent: {task.proposed_agent or 'none'}")
        details.append("Required permissions: " + (", ".join(task.required_permissions) or "none declared"))
        if task.missing_prerequisites:
            details.append("Missing prerequisites: " + "; ".join(task.missing_prerequisites))
    return "\n".join(details)


def record_synthetic_result(state: MissionConversationState, task_id: str, *, result: Any,
                            evidences: tuple[Any, ...] = ()):
    raise MissionConversationError(
        "Direct result recording is unavailable; assess only a durable engine-recorded completion.")
