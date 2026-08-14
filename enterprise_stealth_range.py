"""Bounded browser-session models for a loopback QA range.

Despite the historical module name, this implementation intentionally does not
hide automation, spoof fingerprints, bypass access controls, or contact remote
hosts.  A concrete browser adapter may translate the returned configuration to
Playwright/Selenium while preserving these invariants.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

MAX_ATTEMPTS = 500
ALLOWED_ROLES = frozenset({"standard_user", "admin_test"})
ALLOWED_COMMANDS = frozenset({"set_path", "set_pacing_ms", "snapshot"})


class ConfigurationError(ValueError):
    """A campaign or live command violates a bounded-range invariant."""


class BudgetExhaustedError(RuntimeError):
    """The deterministic attempt budget has been consumed."""


class CampaignMode(str, Enum):
    ZERO_KNOWLEDGE = "zero_knowledge"
    CREDENTIALED = "credentialed"


class PipelineStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    HALTED = "HALTED"


def _loopback_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "127.0.0.1":
        raise ConfigurationError(f"{label} must be an http(s) URL on literal 127.0.0.1")
    if parsed.username or parsed.password or parsed.fragment:
        raise ConfigurationError(f"{label} must not contain userinfo or a fragment")
    return value


@dataclass(frozen=True)
class CampaignScope:
    target_scope_url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    auth_entry_route: str | None = None
    role_profile: str | None = None

    @property
    def mode(self) -> CampaignMode:
        return CampaignMode.CREDENTIALED if self.username is not None else CampaignMode.ZERO_KNOWLEDGE


class AuthorizationScenarioAdapter:
    """Load either exact campaign shape and enforce loopback/same-origin scope."""

    ZERO_KEYS = {"target_scope_url"}
    AUTH_KEYS = ZERO_KEYS | {"username", "password", "auth_entry_route", "role_profile"}

    @classmethod
    def load(cls, path: str | Path = "campaign_scope.json") -> CampaignScope:
        with Path(path).open(encoding="utf-8") as source:
            payload = json.load(source)
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CampaignScope:
        if not isinstance(payload, Mapping) or set(payload) not in {frozenset(cls.ZERO_KEYS), frozenset(cls.AUTH_KEYS)}:
            raise ConfigurationError("campaign_scope.json must use exactly one documented structure")
        target = _loopback_url(payload["target_scope_url"], "target_scope_url")
        if set(payload) == cls.ZERO_KEYS:
            return CampaignScope(target)
        for key in ("username", "password", "auth_entry_route", "role_profile"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise ConfigurationError(f"{key} must be a non-empty string")
        if payload["role_profile"] not in ALLOWED_ROLES:
            raise ConfigurationError("role_profile is not supported")
        auth_url = _loopback_url(urljoin(target, payload["auth_entry_route"]), "auth_entry_route")
        if urlparse(auth_url).netloc != urlparse(target).netloc:
            raise ConfigurationError("auth_entry_route must have the target's origin")
        return CampaignScope(target, payload["username"], payload["password"], auth_url, payload["role_profile"])


@dataclass(frozen=True)
class ProxyConfiguration:
    """QA proxy route; only explicit loopback gateways are accepted."""

    server: str

    def __post_init__(self) -> None:
        _loopback_url(self.server, "proxy server")


@dataclass(frozen=True)
class BrowserConfiguration:
    headless: bool = True
    viewport: tuple[int, int] = (1440, 900)
    proxies: tuple[ProxyConfiguration, ...] = ()
    automation_disclosed: bool = True
    fingerprint_randomization: bool = False

    def __post_init__(self) -> None:
        width, height = self.viewport
        if not (800 <= width <= 3840 and 600 <= height <= 2160):
            raise ConfigurationError("viewport is outside the QA desktop bounds")
        if not self.automation_disclosed or self.fingerprint_randomization:
            raise ConfigurationError("automation disclosure cannot be hidden and fingerprints cannot be spoofed")


async def simulate_human_delay(
    minimum: float = 3.0, maximum: float = 15.0, *, rng: random.Random | None = None
) -> float:
    """Sleep for a seeded-testable QA pacing interval and return its duration."""
    if minimum < 0 or maximum < minimum or maximum > 15.0:
        raise ConfigurationError("delay must be ordered and bounded by 15 seconds")
    delay = (rng or random).uniform(minimum, maximum)
    await asyncio.sleep(delay)
    return delay


def simulate_mouse_trajectory(
    start: tuple[float, float], end: tuple[float, float], *, steps: int = 20
) -> tuple[tuple[float, float], ...]:
    """Return points on a deterministic quadratic Bézier curve for UI QA."""
    if not 2 <= steps <= 100:
        raise ConfigurationError("steps must be in [2, 100]")
    control = ((start[0] + end[0]) / 2, min(start[1], end[1]) - abs(end[0] - start[0]) / 6)
    points = []
    for index in range(steps + 1):
        t = index / steps
        x = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * control[0] + t**2 * end[0]
        y = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * control[1] + t**2 * end[1]
        points.append((x, y))
    return tuple(points)


class BrowserAdapter(Protocol):
    async def stop(self) -> None: ...
    async def snapshot(self) -> None: ...


class HardenedBrowserRunner:
    """Transparency-preserving wrapper for high-fidelity local UI tests."""

    def __init__(self, adapter: BrowserAdapter, config: BrowserConfiguration | None = None) -> None:
        self.adapter = adapter
        self.config = config or BrowserConfiguration()
        self.stopped = False

    def initialization_model(self) -> dict[str, Any]:
        return {
            "headless": self.config.headless,
            "viewport": {"width": self.config.viewport[0], "height": self.config.viewport[1]},
            "proxy": [route.server for route in self.config.proxies],
            "automation_disclosed": True,
            "cdp_evasion_scripts": [],
            "fingerprint_randomization": False,
        }

    async def halt(self) -> None:
        if not self.stopped:
            self.stopped = True
            await self.adapter.stop()


class AuditTrail:
    FIELDS = ("event", "detail")

    def __init__(self, path: str | Path = "logs/audit_trail.csv") -> None:
        self.path = Path(path)

    def append(self, event: str, detail: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        with self.path.open("a", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=self.FIELDS)
            if new:
                writer.writeheader()
            writer.writerow({"event": event, "detail": detail})
            output.flush()


def classification_fingerprint(response: str) -> tuple[str, str]:
    normalized = re.sub(r"(?i)(password|token|secret|cookie)\s*[:=]\s*\S+", r"\1=[REDACTED]", response)
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return digest, normalized[:160]


class LiveCommandChannel:
    """Validate newline-delimited JSON commands from an asynchronous source."""

    def __init__(self, lines: AsyncIterator[str], audit: AuditTrail) -> None:
        self.lines, self.audit = lines, audit

    async def commands(self) -> AsyncIterator[dict[str, Any]]:
        async for line in self.lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                self.audit.append("command_rejected", "invalid JSON")
                continue
            if not isinstance(value, dict) or value.get("command") not in ALLOWED_COMMANDS:
                self.audit.append("command_rejected", "unsupported command")
                continue
            command = value["command"]
            if command == "set_path" and (set(value) != {"command", "path"} or not isinstance(value["path"], str) or not value["path"].startswith("/")):
                self.audit.append("command_rejected", "invalid path")
                continue
            if command == "set_pacing_ms" and (set(value) != {"command", "value"} or isinstance(value["value"], bool) or not isinstance(value["value"], int) or not 0 <= value["value"] <= 15000):
                self.audit.append("command_rejected", "invalid pacing")
                continue
            if command == "snapshot" and set(value) != {"command"}:
                self.audit.append("command_rejected", "invalid snapshot")
                continue
            self.audit.append("command_accepted", command)
            yield value


class SystemArchitect:
    """Route bounded campaigns and own stop/approval decisions."""

    def __init__(self, runner: HardenedBrowserRunner, audit: AuditTrail | None = None) -> None:
        self.runner, self.audit = runner, audit or AuditTrail()
        self.status = PipelineStatus.READY

    def preflight(self, scope: CampaignScope) -> dict[str, Any]:
        if scope.mode is CampaignMode.ZERO_KNOWLEDGE:
            return {"worker": "exploit_dev_01", "phase": "login_gate_analysis", "target": scope.target_scope_url}
        return {"worker": "exploit_dev_01", "phase": "authenticated_authorization_checks", "auth_url": scope.auth_entry_route, "role": scope.role_profile, "session_storage": "memory-only"}

    async def enforce_attempt_budget(self, attempts: int, alert_path: str | Path = "TIMEOUT_ALERT.md") -> None:
        if attempts < MAX_ATTEMPTS:
            return
        await self.runner.halt()
        self.status = PipelineStatus.HALTED
        self.audit.append("budget_exhausted", str(attempts))
        Path(alert_path).write_text(f"# Timeout alert\n\nAttempt budget reached: {attempts}. Browser halted cleanly.\n", encoding="utf-8")
        raise BudgetExhaustedError(f"attempt budget reached ({attempts})")

    async def boundary_breach(self, response: str) -> tuple[str, str]:
        await self.runner.halt()
        self.status = PipelineStatus.APPROVAL_REQUIRED
        digest, redacted = classification_fingerprint(response)
        self.audit.append("approval_required", f"classification={digest}; fingerprint={redacted}")
        return digest, redacted

    async def request_manual_approval(self, reader: Callable[[str], Awaitable[str]]) -> bool:
        if self.status is not PipelineStatus.APPROVAL_REQUIRED:
            raise ConfigurationError("manual approval is only valid at APPROVAL_REQUIRED")
        answer = (await reader("Boundary condition detected. Continue validation? [Yes/No]: ")).strip().lower()
        approved = answer in {"yes", "y"}
        self.audit.append("manual_decision", "approved" if approved else "denied")
        self.status = PipelineStatus.READY if approved else PipelineStatus.HALTED
        return approved
