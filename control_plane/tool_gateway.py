"""Task-bound gateway for explicitly authorized synthetic tools.

Tools are registered as metadata and implementations, but registration never
confers permission. The gateway intentionally ships only a mocked research
provider; it has no network, shell, browser, or filesystem transport.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from control_plane.execution_engine import ExecutionAuthorization, ExecutionRequest


class ToolGatewayError(ValueError):
    """Safe, bounded tool-call rejection."""


@dataclass(frozen=True)
class ToolBudget:
    max_calls: int = 3
    max_cost_microunits: int = 0
    max_duration_seconds: float = 5.0
    max_input_bytes: int = 8192
    max_output_bytes: int = 32768

    def __post_init__(self) -> None:
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or not 0 <= self.max_calls <= 100:
            raise ToolGatewayError("max_calls must be an integer in [0, 100]")
        if isinstance(self.max_cost_microunits, bool) or not isinstance(self.max_cost_microunits, int) or self.max_cost_microunits < 0:
            raise ToolGatewayError("max_cost_microunits must be a non-negative integer")
        if isinstance(self.max_duration_seconds, bool) or not isinstance(self.max_duration_seconds, (int, float)) or not 0 < self.max_duration_seconds <= 60:
            raise ToolGatewayError("max_duration_seconds must be in (0, 60]")
        for name in ("max_input_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_048_576:
                raise ToolGatewayError(f"{name} must be an integer in [1, 1048576]")


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    capability: str
    permission: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], threading.Event], Any]
    cost_microunits: int = 0
    timeout_seconds: float = 2.0
    description: str = ""

    def __post_init__(self) -> None:
        for name in ("tool_id", "capability", "permission"):
            value = getattr(self, name)
            if not _stable_id(value):
                raise ToolGatewayError(f"{name} must be a stable identifier")
        if not callable(self.handler):
            raise ToolGatewayError("tool handler must be callable")
        if isinstance(self.cost_microunits, bool) or not isinstance(self.cost_microunits, int) or self.cost_microunits < 0:
            raise ToolGatewayError("tool cost must be a non-negative integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 60:
            raise ToolGatewayError("tool timeout must be in (0, 60]")
        _validate_schema(self.input_schema)
        _safe_label(self.description, "description", allow_empty=True)


@dataclass(frozen=True)
class ToolResult:
    status: str
    tool_id: str
    task_id: str
    agent_id: str
    data: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "tool_id": self.tool_id, "task_id": self.task_id,
                "agent_id": self.agent_id, "data": self.data, "error": self.error,
                "elapsed_seconds": self.elapsed_seconds}


@dataclass(frozen=True)
class ToolAuditEvent:
    event_id: str
    task_id: str
    mission_id: str
    agent_id: str
    tool_id: str
    status: str
    timestamp: str
    input_digest: str
    cost_microunits: int


@dataclass
class _TaskState:
    request: ExecutionRequest
    authorization: ExecutionAuthorization
    budget: ToolBudget
    cancel_event: threading.Event
    deadline: float
    calls: int = 0
    cost: int = 0


class ToolGateway:
    """Mediates tools for the currently active, explicitly authorized task."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._active: dict[str, _TaskState] = {}
        self._task_history: dict[str, tuple[ExecutionRequest, ExecutionAuthorization]] = {}
        self._cancelled: set[str] = set()
        self._audits: list[ToolAuditEvent] = []
        self._lock = threading.RLock()

    @property
    def audit_events(self) -> tuple[ToolAuditEvent, ...]:
        with self._lock:
            return tuple(self._audits)

    def register_tool(self, tool: ToolDefinition) -> None:
        if not isinstance(tool, ToolDefinition):
            raise ToolGatewayError("a validated tool definition is required")
        with self._lock:
            if tool.tool_id in self._tools:
                raise ToolGatewayError("tool is already registered")
            self._tools[tool.tool_id] = tool

    def discover_tools(self, request: ExecutionRequest, authorization: ExecutionAuthorization | None) -> tuple[dict[str, str], ...]:
        state = self._validated_state(request, authorization)
        with self._lock:
            if self._active.get(request.task_id) is not state:
                return ()
            return tuple({"tool_id": tool.tool_id, "capability": tool.capability,
                          "permission": tool.permission, "description": tool.description}
                         for tool in sorted(self._tools.values(), key=lambda item: item.tool_id)
                         if tool.permission in request.permissions and tool.permission in authorization.permissions)

    def begin_task(self, request: ExecutionRequest, authorization: ExecutionAuthorization | None,
                   cancel_event: threading.Event, *, budget: ToolBudget | None = None) -> None:
        if not isinstance(request, ExecutionRequest) or not isinstance(authorization, ExecutionAuthorization):
            raise ToolGatewayError("authorized task context is required")
        if not isinstance(cancel_event, threading.Event):
            raise ToolGatewayError("task cancellation event is required")
        if set(authorization.permissions) != set(request.permissions) or (
            authorization.task_id != request.task_id or authorization.agent_id != request.agent_id
            or authorization.mission_id != request.mission_id
            or authorization.capability != request.capability
            or authorization.input_digest != _request_digest(request.input_data)
        ):
            raise ToolGatewayError("task authorization binding was rejected")
        now = datetime.now(timezone.utc)
        if not authorization.issued_at <= now < authorization.expires_at:
            raise ToolGatewayError("task authorization is expired or not yet valid")
        budget = budget or ToolBudget()
        if not isinstance(budget, ToolBudget):
            raise ToolGatewayError("a valid tool budget is required")
        state = _TaskState(request, authorization, budget, cancel_event,
                           time.monotonic() + budget.max_duration_seconds)
        with self._lock:
            if request.task_id in self._active or request.task_id in self._cancelled or request.task_id in self._task_history:
                raise ToolGatewayError("task tool context is unavailable")
            self._active[request.task_id] = state
            self._task_history[request.task_id] = (request, authorization)

    def end_task(self, task_id: str) -> None:
        with self._lock:
            self._active.pop(task_id, None)
            self._cancelled.add(task_id)

    def cancel_task(self, task_id: str) -> None:
        with self._lock:
            self._cancelled.add(task_id)
            state = self._active.get(task_id)
            if state is not None:
                state.cancel_event.set()

    def request_tool(self, request: ExecutionRequest, authorization: ExecutionAuthorization,
                     tool_id: str, arguments: Mapping[str, Any]) -> ToolResult:
        started = time.monotonic()
        state: _TaskState | None = None
        tool: ToolDefinition | None = None
        digest = _digest_safely(arguments)
        try:
            state = self._validated_state(request, authorization)
            with self._lock:
                tool = self._tools.get(tool_id)
            if tool is None:
                raise ToolGatewayError("tool is unavailable")
            if tool.permission not in request.permissions or tool.permission not in authorization.permissions:
                raise ToolGatewayError("tool permission was not authorized")
            _validate_input(tool.input_schema, arguments)
            encoded = _canonical_json(arguments)
            if len(encoded) > state.budget.max_input_bytes:
                raise ToolGatewayError("tool input budget exceeded")
            with self._lock:
                if self._active.get(request.task_id) is not state or request.task_id in self._cancelled:
                    raise ToolGatewayError("task is cancelled or inactive")
                if state.cancel_event.is_set():
                    raise ToolGatewayError("task is cancelled or inactive")
                if time.monotonic() >= state.deadline:
                    raise ToolGatewayError("tool time budget exceeded")
                if state.calls >= state.budget.max_calls:
                    raise ToolGatewayError("tool call budget exceeded")
                if state.cost + tool.cost_microunits > state.budget.max_cost_microunits:
                    raise ToolGatewayError("tool cost budget exceeded")
                state.calls += 1
                state.cost += tool.cost_microunits
            # Exactly one invocation. Handlers must be synthetic/cooperative; the
            # gateway does not retry or claim that a timed-out thread was killed.
            data = tool.handler(arguments, state.cancel_event)
            elapsed = time.monotonic() - started
            if state.cancel_event.is_set() or time.monotonic() >= state.deadline:
                raise ToolGatewayError("task cancelled or tool time budget exceeded")
            if elapsed > tool.timeout_seconds:
                raise ToolGatewayError("tool time limit exceeded")
            output = _canonical_json(data)
            if len(output) > state.budget.max_output_bytes:
                raise ToolGatewayError("tool output budget exceeded")
            result = ToolResult("ok", tool.tool_id, request.task_id, request.agent_id, data=data,
                                elapsed_seconds=elapsed)
        except ToolGatewayError as error:
            result = ToolResult("error", tool_id if _stable_id(tool_id) else "unknown",
                                request.task_id if isinstance(request, ExecutionRequest) else "unknown",
                                request.agent_id if isinstance(request, ExecutionRequest) else "unknown",
                                error=_safe_message(error), elapsed_seconds=time.monotonic() - started)
        except Exception as error:
            result = ToolResult("error", tool_id if _stable_id(tool_id) else "unknown",
                                request.task_id if isinstance(request, ExecutionRequest) else "unknown",
                                request.agent_id if isinstance(request, ExecutionRequest) else "unknown",
                                error="tool failed (" + type(error).__name__[:48] + ")",
                                elapsed_seconds=time.monotonic() - started)
        self._audit(request, tool_id, result.status, digest,
                    tool.cost_microunits if result.status == "ok" and tool else 0)
        return result

    def _validated_state(self, request: ExecutionRequest, authorization: ExecutionAuthorization | None) -> _TaskState:
        if not isinstance(request, ExecutionRequest) or not isinstance(authorization, ExecutionAuthorization):
            raise ToolGatewayError("task authorization is required")
        with self._lock:
            state = self._active.get(request.task_id)
            if state is None or request.task_id in self._cancelled:
                historical = self._task_history.get(request.task_id)
                if historical == (request, authorization):
                    raise ToolGatewayError("task is cancelled or inactive")
                raise ToolGatewayError("task tool context is unavailable")
        if state.cancel_event.is_set():
            raise ToolGatewayError("task is cancelled or inactive")
        if state.request != request or state.authorization != authorization:
            raise ToolGatewayError("task or agent authorization binding was rejected")
        if (authorization.task_id != request.task_id or authorization.mission_id != request.mission_id
                or authorization.agent_id != request.agent_id or authorization.capability != request.capability
                or authorization.input_digest != _request_digest(request.input_data)
                or set(authorization.permissions) != set(request.permissions)):
            raise ToolGatewayError("task authorization binding was rejected")
        if not authorization.issued_at <= datetime.now(timezone.utc) < authorization.expires_at:
            raise ToolGatewayError("task authorization is expired or not yet valid")
        return state

    def _audit(self, request: ExecutionRequest, tool_id: str, status: str, digest: str, cost: int) -> None:
        if not isinstance(request, ExecutionRequest):
            return
        safe_id = tool_id if _stable_id(tool_id) else "unknown"
        with self._lock:
            event_id = hashlib.sha256(f"{request.task_id}:{len(self._audits)}:{safe_id}:{status}".encode()).hexdigest()[:24]
            self._audits.append(ToolAuditEvent(event_id, request.task_id, request.mission_id,
                request.agent_id, safe_id, status, datetime.now(timezone.utc).isoformat(), digest, cost))


