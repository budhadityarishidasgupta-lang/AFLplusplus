from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from control_plane.agent_contracts import AgentContract, CapabilityStatus
from control_plane.agent_discovery import AgentDiscovery, AgentSource, DiscoveredAgent
from control_plane.capability_verification import CapabilityVerifier, verification_report


class CapabilityVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.discovery = AgentDiscovery()
        self.agents = self.discovery.discover_existing()
        self.verifier = CapabilityVerifier(self.root)

    def test_static_contract_verification_passes_without_execution(self) -> None:
        report = self.verifier.verify(self.agents[0])
        self.assertEqual(report.status, CapabilityStatus.VERIFIED)
        self.assertFalse(report.execution_ready)
        self.assertTrue(report.evidence)
        self.assertTrue(report.last_verified_at)
        self.assertTrue(all(cap.status is CapabilityStatus.VERIFIED for cap in report.capabilities))
        self.assertTrue(all(not cap.execution_ready for cap in report.capabilities))

    def test_missing_contract_fields_are_reported_without_execution(self) -> None:
        incomplete = replace(self.agents[0].contract, inputs={}, outputs={})
        candidate = replace(self.agents[0], contract=incomplete)
        report = self.verifier.verify(candidate)
        self.assertIn("input and output contracts must both be declared", report.missing_requirements)
        self.assertFalse(report.execution_ready)
        self.assertIsNone(report.last_verified_at)

    def test_unsupported_task_mapping_is_reported(self) -> None:
        unsupported_task_contract = replace(
            self.agents[0].contract,
            supported_tasks=("unrecognized-task-type",),
        )
        report = self.verifier.verify(replace(self.agents[0], contract=unsupported_task_contract))
        self.assertTrue(any("lack a recognized" in item for item in report.missing_requirements))
        self.assertFalse(report.execution_ready)

    def test_unknown_permission_label_blocks_verified_assessment(self) -> None:
        contract = replace(self.agents[0].contract, permissions=("unreviewed-permission",))
        report = self.verifier.verify(replace(self.agents[0], contract=contract))
        self.assertEqual(report.status, CapabilityStatus.UNKNOWN)
        self.assertTrue(any("permissions require interpretation" in item for item in report.missing_requirements))
        self.assertFalse(report.execution_ready)

    def test_missing_execution_integration_is_separate_from_static_verification(self) -> None:
        original = self.agents[0]
        without_integration = replace(
            original,
            source=replace(original.source, execution_integration_available=False),
        )
        report = self.verifier.verify(without_integration)
        self.assertEqual(report.status, CapabilityStatus.VERIFIED)
        self.assertFalse(report.execution_ready)
        self.assertTrue(all(item.status is CapabilityStatus.VERIFIED for item in report.capabilities))

    def test_source_reference_integrity_mismatch_is_unknown(self) -> None:
        original = self.agents[0]
        mismatched = replace(original, source=replace(original.source, source_file="missing_agent_source.py"))
        report = self.verifier.verify(mismatched)
        self.assertEqual(report.status, CapabilityStatus.UNKNOWN)
        self.assertTrue(any("source reference unavailable" in item for item in report.missing_requirements))

    def test_status_assessment_is_deterministic_except_timestamp(self) -> None:
        first = self.verifier.verify(self.agents[0])
        second = self.verifier.verify(self.agents[0])
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.evidence, second.evidence)
        self.assertEqual(first.missing_requirements, second.missing_requirements)
        self.assertEqual(
            [(cap.capability, cap.status, cap.execution_ready) for cap in first.capabilities],
            [(cap.capability, cap.status, cap.execution_ready) for cap in second.capabilities],
        )

    def test_execution_readiness_never_confused_with_verified_metadata(self) -> None:
        report = self.verifier.verify(self.agents[0])
        self.assertEqual(report.status, CapabilityStatus.VERIFIED)
        self.assertFalse(report.execution_ready)
        self.assertIn("No command-capable execution integration", report.to_dict()["execution_readiness"]["reason"])

    def test_brain_facing_report_is_json_serializable_and_natural(self) -> None:
        payload = verification_report(self.discovery, self.root)
        data = json.loads(json.dumps(payload, sort_keys=True))
        self.assertFalse(data["execution_verified"])
        self.assertFalse(data["permissions_granted"])
        self.assertEqual(len(data["agents"]), 3)
        self.assertTrue(any("Declared capability" in item["description"] for item in data["capability_states"]))

    def test_no_legacy_imports_and_no_execution_side_effects(self) -> None:
        # Static verification parses bytes and AST only; it never imports a source module.
        import sys

        for name in ("multi_agent_system", "conversational_launcher", "enterprise_stealth_range"):
            self.assertNotIn(name, sys.modules)
        for agent in self.agents:
            self.verifier.verify(agent)
        for name in ("multi_agent_system", "conversational_launcher", "enterprise_stealth_range"):
            self.assertNotIn(name, sys.modules)

    def test_legacy_sources_match_baseline_hashes(self) -> None:
        import hashlib

        expected = {
            "multi_agent_system.py": "9dedbb2029caf7e763b5e24401aae3804a071bf8b6a3f822a3280388769f5baa",
            "conversational_launcher.py": "6adce0d0f53a6856945db758936a4127db3fcf74ec524a7deb8230d94012fa1e",
            "enterprise_stealth_range.py": "b9ff7c07b0f19052cf7831cd2590efd2b8d373eefad836d05a3191ceb460d42f",
        }
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((self.root / filename).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
