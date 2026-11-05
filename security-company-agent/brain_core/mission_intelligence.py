"""Offline-safe interpretation and validation of explicit user missions.

This module plans only. It never invokes agents, providers, web services, or
persistent memory. Validated mission state can be kept in a caller-owned store.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


_SCHEMA_VERSION = 1
_STATUSES = {"draft", "needs_clarification", "ready_for_review", "approved", "in_progress", "completed", "blocked", "cancelled"}
_READINESS = {"not_assessed", "blocked", "not_ready", "ready_for_review"}
_RISK = {"low", "moderate", "high", "critical", "unknown"}
_AUTHORIZATION = {"none", "user_confirmation", "explicit_approval", "external_authorization", "unknown"}
_MISSION_CUES = re.compile(
    r"\b(research|investigate|compare|evaluate|recommend|plan|design|organize|"
    r"prepare|build|create|develop|implement|migrate|upgrade|set up|configure|"
    r"diagnose|troubleshoot|audit|assess|analy[sz]e|find|identify|book|buy|purchase|"
    r"send|publish|deploy|delete|install|run|execute|schedule|contact|submit)\b", re.I)
_QUESTION_ONLY = re.compile(r"^(?:what|who|when|where|which|why|how)\b", re.I)

_EXTERNAL_ACTION_CUES = re.compile(
    r"\b(book|buy|purchase|send|publish|deploy|delete|install|run|execute|schedule|"
    r"contact|submit|transfer|pay|order|modify|change|access|log ?in|upload|download)\b", re.I)
_INFORMATION_CUES = re.compile(
    r"\b(research|investigate|search|look up|find|identify|compare|evaluate|recommend)\b", re.I)
_NEGATED_ACTION = re.compile(
    r"\b(?:do not|don't|dont|never|without)\s+(?:automatically\s+|directly\s+)?"
    r"(?:book|buy|purchase|send|publish|deploy|delete|install|run|execute|schedule|"
    r"contact|submit|transfer|pay|order|modify|change|access|log ?in|upload|download)\b", re.I)
_CAPABILITY_ALIASES = {
    "information-research": ("research", "search", "web research"),
    "comparative-analysis": ("analysis", "compare", "evaluation", "score-target-feedback"),
    "hardware-compatibility-assessment": ("hardware assessment", "device compatibility", "inspect-local-python-source"),
    "mission-planning": ("planning", "plan", "form-local-fuzzing-plan", "prepare-campaign"),
}


def _capability_match(required: str, declared: str) -> bool:
    if required == declared:
        return True
    aliases = _CAPABILITY_ALIASES.get(required, ())
    return any(alias in declared.casefold() for alias in aliases)


@dataclass(frozen=True)
class MissionDefinition:
    """A validated, non-executing description of a user objective."""

    mission_id: str
    original_objective: str
    intended_outcome: str
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    missing_information: tuple[str, ...]
    dependencies: tuple[str, ...]
    risk_level: str
    authorization_requirements: tuple[str, ...]
    execution_readiness: str
    status: str
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly copy of the validated mission."""
        result = asdict(self)
        for key in ("success_criteria", "constraints", "required_capabilities",
                    "missing_information", "dependencies", "authorization_requirements"):
            result[key] = list(result[key])
        return result


class MissionValidationError(ValueError):
    """Raised when a mission definition is incomplete or unsafe to accept."""


