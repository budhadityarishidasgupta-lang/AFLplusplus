"""Log-first local security-validation campaign runner.

This module is intentionally deterministic and local-only. It does not perform
network requests, invoke a shell, or generate exploit payloads. A real adapter
may be added later, but it must satisfy the same policy and evidence contract.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CampaignPolicy:
    cycle_id: str
    target_host: str = "127.0.0.1"
    target_port: int = 3000
    creator_id: str = "creator_test_99"
    max_attempts: int = 20
    dry_run: bool = True

    def validate(self) -> None:
        if not self.cycle_id or len(self.cycle_id) > 64:
            raise ValueError("cycle_id must contain 1-64 characters")
        if self.target_host != "127.0.0.1":
            raise ValueError("only literal loopback target 127.0.0.1 is allowed")
        if not 1 <= self.target_port <= 65535:
            raise ValueError("target_port must be in [1, 65535]")
        if not self.creator_id or len(self.creator_id) > 64:
            raise ValueError("creator_id must be a bounded non-empty value")
        if not 1 <= self.max_attempts <= 1000:
            raise ValueError("max_attempts must be in [1, 1000]")


@dataclass(frozen=True)
class LogEvent:
    timestamp: str
    cycle_id: str
    phase: str
    status: str
    detail: str
    target: str
    evidence: dict[str, Any]


class MockStreamingTarget:
    """A resettable, in-memory streaming authorization fixture."""

    def __init__(self, creator_id: str) -> None:
        self.creator_id = creator_id
        self._settings = {"stream_enabled": True, "visibility": "private"}

    def authenticate(self, token: str) -> tuple[int, str | None]:
        sessions = {"admin-test-token": "admin", "creator-test-token": "creator"}
        role = sessions.get(token)
        return (200, role) if role else (401, None)

    def creator_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def update_creator_settings(
        self, role: str | None, creator_id: str, patch: dict[str, Any], *, dry_run: bool
    ) -> tuple[int, bool, str]:
        if creator_id != self.creator_id:
            return 404, False, "test creator not found"
        if role != "admin":
            return 403, False, "authorization denied"
        if dry_run:
            return 200, False, "authorized control path validated; dry-run prevented mutation"
        self._settings.update(patch)
        return 200, True, "authorized control path updated fixture"

    def reset(self) -> None:
        self._settings = {"stream_enabled": True, "visibility": "private"}


def _event(policy: CampaignPolicy, phase: str, status: str, detail: str, **evidence: Any) -> LogEvent:
    return LogEvent(
        datetime.now(timezone.utc).isoformat(),
        policy.cycle_id,
        phase,
        status,
        detail,
        f"http://{policy.target_host}:{policy.target_port}",
        evidence,
    )


def run_campaign(policy: CampaignPolicy) -> list[LogEvent]:
    policy.validate()
    target = MockStreamingTarget(policy.creator_id)
    events: list[LogEvent] = []
    before = target.creator_settings()
    events.append(_event(policy, "policy", "PASS", "local-only policy accepted", creator_id=policy.creator_id))

    status, role = target.authenticate("creator-test-token")
    events.append(_event(policy, "authentication", "PASS" if status == 200 else "FAIL", "test creator session established", http_status=status, role=role))

    status, changed, detail = target.update_creator_settings(
        role, policy.creator_id, {"visibility": "public"}, dry_run=policy.dry_run
    )
    after = target.creator_settings()
    safe_denial = status == 403 and not changed and before == after
    events.append(_event(
        policy,
        "authorization",
        "PASS" if safe_denial else "FINDING",
        detail,
        http_status=status,
        mutation_observed=changed,
        state_unchanged=before == after,
        expected="creator session must not change creator settings",
    ))

    target.reset()
    events.append(_event(policy, "reset", "PASS" if target.creator_settings() == before else "FAIL", "fixture reset completed", state=target.creator_settings()))
    return events


def write_reports(events: list[LogEvent], output_dir: Path, *, excel: bool = False) -> dict[str, Path]:
    if not events:
        raise ValueError("at least one event is required")
    output_dir.mkdir(parents=True, exist_ok=True)
    cycle_id = events[0].cycle_id
    md_path = output_dir / f"{cycle_id}.md"
    csv_path = output_dir / f"{cycle_id}.csv"
    lines = [f"# Security Validation Log: `{cycle_id}`", "", "> Local-only deterministic fixture run. No external network requests were made.", "", "| Time (UTC) | Phase | Status | Detail | Evidence |", "|---|---|---|---|---|"]
    for event in events:
        lines.append(f"| {event.timestamp} | {event.phase} | **{event.status}** | {event.detail} | `{json.dumps(event.evidence, sort_keys=True)}` |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "cycle_id", "phase", "status", "detail", "target", "evidence"])
        writer.writeheader()
        for event in events:
            writer.writerow({**event.__dict__, "evidence": json.dumps(event.evidence, sort_keys=True)})
    paths = {"markdown": md_path, "csv": csv_path}
    if excel:
        try:
            from openpyxl import Workbook
        except ImportError as error:
            raise RuntimeError("Excel export requires openpyxl") from error
        xlsx_path = output_dir / f"{cycle_id}.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "events"
        sheet.append(["timestamp", "cycle_id", "phase", "status", "detail", "target", "evidence"])
        for event in events:
            sheet.append([event.timestamp, event.cycle_id, event.phase, event.status, event.detail, event.target, json.dumps(event.evidence, sort_keys=True)])
        workbook.save(xlsx_path)
        paths["excel"] = xlsx_path
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle-id", default="local-demo-01")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--excel", action="store_true")
    args = parser.parse_args()
    events = run_campaign(CampaignPolicy(cycle_id=args.cycle_id))
    paths = write_reports(events, args.output_dir, excel=args.excel)
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0 if all(event.status != "FAIL" for event in events) else 1


if __name__ == "__main__":
    raise SystemExit(main())
