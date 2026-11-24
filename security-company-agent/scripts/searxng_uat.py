"""Standalone, secret-safe, read-only SearXNG gateway UAT runner.

Run explicitly with --live only after credential rotation is confirmed. Default mode
performs local configuration/preflight checks and reports production tests BLOCKED.
Use --config-dir with --config-dir-approved to read allowlisted SearXNG values from an
owner-approved external directory; the runner never copies or modifies that directory's .env.
This module never imports Brain/provider code and never reads Alibaba/OpenAI keys.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://sovereign-brain-search.onrender.com"
QUERIES = (
    ("OpenAI official website", "OpenAI"),
    ("Python documentation", "Python"),
    ("Wikipedia artificial intelligence", "artificial intelligence"),
)
TIMEOUT_SECONDS = 8
RESPONSE_LIMIT = 1_048_576


def _config_file(config_dir: str | Path | None = None) -> Path | None:
    """Resolve a configuration directory and reject redirected/symlink paths."""
    try:
        if config_dir is None:
            directory = ROOT
        else:
            requested = Path(config_dir).expanduser()
            if not requested.is_absolute():
                return None
            directory = requested
        if not directory.is_absolute() or not directory.exists() or not directory.is_dir():
            return None
        if config_dir is not None:
            resolved_requested = directory.resolve(strict=True)
            current = directory.absolute()
            while current != current.parent:
                if current.is_symlink():
                    return None
                current = current.parent
            if not resolved_requested.samefile(directory):
                return None
            external_env = resolved_requested / ".env"
            if not external_env.exists() or external_env.is_symlink() or not external_env.is_file():
                return None
            return external_env
        resolved_directory = directory.resolve(strict=True)
        if not resolved_directory.is_dir() or not resolved_directory.samefile(directory):
            return None
        current = directory.absolute()
        while current != current.parent:
            if current.is_symlink():
                return None
            current = current.parent
        path = resolved_directory / ".env"
        if not path.exists() or path.is_symlink() or not path.is_file() or not path.resolve(strict=True).samefile(path):
            return None
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def _load_search_config(config_dir: str | Path | None = None, *, approved: bool = False) -> tuple[str | None, str | None, str | None]:
    """Read only allowlisted SearXNG fields, opening the checked file without following symlinks."""
    path = _config_file(config_dir)
    if path is None or (config_dir is not None and not approved):
        return None, None, None
    selected: dict[str, str] = {}
    wanted = {"SEARXNG_ENDPOINT", "SEARXNG_API_TOKEN", "SEARXNG_APPROVED"}
    try:
        if config_dir is None:
            lines = path.read_text(encoding="utf-8").splitlines()
        else:
            import os
            import stat
            if os.name == "nt":
                resolved = path.resolve(strict=True)
                if not resolved.samefile(path):
                    return None, None, None
                lines = resolved.read_text(encoding="utf-8").splitlines()
            elif not hasattr(os, "O_NOFOLLOW"):
                return None, None, None
            else:
                if not path.resolve(strict=True).samefile(path):
                    return None, None, None
                flags = os.O_RDONLY | os.O_NOFOLLOW
                descriptor = os.open(path, flags)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    os.close(descriptor)
                    return None, None, None
                with os.fdopen(descriptor, "r", encoding="utf-8") as config_stream:
                    lines = config_stream.read().splitlines()
        for line in lines:
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key in wanted:
                selected[key] = value.strip().strip("'\"")
    except Exception:
        return None, None, None
    return (selected.get("SEARXNG_ENDPOINT"), selected.get("SEARXNG_API_TOKEN"),
            selected.get("SEARXNG_APPROVED"))


def _valid_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        return (parsed.scheme == "https" and parsed.hostname == "sovereign-brain-search.onrender.com"
                and parsed.path.rstrip("/") == "/search" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _http(path: str, *, token: str | None = None, query: str | None = None,
          method: str = "GET", timeout: int = TIMEOUT_SECONDS) -> tuple[int, bytes, float]:
    """One HTTPS request; fixed host, no redirects, bounded body/timeout, no header output."""
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise ValueError("invalid local route")
    url = BASE_URL + path
    if query is not None:
        url += "?" + urllib.parse.urlencode({"q": query, "format": "json"})
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method=method)
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    parsed_url = urllib.parse.urlsplit(request.full_url)
    if (parsed_url.scheme != "https" or parsed_url.hostname != "sovereign-brain-search.onrender.com"
            or parsed_url.port not in (None, 443) or parsed_url.username or parsed_url.password):
        raise ValueError("request host rejected")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    start = time.monotonic()
    try:
        response = opener.open(request, timeout=max(1, min(timeout, 15)))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(RESPONSE_LIMIT + 1)
            if len(body) > RESPONSE_LIMIT:
                raise ValueError("response size exceeded")
            return int(exc.code), body, time.monotonic() - start
        finally:
            exc.close()
    with response:
        status = int(response.status)
        body = response.read(RESPONSE_LIMIT + 1)
    if len(body) > RESPONSE_LIMIT:
        raise ValueError("response size exceeded")
    return status, body, time.monotonic() - start


def _json(body: bytes):
    if len(body) > RESPONSE_LIMIT:
        raise ValueError("response size exceeded")
    return json.loads(body.decode("utf-8"))


def _rows(configured: bool, token_present: bool, *, approval_disabled: bool = True) -> list[dict[str, str]]:
    """Build sanitized test outcomes; every request failure remains an explicit FAIL."""
    rows: list[dict[str, str]] = []

    def add(name: str, expected: str, observed: str, result: str) -> None:
        rows.append({"test": name, "expected": expected, "observed": observed, "result": result})

    if not configured or not approval_disabled:
        for name, expected in [
            ("Public health", "HTTP 200"), ("Anonymous search", "HTTP 401"),
            ("Invalid-token search", "HTTP 401"), ("Authenticated search", "HTTP 200 JSON"),
            ("Relevant public queries", "HTTP 200; relevant nonempty HTTPS results"),
            ("Unknown route", "HTTP 404"), ("Malformed query", "HTTP 4xx"),
            ("Sequential stability", "No 5xx or malformed responses"),
        ]:
            add(name, expected, "Not run; live mode not authorized", "BLOCKED")
        return rows

    def request_case(name: str, expected: str, path: str, *, token: str | None = None,
                     query: str | None = None, method: str = "GET", validate=None):
        try:
            status, body, elapsed = _http(path, token=token, query=query, method=method)
            detail = f"HTTP {status}; {elapsed:.2f}s"
            passed = validate(status, body) if validate else False
            add(name, expected, detail, "PASS" if passed else "FAIL")
            return status, body, passed
        except Exception as exc:
            _ = exc  # Do not serialize exception, URL, headers, or response snippets.
            add(name, expected, "Request failed; exception details suppressed", "FAIL")
            return 0, b"", False

    request_case("Public health", "HTTP 200 JSON status=ok", "/healthz",
                 validate=lambda s, b: s == 200 and _json(b).get("status") == "ok")
    request_case("Anonymous search", "HTTP 401 unauthorized JSON", "/search", query="Python documentation",
                 validate=lambda s, b: s == 401 and isinstance(_json(b), dict))
    if not token_present:
        add("Invalid-token search", "HTTP 401", "Token absent; no request sent", "BLOCKED")
        add("Authenticated search", "HTTP 200 JSON", "Token absent; no request sent", "BLOCKED")
        add("Relevant public queries", "Three nonempty relevant HTTPS result sets",
            "Token absent; no queries sent", "BLOCKED")
    else:
        # Token value is never formatted or logged. The intentionally wrong value is synthetic.
        request_case("Invalid-token search", "HTTP 401 unauthorized JSON", "/search",
                     token="sb49-invalid-uat-token", query="Python documentation",
                     validate=lambda s, b: s == 401 and isinstance(_json(b), dict))
        status, body, valid = request_case("Authenticated search", "HTTP 200 valid JSON", "/search",
                                           token=_TOKEN, query="Python documentation",
                                           validate=lambda s, b: s == 200 and isinstance(_json(b), dict))
        if valid:
            payload = _json(body)
            results = payload.get("results")
            schema_ok = isinstance(results, list)
            add("Authenticated search", "HTTP 200 valid JSON with results array",
                f"HTTP {status}; {len(results) if schema_ok else 0} results", "PASS" if schema_ok else "FAIL")
            add("Authenticated result schema", "JSON object with results array",
                "Schema valid" if schema_ok else "Schema invalid", "PASS" if schema_ok else "FAIL")
            for query, relevant in QUERIES:
                try:
                    qstatus, qbody, elapsed = _http("/search", token=_TOKEN, query=query)
                    data = _json(qbody)
                    items = data.get("results") if isinstance(data, dict) else None
                    good = (qstatus == 200 and isinstance(items, list) and bool(items)
                            and all(isinstance(item, dict) and isinstance(item.get("title"), str)
                                    and isinstance(item.get("url"), str)
                                    and urllib.parse.urlsplit(item["url"]).scheme == "https"
                                    for item in items))
                    relevant_match = any(relevant.casefold() in item["title"].casefold()
                                         or relevant.casefold() in item.get("content", "").casefold()
                                         for item in items) if good else False
                    observed = f"HTTP {qstatus}; {len(items) if isinstance(items, list) else 0} results; {elapsed:.2f}s; relevant={'yes' if relevant_match else 'no'}"
                    add("Public query: " + query, "HTTP 200, nonempty relevant HTTPS results", observed,
                        "PASS" if good and relevant_match else "FAIL")
                except Exception:
                    add("Public query: " + query, "HTTP 200, valid nonempty HTTPS results",
                        "Request or response failed; details suppressed", "FAIL")

    request_case("Unknown route", "HTTP 404", "/uat-route-that-does-not-exist",
                 validate=lambda s, b: s == 404)
    request_case("Malformed query", "HTTP 4xx", "/search", token=_TOKEN if token_present else None,
                 query="\x00\x01", validate=lambda s, b: 400 <= s < 500)

    stability: list[tuple[int, bool, float]] = []
    for query in ("Python documentation", "Wikipedia artificial intelligence", "OpenAI official website"):
        try:
            status, body, elapsed = _http("/search", token=_TOKEN if token_present else None, query=query)
            payload = _json(body)
            schema = isinstance(payload, dict) and isinstance(payload.get("results"), list)
            stability.append((status, schema, elapsed))
        except Exception:
            stability.append((0, False, 0.0))
    stable = bool(stability) and all(status not in {0} and status < 500 and schema
                                    for status, schema, _ in stability)
    codes = ",".join(str(status) for status, _, _ in stability)
    add("Sequential stability (3 requests)", "No 5xx; valid JSON results schema",
        f"Statuses {codes}; valid schemas={'yes' if stable else 'no'}",
        "PASS" if stable else "FAIL")
    return rows


_TOKEN: str | None = None


def _exit_code(rows: list[dict[str, str]]) -> int:
    """Succeed only when every required outcome passed; BLOCKED is not success."""
    return 0 if rows and all(row.get("result") == "PASS" for row in rows) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="allow bounded read-only HTTPS requests (requires operator confirmation)")
    parser.add_argument("--credential-rotation-confirmed", action="store_true", help="required with --live")
    parser.add_argument("--config-dir", help="absolute path to a local configuration directory")
    parser.add_argument("--config-dir-approved", action="store_true", help="explicitly confirm the external directory is owner-approved")
    args = parser.parse_args(argv)
    if args.config_dir_approved and args.config_dir is None:
        parser.error("--config-dir-approved requires --config-dir")
    if args.config_dir is not None and not args.config_dir_approved:
        print("UAT_PREFLIGHT: BLOCKED (external configuration requires explicit approval)")
        print("SEARXNG_APPROVAL_UNCHANGED: BLOCKED")
        for row in _rows(False, False):
            print(f"{row['result']} | {row['test']} | expected: {row['expected']} | observed: {row['observed']}")
        return 1
    config_file = _config_file(args.config_dir)
    if args.config_dir is not None and config_file is None:
        print("UAT_PREFLIGHT: BLOCKED (configuration path unavailable or unsafe)")
        print("SEARXNG_APPROVAL_UNCHANGED: BLOCKED")
        for row in _rows(False, False):
            print(f"{row['result']} | {row['test']} | expected: {row['expected']} | observed: {row['observed']}")
        return 1
    endpoint, token, approval = _load_search_config(args.config_dir, approved=args.config_dir_approved)
    configured = _valid_endpoint(endpoint) and bool(token)
    global _TOKEN
    _TOKEN = token if configured else None
    # Approval is observed in memory only; never changed. Live testing requires it remain disabled.
    approval_disabled = (approval or "").casefold() in {"0", "false", "no"}
    config_valid = config_file is not None and (args.config_dir is None or args.config_dir_approved)
    configured = configured and config_valid
    if not config_valid:
        token = None
        _TOKEN = None
    live_allowed = args.live and args.credential_rotation_confirmed and configured and approval_disabled
    print("UAT_PREFLIGHT: " + ("PASS" if config_valid and configured and approval_disabled else "BLOCKED"))
    print("SEARXNG_APPROVAL_UNCHANGED: " + ("PASS" if approval_disabled else "BLOCKED"))
    if args.live and not live_allowed:
        print("LIVE_UAT_AUTHORIZATION: BLOCKED (requires configured endpoint/token, approval disabled, and explicit rotation confirmation)")
    rows = _rows(live_allowed, bool(token), approval_disabled=approval_disabled)
    for row in rows:
        print(f"{row['result']} | {row['test']} | expected: {row['expected']} | observed: {row['observed']}")
    if not args.live:
        return 0
    if not live_allowed:
        return 1
    return _exit_code(rows)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Constant-only fail closed; no exception data or traceback reaches stdout/stderr.
        print("UAT_RUNNER: FAIL (internal error suppressed)")
        raise SystemExit(2)
