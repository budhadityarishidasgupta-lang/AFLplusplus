import os
import tempfile
import unittest
from pathlib import Path

from authorization_adapter import ADMIN_LOGIN_PATH, CREATOR_SETTINGS_PATH, TEST_ACCOUNT_ID, RUNTIME_DIR, AUDIT_PATH, REPORT_PATH


class ConfigContractTests(unittest.TestCase):
    def test_supplied_defaults(self):
        self.assertEqual(ADMIN_LOGIN_PATH, "/admin/login")
        self.assertEqual(CREATOR_SETTINGS_PATH, "/api/v1/creators/creator_test_99/settings")
        self.assertEqual(TEST_ACCOUNT_ID, "creator_test_99")
        self.assertEqual(str(RUNTIME_DIR), "/dev/shm/fuzzer_runtime")
        self.assertEqual(str(AUDIT_PATH), "logs/audit_trail.csv")
        self.assertEqual(str(REPORT_PATH), "logs/PATCH_ADVISORY.md")


if __name__ == "__main__":
    unittest.main()
