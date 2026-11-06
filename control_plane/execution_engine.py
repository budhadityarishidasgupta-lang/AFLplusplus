"""Bounded in-process execution for explicitly authorized synthetic adapters.

This engine deliberately rejects all discovered legacy specialists. Python
threads cannot be safely force-terminated on Windows, so timeouts cancel only
cooperative adapters and never claim a running adapter has stopped.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from control_plane.agent_contracts import CapabilityRegistry, CapabilityStatus


class ExecutionError(ValueError):
    """Base error for rejected or failed execution requests."""


class ExecutionDenied(ExecutionError):
    """Execution was not explicitly and correctly authorized."""


class ExecutionBusy(ExecutionError):
    """The bounded execution slot is already occupied."""


class ExecutionBudgetExceeded(ExecutionError):
    """A request or result exceeds configured resource bounds."""


class ExecutionState(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    CAPABILITY = "capability"
    UNAVAILABLE = "unavailable"
    BUDGET = "budget"
    ADAPTER = "adapter"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BUSY = "busy"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ExecutionLimits:
    max_concurrent_tasks: int = 1
    max_duration_seconds: float = 30.0
    max_input_bytes: int = 65_536
    max_output_bytes: int = 65_536
    max_depth: int = 2

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrent_tasks, bool)
            or not isinstance(self.max_concurrent_tasks, int)
            or self.max_concurrent_tasks != 1
        ):
            raise ExecutionError("only one concurrent task is supported")
        if (
            isinstance(self.max_duration_seconds, bool)
            or not isinstance(self.max_duration_seconds, (int, float))
            or not 0 < self.max_duration_seconds <= 300
        ):
            raise ExecutionError("max_duration_seconds must be in (0, 300]")
        for name in ("max_input_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 1_048_576
            ):
                raise ExecutionError(f"{name} must be an integer in [1, 1048576]")
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or not 0 <= self.max_depth <= 2
        ):
            raise ExecutionError("max_depth must be an integer in [0, 2]")


@dataclass(frozen=True)
class ExecutionRequest:
    task_id: str
    mission_id: str
    agent_id: str
    capability: str
    input_data: Mapping[str, Any]
    permissions: tuple[str, ...] = ()
    depth: int = 0
    timeout_seconds: float | None = None
    parent_task_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("task_id", "mission_id", "agent_id", "capability"):
            if not _stable_id(getattr(self, name)):
                raise ExecutionError(f"{name} must be a stable identifier")
        if self.parent_task_id is not None and not _stable_id(self.parent_task_id):
            raise ExecutionError("parent_task_id must be a stable identifier")
        if not isinstance(self.input_data, Mapping):
            raise ExecutionError("input_data must be a mapping")
        if not isinstance(self.permissions, tuple) or any(
            not isinstance(item, str) for item in self.permissions
        ):
            raise ExecutionError("permissions must be a tuple of identifiers")
        if (
            len(self.permissions) != len(set(self.permissions))
            or any(not _stable_id(item) for item in self.permissions)
        ):
            raise ExecutionError("permissions must be unique stable identifiers")
        if (
            isinstance(self.depth, bool)
            or not isinstance(self.depth, int)
            or self.depth < 0
        ):
            raise ExecutionError("depth must be a non-negative integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ExecutionError("timeout_seconds must be in (0, 300]")
        _bounded_json(self.input_data, "input_data")
        try:
            snapshot = json.loads(
                json.dumps(
                    self.input_data, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                )
            )
        except (TypeError, ValueError, RecursionError):
            raise ExecutionError("input_data must contain bounded JSON-compatible data") from None
        if not isinstance(snapshot, dict):
            raise ExecutionError("input_data must be a JSON object")
        object.__setattr__(self, "input_data", snapshot)


@dataclass(frozen=True)
class ExecutionAuthorization:
    authorization_id: str
    task_id: str
    mission_id: str
    agent_id: str
    capability: str
    permissions: tuple[str, ...]
    input_digest: str
    approved_by: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.permissions, tuple) or any(
            not isinstance(item, str) for item in self.permissions
        ):
            raise ExecutionError(
                "authorization permissions must be a tuple of identifiers"
            )
        for name in ("authorization_id", "task_id", "mission_id", "agent_id", "capability"):
            if not _stable_id(getattr(self, name)):
                raise ExecutionError(f"{name} must be a stable identifier")
        if (
            len(self.permissions) != len(set(self.permissions))
            or any(not _stable_id(item) for item in self.permissions)
        ):
            raise ExecutionError("authorization permissions must be unique stable identifiers")
        if not isinstance(self.input_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.input_digest
        ):
            raise ExecutionError("input_digest must be lowercase SHA-256")
        _safe_text(self.approved_by, "approved_by")
        if (
            not isinstance(self.issued_at, datetime)
            or self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
        ):
            raise ExecutionError("issued_at must be timezone-aware")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.issued_at
        ):
            raise ExecutionError(
                "expires_at must be later than issued_at and timezone-aware"
            )


@dataclass(frozen=True)
class ExecutionAuditEvent:
    event_id: str
    task_id: str
    mission_id: str
    agent_id: str
    state: ExecutionState
    timestamp: datetime
    category: ErrorCategory | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "agent_id": self.agent_id,
            "state": self.state.value,
            "timestamp": self.timestamp.isoformat(),
            "category": self.category.value if self.category else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ExecutionResult:
    task_id: str
    mission_id: str
    agent_id: str
    state: ExecutionState
    output: Any = None
    error_category: ErrorCategory | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "agent_id": self.agent_id,
            "state": self.state.value,
            "output": self.output,
            "error_category": self.error_category.value if self.error_category else None,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": self.elapsed_seconds,
        }


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Narrow in-process adapter contract; no shell or ambient tool methods."""

    @property
    def agent_id(self) -> str: ...

    @property
    def capabilities(self) -> tuple[str, ...]: ...

    @property
    def permissions(self) -> tuple[str, ...]: ...

    @property
    def synthetic(self) -> bool: ...

    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any: ...


