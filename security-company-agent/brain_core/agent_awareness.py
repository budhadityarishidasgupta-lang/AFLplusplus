"""Validated, read-only awareness of statically reported agent capabilities.

This adapter consumes the existing control-plane report contract. It never
imports or invokes legacy agents, persists private memory, or grants authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from control_plane.agent_contracts import CapabilityStatus

_REPORT_VERSION = 1
_STATUSES = {item.value for item in CapabilityStatus}
_MAX_REPORT_BYTES = 1_000_000
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9._:-]{0,126}[a-zA-Z0-9])?$")


class CapabilityReportError(ValueError):
    """A capability report is malformed, unsafe, or unsupported."""


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > 4000:
        raise CapabilityReportError(f"Invalid {label}.")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise CapabilityReportError(f"Invalid {label}.")
    return value.strip()


def _string_list(value: Any, label: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise CapabilityReportError(f"Invalid {label}.")
    result = []
    for item in value:
        text = _string(item, label)
        if identifiers and not _IDENTIFIER.fullmatch(text):
            raise CapabilityReportError(f"Invalid {label}.")
        result.append(text)
    if len(set(result)) != len(result):
        raise CapabilityReportError(f"Duplicate {label}.")
    return tuple(result)


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise CapabilityReportError(f"Invalid {label}.")
    return value


def validate_capability_report(raw: Any) -> dict[str, Any]:
    """Return a normalized report, rejecting malformed and unsafe input."""
    if not isinstance(raw, Mapping):
        raise CapabilityReportError("Unsupported capability report.")
    try:
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int or schema_version != _REPORT_VERSION:
            raise CapabilityReportError("Unsupported capability report.")
    except (TypeError, ValueError, OverflowError) as error:
        raise CapabilityReportError("Unsupported capability report.") from error
    required_top_level = {"schema_version", "agents", "unsupported_capabilities", "execution_verified", "permissions_granted"}
    if not required_top_level.issubset(raw) or set(raw) - required_top_level - {"execution_note", "capability_states"}:
        raise CapabilityReportError("Unexpected capability report fields.")
    if _boolean(raw["execution_verified"], "execution verification"):
        raise CapabilityReportError("Execution claims are not accepted from static reports.")
    if _boolean(raw["permissions_granted"], "permission grant"):
        raise CapabilityReportError("Permission grants are not accepted from capability reports.")
    agents = raw["agents"]
    unsupported = raw["unsupported_capabilities"]
    if not isinstance(agents, list) or len(agents) > 128:
        raise CapabilityReportError("Invalid agent list.")
    if not isinstance(unsupported, list) or len(unsupported) > 256:
        raise CapabilityReportError("Invalid unsupported capability list.")
    if "execution_note" in raw:
        _string(raw["execution_note"], "execution note")
    if "capability_states" in raw and (not isinstance(raw["capability_states"], list) or len(raw["capability_states"]) > 4096):
        raise CapabilityReportError("Invalid capability states.")
    normalized_agents = []
    seen_agents: set[str] = set()
    declared_agent_ids: set[str] = set()
    for item in agents:
        required_agent_fields = {
            "identity", "version", "verification_status", "evidence", "missing_requirements",
            "execution_readiness", "last_verified_at", "uncertainty_reason", "capabilities",
        }
        if not isinstance(item, Mapping):
            raise CapabilityReportError("Invalid agent record.")
        try:
            agent_fields = set(item)
            if not required_agent_fields.issubset(agent_fields) or agent_fields - required_agent_fields - {"purpose"}:
                raise CapabilityReportError("Invalid agent record.")
        except (TypeError, ValueError) as error:
            raise CapabilityReportError("Invalid agent record.") from error
        identity = _string(item["identity"], "agent identity")
        if not _IDENTIFIER.fullmatch(identity) or identity in seen_agents:
            raise CapabilityReportError("Invalid or duplicate agent identity.")
        if not isinstance(item["version"], str):
            raise CapabilityReportError("Invalid agent version.")
        seen_agents.add(identity)
        declared_agent_ids.add(identity)
        status = _string(item["verification_status"], "verification status")
        if status not in _STATUSES:
            raise CapabilityReportError("Unknown verification status.")
        if "purpose" in item and not _string(item["purpose"], "agent purpose"):
            raise CapabilityReportError("Invalid agent purpose.")
        readiness = item["execution_readiness"]
        if not isinstance(readiness, Mapping):
            raise CapabilityReportError("Invalid execution readiness.")
        try:
            valid_readiness_shape = set(readiness) == {"ready", "reason"}
        except (TypeError, ValueError) as error:
            raise CapabilityReportError("Invalid execution readiness.") from error
        if not valid_readiness_shape:
            raise CapabilityReportError("Invalid execution readiness.")
        ready = _boolean(readiness["ready"], "execution readiness")
        readiness_reason = _string(readiness["reason"], "readiness reason")
        # Static verification must never be interpreted as an executable integration.
        if ready:
            raise CapabilityReportError("Static report cannot assert command readiness.")
        raw_caps = item["capabilities"]
        if not isinstance(raw_caps, list) or len(raw_caps) > 256:
            raise CapabilityReportError("Invalid capability list.")
        caps = []
        seen_caps: set[str] = set()
        for capability in raw_caps:
            required_capability_fields = {
                "capability", "status", "evidence", "missing_requirements", "execution_readiness",
                "required_permissions", "last_verified_at", "uncertainty_reason", "description",
            }
            if not isinstance(capability, Mapping):
                raise CapabilityReportError("Invalid capability record.")
            try:
                valid_capability_shape = set(capability) == required_capability_fields
            except (TypeError, ValueError) as error:
                raise CapabilityReportError("Invalid capability record.") from error
            if not valid_capability_shape:
                raise CapabilityReportError("Invalid capability record.")
            name = _string(capability["capability"], "capability name")
            if not _IDENTIFIER.fullmatch(name) or name in seen_caps:
                raise CapabilityReportError("Invalid or duplicate capability name.")
            seen_caps.add(name)
            cap_status = _string(capability["status"], "capability status")
            if cap_status not in _STATUSES:
                raise CapabilityReportError("Unknown capability status.")
            cap_readiness = capability["execution_readiness"]
            if not isinstance(cap_readiness, Mapping):
                raise CapabilityReportError("Invalid capability execution readiness.")
            try:
                valid_cap_readiness_shape = set(cap_readiness) == {"ready", "reason"}
            except (TypeError, ValueError) as error:
                raise CapabilityReportError("Invalid capability execution readiness.") from error
            if not valid_cap_readiness_shape:
                raise CapabilityReportError("Invalid capability execution readiness.")
            cap_ready = _boolean(cap_readiness["ready"], "capability execution readiness")
            cap_reason = _string(cap_readiness["reason"], "capability readiness reason")
            if cap_ready:
                raise CapabilityReportError("Static report cannot assert capability command readiness.")
            cap_evidence = _string_list(capability["evidence"], "capability evidence")
            required = _string_list(capability["required_permissions"], "permission requirement")
            caps.append({
                "name": name, "status": cap_status,
                "evidence": cap_evidence,
                "missing": _string_list(capability["missing_requirements"], "missing requirement"),
                "ready": False, "readiness_reason": cap_reason,
                "required_permissions": required,
                "last_verified_at": None if capability["last_verified_at"] is None else _string(capability["last_verified_at"], "verification timestamp"),
                "uncertainty": None if capability["uncertainty_reason"] is None else _string(capability["uncertainty_reason"], "uncertainty reason"),
                "description": _string(capability["description"], "capability description"),
            })
        normalized_agents.append({
            "identity": identity,
            "purpose": _string(item["purpose"], "agent purpose") if "purpose" in item else "",
            "version": _string(item["version"], "agent version"),
            "status": status,
            "evidence": _string_list(item["evidence"], "agent evidence"),
            "missing": _string_list(item["missing_requirements"], "missing requirement"),
            "ready": False,
            "readiness_reason": readiness_reason,
            "last_verified_at": None if item["last_verified_at"] is None else _string(item["last_verified_at"], "verification timestamp"),
            "uncertainty": None if item["uncertainty_reason"] is None else _string(item["uncertainty_reason"], "uncertainty reason"),
            "capabilities": caps,
        })
    missing = []
    seen_missing = set()
    for item in unsupported:
        if not isinstance(item, Mapping):
            raise CapabilityReportError("Invalid unsupported capability.")
        try:
            valid_unsupported_shape = set(item) == {"capability", "status", "message"}
        except (TypeError, ValueError) as error:
            raise CapabilityReportError("Invalid unsupported capability.") from error
        if not valid_unsupported_shape:
            raise CapabilityReportError("Invalid unsupported capability.")
        name = _string(item["capability"], "unsupported capability")
        status = _string(item["status"], "unsupported status")
        if not _IDENTIFIER.fullmatch(name) or status not in _STATUSES or name in seen_missing:
            raise CapabilityReportError("Invalid or duplicate unsupported capability.")
        seen_missing.add(name)
        missing.append({"name": name, "status": status, "message": _string(item["message"], "unsupported capability explanation")})
    normalized_agents.sort(key=lambda entry: entry["identity"])
    missing.sort(key=lambda entry: entry["name"])
    states = raw.get("capability_states", [])
    if states and any(not isinstance(state, Mapping) for state in states):
        raise CapabilityReportError("Invalid capability states.")
    state_by_key = {}
    state_keys = set()
    normalized_states = []
    for state in states:
        required_state_fields = {"agent", "capability", "state", "description", "readiness", "reason"}
        if not isinstance(state, Mapping):
            raise CapabilityReportError("Invalid capability state record.")
        try:
            valid_state_shape = set(state) == required_state_fields
        except (TypeError, ValueError) as error:
            raise CapabilityReportError("Invalid capability state record.") from error
        if (
            not valid_state_shape
            or type(state.get("readiness")) is not bool
            or not isinstance(state.get("description"), str)
            or (state.get("reason") is not None and not isinstance(state.get("reason"), str))
        ):
            raise CapabilityReportError("Invalid capability state record.")
        state_reason = "" if state["reason"] is None else state["reason"].casefold()
        state_agent = _string(state["agent"], "capability state agent")
        state_capability = _string(state["capability"], "capability state name")
        state_value = _string(state["state"], "capability state")
        if state_agent not in declared_agent_ids:
            raise CapabilityReportError("Capability state does not reference a declared agent.")
        declared = next((
            capability
            for agent in agents if isinstance(agent, Mapping) and agent.get("identity") == state_agent
            for capability in agent["capabilities"] if capability["capability"] == state_capability
        ), None)
        if declared is None:
            raise CapabilityReportError("Capability state does not reference a declared capability.")
        readiness = declared.get("execution_readiness", {})
        readiness_reason = readiness.get("reason", "").casefold() if isinstance(readiness, Mapping) and isinstance(readiness.get("reason"), str) else ""
        if state["state"] not in _STATUSES:
            raise CapabilityReportError("Unknown capability state.")
        if state["readiness"]:
            raise CapabilityReportError("Static report cannot assert capability command readiness.")
        if state_value == "verified" and not state_reason and not readiness_reason:
            raise CapabilityReportError("Verified capability state is missing readiness limitations.")
        key = (state_agent, state_capability)
        if key in state_by_key or state_value not in _STATUSES:
            raise CapabilityReportError("Invalid or duplicate capability state.")
        state_by_key[key] = state_value
        state_keys.add(key)
        normalized_states.append({
            "agent": key[0], "capability": key[1], "state": state_value,
            "readiness": state["readiness"],
            "description": _string(state["description"], "capability state description"),
            "reason": None if state["reason"] is None else _string(state["reason"], "capability state reason"),
        })
    if states and state_keys != {
        (agent["identity"], capability["name"])
        for agent in normalized_agents for capability in agent["capabilities"]
    }:
        raise CapabilityReportError("Capability states do not match declared capabilities.")
    state_readiness = {(state["agent"], state["capability"]): state["readiness"] for state in normalized_states}
    state_details = {(state["agent"], state["capability"]): state for state in normalized_states}
    for agent in normalized_agents:
        for capability in agent["capabilities"]:
            key = (agent["identity"], capability["name"])
            capability["status"] = state_by_key.get(key, capability["status"])
            capability["ready"] = state_readiness.get(key, capability["ready"])
            if key in state_details and state_details[key]["reason"]:
                capability["readiness_reason"] = state_details[key]["reason"]
            if capability["ready"]:
                raise CapabilityReportError("Static report cannot assert capability command readiness.")
    normalized_agents.sort(key=lambda entry: entry["identity"])
    missing.sort(key=lambda entry: entry["name"])
    normalized_states.sort(key=lambda state: (state["agent"], state["capability"]))
    return {"agents": normalized_agents, "unsupported_capabilities": missing, "capability_states": normalized_states}


def read_capability_report(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load and validate a report from a JSON path or already-decoded mapping."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            with path.open("rb") as stream:
                encoded = stream.read(_MAX_REPORT_BYTES + 1)
            if len(encoded) > _MAX_REPORT_BYTES:
                raise CapabilityReportError("Capability report is too large.")
            raw = json.loads(encoded.decode("utf-8"))
        except CapabilityReportError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CapabilityReportError("Capability report could not be read safely.") from error
    else:
        raw = source
    return validate_capability_report(raw)


def _public_facts(report: Mapping[str, Any]) -> dict[str, Any]:
    return read_capability_report(report)


def answer_agent_question(question: str, report: Mapping[str, Any]) -> str | None:
    """Answer common awareness questions only from a validated report."""
    text = (question or "").casefold()
    data = _public_facts(report)
    agents = data["agents"]
    capabilities = [(agent, cap) for agent in agents for cap in agent["capabilities"]]
    missing = data["unsupported_capabilities"]
    if re.search(r"\b(which agents do you know|what agents do you know|what agents are there|list (?:the )?agents|what agents have you identified|tell me what agents you know)\b", text):
        if not agents:
            return "I have not identified any agents in the verified capability report yet."
        return f"I've identified {len(agents)} specialist agent{'s' if len(agents) != 1 else ''}: " + "; ".join(
            f"{agent['identity'].replace('-', ' ').replace('.', ' ')} — {agent['purpose'] or (agent['capabilities'][0]['description'] if agent['capabilities'] else 'purpose not established')}"
            for agent in agents) + "."
    if re.search(r"\bwhat (?:can|capabilities can) (?:my )?agents do|what are (?:my )?agents capable of|what can my agents do\b", text):
        if not capabilities:
            return "I don't yet have verified capability details for the agents."
        verified = sum(cap["status"] == "verified" for _, cap in capabilities)
        return (f"I understand {len(capabilities)} declared capabilities across {len(agents)} agents. "
                f"{verified} passed static verification; that checks descriptions and interfaces, not successful execution. "
                "I haven't verified that I can command them.")
    if re.search(r"\b(can you command|command (?:the )?.*agents|run (?:the )?.*agents|execute (?:the )?.*agents)\b", text):
        unavailable = len(agents) - sum(agent["ready"] for agent in agents)
        return ("No. I can describe the capabilities in the report, but none has a verified command interface here. "
                f"Static verification does not grant permission or show that I can execute them ({unavailable} currently not command-ready).")
    if re.search(r"\b(which capabilities are missing|what capabilities are missing|what capabilities are still missing|which agents or capabilities are missing|what agents or capabilities are missing|missing capabilities|what is missing)\b", text):
        if not missing:
            unresolved = sorted({
                name
                for agent, cap in capabilities
                for name in cap["missing"]
                if "execution integration is unavailable" not in name.casefold()
                and "cannot currently be commanded" not in name.casefold()
            })
            if not unresolved:
                return "The report lists no unsupported capabilities or missing requirements; command readiness is still separate."
            return "The report says these requirements remain unresolved: " + "; ".join(unresolved) + "."
        return "The report cannot establish support for: " + "; ".join(item["name"].replace('-', ' ') for item in missing) + "."
    if re.search(r"\bwhat have you learned about (?:your|my) agents|what do you understand about (?:your|my) agents\b", text):
        known = sum(agent["status"] in ("understood", "verified") for agent in agents)
        verified = sum(cap["status"] == "verified" for _, cap in capabilities)
        return (f"I've identified {len(agents)} agents and understand the declared capabilities for {known}. "
                f"{verified} capabilities passed static checks. Their declared permissions are requirements, not grants, "
                "and I have not established a way to command or execute them.")
    return None


def report_fingerprint(report: Mapping[str, Any]) -> str:
    """Stable digest for deduplicating meaningful report changes in session state."""
    normalized = read_capability_report(report)
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def capability_change(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> str | None:
    """Return a concise announcement for meaningful changes, or None."""
    now = read_capability_report(current)
    if previous is None:
        if not now["agents"]:
            return None
        return f"I've identified {len(now['agents'])} specialist agents and reviewed their declared capabilities. This is static evidence only; I haven't verified that I can command them."
    before = read_capability_report(previous)
    old_agents = {agent["identity"]: agent for agent in before["agents"]}
    new_agents = {agent["identity"]: agent for agent in now["agents"]}
    added = sorted(set(new_agents) - set(old_agents))
    removed = sorted(set(old_agents) - set(new_agents))
    old_caps = {(agent["identity"], cap["name"]): cap for agent in before["agents"] for cap in agent["capabilities"]}
    new_caps = {(agent["identity"], cap["name"]): cap for agent in now["agents"] for cap in agent["capabilities"]}
    for state in before["capability_states"]:
        key = (state["agent"], state["capability"])
        if key in old_caps:
            old_caps[key]["status"] = state["state"]
    for state in now["capability_states"]:
        key = (state["agent"], state["capability"])
        if key in new_caps:
            new_caps[key]["status"] = state["state"]
    newly_verified = [key for key in new_caps.keys() & old_caps.keys() if old_caps[key]["status"] != "verified" and new_caps[key]["status"] == "verified"]
    changed = [key for key in new_caps.keys() & old_caps.keys() if old_caps[key]["status"] != new_caps[key]["status"]]
    if not (added or removed or changed):
        return None
    pieces = []
    if added:
        pieces.append(f"I identified {len(added)} new agent{'s' if len(added) != 1 else ''}")
    if newly_verified:
        pieces.append(f"static checks now verify {len(newly_verified)} declared capabilities")
    other_changed = len([key for key in changed if key not in newly_verified])
    if other_changed:
        pieces.append(f"{other_changed} capability status{'es' if other_changed != 1 else ''} changed")
    if removed:
        pieces.append(f"{len(removed)} agent{'s' if len(removed) != 1 else ''} no longer appear")
    return "Capability awareness update: " + "; ".join(pieces) + ". This does not establish execution readiness or grant permission."
