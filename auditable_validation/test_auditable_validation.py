import tempfile
import unittest
from pathlib import Path

from authorization_adapter import run_assertions, validate_target, append_audit
from report_export import redact


class AuditableValidationTests(unittest.TestCase):
    def test_scope_rejects_external_and_mutating_urls(self):
        with self.assertRaises(ValueError):
            validate_target("https://example.com")
        with self.assertRaises(ValueError):
            validate_target("http://target:3000/admin")

    def test_scope_accepts_container_target(self):
        self.assertEqual(validate_target("http://target:3000").host, "target")

    def test_mock_target_passes_read_only_assertions(self):
        events = run_assertions("http://127.0.0.1:3000")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event.method == "GET" for event in events))

    def test_audit_is_append_only_jsonl_with_execution_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            append_audit([], path, "campaign-1")
            self.assertIn("execution_hash", path.read_text(encoding="utf-8"))

    def test_export_redacts_sensitive_values(self):
        output = redact("Authorization: Bearer abc123 password=secret cookie: sid=xyz")
        self.assertNotIn("abc123", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("sid=xyz", output)
        self.assertIn("[REDACTED]", output)


if __name__ == "__main__":
    unittest.main()
