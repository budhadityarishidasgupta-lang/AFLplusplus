"""Read-only verification of declared agent contracts and adapter metadata.

Verification is limited to static metadata/source-reference checks. It never
imports or invokes agent modules, and VERIFIED never means execution succeeded.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.agent_contracts import AgentContract, CapabilityStatus
from control_plane.agent_discovery import AgentDiscovery, AgentSource, DiscoveredAgent

_PERMISSION_KNOWLEDGE = frozenset(
    {
        "read-local-source",
        "publish-plan-to-in-memory-workspace",
        "in-memory-payload-generation",
        "consume-simulated-feedback",
        "read-declared-campaign-scope",
        "halt-configured-browser-adapter",
        "write-audit-and-budget-alert",
    }
)
_TASKS_BY_CAPABILITY = {
    "local-python-static-analysis": frozenset({"inspect-local-python-source"}),
    "fuzzing-plan-generation": frozenset({"form-local-fuzzing-plan"}),
    "seed-pool-generation": frozenset({"initialize-byte-seeds"}),
    "genetic-payload-mutation": frozenset({"mutate-byte-payload"}),
    "coverage-feedback-scoring": frozenset({"score-target-feedback"}),
    "bounded-campaign-preflight": frozenset({"prepare-campaign"}),
    "attempt-budget-enforcement": frozenset({"enforce-attempt-budget"}),
    "boundary-approval-gating": frozenset({"request-manual-approval"}),
}


@dataclass(frozen=True)
class CapabilityAssessment:
    capability: str
    status: CapabilityStatus
    evidence: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    execution_ready: bool
    required_permissions: tuple[str, ...]
    last_verified_at: str | None
    uncertainty_reason: str | None
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "missing_requirements": list(self.missing_requirements),
            "execution_readiness": {
                "ready": self.execution_ready,
                "reason": "No execution adapter is available; no agent execution was attempted."
                if not self.execution_ready
                else "Execution readiness assessed separately from metadata verification.",
            },
            "required_permissions": list(self.required_permissions),
            "last_verified_at": self.last_verified_at,
            "uncertainty_reason": self.uncertainty_reason,
            "description": self.description,
        }


@dataclass(frozen=True)
class AgentVerification:
    identity: str
    version: str
    status: CapabilityStatus
    evidence: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    execution_ready: bool
    last_verified_at: str | None
    uncertainty_reason: str | None
    capabilities: tuple[CapabilityAssessment, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "version": self.version,
            "verification_status": self.status.value,
            "evidence": list(self.evidence),
            "missing_requirements": list(self.missing_requirements),
            "execution_readiness": {
                "ready": self.execution_ready,
                "reason": "No command-capable execution integration is available; agent remains execution-unverified."
                if not self.execution_ready
                else "Readiness is tracked independently and does not constitute authorization.",
            },
            "last_verified_at": self.last_verified_at,
            "uncertainty_reason": self.uncertainty_reason,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
        }


class CapabilityVerifier:
    """Assess source-backed discovery records without loading the referenced modules."""

    def __init__(self, workspace: str | Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path(__file__).resolve().parents[1]

    def verify_all(self, discovery: AgentDiscovery) -> tuple[AgentVerification, ...]:
        return tuple(self.verify(agent) for agent in discovery.discover_existing())

    def verify(self, agent: DiscoveredAgent) -> AgentVerification:
        contract, source = agent.contract, agent.source
        evidence: list[str] = [f"Identity {contract.agent_id!r} is present and valid."]
        missing: list[str] = []
        uncertainty: list[str] = []
        file_path = self.workspace / source.source_file

        if not self._version_valid(contract.version):
            missing.append("version is not valid semantic version metadata")
        else:
            evidence.append(f"Version {contract.version} is syntactically valid metadata.")

        if not contract.purpose or not contract.capabilities:
            missing.append("agent identity, purpose, and capabilities must be complete")
        else:
            evidence.append("Purpose and capability declarations are non-empty.")

        if not contract.supported_tasks:
            missing.append("at least one supported task type must be declared")
        elif any(not self._task_supported(capability, contract.supported_tasks) for capability in contract.capabilities):
            missing.append("one or more declared capabilities lack a recognized supported task type")
        else:
            evidence.append("Supported task types are declared for every capability.")

        if not contract.inputs or not contract.outputs:
            missing.append("input and output contracts must both be declared")
        else:
            evidence.append("Input and output contract mappings contain named descriptions.")

        unknown_permissions = sorted(set(contract.permissions) - _PERMISSION_KNOWLEDGE)
        if unknown_permissions:
            missing.append("permissions require interpretation: " + ", ".join(unknown_permissions))
            uncertainty.append("One or more permission labels have no local policy definition.")
        else:
            evidence.append("Declared permissions are recognized metadata labels; none are granted.")

        if not self._adapter_shape_compatible(agent):
            missing.append("adapter does not expose a compatible read-only contract property")
        else:
            evidence.append("Read-only adapter contract shape is compatible; it exposes no execution operation.")
        if not source.execution_integration_available:
            missing.append("execution integration is unavailable; agent cannot currently be commanded")

        source_ok, source_detail = self._inspect_source(file_path, source)
        if source_ok:
            evidence.append(source_detail)
        else:
            missing.append(source_detail)
            uncertainty.append("Source evidence could not be integrity-checked against the expected module reference.")

        if not source.limitations:
            uncertainty.append("Known limitations are not documented.")
        else:
            evidence.append("Known limitations are recorded.")

        capabilities = tuple(
            self._capability_assessment(contract, source, name, tuple(evidence), tuple(missing), tuple(uncertainty))
            for name in contract.capabilities
        )

        if any("source reference" in item or "permission" in item for item in missing):
            status = CapabilityStatus.UNKNOWN
        elif any(item != "execution integration is unavailable; agent cannot currently be commanded" for item in missing):
            status = CapabilityStatus.UNDERSTOOD
        else:
            status = CapabilityStatus.VERIFIED
        if not source.execution_integration_available:
            # Contract verification remains meaningful, but this status is used
            # only for agent availability where a required command path is absent.
            evidence.append("No execution integration is declared; no agent was run.")

        return AgentVerification(
            identity=contract.agent_id,
            version=contract.version,
            status=status,
            evidence=tuple(evidence),
            missing_requirements=tuple(sorted(set(missing))),
            execution_ready=source.execution_integration_available and not missing,
            last_verified_at=datetime.now(timezone.utc).isoformat() if status is CapabilityStatus.VERIFIED else None,
            uncertainty_reason=" ".join(sorted(set(uncertainty))) or None,
            capabilities=capabilities,
        )

    def _capability_assessment(
        self,
        contract: AgentContract,
        source: AgentSource,
        capability: str,
        evidence: tuple[str, ...],
        missing: tuple[str, ...],
        uncertainty: tuple[str, ...],
    ) -> CapabilityAssessment:
        if capability not in _TASKS_BY_CAPABILITY:
            status = CapabilityStatus.UNKNOWN
            reason = "No local task mapping establishes what this capability means."
        elif any("permissions" in item or "source reference" in item for item in missing):
            status = CapabilityStatus.UNKNOWN
            reason = "Required permission or source-integrity evidence is unresolved."
        elif any(item != "execution integration is unavailable; agent cannot currently be commanded" for item in missing):
            status = CapabilityStatus.UNDERSTOOD
            reason = "Capability is described, but one or more contract checks are incomplete."
        elif not source.execution_integration_available:
            status = CapabilityStatus.VERIFIED
            reason = "Metadata and interface checks passed; execution integration is unavailable."
        else:
            status = CapabilityStatus.VERIFIED
            reason = None
        capability_tasks = _TASKS_BY_CAPABILITY.get(capability, frozenset())
        task_evidence = tuple(
            f"Declared supported task: {task}" for task in contract.supported_tasks if task in capability_tasks
        )
        return CapabilityAssessment(
            capability=capability,
            status=status,
            evidence=tuple(sorted(set(evidence + task_evidence))),
            missing_requirements=tuple(sorted(set(missing))),
            execution_ready=False,
            required_permissions=contract.permissions,
            last_verified_at=datetime.now(timezone.utc).isoformat() if status is CapabilityStatus.VERIFIED else None,
            uncertainty_reason=reason or (" ".join(uncertainty) if uncertainty else None),
            description=f"Declared capability: {capability.replace('-', ' ')}.",
        )

    @staticmethod
    def _version_valid(version: str) -> bool:
        return bool(re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version))

    @staticmethod
    def _task_supported(capability: str, tasks: tuple[str, ...]) -> bool:
        required = _TASKS_BY_CAPABILITY.get(capability)
        return required is None or bool(required.intersection(tasks))

    @staticmethod
    def _adapter_shape_compatible(agent: DiscoveredAgent) -> bool:
        # This is a structural check on local declarative dataclasses only. No
        # legacy adapter instance or module is inspected or loaded.
        return isinstance(agent.contract, AgentContract) and bool(agent.contract.agent_id)

    def _inspect_source(self, source_path: Path, source: AgentSource) -> tuple[bool, str]:
        try:
            resolved = source_path.resolve(strict=True)
            if self.workspace not in resolved.parents:
                return False, "source reference resolves outside the configured workspace"
            content = resolved.read_bytes()
            ast.parse(content.decode("utf-8"), filename=str(resolved))
        except (OSError, UnicodeError, SyntaxError) as error:
            return False, f"source reference unavailable or invalid ({type(error).__name__})"
        expected_module = resolved.stem
        if expected_module != source.module:
            return False, f"source module mismatch: expected {source.module}, found {expected_module}"
        digest = hashlib.sha256(content).hexdigest()
        return True, f"Source reference {source.source_file} exists, parses statically, and has SHA-256 {digest}."


def verification_report(
    discovery: AgentDiscovery, workspace: str | Path | None = None
) -> dict[str, Any]:
    """Return Brain-consumable JSON data with verification/readiness kept distinct."""
    verifier = CapabilityVerifier(workspace)
    reports = verifier.verify_all(discovery)
    return {
        "agents": [report.to_dict() for report in reports],
        "execution_verified": False,
        "execution_note": "Contract verification is static metadata inspection only; no agent has been executed.",
        "permissions_granted": False,
        "capability_states": [
            {
                "agent": report.identity,
                "capability": capability.capability,
                "state": capability.status.value,
                "description": capability.description,
                "readiness": capability.execution_ready,
                "reason": capability.uncertainty_reason,
            }
            for report in reports
            for capability in report.capabilities
        ],
    }
