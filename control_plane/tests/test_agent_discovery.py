from __future__ import annotations

import json
import subprocess
import sys
import unittest

from control_plane.agent_contracts import CapabilityNotFoundError, CapabilityRegistry, CapabilityStatus
from control_plane.agent_discovery import AgentDiscovery


class AgentDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        self.discovery = AgentDiscovery(self.registry)

    def test_explicit_discovery_registers_actual_agent_roles_only(self) -> None:
        discovered = self.discovery.discover_existing()
        self.assertEqual(
            [item.contract.agent_id for item in discovered],
            ["multi-agent.local-recon", "multi-agent.evolutionary-exploit-dev", "enterprise-range.system-architect"],
        )
        self.assertEqual(len(self.registry.list_agents()), 3)
        self.assertTrue(all(item.status is CapabilityStatus.UNDERSTOOD for item in discovered))
        # Launcher, browser runner, audit trail, scope adapter and mock target
        # are controllers/infrastructure, not registered specialist agents.
        self.assertFalse(any("launcher" in item.contract.agent_id for item in discovered))
        self.assertFalse(any("browser" in item.contract.agent_id for item in discovered))

    def test_metadata_matches_evidenced_capabilities_and_restrictions(self) -> None:
        agents = {item.contract.agent_id: item for item in self.discovery.discover_existing()}
        recon = agents["multi-agent.local-recon"]
        self.assertIn("local-python-static-analysis", recon.contract.capabilities)
        self.assertIn("read-local-source", recon.contract.permissions)
        self.assertEqual(recon.source.internet_access, "none evidenced")
        developer = agents["multi-agent.evolutionary-exploit-dev"]
        self.assertIn("genetic-payload-mutation", developer.contract.capabilities)
        architect = agents["enterprise-range.system-architect"]
        self.assertIn("boundary-approval-gating", architect.contract.capabilities)
        self.assertFalse(any(agent.source.execution_integration_available for agent in agents.values()))

    def test_unsupported_capability_is_reported_as_unknown(self) -> None:
        self.discovery.discover_existing()
        summary = self.discovery.summarize(("unproven.browser-exploitation",))
        self.assertEqual(len(summary.unsupported_capabilities), 1)
        self.assertEqual(summary.unsupported_capabilities[0].status, CapabilityStatus.UNKNOWN)
        with self.assertRaises(CapabilityNotFoundError):
            self.discovery.lookup_capability("unproven.browser-exploitation")

    def test_capability_lookup_uses_existing_registry_contracts(self) -> None:
        self.discovery.discover_existing()
        matches = self.discovery.lookup_capability("local-python-static-analysis")
        self.assertEqual([item.contract.agent_id for item in matches], ["multi-agent.local-recon"])
        self.assertEqual(matches[0].source.module, "multi_agent_system")

    def test_brain_summary_is_json_serializable_and_explicit_about_verification(self) -> None:
        self.discovery.discover_existing()
        summary = self.discovery.summarize(("capability.unknown",)).to_dict()
        serialized = json.dumps(summary, sort_keys=True)
        restored = json.loads(serialized)
        self.assertFalse(restored["execution_verified"])
        recon = restored["agents"][0]
        self.assertTrue(recon["understanding"]["discovered"])
        self.assertTrue(recon["understanding"]["declared_capabilities_understood"])
        self.assertFalse(recon["understanding"]["execution_verified"])
        self.assertFalse(recon["understanding"]["permission_granted"])
        self.assertEqual(restored["unsupported_capabilities"][0]["status"], "unknown")

    def test_discovery_imports_no_legacy_modules_or_runs_side_effects(self) -> None:
        code = (
            "import sys; import control_plane.agent_discovery as d; "
            "assert not any(name in sys.modules for name in "
            "('multi_agent_system','conversational_launcher','enterprise_stealth_range')); "
            "d.AgentDiscovery().discover_existing(); "
            "assert not any(name in sys.modules for name in "
            "('multi_agent_system','conversational_launcher','enterprise_stealth_range'))"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_behavior_is_preserved_by_source_hashes(self) -> None:
        # Hashes are checked against baseline during this work, before and after
        # adding the new metadata-only modules; this test protects declarations
        # from ever mutating legacy files in the same checkout.
        from pathlib import Path
        import hashlib

        root = Path(__file__).resolve().parents[2]
        expected = {
            "multi_agent_system.py": "9dedbb2029caf7e763b5e24401aae3804a071bf8b6a3f822a3280388769f5baa",
            "conversational_launcher.py": "6adce0d0f53a6856945db758936a4127db3fcf74ec524a7deb8230d94012fa1e",
            "enterprise_stealth_range.py": "b9ff7c07b0f19052cf7831cd2590efd2b8d373eefad836d05a3191ceb460d42f",
        }
        for filename, prefix in expected.items():
            digest = hashlib.sha256((root / filename).read_bytes()).hexdigest()
            self.assertTrue(digest.startswith(prefix), f"unexpected source change: {filename} ({digest})")


if __name__ == "__main__":
    unittest.main()
