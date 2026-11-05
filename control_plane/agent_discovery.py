"""Explicit source-backed, metadata-only discovery of existing agent roles.

This module describes source files and never imports, instantiates, or invokes
legacy modules. Records deliberately distinguish agents from shared controllers,
utilities, browser abstractions, and simulated target infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from control_plane.agent_contracts import (
    AgentContract,
    AgentNotFoundError,
    AgentRegistration,
    CapabilityNotFoundError,
    CapabilityRegistry,
    CapabilityStatus,
    ValidationError,
)


@dataclass(frozen=True)
class AgentSource:
    """Traceability and evidence limits for one source-backed agent role."""

    module: str
    source_file: str
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    tool_requirements: tuple[str, ...]
    internet_access: str
    execution_integration_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "source_file": self.source_file,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "tool_requirements": list(self.tool_requirements),
            "internet_access": self.internet_access,
            "execution_integration_available": self.execution_integration_available,
        }


@dataclass(frozen=True)
class DiscoveredAgent:
    contract: AgentContract
    source: AgentSource
    status: CapabilityStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.contract.agent_id,
            "purpose": self.contract.purpose,
            "capabilities": list(self.contract.capabilities),
            "supported_tasks": list(self.contract.supported_tasks),
            "inputs": dict(self.contract.inputs),
            "outputs": dict(self.contract.outputs),
            "tool_requirements": list(self.source.tool_requirements),
            "internet_access": self.source.internet_access,
            "permissions_and_restrictions": list(self.contract.permissions),
            "limitations": list(self.source.limitations),
            "execution_integration_available": self.source.execution_integration_available,
            "source": self.source.to_dict(),
            "status": self.status.value,
            "understanding": {
                "discovered": True,
                "declared_capabilities_understood": self.status is CapabilityStatus.UNDERSTOOD,
                "execution_verified": False,
                "capability_support_determinable": self.status is not CapabilityStatus.UNKNOWN,
                "permission_granted": False,
            },
        }


@dataclass(frozen=True)
class UnsupportedCapability:
    capability: str
    status: CapabilityStatus = CapabilityStatus.UNKNOWN
    message: str = "Capability support cannot be determined from inspected source."

    def to_dict(self) -> dict[str, str]:
        return {"capability": self.capability, "status": self.status.value, "message": self.message}


@dataclass(frozen=True)
class DiscoveryResult:
    agents: tuple[DiscoveredAgent, ...]
    unsupported_capabilities: tuple[UnsupportedCapability, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": [agent.to_dict() for agent in self.agents],
            "unsupported_capabilities": [item.to_dict() for item in self.unsupported_capabilities],
            "execution_verified": False,
            "permissions_granted": False,
        }


# Evidence is limited to the inspected source's classes, docstrings, method
# signatures, and explicit constants. No source code is loaded by discovery.
_AGENT_DECLARATIONS: tuple[tuple[AgentContract, AgentSource], ...] = (
    (
        AgentContract(
            agent_id="multi-agent.local-recon",
            purpose="Statically inspect a local Python target and form a structured fuzzing plan.",
            version="1.0.0",
            capabilities=("local-python-static-analysis", "fuzzing-plan-generation"),
            supported_tasks=("inspect-local-python-source", "form-local-fuzzing-plan"),
            inputs={"human_instructions": "Non-empty planning instructions", "source_path": "Path to local Python source"},
            outputs={"attack_plan": "Structured plan with target, payload constraints, criteria, and metadata"},
            permissions=("read-local-source", "publish-plan-to-in-memory-workspace"),
        ),
        AgentSource(
            module="multi_agent_system",
            source_file="multi_agent_system.py",
            evidence=("LocalReconAgent", "inspect_local_target reads source and parses AST", "process_directives publishes AttackPlan"),
            limitations=("Only local Python source is inspected", "No general vulnerability analysis is declared"),
            tool_requirements=("local filesystem read", "Python AST parser"),
            internet_access="none evidenced",
            execution_integration_available=False,
        ),
    ),
    (
        AgentContract(
            agent_id="multi-agent.evolutionary-exploit-dev",
            purpose="Generate and mutate byte payloads and score feedback for the local simulated fuzzing workflow.",
            version="1.0.0",
            capabilities=("seed-pool-generation", "genetic-payload-mutation", "coverage-feedback-scoring"),
            supported_tasks=("initialize-byte-seeds", "mutate-byte-payload", "score-target-feedback"),
            inputs={"attack_plan": "Structured AttackPlan", "target_response": "Typed simulated target response", "parent_payload": "Bytes"},
            outputs={"seed_pool": "Generated byte payload list", "mutated_payload": "Mutated bytes", "fitness": "Numeric feedback score"},
            permissions=("in-memory-payload-generation", "consume-simulated-feedback"),
        ),
        AgentSource(
            module="multi_agent_system",
            source_file="multi_agent_system.py",
            evidence=("EvolutionaryExploitDevAgent", "initialize_seed_pool", "apply_genetic_mutation", "evaluate_feedback"),
            limitations=("Designed for the local mock workflow", "No independent external-target capability evidenced"),
            tool_requirements=("Python random module", "in-memory AttackPlan and target feedback"),
            internet_access="none evidenced",
            execution_integration_available=False,
        ),
    ),
    (
        AgentContract(
            agent_id="enterprise-range.system-architect",
            purpose="Coordinate bounded authorized web-validation pipeline state, stop/approval decisions, and audit events.",
            version="1.0.0",
            capabilities=("bounded-campaign-preflight", "attempt-budget-enforcement", "boundary-approval-gating"),
            supported_tasks=("prepare-campaign", "enforce-attempt-budget", "request-manual-approval"),
            inputs={"campaign_scope": "Validated CampaignScope", "attempts": "Attempt count", "operator_response": "Manual approval response"},
            outputs={"preflight": "Worker phase and scoped target metadata", "audit_events": "AuditTrail records", "pipeline_status": "PipelineStatus"},
            permissions=("read-declared-campaign-scope", "halt-configured-browser-adapter", "write-audit-and-budget-alert"),
        ),
        AgentSource(
            module="enterprise_stealth_range",
            source_file="enterprise_stealth_range.py",
            evidence=("SystemArchitect.preflight", "enforce_attempt_budget", "boundary_breach", "request_manual_approval"),
            limitations=("Requires injected browser adapter for halting", "No browser actions are implemented by SystemArchitect"),
            tool_requirements=("injected BrowserAdapter when halt is needed", "local audit/alert filesystem"),
            internet_access="not performed by this class; underlying browser/network behavior not verified",
            execution_integration_available=False,
        ),
    ),
)


class AgentDiscovery:
    """Register source-backed descriptors and expose conservative capability summaries."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CapabilityRegistry()
        self._sources: dict[str, AgentSource] = {}

    def discover_existing(self) -> tuple[DiscoveredAgent, ...]:
        """Register only explicitly catalogued agent roles; no imports or execution."""
        discovered = []
        for contract, source in _AGENT_DECLARATIONS:
            try:
                registration = self.registry.discover(contract.agent_id)
            except AgentNotFoundError:
                registration = self.registry.register(contract, CapabilityStatus.UNDERSTOOD)
            self._sources[contract.agent_id] = source
            discovered.append(self._discovered(registration))
        return tuple(discovered)

    def _discovered(self, registration: AgentRegistration) -> DiscoveredAgent:
        source = self._sources.get(registration.contract.agent_id)
        if source is None:
            raise ValidationError(f"no source evidence registered for {registration.contract.agent_id}")
        return DiscoveredAgent(registration.contract, source, registration.status)

    def lookup_capability(self, capability: str) -> tuple[DiscoveredAgent, ...]:
        """Return registered source-backed support or report support as unknown."""
        registrations = self.registry.query(capability)
        return tuple(self._discovered(registration) for registration in registrations)

    def summarize(self, requested_capabilities: tuple[str, ...] = ()) -> DiscoveryResult:
        agents = tuple(self._discovered(registration) for registration in self.registry.list_agents() if registration.contract.agent_id in self._sources)
        unsupported = []
        for capability in sorted(set(requested_capabilities)):
            try:
                self.registry.query(capability)
            except CapabilityNotFoundError:
                unsupported.append(UnsupportedCapability(capability))
        return DiscoveryResult(agents, tuple(unsupported))
