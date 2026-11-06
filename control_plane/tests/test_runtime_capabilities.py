from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from control_plane.agent_contracts import CapabilityRegistry
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_manifest import generate_manifest
from control_plane.capability_verification import CapabilityVerifier
from control_plane.runtime_capabilities import (
    RuntimeCapabilityError, RuntimeCapabilityVerifier, RuntimeEvidence, RuntimeState,
)


PUBLISHED_REV = "a" * 40
RUNNING_REV = "b" * 40


class RuntimeCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2]
        agent = AgentDiscovery(CapabilityRegistry()).discover_existing()[0]
        self.manifest = generate_manifest(agent, verification=CapabilityVerifier(root).verify(agent),
                                          source_revision=PUBLISHED_REV).to_dict()
        self.published = [self.manifest]
        self.installed = [deepcopy(self.manifest)]
        self.verifier = RuntimeCapabilityVerifier()
        self.key = (self.manifest["agent"]["id"], self.manifest["capabilities"][0])

    def evidence(self, **changes: object) -> RuntimeEvidence:
        values = dict(
            agent_id=self.key[0], capability=self.key[1], installed_revision=PUBLISHED_REV,
            software_revision=RUNNING_REV, adapter_id="synthetic.adapter", adapter_registered=True,
            interface_compatible=True, dependencies=("python-runtime",), missing_dependencies=(),
            execution_integration_verified=True,
            permissions=tuple(self.manifest["required_permissions"]), health_check_passed=True,
            version_compatible=True,
        )
        values.update(changes)
        return RuntimeEvidence(**values)  # type: ignore[arg-type]

    def verify(self, evidence: tuple[RuntimeEvidence, ...] = (), *, installed: list[dict] | None = None,
               published: list[dict] | None = None):
        return self.verifier.verify(published or self.published,
                                    self.installed if installed is None else installed,
                                    evidence, running_revision=RUNNING_REV)

    def test_published_but_not_deployed(self) -> None:
        item = self.verify(installed=[]).assessments[0]
        self.assertEqual(item.state, RuntimeState.NOT_DEPLOYED)
        self.assertTrue(item.published)
        self.assertFalse(item.installed)
        self.assertFalse(item.execution_eligible)

    def test_installed_but_not_loaded(self) -> None:
        item = self.verify().assessments[0]
        self.assertEqual(item.state, RuntimeState.INSTALLED)
        self.assertTrue(item.installed)
        self.assertFalse(item.loaded)

    def test_loaded_but_unverified(self) -> None:
        evidence = self.evidence(health_check_passed=False)
        item = self.verify((evidence,)).assessments[0]
        self.assertEqual(item.state, RuntimeState.LOADED)
        self.assertTrue(item.loaded)
        self.assertFalse(item.verified)
        self.assertIn("runtime health evidence is missing or failed", item.reasons)

    def test_synthetic_adapter_can_be_locally_verified_available_without_authorizing(self) -> None:
        report = self.verify((self.evidence(),))
        item = report.assessments[0]
        self.assertEqual(item.state, RuntimeState.VERIFIED_AVAILABLE)
        self.assertTrue(item.execution_eligible)
        self.assertFalse(item.execution_authorized)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.to_dict()["execution_authorized"])

    def test_missing_dependencies_and_unavailable_agents(self) -> None:
        missing = self.evidence(missing_dependencies=("python-runtime",))
        self.assertEqual(self.verify((missing,)).assessments[0].state, RuntimeState.UNAVAILABLE)
        unavailable = self.evidence(unavailable=True)
        self.assertEqual(self.verify((unavailable,)).assessments[0].state, RuntimeState.UNAVAILABLE)

    def test_version_and_revision_mismatch_fail_closed(self) -> None:
        changed = deepcopy(self.installed)
        changed[0]["agent"]["version"] = "2.0.0"
        self.assertEqual(self.verify(installed=changed).assessments[0].state, RuntimeState.UNAVAILABLE)
        changed_version = deepcopy(self.published)
        changed_version[0]["agent"]["version"] = "2.0.0"
        item = self.verifier.verify(changed_version, self.installed, (),
                                    running_revision=RUNNING_REV).assessments[0]
        self.assertEqual(item.state, RuntimeState.UNAVAILABLE)
        evidence = self.evidence(installed_revision="c" * 40)
        result = self.verify((evidence,))
        self.assertEqual(result.assessments[0].state, RuntimeState.UNAVAILABLE)
        evidence = self.evidence(software_revision="c" * 40)
        with self.assertRaises(RuntimeCapabilityError):
            self.verify((evidence,))
        changed_revision = deepcopy(self.installed)
        changed_revision[0]["source_revision"] = "c" * 40
        self.assertEqual(self.verify(installed=changed_revision).assessments[0].state,
                         RuntimeState.UNAVAILABLE)

    def test_invalid_untrusted_or_conflicting_evidence_is_rejected(self) -> None:
        with self.assertRaises(RuntimeCapabilityError):
            self.evidence(locally_established=False)
        with self.assertRaises(RuntimeCapabilityError):
            self.evidence(adapter_registered=False, health_check_passed=True)
        with self.assertRaises(RuntimeCapabilityError):
            self.verifier.verify(self.published, self.installed, ({"agent_id": "external"},),
                                 running_revision=RUNNING_REV)  # type: ignore[arg-type]
        with self.assertRaises(RuntimeCapabilityError):
            self.verify((self.evidence(), self.evidence()))

    def test_conflicting_and_invalid_manifests_rejected(self) -> None:
        with self.assertRaises(RuntimeCapabilityError):
            self.verify(installed=[self.manifest, deepcopy(self.manifest)])
        conflicting = deepcopy(self.manifest)
        conflicting["required_permissions"].append("other-permission")
        with self.assertRaises(RuntimeCapabilityError):
            self.verifier.verify(self.published, [conflicting], (), running_revision=RUNNING_REV)
        invalid = deepcopy(self.manifest)
        invalid["runtime_available"] = True
        with self.assertRaises(RuntimeCapabilityError):
            self.verify(installed=[invalid])
        with self.assertRaises(RuntimeCapabilityError):
            self.verifier.verify(self.published, self.installed, (), running_revision="unknown")

    def test_unknown_static_manifest_cannot_be_promoted_by_runtime_evidence(self) -> None:
        unverified = deepcopy(self.manifest)
        unverified["verification"]["status"] = "unknown"
        unverified["verification"]["evidence"] = []
        unverified["verification"]["uncertainty"] = ["static verification absent"]
        result = self.verifier.verify([self.manifest], [unverified], (self.evidence(),),
                                      running_revision=RUNNING_REV)
        self.assertEqual(result.assessments[0].state, RuntimeState.UNKNOWN)
        self.assertFalse(result.assessments[0].verified)

    def test_unverified_adapter_requirements_and_permissions_remain_blocking(self) -> None:
        evidence = self.evidence(execution_integration_verified=False)
        result = self.verify((evidence,))
        item = result.assessments[0]
        self.assertEqual(item.state, RuntimeState.LOADED)
        self.assertFalse(item.execution_eligible)
        self.assertTrue(any("execution integration" in reason for reason in item.reasons))
        changed = self.evidence(permissions=())
        self.assertFalse(self.verify((changed,)).assessments[0].execution_eligible)

    def test_brain_summary_is_accurate_json_safe_and_avoids_technical_ids(self) -> None:
        result = self.verify()
        data = result.to_dict()
        json.dumps(data)
        text = result.brain_summary()["message"]
        self.assertIn("cannot claim", text)
        self.assertNotIn(self.key[0], text)
        self.assertNotIn(self.key[1], text)
        published_only = self.verify(installed=[]).brain_summary()["message"]
        self.assertIn("not installed", published_only)

    def test_static_metadata_verification_does_not_fabricate_runtime_readiness(self) -> None:
        self.assertEqual(self.manifest["verification"]["status"], "verified")
        item = self.verify().assessments[0]
        self.assertEqual(item.state, RuntimeState.INSTALLED)
        self.assertFalse(item.verified)
        self.assertFalse(item.execution_eligible)

    def test_no_legacy_agent_imports_or_execution_are_present(self) -> None:
        source = Path(__file__).resolve().parents[1] / "runtime_capabilities.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("import multi_agent_system", text)
        self.assertNotIn("import enterprise_stealth_range", text)
        self.assertNotIn("import conversational_launcher", text)
        self.assertNotIn("subprocess", text)
        self.assertNotIn("importlib", text)


if __name__ == "__main__":
    unittest.main()
