import json
import tempfile
import unittest
from pathlib import Path

from campaign_session_manager import (
    CampaignSessionManager,
    ExecutionMode,
    VolatileSessionStore,
)
from enterprise_stealth_range import ConfigurationError


async def no_delay():
    return 0.0


class FakeSessionBrowser:
    def __init__(self):
        self.external = []
        self.logins = []

    async def run_unauthenticated(self, target_url):
        self.external.append(target_url)

    async def authenticate(self, auth_url, username, password):
        self.logins.append((auth_url, username, password))
        return {
            "cookies": [{"name": "session", "value": "local-test"}],
            "tokens": {"csrf": "verification-value"},
        }


def write_scope(root, **overrides):
    payload = {
        "target_scope_url": "http://127.0.0.1:3000/",
        "username": None,
        "password": None,
        "auth_entry_route": None,
        "role_profile": None,
    }
    payload.update(overrides)
    path = Path(root) / "campaign_scope.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class CampaignSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_credentials_pass_only_target(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = CampaignSessionManager.load_scope(write_scope(directory))
            browser, messages = FakeSessionBrowser(), []
            manager = CampaignSessionManager(
                browser,
                VolatileSessionStore(Path(directory) / "sessions", require_shm=False),
                messages.append,
                no_delay,
            )
            result = await manager.ingest(scope)
            self.assertEqual(result.mode, ExecutionMode.EXTERNAL_PERIMETER_AUDIT)
            self.assertEqual(browser.external, ["http://127.0.0.1:3000/"])
            self.assertEqual(browser.logins, [])
            self.assertEqual(messages, ["EXECUTION_MODE: EXTERNAL_PERIMETER_AUDIT"])

    async def test_credentials_create_unlinked_volatile_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = CampaignSessionManager.load_scope(
                write_scope(
                    directory,
                    username="qa-user",
                    password="local-password",
                    auth_entry_route="/login",
                    role_profile="standard_user",
                )
            )
            browser = FakeSessionBrowser()
            root = Path(directory) / "sessions"
            store = VolatileSessionStore(root, require_shm=False)
            result = await CampaignSessionManager(browser, store, lambda _: None, no_delay).ingest(scope)
            self.assertEqual(result.mode, ExecutionMode.AUTHENTICATED_SESSION)
            self.assertEqual(browser.logins[0][0], "http://127.0.0.1:3000/login")
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(result.session.read()["tokens"]["csrf"], "verification-value")
            result.session.close()
            store.cleanup()
            self.assertFalse(root.exists())

    def test_partial_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_scope(directory, username="qa-user")
            with self.assertRaises(ConfigurationError):
                CampaignSessionManager.load_scope(path)

    def test_unknown_schema_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_scope(directory)
            payload = json.loads(path.read_text())
            payload["extra"] = True
            path.write_text(json.dumps(payload))
            with self.assertRaises(ConfigurationError):
                CampaignSessionManager.load_scope(path)

    def test_store_rejects_unknown_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VolatileSessionStore(Path(directory) / "sessions", require_shm=False)
            with self.assertRaises(ConfigurationError):
                store.save({"password": "must-not-be-stored"})


if __name__ == "__main__":
    unittest.main()
