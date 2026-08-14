import json
import stat
import tempfile
import unittest
from pathlib import Path

from conversational_launcher import (
    LauncherError,
    parse_campaign_command,
    write_campaign_scope,
)
from enterprise_stealth_range import ConfigurationError


class ConversationalParserTests(unittest.TestCase):
    def test_unauthenticated_command_populates_unified_schema(self):
        campaign = parse_campaign_command("test http://127.0.0.1:3000/")
        self.assertEqual(campaign.mode, "unauthenticated")
        self.assertIsNone(campaign.payload["username"])
        self.assertEqual(set(campaign.payload), {
            "target_scope_url", "username", "password", "auth_entry_route", "role_profile"
        })

    def test_authenticated_command_supports_quoted_values(self):
        campaign = parse_campaign_command(
            'test http://127.0.0.1:3000/ user="QA User" pass="local test secret"'
        )
        self.assertEqual(campaign.mode, "authenticated")
        self.assertEqual(campaign.payload["username"], "QA User")
        self.assertEqual(campaign.payload["password"], "local test secret")
        self.assertEqual(campaign.payload["auth_entry_route"], "/login")

    def test_partial_duplicate_and_unknown_parameters_are_rejected(self):
        commands = (
            "test http://127.0.0.1:3000/ user=qa",
            "test http://127.0.0.1:3000/ user=qa user=other pass=test",
            "test http://127.0.0.1:3000/ token=value",
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaises(LauncherError):
                parse_campaign_command(command)

    def test_external_url_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            parse_campaign_command("test https://example.com user=qa pass=test")

    def test_scope_file_is_owner_only_and_complete(self):
        campaign = parse_campaign_command("test http://127.0.0.1:3000/")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_scope.json"
            write_campaign_scope(campaign, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), campaign.payload)


if __name__ == "__main__":
    unittest.main()
