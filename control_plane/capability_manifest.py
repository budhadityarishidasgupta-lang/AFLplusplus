"""Versioned JSON snapshots of declared capability metadata.

Schema validation establishes document integrity, not capability verification,
execution authorization, deployment, or runtime availability.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from control_plane.agent_contracts import AgentRegistration, CapabilityStatus
from control_plane.agent_discovery import AgentSource, DiscoveredAgent
from control_plane.capability_verification import AgentVerification

MANIFEST_SCHEMA_VERSION = 1
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({1})


class ManifestError(ValueError):
    """Invalid manifest data or unsupported schema version."""


@dataclass(frozen=True)
class CapabilityManifest:
    schema_version: int
    agent_id: str
    agent_version: str
    purpose: str
    capabilities: tuple[str, ...]
    supported_tasks: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_permissions: tuple[str, ...]
    limitations: tuple[str, ...]
    adapter_available: bool
    verification_status: str
    verification_evidence: tuple[str, ...]
    verification_uncertainty: tuple[str, ...]
    execution_ready: bool
    source_revision: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version,
                "agent": {"id": self.agent_id, "version": self.agent_version, "purpose": self.purpose},
                "capabilities": list(self.capabilities), "supported_tasks": list(self.supported_tasks),
                "required_tools": list(self.required_tools), "required_permissions": list(self.required_permissions),
                "limitations": list(self.limitations), "adapter": {"available": self.adapter_available},
                "verification": {"status": self.verification_status,
                                 "evidence": list(self.verification_evidence),
                                 "uncertainty": list(self.verification_uncertainty)},
                "execution_readiness": {"ready": self.execution_ready}, "source_revision": self.source_revision,
                "execution_authorized": False, "runtime_available": False}


def generate_manifest(registration: AgentRegistration | DiscoveredAgent,
                      source: AgentSource | None = None,
                      verification: AgentVerification | None = None, *,
                      source_revision: str | None) -> CapabilityManifest:
    if isinstance(registration, DiscoveredAgent):
        source, contract, registry_status = registration.source, registration.contract, registration.status
    elif isinstance(registration, AgentRegistration) and isinstance(source, AgentSource):
        contract, registry_status = registration.contract, registration.status
    else:
        raise ManifestError("a source-backed registry registration or discovered agent is required")
    if not isinstance(source, AgentSource):
        raise ManifestError("source evidence is required")
    if not isinstance(source.execution_integration_available, bool):
        raise ManifestError("adapter availability must be explicitly declared")
    if verification is not None and not isinstance(verification, AgentVerification):
        raise ManifestError("capability verification report has an invalid type")
    if registry_status not in {CapabilityStatus.DISCOVERED, CapabilityStatus.UNDERSTOOD, CapabilityStatus.VERIFIED}:
        raise ManifestError("manifest generation requires a registered capability record")
    if verification is not None and (
        verification.identity != contract.agent_id or verification.version != contract.version
    ):
        raise ManifestError("verification report does not match the registered agent")
    if verification is not None and not isinstance(verification.status, CapabilityStatus):
        raise ManifestError("verification report status is invalid")
    if verification is None:
        verification_evidence: tuple[str, ...] = ()
        verification_uncertainty = ("No trusted verification report establishes the capability verification status.",)
        ready = False
    elif verification.status is CapabilityStatus.VERIFIED:
        verification_evidence = verification.evidence
        verification_uncertainty = ()
        ready = bool(verification.execution_ready and source.execution_integration_available)
    elif verification.status is CapabilityStatus.UNKNOWN:
        verification_evidence = ()
        verification_uncertainty = (verification.uncertainty_reason
                                    or "Verification report did not establish verified capability metadata.",)
        ready = False
    else:
        verification_evidence = ()
        verification_uncertainty = (verification.uncertainty_reason
                                    or f"Verification status is {verification.status.value}; verified evidence is unavailable.",)
        ready = False
    verification_status = (
        verification.status.value
        if verification is not None
        and verification.status is CapabilityStatus.VERIFIED
        and registry_status in {CapabilityStatus.UNDERSTOOD, CapabilityStatus.VERIFIED}
        else CapabilityStatus.UNKNOWN.value
    )
    if (verification_status == CapabilityStatus.VERIFIED.value
            and (not verification.evidence or verification.uncertainty_reason)):
        verification_status = CapabilityStatus.UNKNOWN.value
        verification_evidence = ()
        verification_uncertainty = (
            verification.uncertainty_reason
            or "Verification report lacks trusted evidence; status remains unknown.",
        )
        ready = False
    if verification_status == CapabilityStatus.UNKNOWN.value:
        verification_evidence = ()
        ready = False
    if verification_status == CapabilityStatus.UNKNOWN.value and not verification_uncertainty:
        verification_uncertainty = ("Registry or verification evidence does not establish verified status.",)
        verification_evidence = ()
        ready = False
    _revision(source_revision)
    manifest = CapabilityManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        agent_id=contract.agent_id,
        agent_version=contract.version,
        purpose=contract.purpose,
        capabilities=contract.capabilities,
        supported_tasks=contract.supported_tasks,
        required_tools=source.tool_requirements,
        required_permissions=contract.permissions,
        limitations=source.limitations,
        adapter_available=source.execution_integration_available,
        verification_status=verification_status,
        verification_evidence=verification_evidence,
        verification_uncertainty=verification_uncertainty,
        execution_ready=ready,
        source_revision=source_revision,
    )
    validate_manifest(manifest.to_dict())
    return manifest


def validate_manifest(value: CapabilityManifest | Mapping[str, Any]) -> dict[str, Any]:
    raw = value.to_dict() if isinstance(value, CapabilityManifest) else value
    if not isinstance(raw, Mapping) or raw.get("schema_version") not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
        raise ManifestError("manifest must use a supported schema version")
    required = {"schema_version", "agent", "capabilities", "supported_tasks", "required_tools",
                "required_permissions", "limitations", "adapter", "verification", "execution_readiness",
                "source_revision", "execution_authorized", "runtime_available"}
    if set(raw) != required:
        raise ManifestError("manifest has missing or unexpected fields")
    agent = raw["agent"]
    if not isinstance(agent, Mapping) or set(agent) != {"id", "version", "purpose"}:
        raise ManifestError("agent identity, version, and purpose are required")
    _identifier(agent["id"], "agent id")
    if not isinstance(agent["version"], str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", agent["version"]):
        raise ManifestError("agent version must be semantic version metadata")
    _text(agent["purpose"], "agent purpose")
    for name in ("capabilities", "supported_tasks", "required_tools", "required_permissions", "limitations"):
        _strings(raw[name], name, identifiers=name in {"capabilities", "supported_tasks"})
    if not raw["capabilities"]:
        raise ManifestError("at least one declared capability is required")
    adapter, verification, readiness = raw["adapter"], raw["verification"], raw["execution_readiness"]
    if not isinstance(adapter, Mapping) or set(adapter) != {"available"} or not isinstance(adapter["available"], bool):
        raise ManifestError("adapter availability must be explicit")
    if not isinstance(verification, Mapping) or set(verification) != {"status", "evidence", "uncertainty"}:
        raise ManifestError("verification status, evidence, and uncertainty are required")
    if not isinstance(verification["status"], str) or verification["status"] not in {
        status.value for status in CapabilityStatus
    }:
        raise ManifestError("manifest verification status is unsupported")
    _strings(verification["evidence"], "verification evidence")
    _strings(verification["uncertainty"], "verification uncertainty")
    if verification["status"] == CapabilityStatus.VERIFIED.value:
        if not verification["evidence"]:
            raise ManifestError("verified capabilities require verification evidence")
        if verification["uncertainty"]:
            raise ManifestError("verified capabilities cannot carry unresolved verification uncertainty")
    elif verification["evidence"]:
        raise ManifestError("unverified status cannot carry trusted verification evidence")
    elif not verification["uncertainty"]:
        raise ManifestError("unverified status requires an uncertainty explanation")
    if not isinstance(readiness, Mapping) or set(readiness) != {"ready"} or not isinstance(readiness["ready"], bool):
        raise ManifestError("execution readiness must be explicit")
    if readiness["ready"] and not adapter["available"]:
        raise ManifestError("execution readiness requires an available adapter")
    if raw["execution_authorized"] is not False or raw["runtime_available"] is not False:
        raise ManifestError("manifests cannot grant authority or assert runtime availability")
    if readiness["ready"] and verification["status"] != CapabilityStatus.VERIFIED.value:
        raise ManifestError("execution readiness requires verified metadata")
    _revision(raw["source_revision"])
    try:
        json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ManifestError("manifest must be JSON serializable") from None
    return dict(raw)


def _revision(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ManifestError("source revision must be a full lowercase Git object ID or null")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000 or any(ord(c) < 32 for c in value):
        raise ManifestError(f"{label} must be safe, non-empty text")


def _strings(value: Any, label: str, *, identifiers: bool = False) -> None:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ManifestError(f"{label} must be a string list")
    if len(set(value)) != len(value):
        raise ManifestError(f"{label} must not contain duplicates")
    for item in value:
        _identifier(item, label) if identifiers else _text(item, label)


def _identifier(value: Any, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ManifestError(f"{label} must be a stable identifier")
