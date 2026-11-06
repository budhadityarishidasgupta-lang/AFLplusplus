from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from control_plane.agent_contracts import CapabilityRegistry
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_changes import (
    CapabilityChangeDiscovery, ChangeError, GitRevisionEvidence, PublicationState,
)
from control_plane.capability_manifest import generate_manifest
from control_plane.capability_verification import CapabilityVerifier


OLD = "a" * 40
NEW = "b" * 40


class CapabilityChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2]
        discovery = AgentDiscovery(CapabilityRegistry())
        agents = discovery.discover_existing()
        verifier = CapabilityVerifier(root)
        self.old = [generate_manifest(agent, verification=verifier.verify(agent), source_revision=OLD).to_dict()
                    for agent in agents]
        self.unknown = [generate_manifest(agent, source_revision=OLD).to_dict()
                        for agent in agents]
        self.new = deepcopy(self.old)
        for manifest in self.new:
            manifest["source_revision"] = NEW
        self.from_git = GitRevisionEvidence(OLD, committed=True, remote_verified=True, remote_revision=OLD)
        self.to_git = GitRevisionEvidence(NEW, committed=True, remote_verified=True, remote_revision=NEW)
        self.discovery = CapabilityChangeDiscovery()

    def test_capability_additions_removals_permissions_and_verification_are_detected(self) -> None:
        self.old[0]["adapter"]["available"] = True
        self.old[0]["execution_readiness"]["ready"] = False
        self.old[0]["verification"]["status"] = "unknown"
        self.old[0]["verification"]["evidence"] = []
        self.old[0]["verification"]["uncertainty"] = ["Verification has not been established."]
        target = self.new[0]
        target["capabilities"].append("new-declared-capability")
        target["required_permissions"].append("new-permission")
        target["required_tools"].append("synthetic-tool")
        target["verification"]["evidence"].append("synthetic evidence")
        target["verification"]["status"] = "verified"
        target["adapter"]["available"] = True
        target["execution_readiness"]["ready"] = True
        result = self.discovery.compare(self.old, self.new, from_git=self.from_git, to_git=self.to_git)
        categories = {item.category for item in result.changes}
        self.assertIn("capability_added", categories)
        self.assertIn("required_permissions", categories)
        self.assertIn("required_tools", categories)
        self.assertIn("verification_status", categories)
        self.assertIn("verification_evidence", categories)
        self.assertIn("execution_readiness", categories)
        self.assertNotIn(PublicationState.RUNTIME_AVAILABLE.value, result.publication_state)
        self.assertTrue(any("runtime evidence" in text for item in result.changes
                            for text in item.uncertainty))

    def test_capability_removal_and_manifest_addition_are_reported(self) -> None:
        old = [self.old[0]]
        new = [deepcopy(old[0])]
        new[0]["source_revision"] = NEW
        new[0]["capabilities"].pop()
        extra = deepcopy(new[0])
        extra["agent"]["id"] = "synthetic.extra-agent"
        extra["source_revision"] = NEW
        new.append(extra)
        result = self.discovery.compare(old, new, from_git=self.from_git, to_git=self.to_git)
        self.assertIn("capability_removed", {item.category for item in result.changes})
        self.assertIn("manifest_added", {item.category for item in result.changes})
        self.assertTrue(any("runtime availability" in uncertainty
                            for item in result.changes for uncertainty in item.uncertainty))

    def test_permission_and_execution_readiness_changes_are_reported_only_as_metadata(self) -> None:
        changed = deepcopy(self.new)
        changed[0]["required_permissions"].append("synthetic-permission")
        changed[0]["execution_readiness"]["ready"] = False
        result = CapabilityChangeDiscovery().compare(
            self.old, changed, from_git=self.from_git, to_git=self.to_git)
        permission_change = next(item for item in result.changes
                                 if item.category == "required_permissions")
        self.assertEqual(permission_change.after[-1], "synthetic-permission")
        self.assertNotIn(PublicationState.RUNTIME_AVAILABLE.value, result.publication_state)
        self.assertFalse(result.brain_summary()["execution_authorized"])

    def test_duplicate_change_suppression_is_stable(self) -> None:
        changed = deepcopy(self.new)
        changed[0]["agent"]["version"] = "2.0.0"
        first = self.discovery.compare(self.old, changed, from_git=self.from_git, to_git=self.to_git)
        fresh = CapabilityChangeDiscovery().compare(
            self.old, changed, from_git=self.from_git, to_git=self.to_git)
        second = self.discovery.compare(self.old, changed, from_git=self.from_git, to_git=self.to_git)
        self.assertTrue(first.changes)
        self.assertEqual(second.changes, ())
        self.assertEqual([item.change_id for item in first.changes],
                         [item.change_id for item in fresh.changes])
        self.assertEqual(len({change.change_id for change in first.changes}), len(first.changes))

    def test_missing_mismatched_and_uncommitted_git_evidence_rejected(self) -> None:
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old, self.new, from_git=OLD, to_git=self.to_git)  # type: ignore[arg-type]
        wrong = [deepcopy(item) for item in self.old]
        wrong[0]["source_revision"] = "c" * 40
        with self.assertRaises(ChangeError):
            self.discovery.compare(wrong, self.new, from_git=self.from_git, to_git=self.to_git)
        uncommitted = GitRevisionEvidence(NEW, committed=False)
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old, self.new, from_git=self.from_git, to_git=uncommitted)

    def test_remote_publication_does_not_claim_runtime_availability(self) -> None:
        result = self.discovery.compare(self.old, self.new, from_git=self.from_git, to_git=self.to_git)
        self.assertIn(PublicationState.CODE_COMMITTED.value, result.publication_state)
        self.assertIn(PublicationState.CODE_PUSHED.value, result.publication_state)
        self.assertNotIn(PublicationState.RUNTIME_AVAILABLE.value, result.publication_state)
        self.assertFalse(result.to_dict()["runtime_availability_claimed"])
        self.assertFalse(result.brain_summary()["execution_authorized"])
        self.assertIn("not been established", result.brain_summary()["message"])
        json.dumps(result.brain_summary())

    def test_runtime_assertion_is_rejected_without_independent_deployment_evidence(self) -> None:
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old, self.new, from_git=self.from_git,
                                   to_git=self.to_git, runtime_verified=True)

    def test_unverified_remote_revision_is_not_publication_evidence(self) -> None:
        with self.assertRaises(ChangeError):
            GitRevisionEvidence(NEW, committed=True, remote_verified=False, remote_revision=NEW)

    def test_invalid_status_and_duplicate_manifests_fail_closed(self) -> None:
        invalid = deepcopy(self.new)
        invalid[0]["verification"]["status"] = "invalid-status"
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old, invalid, from_git=self.from_git, to_git=self.to_git)
        downgraded = deepcopy(self.unknown)
        for manifest in downgraded:
            manifest["source_revision"] = NEW
        result = CapabilityChangeDiscovery().compare(
            self.old, downgraded, from_git=self.from_git, to_git=self.to_git)
        self.assertIn("verification_status", {item.category for item in result.changes})
        self.assertIn("verification_evidence", {item.category for item in result.changes})
        self.assertTrue(any("not verified" in uncertainty
                            for item in result.changes for uncertainty in item.uncertainty))
        missing_evidence = deepcopy(self.new)
        missing_evidence[0]["verification"]["evidence"] = []
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old, missing_evidence,
                                   from_git=self.from_git, to_git=self.to_git)
        with self.assertRaises(ChangeError):
            self.discovery.compare(self.old + [deepcopy(self.old[0])], self.new,
                                   from_git=self.from_git, to_git=self.to_git)


if __name__ == "__main__":
    unittest.main()
