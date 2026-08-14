import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from enterprise_stealth_range import (
    AuditTrail, AuthorizationScenarioAdapter, BrowserConfiguration,
    BudgetExhaustedError, ConfigurationError, HardenedBrowserRunner,
    LiveCommandChannel, PipelineStatus, ProxyConfiguration, SystemArchitect,
    simulate_mouse_trajectory,
)


class FakeBrowser:
    def __init__(self): self.stops = 0
    async def stop(self): self.stops += 1
    async def snapshot(self): pass


async def lines(*values):
    for value in values: yield value


class EnterpriseRangeTests(unittest.IsolatedAsyncioTestCase):
    def test_dual_shapes_and_route(self):
        zero = AuthorizationScenarioAdapter.from_payload({"target_scope_url": "http://127.0.0.1:3000/"})
        auth = AuthorizationScenarioAdapter.from_payload({"target_scope_url": "http://127.0.0.1:3000/", "username": "qa", "password": "test", "auth_entry_route": "/login", "role_profile": "standard_user"})
        architect = SystemArchitect(HardenedBrowserRunner(FakeBrowser()))
        self.assertEqual(architect.preflight(zero)["phase"], "login_gate_analysis")
        self.assertEqual(architect.preflight(auth)["phase"], "authenticated_authorization_checks")

    def test_external_targets_and_proxies_rejected(self):
        with self.assertRaises(ConfigurationError):
            AuthorizationScenarioAdapter.from_payload({"target_scope_url": "https://example.com"})
        with self.assertRaises(ConfigurationError): ProxyConfiguration("http://example.com:8080")
        with self.assertRaises(ConfigurationError): BrowserConfiguration(automation_disclosed=False)

    def test_bezier_trajectory_has_endpoints(self):
        points = simulate_mouse_trajectory((0, 0), (100, 50), steps=10)
        self.assertEqual((points[0], points[-1]), ((0, 0), (100, 50)))

    async def test_budget_halts_and_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = FakeBrowser(); root = Path(directory)
            architect = SystemArchitect(HardenedBrowserRunner(browser), AuditTrail(root / "audit.csv"))
            with self.assertRaises(BudgetExhaustedError):
                await architect.enforce_attempt_budget(500, root / "alert.md")
            self.assertEqual(browser.stops, 1); self.assertTrue((root / "alert.md").exists())

    async def test_boundary_requires_manual_decision(self):
        browser = FakeBrowser(); architect = SystemArchitect(HardenedBrowserRunner(browser))
        digest, text = await architect.boundary_breach("token=abc response")
        self.assertEqual(len(digest), 64); self.assertNotIn("abc", text)
        async def no(_): return "No"
        self.assertFalse(await architect.request_manual_approval(no))
        self.assertEqual(architect.status, PipelineStatus.HALTED)

    async def test_live_commands_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            channel = LiveCommandChannel(lines("not json", json.dumps({"command": "set_pacing_ms", "value": 20})), AuditTrail(Path(directory) / "audit.csv"))
            accepted = [item async for item in channel.commands()]
            self.assertEqual(accepted, [{"command": "set_pacing_ms", "value": 20}])


if __name__ == "__main__": unittest.main()