@dataclass(frozen=True)
class MockSearchProvider:
    """Deterministic fixture provider. It never performs a network request."""
    provider_id: str
    results: tuple[Mapping[str, str], ...]

    def search(self, query: str, cancel_event: threading.Event) -> tuple[dict[str, str], ...]:
        if cancel_event.is_set():
            return ()
        return tuple(dict(item) for item in self.results[:5])


class MockResearchAdapter:
    """Registered mocked search tool producing explicitly untrusted evidence."""
    def __init__(self, provider: MockSearchProvider) -> None:
        if not isinstance(provider, MockSearchProvider) or not _stable_id(provider.provider_id):
            raise ToolGatewayError("approved mock search provider is required")
        self.provider = provider

    def definition(self, *, tool_id: str = "research.search", permission: str = "internet-research",
                   cost_microunits: int = 0, timeout_seconds: float = 2.0) -> ToolDefinition:
        provider = self.provider
        def handle(arguments: Mapping[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
            query = arguments["query"].strip()
            results = provider.search(query, cancel_event)
            evidence = validate_research_results(results, provider_id=provider.provider_id)
            return {"provider": provider.provider_id, "query": query,
                    "evidence": evidence, "uncertainty": "Mock search fixtures only; sources and factual accuracy are not independently verified."}
        return ToolDefinition(tool_id, "research.search", permission,
            {"type": "object", "required": ["query"], "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500}}, "additionalProperties": False},
            handle, cost_microunits, timeout_seconds,
            "Search approved mocked results; returned content is untrusted.")


def validate_research_results(results: Any, *, provider_id: str, retrieved_at: str | None = None) -> tuple[dict[str, Any], ...]:
    """Bound, deduplicate and provenance-wrap mock results as untrusted evidence."""
    if not _stable_id(provider_id) or not isinstance(results, (tuple, list)):
        return ()
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat()
    if not isinstance(timestamp, str) or not timestamp.strip():
        timestamp = datetime.now(timezone.utc).isoformat()
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results[:20]:
        if not isinstance(result, Mapping):
            continue
        title, url, snippet = result.get("title"), result.get("url"), result.get("snippet")
        if not all(isinstance(value, str) for value in (title, url, snippet)):
            continue
        if len(title) > 500 or len(url) > 2048 or len(snippet) > 2000:
            continue
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            continue
        if parsed.scheme.casefold() != "https" or not hostname or parsed.username is not None or not parsed.netloc:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in url) or not title.strip() or not snippet.strip():
            continue
        canonical = f"https://{parsed.netloc.casefold()}{parsed.path or '/'}?{parsed.query}"
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        # Instruction-like source text remains data and is never interpreted.
        evidence.append({"claim": snippet, "snippet": snippet, "source_url": url,
            "title": title, "provider": provider_id, "retrieved_at": timestamp,
            "confidence": 0.25,
            "uncertainty": "Search snippet is unverified; source content and factual accuracy have not been independently checked.",
            "content_trusted": False, "instructions_accepted": False})
    return tuple(evidence)


def _validate_schema(schema: Mapping[str, Any]) -> None:
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise ToolGatewayError("tool input schema must be an object schema")
    properties, required = schema.get("properties"), schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, (tuple, list)):
        raise ToolGatewayError("tool input schema is invalid")
    if schema.get("additionalProperties", False) is not False or not set(required).issubset(properties):
        raise ToolGatewayError("tool input schema must be closed and internally consistent")
    for name, spec in properties.items():
        if not _stable_id(name) or not isinstance(spec, Mapping) or spec.get("type") not in {"string", "integer", "number", "boolean", "object", "array"}:
            raise ToolGatewayError("tool input schema property is invalid")


def _validate_input(schema: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ToolGatewayError("tool input must be an object")
    properties, required = schema["properties"], schema.get("required", [])
    if set(value) - set(properties) or set(required) - set(value):
        raise ToolGatewayError("tool input does not match registered schema")
    for name, item in value.items():
        spec = properties[name]
        kind = spec["type"]
        if kind == "string":
            if not isinstance(item, str) or len(item) < spec.get("minLength", 0) or len(item) > spec.get("maxLength", 1_000_000):
                raise ToolGatewayError("tool input does not match registered schema")
        elif kind == "integer":
            valid = isinstance(item, int) and not isinstance(item, bool)
        elif kind == "number":
            valid = isinstance(item, (int, float)) and not isinstance(item, bool)
        elif kind == "boolean":
            valid = isinstance(item, bool)
        elif kind == "object":
            valid = isinstance(item, Mapping)
        else:
            valid = isinstance(item, (tuple, list))
        if kind != "string" and not valid:
            raise ToolGatewayError("tool input does not match registered schema")
    _canonical_json(value)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ToolGatewayError("tool data must be bounded JSON-compatible data") from None


def _request_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _digest_safely(value: Any) -> str:
    try:
        return hashlib.sha256(_canonical_json(value)).hexdigest()
    except ToolGatewayError:
        return "invalid"


def _stable_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value))


def _safe_label(value: str, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or any(ord(char) < 32 for char in value):
        raise ToolGatewayError(f"{label} must be safe text")


def _safe_message(error: Exception) -> str:
    message = str(error)
    if any(ord(char) < 32 for char in message) or re.search(r"(?i)(api[_-]?key|secret|password|token|credential)\s*[:=]", message):
        return "tool request was rejected"
    return message[:160] or "tool request was rejected"
