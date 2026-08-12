"""Deterministic campaign state machine with digest-bound human approval.

This module contains no persistence, worker execution, or agent integration.
Callers must serialize transitions if a campaign is shared between threads or
processes and persist returned events in an append-only store.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
ALLOWED_MUTATION_PROFILES = frozenset({"balanced", "coverage", "havoc"})
TERMINAL_STATES: frozenset["CampaignState"]


class CampaignError(ValueError):
    """Base class for deterministic domain validation failures."""


class InvalidTransition(CampaignError):
    """The requested state transition is not permitted."""


class ApprovalError(CampaignError):
    """Approval is absent, stale, malformed, or bound to another campaign."""


class SpecificationError(CampaignError):
    """The bounded campaign specification violates a security invariant."""


class CampaignState(str, Enum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"
    SUCCEEDED = "SUCCEEDED"


TERMINAL_STATES = frozenset(
    {
        CampaignState.BLOCKED,
        CampaignState.CANCELLED,
        CampaignState.FAILED,
        CampaignState.EXHAUSTED,
        CampaignState.SUCCEEDED,
    }
)


@dataclass(frozen=True)
class Approval:
    """Human approval cryptographically bound to one exact specification."""

    campaign_id: str
    specification_digest: str
    operator: str
    approved_at: datetime


@dataclass(frozen=True)
class StateEvent:
    """Complete transition fact suitable for a future append-only audit log."""

    previous_state: CampaignState
    new_state: CampaignState
    timestamp: datetime
    actor: str
    reason: str


def _reject_floats(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise SpecificationError(f"floating-point value is forbidden at {path}")
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpecificationError(f"object key must be a string at {path}")
            _reject_floats(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")
        return
    raise SpecificationError(f"unsupported value type at {path}: {type(value).__name__}")


def canonical_specification(spec: Mapping[str, Any]) -> bytes:
    """Return stable UTF-8 JSON bytes after rejecting ambiguous number types."""
    _reject_floats(spec)
    try:
        encoded = json.dumps(
            _plain_json(spec),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise SpecificationError("specification is not canonical JSON") from error
    return encoded.encode("utf-8")


def _plain_json(value: Any) -> Any:
    """Convert immutable domain containers to JSON encoder primitives."""
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def specification_digest(spec: Mapping[str, Any]) -> str:
    """Return the lowercase SHA-256 digest of the canonical specification."""
    return hashlib.sha256(canonical_specification(spec)).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def validate_campaign_id(campaign_id: str) -> None:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
        raise SpecificationError("campaign_id must be 1-64 lowercase URL-safe characters")


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise SpecificationError(f"{path} keys must be exactly {sorted(expected)}")


def _bounded_int(value: Any, minimum: int, maximum: int, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SpecificationError(f"{path} must be an integer in [{minimum}, {maximum}]")


def validate_bounded_spec(spec: Mapping[str, Any]) -> None:
    """Enforce the complete local campaign domain independently of JSON Schema."""
    if not isinstance(spec, Mapping):
        raise SpecificationError("specification must be an object")
    _reject_floats(spec)
    _exact_keys(spec, {"target", "limits", "strategy", "metadata"}, "specification")

    target = spec["target"]
    limits = spec["limits"]
    strategy = spec["strategy"]
    metadata = spec["metadata"]
    if not all(isinstance(item, Mapping) for item in (target, limits, strategy, metadata)):
        raise SpecificationError("target, limits, strategy, and metadata must be objects")

    _exact_keys(target, {"kind", "host", "port"}, "target")
    if target["kind"] != "mock_http" or target["host"] != "127.0.0.1":
        raise SpecificationError("target must be mock_http on literal host 127.0.0.1")
    _bounded_int(target["port"], 1, 65535, "target.port")

    _exact_keys(limits, {"max_executions", "max_seconds", "max_input_bytes"}, "limits")
    _bounded_int(limits["max_executions"], 1, 1_000_000, "limits.max_executions")
    _bounded_int(limits["max_seconds"], 1, 86_400, "limits.max_seconds")
    _bounded_int(limits["max_input_bytes"], 1, 1_048_576, "limits.max_input_bytes")

    _exact_keys(strategy, {"mutation_profile", "seed"}, "strategy")
    if strategy["mutation_profile"] not in ALLOWED_MUTATION_PROFILES:
        raise SpecificationError("unsupported mutation profile")
    _bounded_int(strategy["seed"], 0, 4_294_967_295, "strategy.seed")

    for key, value in metadata.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise SpecificationError("metadata keys must contain 1-64 characters")
        if not isinstance(value, str) or len(value) > 1024:
            raise SpecificationError("metadata values must be strings of at most 1024 characters")
    canonical_specification(spec)


def _identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError(f"{label} identity must not be empty")
    return value.strip()


@dataclass
class Campaign:
    """In-memory aggregate for one bounded, human-approved local campaign.

    The nested specification is deeply frozen at construction. The digest is
    still recalculated at security boundaries to detect illicit replacement via
    reflection/deserialization. This object is deliberately not thread-safe.
    """

    campaign_id: str
    spec: Mapping[str, Any]
    state: CampaignState = field(default=CampaignState.DRAFT, init=False)
    approval: Approval | None = field(default=None, init=False)
    _events: list[StateEvent] = field(default_factory=list, init=False, repr=False)
    _created_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        validate_campaign_id(self.campaign_id)
        validate_bounded_spec(self.spec)
        frozen = _deep_freeze(self.spec)
        object.__setattr__(self, "spec", frozen)
        object.__setattr__(self, "_created_digest", specification_digest(frozen))

    @property
    def digest(self) -> str:
        return specification_digest(self.spec)

    @property
    def events(self) -> tuple[StateEvent, ...]:
        """Return an immutable snapshot of emitted transition facts."""
        return tuple(self._events)

    def _transition(self, new_state: CampaignState, actor: str, reason: str) -> StateEvent:
        actor = _identity(actor, "actor")
        if not isinstance(reason, str) or not reason.strip():
            raise CampaignError("transition reason must not be empty")
        if self.state in TERMINAL_STATES:
            raise InvalidTransition(f"terminal campaign in {self.state.value} cannot transition")
        event = StateEvent(self.state, new_state, datetime.now(timezone.utc), actor, reason.strip())
        self.state = new_state
        self._events.append(event)
        return event

    def request_approval(self, actor: str, reason: str = "submitted for human approval") -> StateEvent:
        if self.state is not CampaignState.DRAFT:
            raise InvalidTransition("only DRAFT can request approval")
        return self._transition(CampaignState.PENDING_APPROVAL, actor, reason)

    def approve(self, campaign_id: str, digest: str, operator: str) -> StateEvent:
        if self.state is not CampaignState.PENDING_APPROVAL:
            raise InvalidTransition("only PENDING_APPROVAL can be approved")
        operator = _identity(operator, "operator")
        if campaign_id != self.campaign_id:
            raise ApprovalError("approval campaign ID mismatch")
        current = self.digest
        if current != self._created_digest or digest != current:
            raise ApprovalError("approval specification digest mismatch or specification tampering")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ApprovalError("approval digest must be lowercase SHA-256")
        self.approval = Approval(self.campaign_id, digest, operator, datetime.now(timezone.utc))
        return self._transition(CampaignState.APPROVED, operator, "exact specification approved")

    def queue(self, actor: str, reason: str = "approved campaign queued") -> StateEvent:
        if self.state is not CampaignState.APPROVED:
            raise InvalidTransition("only APPROVED can be queued")
        self._verify_approval()
        return self._transition(CampaignState.QUEUED, actor, reason)

    def start(self, worker: str) -> StateEvent:
        if self.state is not CampaignState.QUEUED:
            raise InvalidTransition("only QUEUED can start")
        worker = _identity(worker, "worker")
        self._verify_approval()
        return self._transition(CampaignState.RUNNING, worker, "worker started campaign")

    def _verify_approval(self) -> None:
        if self.approval is None:
            raise ApprovalError("campaign has no approval")
        if self.approval.campaign_id != self.campaign_id:
            raise ApprovalError("approval campaign ID is stale")
        if self.digest != self._created_digest or self.approval.specification_digest != self.digest:
            raise ApprovalError("approval is stale because the specification changed")

    def finish(self, outcome: CampaignState, actor: str, reason: str) -> StateEvent:
        if self.state is not CampaignState.RUNNING:
            raise InvalidTransition("only RUNNING can finish")
        if outcome not in {CampaignState.FAILED, CampaignState.EXHAUSTED, CampaignState.SUCCEEDED}:
            raise InvalidTransition("invalid running outcome")
        return self._transition(outcome, actor, reason)

    def block(self, actor: str, reason: str) -> StateEvent:
        return self._transition(CampaignState.BLOCKED, actor, reason)

    def cancel(self, actor: str, reason: str) -> StateEvent:
        return self._transition(CampaignState.CANCELLED, actor, reason)