class ExecutionAuthorizationIssuer:
    """In-memory explicit authorization issuer intended for trusted host approval UI."""

    def __init__(self) -> None:
        self._issued: dict[str, ExecutionAuthorization] = {}
        self._used: set[str] = set()
        self._revoked: set[str] = set()
        self._generation = 0
        self._lock = threading.Lock()

    def authorize(
        self,
        request: ExecutionRequest,
        *,
        approved_by: str,
        permissions: tuple[str, ...],
        now: datetime | None = None,
        validity: timedelta = timedelta(minutes=10),
    ) -> ExecutionAuthorization:
        if not isinstance(request, ExecutionRequest):
            raise ExecutionDenied("a valid task request is required")
        if not isinstance(permissions, tuple) or any(
            not isinstance(item, str) for item in permissions
        ):
            raise ExecutionDenied(
                "authorization permissions must be a tuple of identifiers"
            )
        if isinstance(validity, bool) or not isinstance(validity, timedelta):
            raise ExecutionDenied("authorization validity must be a bounded duration")
        _safe_text(approved_by, "approved_by")
        now = now or datetime.now(timezone.utc)
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or validity <= timedelta(0)
            or validity > timedelta(hours=1)
        ):
            raise ExecutionDenied(
                "authorization validity must be positive, at most one hour, and timezone-aware"
            )
        if (
            len(permissions) != len(set(permissions))
            or any(not _stable_id(item) for item in permissions)
        ):
            raise ExecutionDenied(
                "authorization permissions must be unique stable identifiers"
            )
        if set(permissions) != set(request.permissions):
            raise ExecutionDenied(
                "execution authorization must exactly match the declared task permission scope"
            )
        digest = _digest(request.input_data)
        with self._lock:
            self._generation += 1
            generation = self._generation
        material = ":".join(
            (
                request.task_id, request.mission_id, request.agent_id,
                request.capability, digest, now.isoformat(), approved_by,
                str(generation),
            )
        )
        auth_id = "execauth-" + hashlib.sha256(material.encode()).hexdigest()[:32]
        authorization = ExecutionAuthorization(
            authorization_id=auth_id,
            task_id=request.task_id,
            mission_id=request.mission_id,
            agent_id=request.agent_id,
            capability=request.capability,
            permissions=permissions,
            input_digest=digest,
            approved_by=approved_by,
            issued_at=now,
            expires_at=now + validity,
        )
        with self._lock:
            if auth_id in self._issued:
                raise ExecutionDenied("authorization identifier replay rejected")
            self._issued[auth_id] = authorization
        return authorization

    def revoke(self, authorization: ExecutionAuthorization) -> bool:
        if not isinstance(authorization, ExecutionAuthorization):
            return False
        with self._lock:
            if self._issued.get(authorization.authorization_id) != authorization:
                return False
            if authorization.authorization_id in self._used:
                return False
            self._revoked.add(authorization.authorization_id)
            return True

    def _consume(self, authorization: ExecutionAuthorization, request: ExecutionRequest, now: datetime) -> None:
        if not isinstance(authorization, ExecutionAuthorization):
            raise ExecutionDenied("explicit execution authorization is required")
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ExecutionDenied("authorization validation time must be timezone-aware")
        with self._lock:
            registered = self._issued.get(authorization.authorization_id)
            if (
                registered != authorization
                or authorization.authorization_id in self._used
                or authorization.authorization_id in self._revoked
            ):
                raise ExecutionDenied("authorization is unknown, altered, revoked, or already consumed")
            if not authorization.issued_at <= now < authorization.expires_at:
                raise ExecutionDenied("authorization is not currently valid")
            if (
                authorization.task_id != request.task_id
                or authorization.mission_id != request.mission_id
                or authorization.agent_id != request.agent_id
                or authorization.capability != request.capability
                or authorization.input_digest != _digest(request.input_data)
                or set(request.permissions) != set(authorization.permissions)
            ):
                raise ExecutionDenied("authorization does not match task, scope, adapter, or input")
            self._used.add(authorization.authorization_id)