def validate_mission(raw: Any) -> MissionDefinition:
    """Strictly validate deterministic or model-proposed mission data."""
    if isinstance(raw, MissionDefinition):
        raw = raw.to_dict()
    if not isinstance(raw, Mapping):
        raise MissionValidationError("Mission must be a mapping.")
    if len(raw) > 64:
        raise MissionValidationError("Mission object is too large.")
    if sum(len(value) for value in raw.values() if isinstance(value, str)) > 16000:
        raise MissionValidationError("Mission text is too large.")
    expected = {
        "mission_id", "original_objective", "intended_outcome", "success_criteria", "constraints",
        "required_capabilities", "missing_information", "dependencies", "risk_level",
        "authorization_requirements", "execution_readiness", "status", "provenance", "schema_version",
    }
    if set(raw) != expected:
        raise MissionValidationError("Mission fields are incomplete or unexpected.")
    if not isinstance(raw["mission_id"], str) or not isinstance(raw["original_objective"], str):
        raise MissionValidationError("Mission identity and objective must be text.")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
        raise MissionValidationError("Unsupported mission schema.")

    def text(value: Any, label: str, *, empty: bool = False, maximum: int = 4000) -> str:
        if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
            raise MissionValidationError(f"Invalid {label}.")
        if any(ord(char) < 32 for char in value):
            raise MissionValidationError(f"Invalid {label}.")
        return value.strip()

    def texts(value: Any, label: str, maximum: int = 32) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or len(value) > maximum:
            raise MissionValidationError(f"Invalid {label}.")
        values = tuple(text(item, label, maximum=1000) for item in value)
        if len(set(values)) != len(values):
            raise MissionValidationError(f"Duplicate {label}.")
        return values

    mission_id = text(raw["mission_id"], "mission ID", maximum=128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", mission_id):
        raise MissionValidationError("Invalid mission ID.")
    objective = text(raw["original_objective"], "objective")
    outcome = text(raw["intended_outcome"], "intended outcome")
    criteria = texts(raw["success_criteria"], "success criteria")
    if not criteria:
        raise MissionValidationError("At least one success criterion is required.")
    constraints = texts(raw["constraints"], "constraint")
    capabilities = texts(raw["required_capabilities"], "required capability")
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", name) for name in capabilities):
        raise MissionValidationError("Required capabilities must use stable capability identifiers.")
    missing = texts(raw["missing_information"], "missing information")
    dependencies = texts(raw["dependencies"], "dependency")
    if not isinstance(raw["risk_level"], str) or raw["risk_level"] not in _RISK:
        raise MissionValidationError("Invalid risk level.")
    authorization = texts(raw["authorization_requirements"], "authorization requirement")
    if any(item not in _AUTHORIZATION for item in authorization):
        raise MissionValidationError("Invalid authorization requirement.")
    if not isinstance(raw["execution_readiness"], str) or raw["execution_readiness"] not in _READINESS:
        raise MissionValidationError("Invalid execution readiness.")
    if not isinstance(raw["status"], str) or raw["status"] not in _STATUSES:
        raise MissionValidationError("Invalid mission status.")
    if raw["status"] in {"approved", "in_progress", "completed"}:
        raise MissionValidationError("Mission status cannot imply approval or execution from a plan.")
    if raw["execution_readiness"] == "ready_for_review" and raw["status"] not in {"ready_for_review", "draft"}:
        raise MissionValidationError("Mission readiness and status are inconsistent.")
    if raw["status"] == "blocked" and raw["execution_readiness"] != "blocked":
        raise MissionValidationError("Blocked mission status requires blocked readiness.")
    if raw["status"] == "needs_clarification" and raw["execution_readiness"] == "ready_for_review":
        raise MissionValidationError("Missions requiring clarification cannot be ready for review.")
    if raw["status"] == "blocked" and not authorization and not missing:
        raise MissionValidationError("Blocked missions must state a blocker.")
    if raw["status"] == "ready_for_review" and raw["execution_readiness"] in {"blocked", "not_ready"}:
        raise MissionValidationError("Blocked missions cannot be ready for review.")
    if not isinstance(raw["provenance"], Mapping):
        raise MissionValidationError("Invalid mission provenance.")
    provenance = dict(raw["provenance"])
    if set(provenance) - {"source", "interpreted_at", "provider", "capability_report_fingerprint"}:
        raise MissionValidationError("Unexpected mission provenance fields.")
    for key, value in provenance.items():
        provenance[key] = text(value, f"provenance {key}", maximum=256)
    if "interpreted_at" in provenance:
        try:
            datetime.fromisoformat(provenance["interpreted_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise MissionValidationError("Invalid mission interpretation timestamp.") from error
    if "capability_report_fingerprint" in provenance and not re.fullmatch(
            r"[a-f0-9]{64}", provenance["capability_report_fingerprint"]):
        raise MissionValidationError("Invalid capability-report fingerprint.")
    if "provider" in provenance and provenance["provider"] not in {"local", "openai", "alibaba_qwen"}:
        raise MissionValidationError("Unknown mission interpretation provider.")
    if "source" in provenance and provenance["source"] not in {"deterministic_interpretation", "model_assisted_interpretation"}:
        raise MissionValidationError("Unknown mission provenance source.")
    if provenance.get("source") == "model_assisted_interpretation" and "provider" not in provenance:
        raise MissionValidationError("Model-assisted mission provenance must identify its provider.")
    return MissionDefinition(
        mission_id, objective, outcome, criteria, constraints, capabilities, missing, dependencies,
        raw["risk_level"], authorization, raw["execution_readiness"], raw["status"],
        provenance, _SCHEMA_VERSION,
    )


def classify_mission_intent(text: str) -> str:
    """Classify as ordinary chat, information gathering, mission, or external action."""
    body = (text or "").strip()
    if not body or len(body) > 4000:
        return "ordinary"
    folded = body.casefold()
    if (re.search(r"\b(hello|hi|hey|good morning|good evening|how are you|thank you|thanks|who are you|"
                  r"approved lessons|what do you remember|recall|teach you|training lesson)\b", folded)
            or re.match(r"^(?:who|when|where|why)\b", folded)
            or re.search(r"\bwhat have you learned about (?:your|my|the) agents?\b", folded)
            or folded.startswith("which agents do you know")
            or ("?" in body and not re.search(r"\b(help me|i need|i want|my goal|my objective)\b", folded))
            or (_QUESTION_ONLY.match(body) and not re.search(
                r"\b(i need|i want|please|help me|my goal|my objective|for my)\b", folded))):
        return "ordinary"
    if not _MISSION_CUES.search(folded):
        return "ordinary"
    negated_goal = re.search(
        r"\b(do not|don't|dont|never)\s+(?:research|investigate|compare|evaluate|recommend|plan|design|prepare)\b",
        folded)
    if negated_goal or re.search(r"\b(?:do not|don't|dont|never)\s+(?:directly\s+)?send\b", folded):
        return "ordinary"
    negated_action = _NEGATED_ACTION.search(folded)
    if negated_action:
        action = _EXTERNAL_ACTION_CUES.search(folded)
        if action is None or action.start() <= negated_action.start():
            return "ordinary"
    if _EXTERNAL_ACTION_CUES.search(folded) and not _NEGATED_ACTION.search(folded):
        if re.search(r"\bplan|help me|prepare|draft|organize\b", folded):
            return "multi_step_mission"
        return "external_action"
    if _INFORMATION_CUES.search(folded):
        return "information_gathering"
    return "multi_step_mission"


def _outcome_for(objective: str, kind: str) -> str:
    if kind == "information_gathering":
        return "A grounded, evidence-aware result addressing the user's stated objective and limits."
    return "A reviewed plan or deliverable addressing the user's stated objective."


def _criteria_for(objective: str, kind: str) -> tuple[str, ...]:
    criteria = ["The result directly addresses the stated objective.",
                "Assumptions, limitations, and unresolved information are stated explicitly."]
    if kind == "information_gathering":
        criteria.append("Recommendations are supported by available evidence, with uncertainty identified.")
    else:
        criteria.append("Any proposed deliverable is reviewable and no action is taken without authorization.")
    if re.search(r"\b(recommend|whether|should i|best)\b", objective, re.I):
        criteria.append("The recommendation explains trade-offs and states when the objective's constraints are not met.")
    return tuple(criteria)


def _missing_for(objective: str, kind: str) -> tuple[str, ...]:
    missing = []
    folded = objective.casefold()
    if re.search(r"\b(laptop|computer|pc|device|hardware|my system)\b", folded) and not re.search(
            r"\b(ram|memory|gpu|graphics|vram|processor|cpu|model number|operating system|os)\b", folded):
        missing.append("Relevant hardware limits (such as memory, CPU/GPU, and device model).")
    if kind == "information_gathering":
        if not re.search(r"\b(criteria|prioriti[sz]e|focus on|prefer|budget|under|within|before|by)\b", folded):
            missing.append("Preferred evaluation criteria and any source or geographic constraints.")
        if re.search(r"\b(best|latest|current|recent|recommend|research|investigate)\b", folded):
            missing.append("Current sources have not been gathered or independently verified.")
    return tuple(dict.fromkeys(missing))


def _required_capabilities(objective: str, kind: str) -> tuple[str, ...]:
    folded = objective.casefold()
    needed = []
    if kind == "information_gathering":
        needed.append("information-research")
    elif kind == "multi_step_mission":
        needed.append("mission-planning")
    if re.search(r"\b(recommend|whether|compare|evaluate|best)\b", folded):
        needed.append("comparative-analysis")
    if re.search(r"\b(laptop|computer|pc|device|hardware)\b", folded):
        needed.append("hardware-compatibility-assessment")
    return tuple(dict.fromkeys(needed))


def define_mission(objective: str, *, capability_report: Mapping[str, Any] | None = None,
                   provenance: Mapping[str, Any] | None = None) -> MissionDefinition | None:
    """Create a conservative, deterministic mission draft for explicit objectives only."""
    if not isinstance(objective, str) or not objective.strip() or len(objective.strip()) > 4000:
        return None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in objective):
        return None
    kind = classify_mission_intent(objective)
    if kind == "ordinary":
        return None
    required = _required_capabilities(objective, kind)
    mission_provenance = dict(provenance or {"source": "deterministic_interpretation"})
    if set(mission_provenance) - {"source", "interpreted_at", "provider", "capability_report_fingerprint"}:
        raise MissionValidationError("Unexpected mission provenance fields.")
    try:
        validated_provenance = validate_mission({
            "mission_id": "provenance-check", "original_objective": objective.strip(),
            "intended_outcome": "provenance validation", "success_criteria": ["provenance validation"],
            "constraints": [], "required_capabilities": [], "missing_information": [],
            "dependencies": [], "risk_level": "unknown", "authorization_requirements": [],
            "execution_readiness": "not_assessed", "status": "draft",
            "provenance": mission_provenance, "schema_version": _SCHEMA_VERSION,
        })
        mission_provenance = dict(validated_provenance.provenance)
    except MissionValidationError:
        raise
    missing = list(_missing_for(objective, kind))
    if mission_provenance.get("source") == "model_assisted_interpretation":
        missing.append("Model-assisted mission interpretation is not enabled without separate provider authorization.")
    if "interpreted_at" in mission_provenance:
        interpretation_time = datetime.fromisoformat(mission_provenance["interpreted_at"].replace("Z", "+00:00"))
    else:
        interpretation_time = datetime.now(timezone.utc)
    unsupported = []
    if capability_report is not None:
        try:
            from .agent_awareness import read_capability_report
            report = read_capability_report(capability_report)
            from .agent_awareness import report_fingerprint
            mission_provenance["capability_report_fingerprint"] = report_fingerprint(capability_report)
            declared = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]}
            supported = {cap["name"] for agent in report["agents"] for cap in agent["capabilities"]
                         if cap["status"] in {"understood", "verified"}}
            unsupported = [name for name in required
                           if not any(_capability_match(name, item) for item in declared)]
            uncertain = [name for name in required
                         if any(_capability_match(name, item) for item in declared)
                         and not any(_capability_match(name, item) for item in supported)]
            command_ready = any(
                agent["ready"] and all(
                    cap["ready"] for cap in agent["capabilities"]
                    if any(_capability_match(name, cap["name"]) for name in required))
                for agent in report["agents"] if any(
                    _capability_match(name, cap["name"])
                    for cap in agent["capabilities"] for name in required)
            )
            if not command_ready and required:
                missing.append("Static capability awareness does not establish an execution-ready command interface.")
            elif command_ready:
                missing.append("Capability data is informational only; no agent was assigned or executed.")
            if required and any(
                    cap["ready"] for agent in report["agents"] for cap in agent["capabilities"]
                    if any(_capability_match(name, cap["name"]) for name in required)):
                missing.append("Reported readiness is not an authorization grant; no action is taken by this plan.")
            if unsupported:
                missing.append("No declared agent capability was found for: " + ", ".join(unsupported) + ".")
            if uncertain:
                missing.append("Capability status is not verified for: " + ", ".join(uncertain) + ".")
        except (ValueError, TypeError, KeyError, AttributeError):
            missing.append("A valid verified capability report is unavailable.")
            mission_provenance.pop("capability_report_fingerprint", None)
            if mission_provenance.get("source") == "deterministic_interpretation":
                mission_provenance.pop("interpreted_at", None)
    external = kind == "external_action"
    planned_actions = bool(re.search(
        r"\b(delete|deploy|purchase|pay|transfer|install|access|send|publish|book|buy|contact|submit)\b",
        objective, re.I))
    has_action_plan = bool(re.search(r"\b(plan|draft|prepare|organize)\b", objective, re.I))
    consequential = external or (kind == "multi_step_mission" and planned_actions
        and not _NEGATED_ACTION.search(objective) and not has_action_plan)
    if has_action_plan and planned_actions:
        missing.append("Any later consequential action requires explicit approval; this plan takes no action.")
    risk = "critical" if consequential and re.search(r"\b(delete|deploy|purchase|pay|transfer)\b", objective, re.I) else (
        "high" if consequential else ("moderate" if kind == "multi_step_mission" else "low"))
    authorization = ("explicit_approval",) if consequential else ()
    if external:
        missing.append("The requested external action is not authorized and will not be performed.")
    if consequential and not external and not has_action_plan:
        missing.append("This consequential action requires explicit approval and will not be performed by mission planning.")
    status = "needs_clarification" if missing else "ready_for_review"
    outcome = _outcome_for(objective, kind)
    mission_id = str(uuid.uuid4())
    mission_provenance.setdefault("interpreted_at", interpretation_time.isoformat())
    raw = {
        "mission_id": mission_id,
        "original_objective": objective.strip(),
        "intended_outcome": outcome,
        "success_criteria": _criteria_for(objective, kind),
        "constraints": ("No execution, external action, or permission change is implied by this plan.",),
        "required_capabilities": required,
        "missing_information": tuple(missing),
        "dependencies": ("User review of this mission definition.",),
        "risk_level": risk,
        "authorization_requirements": authorization,
        "execution_readiness": "blocked" if consequential or unsupported else "not_assessed",
        "status": "blocked" if consequential or unsupported or (capability_report is not None and any(
            "execution-ready command interface" in item for item in missing)) else status,
        "provenance": mission_provenance,
        "schema_version": _SCHEMA_VERSION,
    }
    if unsupported or consequential or (capability_report is not None and any(
            "execution-ready command interface" in item for item in missing)):
        raw["execution_readiness"] = "blocked"
        raw["status"] = "blocked"
    return validate_mission(raw)


