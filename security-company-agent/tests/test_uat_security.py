"""Offline tests for secret-safe UAT helpers; synthetic credentials only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
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
            path.write_text("OPENAI_" + "API_KEY=must-not-be-read\nSEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n" + "SEARXNG_API_" + "TOKEN=dummy-search-token\nSEARXNG_APPROVED=0\n", encoding="utf-8")
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

    def test_default_config_mode_works_in_clean_checkout_with_external_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp)
            dummy_token = "synthetic-external-token-0123456789abcdef"
            (config_dir / ".env").write_text(
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}={dummy_token}\nSEARXNG_APPROVED=0\n", encoding="utf-8"
            )
            with patch.object(uat, "ROOT", Path(temp) / "clean-worktree-no-config"):
                with patch.object(uat, "_rows", return_value=[]), patch.object(uat, "_http") as http, patch("builtins.print") as output:
                    self.assertNotEqual(uat.main(["--config-dir", str(config_dir)]), 0)
                    self.assertEqual(uat.main(["--config-dir", str(config_dir), "--config-dir-approved"]), 0)
                    http.assert_not_called()
            self.assertNotIn(dummy_token, " ".join(str(c.args[0]) for c in output.call_args_list))

    def test_missing_live_authorization_blocks_all_production_cases(self):
        rows = uat._rows(False, False)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["result"] == "BLOCKED" for row in rows))
        self.assertEqual(uat._exit_code(rows), 1)
        with patch.object(uat, "_load_search_config", return_value=(None, None, None)):
            with patch("builtins.print") as output:
                self.assertEqual(uat.main([]), 0)
        lines = [str(call.args[0]) for call in output.call_args_list]
        self.assertIn("UAT_PREFLIGHT: BLOCKED", lines)
        self.assertIn("SEARXNG_APPROVAL_UNCHANGED: BLOCKED", lines)
        self.assertFalse(any("https://sovereign-brain-search.onrender.com" in str(call) for call in output.call_args_list))
        self.assertTrue(all(row["result"] == "BLOCKED" for row in uat._rows(True, True, approval_disabled=False)))

    def test_live_result_exit_code_requires_every_required_test_to_pass(self):
        passing = [{"test": "required", "result": "PASS"}]
        failing = [{"test": "required", "result": "FAIL"}]
        blocked = [{"test": "required", "result": "BLOCKED"}]
        self.assertEqual(uat._exit_code(passing), 0)
        self.assertNotEqual(uat._exit_code([{**passing[0], "result": "PASS"}, failing[0]]), 0)
        self.assertNotEqual(uat._exit_code(failing), 0)
        self.assertNotEqual(uat._exit_code(blocked), 0)
        self.assertNotEqual(uat._exit_code([]), 0)

    def test_no_provider_modules_or_provider_secrets_are_referenced(self):
        source = (ROOT / "scripts" / "searxng_uat.py").read_text(encoding="utf-8")
        for forbidden in ("brain_core", "DASH" + "SCOPE_API_KEY", "OPENAI_" + "API_KEY", "BRAVE_API_KEY"):
            self.assertNotIn(forbidden, source)

    def test_all_required_live_tests_passing_returns_zero_without_network(self):
        rows = [{"test": f"required-{index}", "expected": "PASS", "observed": "PASS", "result": "PASS"} for index in range(14)]
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp)
            (config_dir / ".env").write_text(
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}=synthetic-token-only\nSEARXNG_APPROVED=0\n", encoding="utf-8"
            )
            with patch.object(uat, "_rows", return_value=rows), patch.object(uat, "_http") as http:
                self.assertEqual(uat.main(["--live", "--credential-rotation-confirmed", "--config-dir", str(config_dir), "--config-dir-approved"]), 0)
            http.assert_not_called()

    def test_live_network_exception_is_sanitized_and_required_failure_exits_nonzero(self):
        secret_marker = "synthetic-network-secret-marker"
        secret_token = "synthetic-token-only"
        with tempfile.TemporaryDirectory() as temp:
            config_dir = Path(temp)
            (config_dir / ".env").write_text(
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}={secret_token}\nSEARXNG_APPROVED=0\n", encoding="utf-8"
            )
            with patch.object(uat, "_http", side_effect=RuntimeError(secret_marker)), patch("builtins.print") as output:
                code = uat.main(["--live", "--credential-rotation-confirmed", "--config-dir", str(config_dir), "--config-dir-approved"])
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list)
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret_marker, rendered)
        self.assertNotIn(secret_token, rendered)
        self.assertIn("diagnostic=UNKNOWN_NETWORK_ERROR", rendered)

    def test_config_file_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            config_dir.mkdir()
            outside = root / "outside.env"
            outside.write_text(
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}=synthetic-symlink-token-0123456789abcdef\nSEARXNG_APPROVED=0\n",
                encoding="utf-8",
            )
            try:
                (config_dir / ".env").symlink_to(outside)
            except OSError:
                self.skipTest("file symlink creation unavailable on this Windows environment")
            self.assertIsNone(uat._config_file(config_dir))
            self.assertEqual(uat._load_search_config(config_dir, approved=True), (None, None, None))

    def test_config_path_expansion_failure_fails_closed(self):
        with patch.object(uat.Path, "expanduser", side_effect=RuntimeError("synthetic path error")):
            self.assertIsNone(uat._config_file("~/not-approved"))

    def test_config_path_with_symlinked_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real_dir = root / "real-config"
            real_dir.mkdir()
            (real_dir / ".env").write_text(
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}=synthetic-parent-token-0123456789abcdef\nSEARXNG_APPROVED=0\n",
                encoding="utf-8",
            )
            link = root / "config-link"
            try:
                link.symlink_to(real_dir, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlink creation unavailable on this Windows environment")
            self.assertIsNone(uat._config_file(link))

    def test_explicit_config_directory_is_read_only_and_path_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "authorized-config"
            config_dir.mkdir()
            env_file = config_dir / ".env"
            dummy_token = "synthetic-config-token-0123456789abcdef"
            content = (
                "SEARXNG_ENDPOINT=https://sovereign-brain-search.onrender.com/search\n"
                f"{('SEARXNG_API_' + 'TOKEN')}={dummy_token}\nSEARXNG_APPROVED=0\n"
            )
            env_file.write_text(content, encoding="utf-8")
            before = env_file.read_bytes()
            with patch.object(uat, "_rows", return_value=[]), patch("builtins.print") as output:
                self.assertNotEqual(uat.main(["--config-dir", str(config_dir)]), 0)
                self.assertEqual(uat.main(["--config-dir", str(config_dir), "--config-dir-approved"]), 0)
            self.assertEqual(env_file.read_bytes(), before)
            self.assertNotIn(dummy_token, " ".join(str(c.args[0]) for c in output.call_args_list))
            for invalid in (str(root / "missing"), "relative-config-dir", str(config_dir / ".env")):
                with patch("builtins.print") as invalid_output:
                    self.assertNotEqual(uat.main(["--config-dir", invalid]), 0)
                self.assertNotIn(dummy_token, " ".join(str(c.args[0]) for c in invalid_output.call_args_list))
            link = root / "linked-config"
            try:
                link.symlink_to(config_dir, target_is_directory=True)
            except OSError:
                pass
            else:
                with patch("builtins.print") as link_output:
                    self.assertNotEqual(uat.main(["--config-dir", str(link), "--config-dir-approved"]), 0)
                self.assertNotIn(dummy_token, " ".join(str(c.args[0]) for c in link_output.call_args_list))

    def test_default_mode_without_config_has_no_network(self):
        with patch.object(uat, "ROOT", Path(tempfile.gettempdir()) / "missing-uat-config"):
            with patch.object(uat, "_http") as http, patch("builtins.print"):
                self.assertEqual(uat.main([]), 0)
                http.assert_not_called()

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

    def test_health_shape_and_anonymous_401_shape_validation(self):
        self.assertFalse(uat._health_ok(200, b"not-json"))
        self.assertFalse(uat._health_ok(200, b"[]"))
        self.assertFalse(uat._health_ok(503, b'{"status":"ok"}'))
        self.assertTrue(uat._health_ok(200, b'{"status":"ok"}'))
        self.assertFalse(uat._anonymous_ok(401, b"not-json"))
        self.assertFalse(uat._anonymous_ok(401, b"[]"))
        self.assertTrue(uat._anonymous_ok(401, b'{"error":"unauthorized"}'))
        self.assertFalse(uat._anonymous_ok(200, b'{"error":"unauthorized"}'))

    def test_diagnostics_preserve_http_statuses_and_sanitize_connection_failures(self):
        class HttpErrorOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 503, "private response header", {}, None)
        with patch.object(uat.urllib.request, "build_opener", return_value=HttpErrorOpener()):
            status, _body, _elapsed = uat._http("/healthz")
        self.assertEqual(status, 503)
        with patch.object(uat.urllib.request, "build_opener", side_effect=ConnectionError("private connection detail")):
            try:
                uat._http("/healthz")
            except Exception as exc:
                self.assertEqual(uat._diagnostic_category(exc), "CONNECTION_ERROR")

    def test_http_failure_categories_are_fixed_and_sanitized(self):
        cases = [
            (urllib.error.URLError(socket.gaierror("secret-host-marker")), "DNS_FAILURE"),
            (urllib.error.URLError(ssl.SSLError("private TLS detail")), "TLS_FAILURE"),
            (TimeoutError("secret timeout detail"), "CONNECT_TIMEOUT"),
            (urllib.error.URLError("proxy secret user:pass"), "PROXY_ERROR"),
            (ConnectionError("secret connection detail"), "CONNECTION_ERROR"),
            (ValueError("secret parser detail"), "RESPONSE_FORMAT_ERROR"),
            (RuntimeError("secret unexpected detail"), "UNKNOWN_NETWORK_ERROR"),
        ]
        cases.extend([
            (TimeoutError("read timeout detail"), "CONNECT_TIMEOUT"),
            (socket.timeout("read timeout detail"), "CONNECT_TIMEOUT"),
            (urllib.error.URLError(ConnectionRefusedError("private proxy detail")), "PROXY_ERROR"),
        ])
        with patch.object(uat, "_proxy_configured", return_value=True):
            for exc, category in cases:
                with self.subTest(category=category):
                    with patch.object(uat.urllib.request, "build_opener", side_effect=exc):
                        try:
                            uat._http("/healthz")
                        except Exception as caught:
                            rendered = uat._diagnostic_category(caught)
                        else:
                            self.fail("transport error expected")
                    self.assertIn(rendered, uat.DIAGNOSTIC_CATEGORIES)
                    self.assertEqual(rendered, category)
                    self.assertNotIn("secret", rendered.casefold())
                    self.assertNotIn("private", rendered.casefold())

    def test_http_error_is_returned_and_transport_proxy_state_is_boolean_only(self):
        class ErrorOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 401, "dummy response", {}, None)
        with patch.object(uat.urllib.request, "build_opener", return_value=ErrorOpener()):
            status, body, _ = uat._http("/search", query="Python")
        self.assertEqual(status, 401)
        self.assertEqual(uat._proxy_configured({"https": "http://user:secret@proxy.invalid"}), True)
        self.assertEqual(uat._proxy_configured({}), False)
        self.assertNotIn("proxy.invalid", repr(uat._proxy_configured({"https": "http://user:secret@proxy.invalid"})))
        with patch.object(uat.urllib.request, "getproxies", return_value={"https": "http://proxy.invalid"}):
            self.assertTrue(uat._proxy_configured())

    def test_fixed_host_no_redirect_and_bounded_request_remain(self):
        self.assertTrue(uat._valid_endpoint("https://sovereign-brain-search.onrender.com/search"))
        with patch.object(uat.urllib.request, "build_opener") as build:
            build.side_effect = RuntimeError("private detail")
            try:
                uat._http("/healthz", timeout=999)
            except RuntimeError:
                pass
        self.assertTrue(any(isinstance(h, uat._NoRedirect) for h in build.call_args.args))
        self.assertTrue(any(isinstance(h, uat.urllib.request.ProxyHandler) and h.proxies == {}
                            for h in build.call_args.args))

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

    def test_env_template_allowed_but_contents_still_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            template = root / ".env.example"
            template.write_text("SEARXNG_ENDPOINT=https://example.invalid/search\n", encoding="utf-8")
            def runner_for(name):
                def git_runner(*args):
                    if args == ("ls-files", "-z"):
                        return type("Result", (), {"returncode": 0, "stdout": name + "\0"})()
                    if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
                        return type("Result", (), {"returncode": 0, "stdout": ""})()
                    return type("Result", (), {"returncode": 0, "stdout": ""})()
                return git_runner
            ok, findings = security.scan_tracked_files(root, git_runner=runner_for(".env.example"))
            self.assertTrue(ok)
            self.assertEqual(findings, [])
            dummy = "synthetic-template-token-0123456789abcdef"
            template.write_text(f"{('SEARXNG_API_' + 'TOKEN')}={dummy}\n", encoding="utf-8")
            ok, findings = security.scan_tracked_files(root, git_runner=runner_for(".env.example"))
            self.assertTrue(ok)
            self.assertTrue(any(category == "credential_assignment" for _, category, _ in findings), repr(findings))
            self.assertNotIn(dummy, repr(findings))

    def test_tracked_env_local_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.local").write_text("SAFE=value\n", encoding="utf-8")
            def git_runner(*args):
                stdout = ".env.local\0" if args == ("ls-files", "-z") else ""
                return type("Result", (), {"returncode": 0, "stdout": stdout})()
            ok, findings = security.scan_tracked_files(root, git_runner=git_runner)
            self.assertTrue(ok)
            self.assertIn((".env.local", "tracked_env_file", 1), findings)

    def test_scanner_failure_returns_nonzero(self):
        with patch.object(security, "run_checks", return_value=[("SCANNER_EXECUTION", False)]):
            with patch("builtins.print"):
                self.assertNotEqual(security.main(), 0)

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
