"""Deterministic campaign lifecycle and immutable approval binding.

This module is intentionally dependency-free. It is the domain boundary between
agent-proposed campaign specifications and a future AFL++ worker adapter.
Agents never receive process-execution authority from this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class CampaignError(RuntimeError):
    """Base error for campaign-domain failures."""


class InvalidTransition(CampaignError):
    """Raised when a requested state transition is not explicitly allowed."""


class ApprovalError(CampaignError):
    """Raised when approval is absent, stale, malformed, or does not match."""


class SpecError(CampaignError):
    """Raised when a campaign specification violates the bounded domain model."""


class CampaignState(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    EXHAUSTED = "EXHAUSTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


TERMINAL_STATES = frozenset(
    {
        CampaignState.SUCCEEDED,
        CampaignState.EXHAUSTED,
        CampaignState.CANCELLED,
        CampaignState.FAILED,
        CampaignState.BLOCKED,
    }
)

_ALLOWED_TRANSITIONS: Mapping[CampaignState, frozenset[CampaignState]] = {
    CampaignState.DRAFT: frozenset({CampaignState.VALIDATED, CampaignState.BLOCKED}),
    CampaignState.VALIDATED: frozenset(
        {CampaignState.AWAITING_APPROVAL, CampaignState.BLOCKED}
    ),
    CampaignState.AWAITING_APPROVAL: frozenset(
        {CampaignState.APPROVED, CampaignState.BLOCKED, CampaignState.CANCELLED}
    ),
    CampaignState.APPROVED: frozenset(
        {CampaignState.QUEUED, CampaignState.BLOCKED, CampaignState.CANCELLED}
    ),
    CampaignState.QUEUED: frozenset(
        {CampaignState.RUNNING, CampaignState.BLOCKED, CampaignState.CANCELLED, CampaignState.FAILED}
    ),
    CampaignState.RUNNING: frozenset(TERMINAL_STATES),
    CampaignState.SUCCEEDED: frozenset(),
    CampaignState.EXHAUSTED: frozenset(),
    CampaignState.CANCELLED: frozenset(),
    CampaignState.FAILED: frozenset(),
    CampaignState.BLOCKED: frozenset(),
}

_ALLOWED_TARGET_KINDS = frozenset({"local_binary", "local_harness", "loopback_service"})
_ALLOWED_MUTATION_PROFILES = frozenset({"default", "exploration", "exploitation", "adaptive"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_spec_bytes(spec: Mapping[str, Any]) -> bytes:
    """Return stable UTF-8 JSON bytes used for cryptographic approval binding.

    Floats are intentionally prohibited because cross-runtime float serialization
    can undermine reproducible digests. Campaign schemas should use integers,
    strings, booleans, nulls, arrays, and objects only.
    """

    def reject_floats(value: Any, path: str = "$") -> None:
        if isinstance(value, float):
            raise SpecError(f"floating-point values are not permitted in campaign specs: {path}")
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise SpecError(f"object keys must be strings: {path}")
                reject_floats(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                reject_floats(child, f"{path}[{index}]")

    reject_floats(spec)
    try:
        encoded = json.dumps(
            spec,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise SpecError("campaign spec must be canonical JSON data") from exc
    return encoded.encode("utf-8")


def spec_digest(spec: Mapping[str, Any]) -> str:
    """Return a versioned SHA-256 digest for an immutable campaign spec."""

    return "sha256:" + hashlib.sha256(canonical_spec_bytes(spec)).hexdigest()


def validate_bounded_spec(spec: Mapping[str, Any]) -> None:
    """Enforce security-critical campaign bounds independently of the LLM layer.

    This is deliberately a compact domain validator, not a replacement for the
    repository JSON Schema. API ingress should perform JSON Schema validation
    first, then this validator enforces invariants the worker relies upon.
    """

    required = {"schema_version", "campaign_id", "target", "limits", "strategy"}
    missing = required.difference(spec)
    if missing:
        raise SpecError(f"missing required campaign fields: {sorted(missing)}")
    if spec.get("schema_version") != 1:
        raise SpecError("unsupported campaign schema_version")

    campaign_id = spec.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 64:
        raise SpecError("campaign_id must be a non-empty string of at most 64 characters")
    if not campaign_id[0].isalnum() or any(
        not (char.isalnum() or char in "._-") for char in campaign_id
    ):
        raise SpecError("campaign_id contains unsupported characters")

    target = spec.get("target")
    if not isinstance(target, Mapping):
        raise SpecError("target must be an object")
    kind = target.get("kind")
    if kind not in _ALLOWED_TARGET_KINDS:
        raise SpecError("target kind is not allowlisted")
    identifier = target.get("identifier")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
        raise SpecError("target identifier must be a bounded non-empty string")
    if kind == "loopback_service":
        if target.get("host") != "127.0.0.1":
            raise SpecError("loopback_service host must be 127.0.0.1")
        port = target.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise SpecError("loopback_service port must be between 1 and 65535")
    elif "host" in target or "port" in target:
        raise SpecError("host/port are only valid for loopback_service targets")

    limits = spec.get("limits")
    if not isinstance(limits, Mapping):
        raise SpecError("limits must be an object")
    bounded_limits = {
        "max_seconds": (1, 86400),
        "max_executions": (1, 1_000_000_000),
        "max_input_bytes": (1, 1_048_576),
        "memory_mb": (32, 32768),
    }
    for name, (minimum, maximum) in bounded_limits.items():
        value = limits.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise SpecError(f"{name} must be between {minimum} and {maximum}")

    strategy = spec.get("strategy")
    if not isinstance(strategy, Mapping):
        raise SpecError("strategy must be an object")
    seed = strategy.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise SpecError("strategy.seed must be a non-negative integer")
    mutation_profile = strategy.get("mutation_profile", "default")
    if mutation_profile not in _ALLOWED_MUTATION_PROFILES:
        raise SpecError("mutation_profile is not allowlisted")

    canonical_spec_bytes(spec)


@dataclass(frozen=True)
class ApprovalRecord:
    campaign_id: str
    spec_digest: str
    operator_id: str
    approved_at: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.operator_id.strip():
            raise ApprovalError("operator_id is required")
        if self.approved_at.tzinfo is None:
            raise ApprovalError("approved_at must be timezone-aware")
        if len(self.reason) > 2048:
            raise ApprovalError("approval reason exceeds 2048 characters")


@dataclass(frozen=True)
class StateEvent:
    previous: CampaignState
    current: CampaignState
    occurred_at: datetime
    actor: str
    reason: str


@dataclass
class CampaignRecord:
    """In-memory campaign aggregate with fail-closed lifecycle enforcement.

    Persistence is intentionally delegated to a later repository layer. Callers
    must store the original spec and event stream atomically in durable storage.
    """

    spec: Mapping[str, Any]
    state: CampaignState = CampaignState.DRAFT
    approval: ApprovalRecord | None = None
    events: list[StateEvent] = field(default_factory=list)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Canonicalize through JSON to detach from caller-owned mutable objects.
        frozen_copy = json.loads(canonical_spec_bytes(self.spec).decode("utf-8"))
        self.spec = frozen_copy
        self._digest = spec_digest(frozen_copy)

    @property
    def campaign_id(self) -> str:
        return str(self.spec.get("campaign_id", ""))

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def assert_spec_integrity(self) -> None:
        current = spec_digest(self.spec)
        if current != self._digest:
            raise ApprovalError("campaign specification changed after record creation")
        if self.approval is not None and self.approval.spec_digest != current:
            raise ApprovalError("approval does not match the current campaign specification")

    def validate(self, *, actor: str = "control-plane") -> None:
        validate_bounded_spec(self.spec)
        self._transition(CampaignState.VALIDATED, actor, "campaign spec validated")

    def request_approval(self, *, actor: str = "control-plane") -> None:
        self.assert_spec_integrity()
        self._transition(
            CampaignState.AWAITING_APPROVAL,
            actor,
            "explicit operator approval required",
        )

    def approve(
        self,
        *,
        operator_id: str,
        supplied_digest: str,
        reason: str = "",
        approved_at: datetime | None = None,
    ) -> ApprovalRecord:
        self.assert_spec_integrity()
        if self.state != CampaignState.AWAITING_APPROVAL:
            raise InvalidTransition(f"cannot approve campaign from {self.state.value}")
        if supplied_digest != self._digest:
            raise ApprovalError("approval digest does not match immutable campaign spec")
        approval = ApprovalRecord(
            campaign_id=self.campaign_id,
            spec_digest=self._digest,
            operator_id=operator_id,
            approved_at=approved_at or utc_now(),
            reason=reason,
        )
        self.approval = approval
        self._transition(CampaignState.APPROVED, operator_id, "campaign explicitly approved")
        return approval

    def queue(self, *, actor: str = "control-plane") -> None:
        self._require_valid_approval()
        self._transition(CampaignState.QUEUED, actor, "campaign queued for bounded worker")

    def start(self, *, worker_id: str) -> None:
        if not worker_id.strip():
            raise CampaignError("worker_id is required")
        self._require_valid_approval()
        self._transition(CampaignState.RUNNING, worker_id, "bounded worker started campaign")

    def finish(self, terminal_state: CampaignState, *, actor: str, reason: str) -> None:
        if terminal_state not in TERMINAL_STATES:
            raise InvalidTransition("finish requires a terminal state")
        if not reason.strip():
            raise CampaignError("terminal transition requires a reason")
        self.assert_spec_integrity()
        self._transition(terminal_state, actor, reason)

    def block(self, *, actor: str, reason: str) -> None:
        if not reason.strip():
            raise CampaignError("blocked transition requires a reason")
        self._transition(CampaignState.BLOCKED, actor, reason)

    def cancel(self, *, actor: str, reason: str) -> None:
        if not reason.strip():
            raise CampaignError("cancelled transition requires a reason")
        self._transition(CampaignState.CANCELLED, actor, reason)

    def _require_valid_approval(self) -> None:
        self.assert_spec_integrity()
        if self.approval is None:
            raise ApprovalError("campaign has no approval")
        if self.approval.campaign_id != self.campaign_id:
            raise ApprovalError("approval campaign_id mismatch")
        if self.approval.spec_digest != self._digest:
            raise ApprovalError("approval digest mismatch")

    def _transition(
        self,
        target: CampaignState,
        actor: str,
        reason: str,
    ) -> None:
        if not actor.strip():
            raise CampaignError("transition actor is required")
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise InvalidTransition(f"transition {self.state.value} -> {target.value} is not allowed")
        previous = self.state
        self.state = target
        self.events.append(
            StateEvent(
                previous=previous,
                current=target,
                occurred_at=utc_now(),
                actor=actor,
                reason=reason,
            )
        )
