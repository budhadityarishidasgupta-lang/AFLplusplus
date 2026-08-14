#!/usr/bin/env python3
"""Finite, read-only staging authorization assertions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

_ALLOWED_HOST = re.compile(r"^(localhost|127\.0\.0\.1|target|mock-streaming|[a-z0-9-]+\.local)$")
_ALLOWED_PORTS = {80, 3000, 8000, 8080}


@dataclass(frozen=True)
class Scope:
    url: str
    host: str
    port: int


def validate_target(raw: str) -> Scope:
    if not raw or any(c in raw for c in "\r\n\t @"):
        raise ValueError("TARGET_SCOPE_URL is missing or contains forbidden characters")
    parsed = urlparse(raw.rstrip("/"))
    host = (parsed.hostname or "").lower()
    port = parsed.port or 80
    if parsed.scheme != "http" or not _ALLOWED_HOST.fullmatch(host) or port not in _ALLOWED_PORTS:
        raise ValueError("TARGET_SCOPE_URL is outside the local staging allowlist")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("TARGET_SCOPE_URL may contain only scheme, host, and port")
    return Scope(raw.rstrip("/"), host, port)


@dataclass(frozen=True)
class Assertion:
    assertion_id: str
    phase: str
    method: str
    path: str
    expected_status: tuple[int, ...]
    observed_status: int | None
    outcome: str
    response_sha256: str | None
    detail: str


def _fingerprint(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _request(scope: Scope, path: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    request = urllib.request.Request(scope.url + path, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(8192)
    except urllib.error.HTTPError as error:
        return error.code, error.read(8192)


def run_assertions(target_url: str, *, max_attempts: int = 3) -> list[Assertion]:
    scope = validate_target(target_url)
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be 1-10")
    cases = [
        ("admin-boundary", "/admin/login", (401, 403)),
        ("creator-isolation", "/api/v1/creators/creator_test_99/settings", (401, 403)),
    ]
    results: list[Assertion] = []
    for assertion_id, path, expected in cases:
        status = None
        digest = None
        detail = ""
        for _ in range(min(max_attempts, 1)):
            started = time.monotonic()
            try:
                status, body = _request(scope, path)
                digest = _fingerprint(body)
                detail = f"response_ms={(time.monotonic() - started) * 1000:.1f}"
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                detail = type(error).__name__
        outcome = "PASS" if status in expected else ("FINDING" if status in (200, 204) else "ERROR")
        results.append(Assertion(assertion_id, "authorization", "GET", path, expected, status, outcome, digest, detail))
    return results


def append_audit(events: list[Assertion], audit_path: Path, campaign_id: str) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "campaign_id": campaign_id,
        "execution_hash": hashlib.sha256(json.dumps([asdict(e) for e in events], sort_keys=True).encode()).hexdigest(),
        "events": [asdict(e) for e in events],
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    audit_path.chmod(0o640)


def main() -> int:
    target = os.environ.get("TARGET_SCOPE_URL", "")
    campaign = os.environ.get("CAMPAIGN_ID", "validation-01")
    audit = Path(os.environ.get("AUDIT_PATH", "/audit/audit.jsonl"))
    events = run_assertions(target)
    append_audit(events, audit, campaign)
    for event in events:
        print(json.dumps(asdict(event), sort_keys=True))
    return 1 if any(event.outcome == "FINDING" for event in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