class ExecutionEngine:
    """Execute only registered synthetic adapters, one bounded task at a time."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        authorization_issuer: ExecutionAuthorizationIssuer,
        *,
        limits: ExecutionLimits | None = None,
        tool_gateway: Any | None = None,
    ) -> None:
        if (
            not isinstance(registry, CapabilityRegistry)
            or not isinstance(authorization_issuer, ExecutionAuthorizationIssuer)
        ):
            raise ExecutionError("registry and authorization issuer are required")
        if limits is not None and not isinstance(limits, ExecutionLimits):
            raise ExecutionError("limits must be an ExecutionLimits instance")
        if tool_gateway is not None and not callable(getattr(tool_gateway, "begin_task", None)):
            raise ExecutionError("tool_gateway must implement the bounded gateway contract")
        self.registry = registry
        self.tool_gateway = tool_gateway
        self.authorization_issuer = authorization_issuer
        self.limits = limits or ExecutionLimits()
        self._adapters: dict[str, ExecutionAdapter] = {}
        self._lock = threading.RLock()
        self._active: tuple[ExecutionRequest, threading.Event] | None = None
        self._workers: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()
        self._audits: list[ExecutionAuditEvent] = []
        self._results: dict[str, ExecutionResult] = {}

    @property
    def audit_events(self) -> tuple[ExecutionAuditEvent, ...]:
        with self._lock:
            return tuple(self._audits)

    def register_adapter(self, adapter: ExecutionAdapter) -> None:
        if (
            not isinstance(adapter, ExecutionAdapter)
            or adapter.synthetic is not True
        ):
            raise ExecutionError(
                "only structurally valid synthetic adapters can be registered"
            )
        if (
            not isinstance(adapter.capabilities, tuple)
            or not isinstance(adapter.permissions, tuple)
        ):
            raise ExecutionError("adapter capabilities and permissions must be tuples")
        if not adapter.capabilities or any(
            not isinstance(item, str)
            for item in adapter.capabilities + adapter.permissions
        ):
            raise ExecutionError(
                "adapter metadata must contain stable identifiers"
            )
        if (
            not _stable_id(adapter.agent_id)
            or not adapter.capabilities
            or any(not _stable_id(item) for item in adapter.capabilities)
        ):
            raise ExecutionError(
                "adapter identity and capabilities must be stable identifiers"
            )
        try:
            registration = self.registry.discover(adapter.agent_id)
        except Exception:
            raise ExecutionError("agent metadata is unavailable") from None
        if registration.status is not CapabilityStatus.VERIFIED:
            raise ExecutionError(
                "agent metadata is not verified for execution"
            )
        if (
            len(adapter.capabilities) != len(set(adapter.capabilities))
            or len(adapter.permissions) != len(set(adapter.permissions))
            or any(not _stable_id(item) for item in adapter.permissions)
        ):
            raise ExecutionError(
                "adapter metadata contains invalid or duplicate identifiers"
            )
        if not set(adapter.capabilities).issubset(
            registration.contract.capabilities
        ):
            raise ExecutionError("adapter capabilities exceed registered metadata")
        if not set(adapter.permissions).issubset(
            registration.contract.permissions
        ):
            raise ExecutionError("adapter permissions exceed registered metadata")
        if set(adapter.permissions) != set(registration.contract.permissions):
            raise ExecutionError(
                "adapter must declare the complete registered permission scope"
            )
        with self._lock:
            if adapter.agent_id in self._adapters:
                raise ExecutionError("adapter already registered")
            self._adapters[adapter.agent_id] = adapter

    def submit(
        self,
        request: ExecutionRequest,
        authorization: ExecutionAuthorization | None,
    ) -> ExecutionResult:
        if not isinstance(request, ExecutionRequest):
            raise ExecutionError("request must be a validated ExecutionRequest")
        if (
            authorization is not None
            and not isinstance(authorization, ExecutionAuthorization)
        ):
            authorization = None
        started = datetime.now(timezone.utc)
        monotonic_start = time.monotonic()
        task_lock = threading.Event()
        with self._lock:
            if request.task_id in self._results or (
                self._active is not None
                and self._active[0].task_id == request.task_id
            ):
                raise ExecutionError("task_id has already been submitted")
            if request.task_id in self._cancelled:
                raise ExecutionError("task cancellation is already pending")
            if self._active is not None:
                self._audit(
                    request, ExecutionState.FAILED, ErrorCategory.BUSY,
                    "single execution slot occupied",
                )
                raise ExecutionBusy("single execution slot is occupied")
            self._active = (request, task_lock)
            self._audit(
                request, ExecutionState.PENDING, None, "task accepted"
            )
        state = ExecutionState.FAILED
        category: ErrorCategory | None = None
        error_message: str | None = None
        output: Any = None
        try:
            adapter = self._validate_invocation(request)
            self.authorization_issuer._consume(authorization, request, started)
            self._audit(
                request, ExecutionState.AUTHORIZED, None,
                "execution authorization accepted",
            )
            state = ExecutionState.AUTHORIZED
            if self._cancelled_task(request.task_id):
                with self._lock:
                    self._cancelled.discard(request.task_id)
                state = ExecutionState.CANCELLED
                category = ErrorCategory.CANCELLED
                error_message = "task cancelled before adapter invocation"
                return self._finish(
                    request, state, started, monotonic_start,
                    category, error_message,
                )
            self._audit(
                request, ExecutionState.RUNNING, None,
                "synthetic adapter invoked",
            )
            if self.tool_gateway is not None:
                try:
                    self.tool_gateway.begin_task(request, authorization, task_lock)
                except Exception:
                    state = ExecutionState.FAILED
                    category = ErrorCategory.AUTHORIZATION
                    error_message = "tool gateway authorization was rejected"
                    return self._finish(request, state, started, monotonic_start, category, error_message)
            deadline = min(
                request.timeout_seconds or self.limits.max_duration_seconds,
                self.limits.max_duration_seconds,
            )
            done = threading.Event()
            holder: dict[str, Any] = {}

            def invoke() -> None:
                try:
                    bind = getattr(adapter, "bind_tool_gateway", None)
                    if self.tool_gateway is not None and callable(bind):
                        bind(self.tool_gateway, request, authorization)
                    holder["output"] = adapter.invoke(
                        request.capability, request.input_data, task_lock
                    )
                except Exception as error:  # sanitize adapter exceptions
                    holder["error_type"] = type(error).__name__
                finally:
                    if self.tool_gateway is not None:
                        try:
                            self.tool_gateway.end_task(request.task_id)
                        except Exception:
                            pass
                    done.set()

            def release_worker_slot() -> None:
                worker.join()
                with self._lock:
                    self._workers.pop(request.task_id, None)
                    self._cancelled.discard(request.task_id)
                    if self._active and self._active[0].task_id == request.task_id:
                        self._active = None

            worker = threading.Thread(
                target=invoke,
                name=f"synthetic-task-{request.task_id}",
                daemon=True,
            )
            worker.start()
            with self._lock:
                self._workers[request.task_id] = worker
            threading.Thread(
                target=release_worker_slot,
                name=f"synthetic-reaper-{request.task_id}",
                daemon=True,
            ).start()
            if not done.wait(deadline):
                task_lock.set()
                with self._lock:
                    self._cancelled.add(request.task_id)
                state = ExecutionState.TIMED_OUT
                category = ErrorCategory.TIMEOUT
                error_message = (
                    "adapter exceeded its deadline; cooperative cancellation "
                    "requested; worker may still be running"
                )
                return self._finish(
                    request, state, started, monotonic_start,
                    category, error_message, release_slot=False,
                )
            if task_lock.is_set() or self._cancelled_task(request.task_id):
                with self._lock:
                    self._cancelled.discard(request.task_id)
                state = ExecutionState.CANCELLED
                category = ErrorCategory.CANCELLED
                error_message = "task cancellation requested"
                return self._finish(
                    request, state, started, monotonic_start,
                    category, error_message,
                )
            if "error_type" in holder:
                state = ExecutionState.FAILED
                category = ErrorCategory.ADAPTER
                error_message = "adapter failed (" + holder["error_type"][:64] + ")"
                return self._finish(
                    request, state, started, monotonic_start,
                    category, error_message,
                )
            output = holder.get("output")
            try:
                output_bytes = _bounded_json(output, "adapter output")
            except ExecutionError:
                raise ExecutionBudgetExceeded(
                    "adapter output was not bounded JSON-compatible data"
                ) from None
            if output_bytes > self.limits.max_output_bytes:
                raise ExecutionBudgetExceeded(
                    "adapter output exceeded configured size"
                )
            state = ExecutionState.COMPLETED
            return self._finish(
                request, state, started, monotonic_start, output=output
            )
        except ExecutionDenied:
            state = ExecutionState.FAILED
            category = ErrorCategory.AUTHORIZATION
            error_message = "execution authorization was rejected"
            return self._finish(
                request, state, started, monotonic_start,
                category, error_message,
            )
        except ExecutionBusy:
            raise
        except ExecutionBudgetExceeded as error:
            state = ExecutionState.FAILED
            category = ErrorCategory.BUDGET
            error_message = _safe_error_message(error, "resource budget exceeded")
            return self._finish(
                request, state, started, monotonic_start,
                category, error_message,
            )
        except ExecutionError as error:
            state = ExecutionState.FAILED
            category = ErrorCategory.CAPABILITY
            error_message = _safe_error_message(error, "execution validation failed")
            return self._finish(
                request, state, started, monotonic_start,
                category, error_message,
            )
        except Exception as error:
            state = ExecutionState.FAILED
            category = ErrorCategory.INTERNAL
            error_message = "execution failed (" + type(error).__name__[:64] + ")"
            return self._finish(
                request, state, started, monotonic_start,
                category, error_message,
            )

    def cancel(self, task_id: str) -> bool:
        if not _stable_id(task_id):
            raise ExecutionError("task_id must be a stable identifier")
        with self._lock:
            if self._active is None or self._active[0].task_id != task_id:
                return False
            if self._active[1].is_set():
                return True
            self._cancelled.add(task_id)
            self._active[1].set()
            return True

    def result(self, task_id: str) -> ExecutionResult | None:
        with self._lock:
            return self._results.get(task_id)

    def _validate_invocation(self, request: ExecutionRequest) -> ExecutionAdapter:
        if request.depth > self.limits.max_depth or request.depth > 2:
            raise ExecutionBudgetExceeded("delegation depth budget exceeded")
        if _bounded_json(request.input_data, "task input") > self.limits.max_input_bytes:
            raise ExecutionBudgetExceeded("task input exceeded configured size")
        try:
            registration = self.registry.discover(request.agent_id)
        except Exception:
            raise ExecutionError("agent metadata is unavailable") from None
        if registration.status is not CapabilityStatus.VERIFIED:
            raise ExecutionError("agent capability metadata is unavailable")
        if request.capability not in registration.contract.capabilities:
            raise ExecutionError("requested capability is not registered for the agent")
        if not set(request.permissions).issubset(registration.contract.permissions):
            raise ExecutionError("task permissions exceed registered agent scope")
        with self._lock:
            adapter = self._adapters.get(request.agent_id)
        if adapter is None:
            raise ExecutionError("agent execution adapter is unavailable")
        if request.capability not in adapter.capabilities:
            raise ExecutionError("adapter does not implement the requested capability")
        if not set(request.permissions).issubset(adapter.permissions):
            raise ExecutionError("task permissions exceed adapter scope")
        return adapter

    def _finish(
        self,
        request: ExecutionRequest,
        state: ExecutionState,
        started: datetime,
        monotonic_start: float,
        category: ErrorCategory | None = None,
        error_message: str | None = None,
        output: Any = None,
        release_slot: bool = True,
    ) -> ExecutionResult:
        result = ExecutionResult(
            task_id=request.task_id,
            mission_id=request.mission_id,
            agent_id=request.agent_id,
            state=state,
            output=output if state is ExecutionState.COMPLETED else None,
            error_category=category,
            error_message=error_message,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            elapsed_seconds=max(0.0, time.monotonic() - monotonic_start),
        )
        with self._lock:
            self._results[request.task_id] = result
            self._audit(request, state, category, error_message or "task completed")
            if release_slot and self._active and self._active[0].task_id == request.task_id:
                self._active = None
            if release_slot:
                self._cancelled.discard(request.task_id)
        return result

    def _cancelled_task(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def _audit(
        self,
        request: ExecutionRequest,
        state: ExecutionState,
        category: ErrorCategory | None,
        detail: str,
    ) -> None:
        # Only stable metadata/category is retained; never task inputs, output, or exception text.
        event_id = hashlib.sha256(
            f"{request.task_id}:{len(self._audits)}:{state.value}".encode()
        ).hexdigest()[:24]
        self._audits.append(
            ExecutionAuditEvent(
                event_id, request.task_id, request.mission_id,
                request.agent_id, state, datetime.now(timezone.utc),
                category, detail[:160],
            )
        )


def _stable_id(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
    )


def _safe_error_message(error: Exception, fallback: str) -> str:
    message = str(error)
    if any(ord(character) < 32 for character in message):
        return fallback
    if re.search(r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]", message):
        return fallback
    return message[:160] or fallback


def _safe_text(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ExecutionError(f"{label} must be non-empty safe text")


def _bounded_json(value: Any, label: str) -> int:
    try:
        if _depth(value) > 16:
            raise ExecutionError(f"{label} exceeds maximum nesting depth")
        if not _all_string_keys(value):
            raise ExecutionError(f"{label} object keys must be strings")
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        if len(encoded) > 1_048_576:
            raise ExecutionError(f"{label} exceeds maximum serialized size")
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        raise ExecutionError(f"{label} must contain bounded JSON-compatible data") from None
    if decoded != value:
        raise ExecutionError(
            f"{label} must contain canonical JSON-compatible data"
        )
    return len(encoded.encode("utf-8"))


def _all_string_keys(value: Any) -> bool:
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _all_string_keys(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_all_string_keys(item) for item in value)
    return True


def _depth(value: Any) -> int:
    if isinstance(value, Mapping):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, (tuple, list)):
        return 1 + max((_depth(item) for item in value), default=0)
    return 0


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
