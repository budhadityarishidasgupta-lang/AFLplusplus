#!/usr/bin/env python3
"""Finite, read-only staging authorization assertions."""
from __future__ import annotations

import csv
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

from report_export import redact

_ALLOWED_HOST = re.compile(r"^(localhost|127\.0\.0\.1|target|mock-streaming|[a-z0-9-]+\.local)$")
_ALLOWED_PORTS = {80, 3000, 8000, 8080}
ADMIN_LOGIN_PATH = os.environ.get("ADMIN_LOGIN_PATH", "/admin/login")
CREATOR_SETTINGS_PATH = os.environ.get("CREATOR_SETTINGS_PATH", "/api/v1/creators/creator_test_99/settings")
TEST_ACCOUNT_ID = os.environ.get("TEST_ACCOUNT_ID", "creator_test_99")
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", "/dev/shm/fuzzer_runtime"))
AUDIT_PATH = Path(os.environ.get("AUDIT_PATH", "./logs/audit_trail.csv"))
REPORT_PATH = Path(os.environ.get("REPORT_PATH", "./logs/PATCH_ADVISORY.md"))
TIMEOUT_ALERT_PATH = Path(os.environ.get("TIMEOUT_ALERT_PATH", "./logs/TIMEOUT_ALERT.md"))


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


class BudgetExhaustedError(RuntimeError):
    def __init__(self, attempts: int, budget: int, events: list[Assertion]):
        super().__init__(f"attempt budget exhausted: {attempts}>{budget}")
        self.attempts = attempts
        self.budget = budget
        self.events = events


def _fingerprint(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _request(scope: Scope, path: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    request = urllib.request.Request(scope.url + path, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(8192)
    except urllib.error.HTTPError as error:
        return error.code, error.read(8192)


def run_assertions(target_url: str, *, max_attempts: int = 3, attempt_budget: int = 500, repetitions: int = 1) -> list[Assertion]:
    scope = validate_target(target_url)
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be 1-10")
    if not 1 <= attempt_budget <= 500:
        raise ValueError("attempt_budget must be 1-500")
    if not 1 <= repetitions <= 1000:
        raise ValueError("repetitions must be 1-1000")
    cases = [
        ("admin-boundary", ADMIN_LOGIN_PATH, (401, 403)),
        ("creator-isolation", CREATOR_SETTINGS_PATH, (401, 403)),
    ]
    results: list[Assertion] = []
    current_attempts = 0
    for assertion_id, path, expected in cases:
        status = None
        digest = None
        detail = ""
        for _ in range(repetitions):
            if current_attempts > attempt_budget:
                raise BudgetExhaustedError(current_attempts, attempt_budget, results)
            current_attempts += 1
            started = time.monotonic()
            try:
                status, body = _request(scope, path)
                digest = _fingerprint(body)
                detail = f"response_ms={(time.monotonic() - started) * 1000:.1f}"
            except (urllib.error.URLError, TimeoutError, ValueError) as error:
                detail = type(error).__name__
        outcome = "PASS" if status in expected else ("FINDING" if status in (200, 204) else "ERROR")
        results.append(Assertion(assertion_id, "authorization", "GET", path, expected, status, outcome, digest, detail))
    if current_attempts > attempt_budget:
        raise BudgetExhaustedError(current_attempts, attempt_budget, results)
    return results


def append_audit(events: list[Assertion], audit_path: Path, campaign_id: str) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    execution_hash = hashlib.sha256(json.dumps([asdict(e) for e in events], sort_keys=True).encode()).hexdigest()
    fields = ["campaign_id", "execution_hash", "assertion_id", "phase", "method", "path", "expected_status", "observed_status", "outcome", "response_sha256", "detail"]
    new_file = not audit_path.exists() or audit_path.stat().st_size == 0
    with audit_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new_file:
            writer.writeheader()
        for event in events:
            row = asdict(event)
            writer.writerow({"campaign_id": campaign_id, "execution_hash": execution_hash, **row, "expected_status": ";".join(map(str, event.expected_status))})
        handle.flush()
        os.fsync(handle.fileno())
    audit_path.chmod(0o640)


def write_timeout_alert(error: BudgetExhaustedError, alert_path: Path, campaign_id: str) -> None:
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    alert_path.write_text(
        "# TIMEOUT ALERT\n\n"
        f"- Campaign: `{campaign_id}`\n"
        f"- Status: `BUDGET_EXHAUSTED`\n"
        f"- Attempts: `{error.attempts}`\n"
        f"- Budget: `{error.budget}`\n"
        "- Action: supervisor review required; no further attempts were issued.\n",
        encoding="utf-8",
    )
    alert_path.chmod(0o640)


def write_developer_report(events: list[Assertion], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# PATCH ADVISORY", "", "Generated from a finite, read-only staging authorization assertion run.", ""]
    for event in events:
        lines.append(f"- `{event.assertion_id}`: **{event.outcome}**; observed={event.observed_status}; expected={event.expected_status}; path=`{event.path}`")
    report_path.write_text(redact("\n".join(lines) + "\n"), encoding="utf-8")
    report_path.chmod(0o640)


def main() -> int:
    target = os.environ.get("TARGET_SCOPE_URL", "")
    campaign = os.environ.get("CAMPAIGN_ID", "validation-01")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    audit = Path(os.environ.get("AUDIT_PATH", str(AUDIT_PATH)))
    try:
        events = run_assertions(target)
    except BudgetExhaustedError as error:
        events = list(error.events)
        events.append(Assertion("budget-exhausted", "governance", "N/A", "N/A", (), None, "BUDGET_EXHAUSTED", None, f"attempts={error.attempts};budget={error.budget}"))
        append_audit(events, audit, campaign)
        write_timeout_alert(error, Path(os.environ.get("TIMEOUT_ALERT_PATH", str(TIMEOUT_ALERT_PATH))), campaign)
        write_developer_report(events, Path(os.environ.get("REPORT_PATH", str(REPORT_PATH))))
        for event in events:
            print(json.dumps(asdict(event), sort_keys=True))
        return 2
    append_audit(events, audit, campaign)
    write_developer_report(events, Path(os.environ.get("REPORT_PATH", str(REPORT_PATH))))
    for event in events:
        print(json.dumps(asdict(event), sort_keys=True))
    return 1 if any(event.outcome == "FINDING" for event in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
