from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from control_plane.agent_contracts import CapabilityRegistry, CapabilityStatus
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_manifest import ManifestError, generate_manifest, validate_manifest
from control_plane.capability_verification import CapabilityVerifier


REVISION = "a" * 40


class CapabilityManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]
        self.discovery = AgentDiscovery(CapabilityRegistry())
        self.agent = self.discovery.discover_existing()[0]
        self.verification = CapabilityVerifier(self.root).verify(self.agent)

    def test_manifest_generation_uses_verified_discovered_contract(self) -> None:
        manifest = generate_manifest(self.agent, verification=self.verification, source_revision=REVISION)
        encoded = json.dumps(manifest.to_dict(), sort_keys=True)
        self.assertIn("Only local Python source is inspected", encoded)
        self.assertFalse(manifest.execution_ready)
        self.assertFalse(manifest.to_dict()["execution_authorized"])
        self.assertFalse(manifest.to_dict()["runtime_available"])
        self.assertEqual(validate_manifest(manifest)["agent"]["id"], self.agent.contract.agent_id)
        self.assertIsNone(validate_manifest({**manifest.to_dict(), "source_revision": None})["source_revision"])

    def test_discovered_capability_without_verification_is_valid_and_remains_unknown(self) -> None:
        registration = self.discovery.registry.discover(self.agent.contract.agent_id)
        manifest = generate_manifest(registration, source=self.agent.source, source_revision=REVISION)
        raw = validate_manifest(manifest)
        self.assertEqual(raw["verification"]["status"], CapabilityStatus.UNKNOWN.value)
        self.assertEqual(raw["verification"]["evidence"], [])
        self.assertTrue(raw["verification"]["uncertainty"])
        self.assertFalse(raw["execution_readiness"]["ready"])
        self.assertFalse(raw["execution_authorized"])
        self.assertFalse(raw["runtime_available"])

    def test_unverified_or_missing_evidence_cannot_be_promoted_to_verified(self) -> None:
        registration = self.discovery.registry.discover(self.agent.contract.agent_id)
        unknown = generate_manifest(registration, source=self.agent.source, source_revision=REVISION).to_dict()
        unknown["verification"]["status"] = CapabilityStatus.VERIFIED.value
        with self.assertRaises(ManifestError):
            validate_manifest(unknown)
        fabricated = generate_manifest(
            self.agent, verification=self.verification, source_revision=REVISION
        ).to_dict()
        fabricated["verification"]["evidence"] = []
        with self.assertRaises(ManifestError):
            validate_manifest(fabricated)
        unknown["verification"]["status"] = CapabilityStatus.UNKNOWN.value
        unknown["verification"]["uncertainty"] = []
        with self.assertRaises(ManifestError):
            validate_manifest(unknown)

    def test_unverified_registry_status_keeps_verified_report_unknown(self) -> None:
        registration = self.discovery.registry.discover(self.agent.contract.agent_id)
        unverified_registration = replace(registration, status=CapabilityStatus.DISCOVERED)
        manifest = generate_manifest(unverified_registration, source=self.agent.source,
                                     verification=self.verification, source_revision=REVISION)
        self.assertEqual(validate_manifest(manifest)["verification"]["status"],
                         CapabilityStatus.UNKNOWN.value)
        self.assertFalse(manifest.execution_ready)

    def test_nonverified_verification_report_stays_unknown_not_understood_or_discovered(self) -> None:
        manifest = generate_manifest(
            self.agent,
            verification=replace(self.verification, status=CapabilityStatus.UNDERSTOOD),
            source_revision=REVISION,
        )
        raw = validate_manifest(manifest)
        self.assertEqual(raw["verification"]["status"], CapabilityStatus.UNKNOWN.value)
        self.assertEqual(raw["verification"]["evidence"], [])
        self.assertTrue(raw["verification"]["uncertainty"])
        self.assertFalse(raw["execution_readiness"]["ready"])

    def test_mismatched_verification_report_and_revision_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            generate_manifest(self.agent, verification=replace(self.verification, identity="other.agent"),
                              source_revision=REVISION)
        with self.assertRaises(ManifestError):
            generate_manifest(self.agent, verification=self.verification, source_revision="abc")

    def test_manifest_schema_version_and_forbidden_authority_are_validated(self) -> None:
        manifest = generate_manifest(self.agent, verification=self.verification, source_revision=REVISION).to_dict()
        manifest["schema_version"] = 99
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)
        manifest["schema_version"] = 1
        manifest["runtime_available"] = True
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)

    def test_invalid_manifest_fields_and_missing_verified_evidence_fail_closed(self) -> None:
        manifest = generate_manifest(self.agent, verification=self.verification, source_revision=REVISION).to_dict()
        missing = dict(manifest)
        missing.pop("capabilities")
        with self.assertRaises(ManifestError):
            validate_manifest(missing)
        manifest["verification"]["evidence"] = []
        with self.assertRaises(ManifestError):
            validate_manifest(manifest)

    def test_manifest_does_not_infer_from_filename_or_version_metadata(self) -> None:
        with self.assertRaises(ManifestError):
            generate_manifest(self.agent.contract, verification=self.verification, source_revision=REVISION)  # type: ignore[arg-type]
        other_contract = replace(self.agent.contract, version="2.0.0")
        other_agent = replace(self.agent, contract=other_contract)
        with self.assertRaises(ManifestError):
            generate_manifest(other_agent, verification=self.verification, source_revision=REVISION)


if __name__ == "__main__":
    unittest.main()
