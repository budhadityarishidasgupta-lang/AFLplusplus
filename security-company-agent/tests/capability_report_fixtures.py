"""Deterministic synthetic SB-34 capability reports for Brain tests."""
from __future__ import annotations


_AGENT_CAPABILITIES = {
    "multi-agent.local-recon": (
        "Statically inspect a local Python target and form a structured fuzzing plan.",
        ("local-python-static-analysis", "fuzzing-plan-generation"),
        ("read-local-source", "publish-plan-to-in-memory-workspace"),
    ),
    "multi-agent.evolutionary-exploit-dev": (
        "Generate and mutate byte payloads and score feedback for the local simulated fuzzing workflow.",
        ("seed-pool-generation", "genetic-payload-mutation", "coverage-feedback-scoring"),
        ("in-memory-payload-generation", "consume-simulated-feedback"),
    ),
    "enterprise-range.system-architect": (
        "Coordinate bounded authorized web-validation pipeline state, stop/approval decisions, and audit events.",
        ("bounded-campaign-preflight", "attempt-budget-enforcement", "boundary-approval-gating"),
        ("read-declared-campaign-scope", "halt-configured-browser-adapter", "write-audit-and-budget-alert"),
    ),
}


def valid_report():
    agents = []
    states = []
    for identity, (purpose, capabilities, permissions) in _AGENT_CAPABILITIES.items():
        capability_records = []
        for name in capabilities:
            capability_records.append({
                "capability": name,
                "status": "understood",
                "evidence": ["Synthetic fixture records the declared capability name."],
                "missing_requirements": ["execution integration is unavailable"],
                "execution_readiness": {
                    "ready": False,
                    "reason": "No execution integration is declared; no agent was run.",
                },
                "required_permissions": list(permissions),
                "last_verified_at": None,
                "uncertainty_reason": "Synthetic static metadata; runtime availability is unknown.",
                "description": f"Declared capability: {name.replace('-', ' ')}.",
            })
            states.append({
                "agent": identity,
                "capability": name,
                "state": "understood",
                "description": f"Declared capability: {name.replace('-', ' ')}.",
                "readiness": False,
                "reason": "No execution integration is declared; no agent was run.",
            })
        agents.append({
            "identity": identity,
            "version": "1.0.0",
            "verification_status": "understood",
            "purpose": purpose,
            "evidence": ["Synthetic fixture lists the existing agent contract identity."],
            "missing_requirements": ["execution integration is unavailable"],
            "execution_readiness": {
                "ready": False,
                "reason": "No execution integration is declared; no agent was run.",
            },
            "last_verified_at": None,
            "uncertainty_reason": "Synthetic metadata only; no runtime checks were performed.",
            "capabilities": capability_records,
        })
    return {
        "schema_version": 1,
        "agents": agents,
        "unsupported_capabilities": [],
        "execution_verified": False,
        "permissions_granted": False,
        "capability_states": states,
    }