def summarize_mission(mission: MissionDefinition | Mapping[str, Any]) -> str:
    """Render a user-facing summary without implementation identifiers or execution claims."""
    definition = validate_mission(mission)
    kind = classify_mission_intent(definition.original_objective)
    folded = definition.original_objective.casefold()
    if kind == "information_gathering" and "laptop" in folded:
        outcome = "identify a suitable local model that improves reasoning without exceeding your hardware limits"
    elif kind == "information_gathering" and "local model" in folded:
        outcome = "identify a suitable local model that fits your stated requirements"
    else:
        outcome = definition.intended_outcome
    message = f"I understand your objective: {outcome}. I can define the research and evaluation steps, but I haven't assigned an agent or started execution yet."
    if kind == "information_gathering":
        message += " I can describe a research plan, but no sources have been gathered and I have not verified current results."
    if definition.success_criteria:
        message += " Success means the result addresses your objective, explains evidence and trade-offs, and makes limitations clear."
    if definition.missing_information:
        if kind == "information_gathering" and "laptop" in folded:
            message += " To check hardware fit, I need your laptop's key specifications."
        if "Current sources have not been gathered or independently verified." in definition.missing_information:
            message += " No current sources have been gathered or independently verified."
        remaining = [item for item in definition.missing_information
                     if item != "Current sources have not been gathered or independently verified."]
        if remaining:
            message += " Key details remain to be established: " + " ".join(remaining)
    if definition.authorization_requirements:
        message += " Any external action would require explicit approval; this plan does not authorize it."
    if definition.execution_readiness == "blocked":
        message += " Readiness is blocked; no action has been taken."
    return message


class MissionStateStore:
    """Caller-owned, in-memory mission drafts isolated by conversation ID."""

    def __init__(self) -> None:
        self._missions: dict[str, MissionDefinition] = {}

    def save(self, conversation_id: str, mission: MissionDefinition | Mapping[str, Any]) -> MissionDefinition:
        if not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 256:
            raise MissionValidationError("Conversation ID is required and must be bounded.")
        validated = validate_mission(mission)
        if validated.status not in {"draft", "needs_clarification", "ready_for_review", "blocked", "cancelled"}:
            raise MissionValidationError("Only non-executing mission drafts can be stored here.")
        self._missions[conversation_id] = validated
        return validated

    def get(self, conversation_id: str) -> MissionDefinition | None:
        if not isinstance(conversation_id, str):
            return None
        return self._missions.get(conversation_id)

    def clear(self, conversation_id: str) -> MissionDefinition | None:
        if not isinstance(conversation_id, str):
            return None
        return self._missions.pop(conversation_id, None)

    def __len__(self) -> int:
        return len(self._missions)
