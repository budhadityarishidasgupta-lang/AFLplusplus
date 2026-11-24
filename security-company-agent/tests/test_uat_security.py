"""Offline tests for secret-safe UAT helpers; synthetic credentials only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


security = load_script("uat_security_checks", "uat_security_checks.py")
uat = load_script("searxng_uat", "searxng_uat.py")


class UatSecurityTests(unittest.TestCase):
    def test_redaction_removes_dummy_tokens_headers_and_encodings(self):
        dummy = "dummy-token-0123456789-abcdef0123456789abcdef"
        import base64
        text = f"Authorization: {('Bear' + 'er')} {dummy}; key={dummy}; hex={dummy.encode().hex()}; b64={base64.b64encode(dummy.encode()).decode()}"
        output = security.redact(text, [dummy])
        self.assertNotIn(dummy, output)
        self.assertNotIn(dummy.encode().hex(), output)
        self.assertNotIn(base64.b64encode(dummy.encode()).decode(), output)
        self.assertGreaterEqual(output.count(security.REDACTION), 4)

    def test_exception_redaction_is_constant(self):
        self.assertEqual(security.safe_error(RuntimeError(f"Authorization: {('Bear' + 'er')} dummy-secret0123456789abcdef")), "[redacted error]")

    def test_env_parser_only_extracts_allowlisted_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENAI_" + "API_KEY=must-not-be-read\nSEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\nSEARXNG_API_TOKEN=dummy-search-token\nSEARXNG_APPROVED=0\n", encoding="utf-8")
            with patch.object(uat, "ROOT", Path(tmp)):
                endpoint, token, approval = uat._load_search_config()
        self.assertEqual(endpoint, "https://sovereign-brain-search.onrender.com/search")
        self.assertEqual(token, "dummy-search-token")
        self.assertEqual(approval, "0")
        self.assertNotIn("OPENAI_" + "API_KEY", repr((endpoint, token, approval)))

    def test_endpoint_restricts_https_host_and_search_path(self):
        self.assertTrue(uat._valid_endpoint("https://sovereign-brain-search.onrender.com/search"))
        for value in ("http://sovereign-brain-search.onrender.com/search", "https://example.com/search", "https://sovereign-brain-search.onrender.com/", "https://user:pw@sovereign-brain-search.onrender.com/search"):
            self.assertFalse(uat._valid_endpoint(value), value)

    def test_missing_live_authorization_blocks_all_production_cases(self):
        rows = uat._rows(False, False)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["result"] == "BLOCKED" for row in rows))
        with patch.object(uat, "_load_search_config", return_value=(None, None, None)):
            with patch("builtins.print") as output:
                self.assertEqual(uat.main([]), 1)
        lines = [str(call.args[0]) for call in output.call_args_list]
        self.assertIn("UAT_PREFLIGHT: BLOCKED", lines)
        self.assertIn("SEARXNG_APPROVAL_UNCHANGED: BLOCKED", lines)
        self.assertTrue(all(row["result"] == "BLOCKED" for row in uat._rows(True, True, approval_disabled=False)))

    def test_no_provider_modules_or_provider_secrets_are_referenced(self):
        source = (ROOT / "scripts" / "searxng_uat.py").read_text(encoding="utf-8")
        for forbidden in ("brain_core", "DASH" + "SCOPE_API_KEY", "OPENAI_" + "API_KEY", "BRAVE_API_KEY"):
            self.assertNotIn(forbidden, source)

    def test_http_errors_are_returned_without_exception_body(self):
        import urllib.error
        import urllib.request
        with patch.object(urllib.request.OpenerDirector, "open", side_effect=urllib.error.HTTPError("https://example.invalid", 401, "private dummy-token", {}, None)):
            status, body, _ = uat._http("/search", token="dummy-token", query="Python")
        self.assertEqual(status, 401)
        self.assertNotIn(b"dummy-token", body)

    def test_request_hosts_are_fixed_https_and_token_is_header_only(self):
        import urllib.request
        captured = {}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{"results":[]}'
        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["auth"] = request.get_header("Authorization")
                captured["timeout"] = timeout
                return Response()
        with patch.object(urllib.request, "build_opener", return_value=Opener()):
            status, _, _ = uat._http("/search", token="dummy-only-token", query="Python", timeout=999)
        self.assertEqual(status, 200)
        self.assertNotIn("dummy-only-token", captured["url"])
        self.assertEqual(captured["auth"], f"{('Bear' + 'er')} dummy-only-token")
        self.assertLessEqual(captured["timeout"], 15)

    def test_scanner_detects_dummy_credentials_in_removed_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragments = ("synthetic", "fixture", "not-a-real-credential")
            assembled = "sk-proj-" + "".join(fragments) + "0123456789abcdef"
            alibaba = "sk-ws-" + "".join(fragments) + "0123456789abcdef"
            bearer = "synthetic-bearer-" + "".join(fragments) + "0123456789abcdef"
            (root / "fixtures.py").write_text(
                f"{('OPENAI_' + 'API_KEY')}='{assembled}'\n{('DASH' + 'SCOPE_API_KEY')}='{alibaba}'\nAuthorization: {('Bear' + 'er')} {bearer}", encoding="utf-8")
            (root / "key.pem").write_text(f"{'-----BEGIN ' + 'PRIVATE ' + 'KEY-----'}\nfake-data\n{'-----END ' + 'PRIVATE ' + 'KEY-----'}", encoding="utf-8")
            (root / ".env").write_text("synthetic only", encoding="utf-8")
            def git_runner(*args):
                if args == ("ls-files", "-z"):
                    stdout = "fixtures.py\0key.pem\0.env\0"
                elif args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
                    stdout = ""
                else:
                    stdout = ""
                return type("Result", (), {"returncode": 0, "stdout": stdout})()
            ok, findings = security.scan_tracked_files(root, git_runner=git_runner)
            self.assertTrue(ok)
            categories = {category for _, category, _ in findings}
            self.assertTrue({"openai_key", "alibaba_credential", "bearer_token", "private_key_block", "tracked_env_file"}.issubset(categories))
            safe_metadata = repr(findings)
            for value in (assembled, alibaba, bearer):
                self.assertNotIn(value, safe_metadata)

    def test_scanner_never_returns_secret_text_and_clean_file_passes(self):
        findings = security._scan_content([("safe.py", "value = 1\\n")])
        self.assertEqual(findings, [])

    def test_inventory_error_truncated_inventory_and_read_error_fail_closed(self):
        class Result:
            def __init__(self, returncode, stdout):
                self.returncode = returncode
                self.stdout = stdout
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = lambda *_: Result(0, "tracked.py\0")
            self.assertFalse(security.scan_tracked_files(root, git_runner=lambda *_: Result(2, ""))[0])
            self.assertFalse(security.scan_tracked_files(root, git_runner=lambda *_: Result(0, "tracked.py"))[0])
            (root / "tracked.py").mkdir()
            self.assertFalse(security.scan_tracked_files(root, git_runner=good)[0])

    def test_scanner_reports_changed_and_baseline_findings_by_path(self):
        class Result:
            def __init__(self, stdout, returncode=0):
                self.returncode = returncode
                self.stdout = stdout
        def git_runner(*args):
            if args == ("merge-base", "origin/stable", "HEAD"):
                return Result("base-sha\n")
            if args == ("diff", "--name-only", "-z", "base-sha", "HEAD"):
                return Result("new.py\0")
            if args == ("diff", "--cached", "--name-only", "-z"):
                return Result("")
            if args == ("diff", "--name-only", "-z"):
                return Result("")
            if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
                return Result("")
            if args == ("check-ignore", "-q", ".env"):
                return Result("", returncode=0)
            if args == ("ls-files", "--error-unmatch", ".env"):
                return Result("", returncode=1)
            return Result("")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragments = ("synthetic", "temporary", "fixture", "0123456789abcdef")
            openai_dummy = "sk-proj-" + "".join(fragments)
            alibaba_dummy = "sk-ws-" + "".join(fragments)
            (root / "old.py").write_text(f"{'OPENAI_' + 'API_KEY'}='{openai_dummy}'", encoding="utf-8")
            (root / "new.py").write_text(f"{'DASH' + 'SCOPE_API_KEY'}='{alibaba_dummy}'", encoding="utf-8")
            original_scan = security.scan_tracked_files
            def fake_git(*args):
                if args == ("ls-files", "-z"):
                    return Result("old.py\0new.py\0")
                if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
                    return Result("")
                return git_runner(*args)
            with patch.object(security, "ROOT", root), patch.object(security, "_git", side_effect=fake_git), patch.object(security, "scan_tracked_files", side_effect=lambda: original_scan(root, git_runner=fake_git)), patch("builtins.print") as output:
                checks = security.run_checks()
        lines = [str(call.args[0]) for call in output.call_args_list]
        self.assertIn("SECRET_SCAN_SUMMARY new_findings=1 baseline_findings=1", lines)
        self.assertNotIn(openai_dummy, " ".join(lines))
        self.assertNotIn(alibaba_dummy, " ".join(lines))
        self.assertIn(("NEW_CHANGE_SECRET_SCAN", False), checks)
        self.assertIn(("BASELINE_SECRET_FINDINGS_RECORDED", True), checks)
        self.assertIn(("PR_DELTA_SECRET_SCAN", False), checks)
        self.assertIn(("OWNER_CONFIG_READINESS", "BLOCKED"), checks)
        self.assertIn(("LIVE_UAT", "BLOCKED"), checks)
        self.assertIn("PR_DELTA_SECRET_FINDINGS=1", lines)

    def test_scanner_execution_failure_fails_closed(self):
        class Failed:
            returncode = 128
            stdout = ""
        ok, findings = security.scan_tracked_files(git_runner=lambda *_: Failed())
        self.assertFalse(ok)
        self.assertEqual(findings, [])

    def test_redirect_handler_rejects_redirects(self):
        handler = uat._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))

    def test_http_rejects_nonfixed_host_before_network_open(self):
        import urllib.request
        with patch.object(uat, "BASE_URL", "https://evil.example"):
            with patch.object(urllib.request, "build_opener") as build_opener:
                with self.assertRaises(ValueError):
                    uat._http("/search")
                build_opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
