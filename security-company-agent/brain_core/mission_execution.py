"""Brain projection and approval bridge for the synthetic execution contract.

The durable control-plane ledger remains the execution authority. Brain records
intent and a UI projection, but never trusts its own status as admission proof.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SYNTHETIC_AGENT_ID = "synthetic.brain-acceptance"
SYNTHETIC_CAPABILITY = "synthetic-mission-report"
SYNTHETIC_PERMISSION = "read-fixture"


class _TrustedAcceptanceHarness:
    """Unforgeable in-process authority used only by explicit test injection."""


_TRUSTED_ACCEPTANCE_HARNESS = _TrustedAcceptanceHarness()


class MissionExecutionError(ValueError):
    """Invalid or unsafe Brain-to-execution transition."""


class ExecutionApproval:
    """Distinct human decision, separate from mission-planning approval."""
    def __init__(self, mission_id: str, task_id: str, adapter_id: str,
                 scope: tuple[str, ...], approved_by: str, reason: str):
        self.mission_id = mission_id
        self.task_id = task_id
        self.adapter_id = adapter_id
        self.scope = scope
        self.approved_by = approved_by
        self.reason = reason


class SyntheticMissionAdapter:
    """Deterministic fixture adapter with no external or specialist access."""
    agent_id = SYNTHETIC_AGENT_ID
    capabilities = (SYNTHETIC_CAPABILITY,)
    permissions = (SYNTHETIC_PERMISSION,)
    synthetic = True

    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
        if capability != SYNTHETIC_CAPABILITY or cancel_event.is_set():
            raise RuntimeError("synthetic fixture task cancelled or out of scope")
        criteria = tuple(input_data.get("success_criteria", ()))
        objective = input_data.get("objective")
        if not isinstance(objective, str) or not criteria or any(not isinstance(item, str) for item in criteria):
            raise RuntimeError("invalid synthetic fixture input")
        return {
            "Capability support assessment": {
                "objective": objective,
                "limitations": "Deterministic local fixture; no external research or actions.",
                "criteria": list(criteria),
            },
            "criterion_evidence": {criterion: True for criterion in criteria},
            "The deterministic fixture addresses the stated synthetic objective.": True,
            "The synthetic result states its limitations and performs no external action.": True,
        }


class OwnerSimulationAdapter(SyntheticMissionAdapter):
    """Owner-facing deterministic fixture adapter; has no external interfaces."""
    agent_id = "synthetic.brain-owner-simulation"


class MissionExecutionService:
    """One-shot service using explicitly injected existing orchestration APIs."""
    def __init__(self, *, registry: Any, issuer: Any, engine: Any,
                 verifier: Any | None = None, now: Any = None, evidence_factory: Any = None,
                 notes_persist: Callable[[Any, str, str], None] | None = None,
                 persist_state: Callable[[Any], bool] | None = None,
                 _acceptance_context: Any = None, _owner_authority: Any = None):
        from . import owner_simulation
        trusted_harness = _acceptance_context is _TRUSTED_ACCEPTANCE_HARNESS
        owner_simulation_authorized = owner_simulation.validate_owner_authority(
            _owner_authority, "synthetic.brain-owner-simulation")
        if not trusted_harness and not owner_simulation_authorized:
            raise MissionExecutionError("Synthetic execution requires trusted acceptance-harness or owner-local authority.")
        self.__owner_simulation_authority = _owner_authority if owner_simulation_authorized else None
        self.__owner_simulation_context = owner_simulation_authorized
        from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus
        from control_plane.execution_engine import ExecutionAuthorizationIssuer, ExecutionEngine
        if not isinstance(registry, CapabilityRegistry):
            raise MissionExecutionError("A verified synthetic CapabilityRegistry is required.")
        if not isinstance(issuer, ExecutionAuthorizationIssuer) or not isinstance(engine, ExecutionEngine):
            raise MissionExecutionError("Trusted context requires the reviewed execution issuer and engine.")
        if engine.registry is not registry or engine.authorization_issuer is not issuer:
            raise MissionExecutionError("Registry, issuer, and execution engine must share the same authority.")
        if engine.durable_store is None or issuer._durable_store is not engine.durable_store:
            raise MissionExecutionError("A shared durable execution ledger is required.")
        expected_adapter = ("synthetic.brain-owner-simulation"
                            if owner_simulation_authorized else SYNTHETIC_AGENT_ID)
        expected_type = OwnerSimulationAdapter if owner_simulation_authorized else SyntheticMissionAdapter
        if (set(engine._adapters) != {expected_adapter}
                or type(engine._adapters[expected_adapter]) is not expected_type
                or tuple(engine._adapters[expected_adapter].permissions) != (SYNTHETIC_PERMISSION,)
                or tuple(engine._adapters[expected_adapter].capabilities) != (SYNTHETIC_CAPABILITY,)):
            raise MissionExecutionError("Execution engine must expose only its exact approved synthetic adapter.")
        self.__trusted_acceptance_context = _acceptance_context
        self.__owner_simulation_authority = _owner_authority if owner_simulation_authorized else None
        self.__owner_simulation_context = owner_simulation_authorized
        self.registry = registry
        self.issuer = issuer
        self.engine = engine
        self.verifier = verifier
        self.evidence_factory = evidence_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.persist_state = persist_state
        self.notes_persist = notes_persist
        self._requests: dict[tuple[str, str], Any] = {}
        self._authorizations: dict[tuple[str, str], Any] = {}
        self._results: dict[tuple[str, str], Any] = {}
        self._observed_claims: set[str] = set()
        self._verified: dict[tuple[str, str], Any] = {}
        self._lock = threading.RLock()

    @classmethod
    def synthetic(cls, *, now: Any = None, limits: Any = None,
                  durable_store: Any = None, store_path: str | Path | None = None,
                  persist_state: Callable[[Any], bool] | None = None,
                  registry: Any = None,
                  _acceptance_context: Any = None,
                  _owner_authority: Any = None) -> "MissionExecutionService":
        from . import owner_simulation
        trusted_harness = _acceptance_context is _TRUSTED_ACCEPTANCE_HARNESS
        owner_authorized = owner_simulation.validate_owner_authority(
            _owner_authority, "synthetic.brain-owner-simulation")
        if not trusted_harness and not owner_authorized:
            raise MissionExecutionError("Synthetic fixture registration requires trusted harness or owner-local authority.")
        from . import mission_conversation
        mission_conversation._orchestration()
        from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus, AgentNotFoundError
        if not isinstance(registry, CapabilityRegistry):
            raise MissionExecutionError("Owner simulation requires an explicit CapabilityRegistry.")
        if owner_authorized and registry.list_agents():
            raise MissionExecutionError("Owner simulation must use an isolated registry containing only its synthetic fixture.")
        from control_plane.execution_durability import SQLiteExecutionStore
        from control_plane.execution_engine import ExecutionAuthorizationIssuer, ExecutionEngine
        adapter_id = "synthetic.brain-owner-simulation" if owner_authorized else SYNTHETIC_AGENT_ID
        try:
            registration = registry.discover(adapter_id)
        except AgentNotFoundError:
            registration = registry.register(AgentContract(
                agent_id=adapter_id,
                purpose=("Deterministic owner-supervised local simulation fixture." if owner_authorized
                         else "Deterministic local fixture for Brain acceptance only."),
                version="1.0.0", capabilities=(SYNTHETIC_CAPABILITY,),
                supported_tasks=("synthetic-mission-report",),
                inputs={"objective": "synthetic text", "success_criteria": "synthetic criteria"},
                outputs={"deliverable.synthetic_report": "synthetic report"},
                permissions=(SYNTHETIC_PERMISSION,),
            ), CapabilityStatus.VERIFIED)
        if (registration.status is not CapabilityStatus.VERIFIED
                or len(registry.list_agents()) != 1
                or registration.contract.agent_id != adapter_id
                or registration.contract.purpose != (
                    "Deterministic owner-supervised local simulation fixture." if owner_authorized
                    else "Deterministic local fixture for Brain acceptance only.")
                or registration.contract.capabilities != (SYNTHETIC_CAPABILITY,)
                or registration.contract.supported_tasks != ("synthetic-mission-report",)
                or registration.contract.permissions != (SYNTHETIC_PERMISSION,)):
            raise MissionExecutionError("Injected synthetic capability registration is not the verified fixture contract.")
        if durable_store is None:
            if store_path is None:
                import tempfile
                store_path = Path(tempfile.mkdtemp(prefix="brain-acceptance-")) / "mission-execution.sqlite3"
            durable_store = SQLiteExecutionStore(store_path)
        store_cache = getattr(durable_store, "_brain_execution_service_cache", None)
        if not isinstance(store_cache, dict):
            store_cache = {}
            durable_store._brain_execution_service_cache = store_cache
        if store_path is not None:
            backend_cache = getattr(mission_conversation, "_acceptance_execution_stores", None)
            if not isinstance(backend_cache, dict):
                backend_cache = {}
                mission_conversation._acceptance_execution_stores = backend_cache
            backend_cache[str(Path(store_path))] = durable_store
        issuer = ExecutionAuthorizationIssuer()
        issuer.bind_durable_store(durable_store)
        issued_authorizations = store_cache.setdefault("issued_authorizations", {})
        engine = ExecutionEngine(registry, issuer, limits=limits, durable_store=durable_store)
        issuer._trusted_acceptance_context = lambda: (
            _TRUSTED_ACCEPTANCE_HARNESS if _acceptance_context is _TRUSTED_ACCEPTANCE_HARNESS else None)
        issuer._trusted_owner_simulation_context = lambda: owner_authorized and owner_simulation.validate_owner_authority(
            _owner_authority, "synthetic.brain-owner-simulation") is True
        engine._trusted_acceptance_context = issuer._trusted_acceptance_context
        engine._trusted_owner_simulation_context = lambda: (
            bool(issuer._trusted_owner_simulation_context())
            if callable(issuer._trusted_owner_simulation_context) else False)
        engine.register_adapter(OwnerSimulationAdapter() if owner_authorized else SyntheticMissionAdapter())
        from control_plane.result_verification import ResultVerifier
        service = cls(registry=registry, issuer=issuer, engine=engine,
                      verifier=ResultVerifier(), now=now, persist_state=persist_state,
                      _acceptance_context=_acceptance_context,
                      _owner_authority=_owner_authority)
        store_cache.update({"issuer": issuer, "engine": engine, "service": service})
        service._issued_authorizations = issued_authorizations
        if owner_authorized:
            object.__setattr__(_owner_authority, "_service", service)
        return service

    def mission_capability_discovery(self, *, _acceptance_context: Any = None,
                                     _owner_authority: Any = None):
        """Return fixture discovery for exactly one trusted context."""
        from . import owner_simulation
        trusted_harness = (_acceptance_context is _TRUSTED_ACCEPTANCE_HARNESS
                           and getattr(self, "_MissionExecutionService__trusted_acceptance_context", None)
                           is _TRUSTED_ACCEPTANCE_HARNESS
                           and callable(getattr(self.engine, "_trusted_acceptance_context", None))
                           and self.engine._trusted_acceptance_context() is _TRUSTED_ACCEPTANCE_HARNESS)
        owner_authorized = (owner_simulation.validate_owner_authority(
                                _owner_authority, "synthetic.brain-owner-simulation")
                            and getattr(self, "_MissionExecutionService__owner_simulation_context", False)
                            and owner_simulation.validate_owner_authority(
                                getattr(self, "_MissionExecutionService__owner_simulation_authority", None),
                                "synthetic.brain-owner-simulation")
                            and callable(getattr(self.engine, "_trusted_owner_simulation_context", None))
                            and self.engine._trusted_owner_simulation_context())
        if not trusted_harness and not owner_authorized:
            raise MissionExecutionError("Synthetic capability discovery requires trusted harness or owner-local authority.")
        context = _TRUSTED_ACCEPTANCE_HARNESS if trusted_harness else _owner_authority
        from control_plane.agent_discovery import AgentDiscovery, AgentSource
        discovery = AgentDiscovery(self.registry)
        discovery._trusted_acceptance_context = lambda: (
            _TRUSTED_ACCEPTANCE_HARNESS if trusted_harness else None)
        if owner_authorized:
            discovery._trusted_owner_simulation_context = lambda: (
                context if owner_simulation.validate_owner_authority(
                    context, "synthetic.brain-owner-simulation") else None)
        if owner_authorized:
            self.__owner_simulation_discovery = discovery
        discovery._acceptance_service = self
        if not owner_authorized:
            discovery.discover_existing()
        agent_id = "synthetic.brain-owner-simulation" if owner_authorized else SYNTHETIC_AGENT_ID
        discovery.register_synthetic_source(agent_id, AgentSource(
            module="synthetic_fixture", source_file="mission_execution.py",
            evidence=("invoke",),
            limitations=("Deterministic local synthetic fixture only; no research, external service, or specialist access.",),
            tool_requirements=(), internet_access="none",
            execution_integration_available=False))
        return discovery

    def _contracts(self):        # Keep the exact contract classes used to construct this engine. Reloading
        # modules here would invalidate live issuers, requests, and durable stores.
        from control_plane.execution_engine import ExecutionRequest
        from control_plane.result_verification import Evidence, TaskVerificationInput
        return ExecutionRequest, Evidence, TaskVerificationInput

    def _task(self, state: Any, task_id: str):
        from . import owner_simulation
        task = next((item for item in state.plan.tasks if item.task_id == task_id), None)
        if task is None:
            raise MissionExecutionError("Task does not belong to this mission.")
        if not task.proposed_agent:
            raise MissionExecutionError("The current mission plan has no proposed task to bind for synthetic acceptance.")
        from . import owner_simulation
        allowed_adapter = ("synthetic.brain-owner-simulation"
                           if getattr(self, "_MissionExecutionService__owner_simulation_context", False)
                           and owner_simulation.validate_owner_authority(
                               getattr(self, "_MissionExecutionService__owner_simulation_authority", None),
                               "synthetic.brain-owner-simulation") else SYNTHETIC_AGENT_ID)
        if task.proposed_agent != allowed_adapter:
            raise MissionExecutionError("Only the explicitly scoped deterministic synthetic adapter is available.")
        return task

    def _request(self, state: Any, task: Any):
        from . import owner_simulation
        ExecutionRequest, _, _ = self._contracts()
        key = (state.mission.mission_id, task.task_id)
        if task.proposed_agent not in {SYNTHETIC_AGENT_ID, "synthetic.brain-owner-simulation"}:
            raise MissionExecutionError("Only registered deterministic synthetic adapters can be authorized by this bridge.")
        if task.proposed_agent == "synthetic.brain-owner-simulation" and not (
                getattr(self, "_MissionExecutionService__owner_simulation_context", False)
                and owner_simulation.validate_owner_authority(
                    getattr(self, "_MissionExecutionService__owner_simulation_authority", None),
                    "synthetic.brain-owner-simulation")):
            raise MissionExecutionError("Owner simulation authority is absent or revoked.")
        if tuple(task.required_permissions) != (SYNTHETIC_PERMISSION,):
            raise MissionExecutionError("The planned permission scope differs from the synthetic adapter contract.")
        if tuple(task.required_capabilities) != (SYNTHETIC_CAPABILITY,):
            raise MissionExecutionError("The planned capability differs from the synthetic adapter contract.")
        request = ExecutionRequest(
            task_id=task.task_id, mission_id=state.mission.mission_id,
            agent_id=task.proposed_agent, capability=SYNTHETIC_CAPABILITY,
            input_data={"objective": state.mission.original_objective,
                        "success_criteria": list(state.mission.success_criteria)},
            permissions=(SYNTHETIC_PERMISSION,), depth=0, timeout_seconds=5.0)
        prior = self._requests.get(key)
        if prior is not None and self._request_digest(prior) != self._request_digest(request):
            raise MissionExecutionError("Mission task input changed after an execution request was bound.")
        self._requests[key] = request
        return request

    def authorize(self, state: Any, task_id: str, approval: ExecutionApproval, *, persist_claim: Any = None):
        from . import owner_simulation
        from control_plane.execution_engine import ExecutionAuthorization, ExecutionAuthorizationIssuer
        with self._lock:
            task = self._task(state, task_id)
            if not hasattr(state, "execution_records"):
                state.execution_records = {}
            if not hasattr(state, "execution_intents"):
                state.execution_intents = {}
            store = getattr(self.engine, "durable_store", None)
            if store is None:
                raise MissionExecutionError("Durable control-plane execution storage is required.")
            current = (state.execution_records or {}).get(task_id, {})
            if current.get("status") not in {None, "planned", "blocked"}:
                raise MissionExecutionError("Current task state does not permit new execution consent.")
            if any(item.get("status") in {"authorization_issued", "running", "interrupted", "completed", "failed", "cancelled", "timed_out"}
                   for item in (state.execution_records or {}).values()):
                raise MissionExecutionError("A prior mission execution or intent blocks further execution in this mission.")
            key = (state.mission.mission_id, task_id)
            if store.get_claim(task_id) is not None or key in self._authorizations:
                raise MissionExecutionError("This task already has a durable claim or issued authorization.")
            if not isinstance(approval, ExecutionApproval):
                raise MissionExecutionError("A distinct explicit execution approval is required.")
            try:
                if not isinstance(self.issuer, ExecutionAuthorizationIssuer):
                    raise MissionExecutionError("A host-injected control-plane authorization issuer is required.")
                if getattr(self.engine, "authorization_issuer", None) is not self.issuer:
                    raise MissionExecutionError("Execution engine and authorization issuer are not the same control-plane authority.")
                if getattr(self.issuer, "_durable_store", None) is not self.engine.durable_store:
                    raise MissionExecutionError("The authorization issuer is not bound to the execution ledger.")
            except ImportError as error:
                raise MissionExecutionError("Reviewed execution authorization contracts are unavailable.") from error
            expected_adapter = ("synthetic.brain-owner-simulation"
                                if self.__owner_simulation_context and owner_simulation.validate_owner_authority(
                                    self.__owner_simulation_authority, "synthetic.brain-owner-simulation")
                                else SYNTHETIC_AGENT_ID)
            if (approval.mission_id != state.mission.mission_id or approval.task_id != task_id
                    or approval.adapter_id != expected_adapter
                    or approval.scope != (SYNTHETIC_PERMISSION,)
                    or not isinstance(approval.approved_by, str) or not approval.approved_by.strip()
                    or not isinstance(approval.reason, str) or not approval.reason.strip()):
                raise MissionExecutionError("Execution approval does not match the exact synthetic task and scope.")
            request = self._request(state, task)
            try:
                authorization = self.issuer.authorize(
                    request, approved_by=approval.approved_by,
                    permissions=approval.scope, now=self._now(), validity=timedelta(minutes=5))
            except Exception as error:
                raise MissionExecutionError("Execution authorization was rejected by the control plane.") from error
            if not isinstance(authorization, ExecutionAuthorization):
                raise MissionExecutionError("Control plane did not issue a valid execution authorization.")
            # Persist intent before submit. The engine remains responsible for
            # atomically consuming this authorization and claiming the task.
            authorization_intent = {
                "status": "authorization_issued",
                "request_digest": self._request_digest(request),
                "authorization_binding": self._binding(request),
                "authorization_expiry": authorization.expires_at.isoformat(),
                "execution_approval": {"approved_by": approval.approved_by,
                                       "reason": approval.reason,
                                       "mission_id": approval.mission_id,
                                       "task_id": approval.task_id,
                                       "adapter_id": approval.adapter_id,
                                       "scope": list(approval.scope)},
                "authorization_id": authorization.authorization_id,
            }
            self._record(state, task_id, **authorization_intent)
            state.execution_intents[task_id] = dict(authorization_intent)
            try:
                persist = persist_claim if callable(persist_claim) else self.persist_state
                persisted = persist(state) is True if callable(persist) else False
            except Exception as error:
                self._last_persistence_error = error
                persisted = False
            if not persisted:
                # The durable authorization may already exist; retain a fail-closed
                # local intent but never permit submission without confirmed persistence.
                raise MissionExecutionError("Could not persist execution intent; the task was not submitted.")
            self._authorizations[key] = authorization
            issued = getattr(self, "_issued_authorizations", None)
            if isinstance(issued, dict):
                issued[authorization.authorization_id] = authorization
            return authorization

    def deny(self, state: Any, task_id: str, reason: str = "Human denied synthetic execution.") -> None:
        task = self._task(state, task_id)
        key = (state.mission.mission_id, task.task_id)
        with self._lock:
            if not hasattr(state, "execution_records"):
                state.execution_records = {}
            status = (state.execution_records or {}).get(task_id, {}).get("status")
            if status not in {None, "planned", "authorized", "blocked"}:
                raise MissionExecutionError("Current task state does not permit denial.")
            if status in {"authorization_issued", "authorized"}:
                raise MissionExecutionError("An issued authorization cannot be converted into denial; expire and review a new mission.")
            if status not in {None, "planned", "blocked"}:
                raise MissionExecutionError("An attempted or terminal task cannot be denied or retried.")
            authorization = self._authorizations.pop(key, None)
            if authorization is not None:
                self.issuer.revoke(authorization)
            self._record(state, task.task_id, status="denied", reason=reason)

    def lookup(self, state: Any, task_id: str) -> dict[str, Any]:
        """Refresh the Brain view from the durable claim; never infer from UI state."""
        task = self._task(state, task_id)
        store = getattr(self.engine, "durable_store", None)
        if store is None:
            raise MissionExecutionError("Durable control-plane execution storage is required.")
        claim = store.get_claim(task_id)
        if claim is None:
            if (state.execution_records or {}).get(task_id, {}).get("status") in {
                    "authorization_issued", "running", "interrupted", "completed", "failed", "cancelled", "timed_out"}:
                raise MissionExecutionError("Persisted execution intent has no matching durable claim; no replay is permitted.")
            return {"state": "NOT_SUBMITTED", "claim": None, "result": None}
        if (claim.get("mission_id") != state.mission.mission_id
                or claim.get("task_id") != task.task_id
                or claim.get("agent_id") != task.proposed_agent):
            raise MissionExecutionError("Durable claim does not match this mission and task.")
        intent = (state.execution_records or {}).get(task_id, {})
        expected = self._request(state, task)
        if claim.get("capability") != expected.capability or claim.get("request_digest") != self._claim_digest(expected):
            raise MissionExecutionError("Durable claim does not match the approved input and scope.")
        binding = intent.get("authorization_binding")
        if binding is not None and binding != self._binding(expected):
            raise MissionExecutionError("Durable claim differs from the persisted approval binding.")
        if (intent.get("authorization_id") is not None
                and claim.get("authorization_id") != intent.get("authorization_id")):
            raise MissionExecutionError("Durable claim differs from persisted authorization identity.")
        durable_state = claim.get("state")
        if durable_state == "RECONCILIATION_REQUIRED":
            self._record(state, task_id, status="interrupted",
                         authorization_id=claim.get("authorization_id"),
                         reason="RECONCILIATION_REQUIRED; human notes only; replay forbidden")
            return {"state": durable_state, "claim": claim, "result": None}
        mapped = {
            "CLAIMED": "running", "AUTHORIZED": "running", "INVOKING": "running",
            "COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "cancelled",
            "TIMED_OUT": "timed_out", "RECONCILIATION_REQUIRED": "interrupted",
        }.get(durable_state)
        if mapped is None:
            raise MissionExecutionError("Durable claim has an unknown state.")
        if mapped == "completed" and self.engine.result(task_id) is None:
            try:
                from control_plane.execution_engine import ExecutionResult, ExecutionState, ErrorCategory
                payload = json.loads(claim.get("result_json") or "{}")
                result = ExecutionResult(
                    task_id=payload["task_id"], mission_id=payload["mission_id"],
                    agent_id=payload["agent_id"], state=ExecutionState(payload["state"]),
                    output=payload.get("output"),
                    error_category=(ErrorCategory(payload["error_category"])
                                    if payload.get("error_category") else None),
                    error_message=payload.get("error_message"),
                    started_at=datetime.fromisoformat(payload["started_at"]) if payload.get("started_at") else None,
                    finished_at=datetime.fromisoformat(payload["finished_at"]) if payload.get("finished_at") else None,
                    elapsed_seconds=payload.get("elapsed_seconds", 0.0))
                if (result.task_id != task.task_id or result.mission_id != state.mission.mission_id
                        or result.agent_id != task.proposed_agent or result.state is not ExecutionState.COMPLETED):
                    raise ValueError("durable result identity or state mismatch")
                claim_result = json.loads(claim.get("result_json") or "{}")
                if result.to_dict() != claim_result:
                    raise ValueError("reconstructed result differs from durable engine result")
            except Exception as error:
                raise MissionExecutionError("Durable completion result could not be reconstructed safely.") from error
        else:
            result = self.engine.result(task_id)
        if mapped == "completed" and result is None:
            raise MissionExecutionError("Durable completion has no result available for structural assessment.")
        if mapped in {"failed", "cancelled", "timed_out"} and result is None:
            from control_plane.execution_engine import ErrorCategory, ExecutionResult, ExecutionState
            try:
                error_category = ErrorCategory(claim.get("error_category")) if claim.get("error_category") else None
                result = ExecutionResult(
                    task_id=claim["task_id"], mission_id=claim["mission_id"],
                    agent_id=claim["agent_id"], state=ExecutionState(mapped),
                    error_category=error_category, error_message=claim.get("error_message"))
            except (KeyError, TypeError, ValueError) as error:
                raise MissionExecutionError("Durable terminal execution claim is malformed.") from error
        if mapped in {"completed", "failed", "cancelled", "timed_out"}:
            self._results[(state.mission.mission_id, task_id)] = result
        self._record(state, task_id, status=mapped,
                     authorization_id=claim.get("authorization_id"),
                     reason=("execution outcome uncertain; notes only and replay forbidden"
                             if mapped == "interrupted" else "derived from durable execution claim"))
        return {"state": durable_state, "claim": claim, "result": result}

    def submit(self, state: Any, task_id: str, authorization: Any = None, *,
               persist_state: Callable[[Any], bool] | None = None):
        with self._lock:
            task = self._task(state, task_id)
            store = getattr(self.engine, "durable_store", None)
            if store is None:
                raise MissionExecutionError("Durable control-plane execution storage is required.")
            request = self._request(state, task)
            intent = (state.execution_records or {}).get(task_id, {})
            key = (state.mission.mission_id, task_id)
            expected = self._authorizations.get(key)
            if expected is None or authorization is not expected:
                claim = store.get_claim(task_id)
                if claim is not None:
                    self.lookup(state, task_id)
                raise MissionExecutionError("A live single-use execution approval is required; no replay is allowed.")
            if intent.get("status") != "authorization_issued":
                raise MissionExecutionError("Persisted execution intent is required before submission.")
            if intent.get("authorization_expiry"):
                try:
                    if self._now() >= datetime.fromisoformat(intent["authorization_expiry"]):
                        self._record(state, task_id, status="interrupted",
                                     reason="execution approval expired before durable admission")
                        raise MissionExecutionError("Execution approval expired; request a new mission rather than retrying.")
                except MissionExecutionError:
                    raise
                except ValueError as error:
                    raise MissionExecutionError("Persisted authorization expiry is invalid.") from error
            if intent.get("request_digest") != self._request_digest(request):
                raise MissionExecutionError("Persisted intent does not match the current request.")
            approval_record = intent.get("execution_approval")
            if (not isinstance(approval_record, Mapping)
                    or approval_record.get("mission_id") != state.mission.mission_id
                    or approval_record.get("task_id") != task_id
                    or approval_record.get("adapter_id") != task.proposed_agent
                    or tuple(approval_record.get("scope", ())) != tuple(request.permissions)
                    or not isinstance(approval_record.get("approved_by"), str)
                    or not approval_record.get("approved_by", "").strip()
                    or not isinstance(approval_record.get("reason"), str)
                    or not approval_record.get("reason", "").strip()):
                raise MissionExecutionError("Persisted execution approval does not match task, agent, scope, and approver.")
            binding = intent.get("authorization_binding")
            if binding != self._binding(request):
                raise MissionExecutionError("Persisted approval binding does not match the current execution scope.")
            claim = store.get_claim(task_id)
            if claim is not None:
                self.lookup(state, task_id)
                raise MissionExecutionError("Task already has a durable claim; duplicate or replay rejected.")
            self._authorizations.pop(key, None)
            try:
                self._record(state, task_id, status="running", reason="explicit user submitted; durable claim lookup follows")
                self._persist_projection(state, persist_state)
            except Exception:
                self._authorizations[key] = expected
                raise
            try:
                result = self.engine.submit(request, expected)
            except Exception:
                claim = store.get_claim(task_id)
                if claim is not None:
                    self.lookup(state, task_id)
                else:
                    self._record(state, task_id, status="authorization_issued",
                                 reason="engine rejected before durable admission; explicit review required")
                self._persist_projection(state, persist_state)
                raise
            if result.task_id != task.task_id or result.mission_id != state.mission.mission_id:
                raise MissionExecutionError("Execution engine returned a result for a different mission or task.")
            self._results[key] = result
            projection = self.lookup(state, task_id)
            if projection["state"] in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                self._record(state, task_id, status=projection["result"].state.value,
                             reason="terminal engine result derived from durable claim")
            self._persist_projection(state, persist_state)
            return result

    def verify(self, state: Any, task_id: str, result: Any = None):
        _, Evidence, TaskVerificationInput = self._contracts()
        task = self._task(state, task_id)
        key = (state.mission.mission_id, task_id)
        projection = self.lookup(state, task_id)
        actual = projection["result"]
        if result is not None and result is not actual:
            raise MissionExecutionError("Only the durable engine result can be assessed.")
        if projection["state"] == "RECONCILIATION_REQUIRED":
            self._record(state, task_id, status="interrupted",
                         reason="execution is uncertain; reconciliation notes cannot promote it")
            raise MissionExecutionError("This execution is uncertain and requires reconciliation notes only.")
        if projection["state"] != "COMPLETED" or actual is None:
            raise MissionExecutionError("Only an engine-recorded durable completion can be assessed.")
        with self._lock:
            prior = self._verified.get(key)
            if prior is not None:
                return prior
            if actual.task_id != task.task_id or actual.mission_id != state.mission.mission_id:
                raise MissionExecutionError("Execution result does not match the mission task.")
        if actual.state.value in {"failed", "timed_out", "cancelled"}:
            outcome = self._default_verifier().assess_mission(
                self._mission_reference(state), (self._verification_input(task, actual, ()),))
            state.outcomes[task_id] = outcome
            self._record(state, task_id, status=actual.state.value,
                         reason="terminal failure derived from durable execution claim")
            self._verified[key] = outcome
            return outcome
        if actual.state.value != "completed":
            raise MissionExecutionError("Only a completed engine result can enter structural assessment.")
        if not isinstance(actual.output, Mapping) or actual.output.get("criterion_evidence") != {
                criterion: True for criterion in state.mission.success_criteria}:
            raise MissionExecutionError("Structural assessment requires the engine-recorded deterministic fixture output.")
        support = actual.output.get("Capability support assessment")
        if (not isinstance(support, Mapping)
                or support.get("objective") != state.mission.original_objective
                or support.get("limitations") != "Deterministic local fixture; no external research or actions."
                or tuple(support.get("criteria", ())) != tuple(state.mission.success_criteria)):
            raise MissionExecutionError("Synthetic output does not bind the persisted objective, criteria, and no-external-action limitation.")
        evidences = []
        if self.evidence_factory is None:
            flags = actual.output.get("criterion_evidence", {})
            for index, criterion in enumerate(state.mission.success_criteria):
                if isinstance(flags, Mapping) and flags.get(criterion) is True:
                    evidences.append(Evidence(
                        evidence_id=f"ev-{task_id}-{index}", mission_id=state.mission.mission_id,
                        task_id=task_id, claim=criterion, source="deterministic synthetic execution fixture",
                        verified=True, content={"criterion": criterion, "fixture_result": actual.output},
                        provenance="engine-recorded-structural-fixture-only"))
            deliverable = "Capability support assessment"
            evidences.append(Evidence(
                evidence_id=f"ev-{task_id}-deliverable", mission_id=state.mission.mission_id,
                task_id=task_id, claim=deliverable, source="deterministic synthetic execution fixture",
                verified=True, content={"fixture_result": actual.output},
                provenance="engine-recorded-structural-fixture-only"))
        if self.evidence_factory is not None:
            raise MissionExecutionError("Caller-supplied evidence is unsupported; assessment uses only the engine-recorded synthetic fixture.")
        # ResultVerifier requires the plan's deliverables; the actual synthetic
        # result is also bound to the one harmless deterministic fixture deliverable.
        output = dict(actual.output) if isinstance(actual.output, Mapping) else actual.output
        if not isinstance(output, dict) or not isinstance(output.get("Capability support assessment"), Mapping):
            raise MissionExecutionError("Engine-recorded fixture output lacks its declared deliverable.")
        assessment_output = output["Capability support assessment"]
        if (assessment_output.get("objective") != state.mission.original_objective
                or assessment_output.get("limitations") != "Deterministic local fixture; no external research or actions."):
            raise MissionExecutionError("Engine-recorded fixture output does not match the approved objective and limitation.")
        item = TaskVerificationInput(
            task=task, result=actual, evidences=tuple(evidences),
            agent_capabilities=tuple(task.required_capabilities),
            authorized_permissions=(SYNTHETIC_PERMISSION,),
            required_permissions=(SYNTHETIC_PERMISSION,))
        from control_plane.mission_planning import mission_reference_from_definition
        reference = mission_reference_from_definition(state.mission.to_dict())
        from dataclasses import replace as dc_replace
        assessment_criteria = tuple(state.mission.success_criteria)
        from control_plane.mission_planning import MissionReference
        verification_mission = dc_replace(reference, success_criteria=assessment_criteria)
        synthetic_evidences = tuple(evidences)
        item = dc_replace(item, evidences=synthetic_evidences)
        verifier = self.verifier or self._default_verifier()
        verification_task = dc_replace(task, expected_deliverables=("Capability support assessment",))
        item = dc_replace(item, task=verification_task)
        assessment = verifier.assess_mission(verification_mission, (item,))
        task_assessment = verifier.verify_task(verification_mission, item)
        self._record(state, task_id, status="completed",
                     structural_assessment=assessment.status.value,
                     structural_uncertainties=list(assessment.uncertainties),
                     structural_task_assessment=task_assessment.to_dict(),
                     external_authentication="unsupported")
        state.outcomes[task_id] = assessment
        self._verified[key] = assessment
        return assessment

    def record_reconciliation_note(self, state: Any, task_id: str, note: str) -> None:
        """Store a human observation only; it cannot alter a durable claim."""
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise MissionExecutionError("Reconciliation note must be non-empty safe text up to 2000 characters.")
        self.lookup(state, task_id)
        store = getattr(self.engine, "durable_store", None)
        claim = store.get_claim(task_id) if store is not None else None
        if claim is None or claim.get("state") != "RECONCILIATION_REQUIRED":
            raise MissionExecutionError("Task is not awaiting reconciliation.")
        store.record_reconciliation_note(task_id, note)
        self._record(state, task_id, status="interrupted",
                     reason="RECONCILIATION_REQUIRED; note recorded, no outcome promotion")
        if callable(self.notes_persist):
            self.notes_persist(state, task_id, note)
        self._persist_projection(state)

    def _persist_projection(self, state: Any, callback: Callable[[Any], bool] | None = None) -> None:
        persist = callback if callable(callback) else self.persist_state
        if not callable(persist):
            raise MissionExecutionError("Durable Brain mission persistence is required for execution status transitions.")
        try:
            if persist(state) is not True:
                raise MissionExecutionError("Brain status projection was not persisted.")
        except Exception as error:
            raise MissionExecutionError(
                "Durable control-plane state is authoritative; Brain projection persistence failed. "
                "Look up the claim before any further action; automatic resubmission is forbidden."
            ) from error

    @staticmethod
    def _request_digest(request: Any) -> str:
        return hashlib.sha256(json.dumps({"mission_id": request.mission_id,
                                         "task_id": request.task_id,
                                         "agent_id": request.agent_id,
                                         "capability": request.capability,
                                         "permissions": request.permissions,
                                         "input": request.input_data},
                                        sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _claim_digest(request: Any) -> str:
        return hashlib.sha256(json.dumps({"task_id": request.task_id,
                                         "mission_id": request.mission_id,
                                         "agent_id": request.agent_id,
                                         "capability": request.capability,
                                         "input": request.input_data,
                                         "permissions": request.permissions},
                                        sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _binding(request: Any) -> dict[str, Any]:
        return {"mission_id": request.mission_id, "task_id": request.task_id,
                "agent_id": request.agent_id, "capability": request.capability,
                "permissions": list(request.permissions),
                "input_digest": hashlib.sha256(json.dumps(request.input_data,
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    allow_nan=False).encode()).hexdigest()}

    @staticmethod
    def _mission_reference(state: Any):
        from control_plane.mission_planning import mission_reference_from_definition
        return mission_reference_from_definition(state.mission.to_dict())

    @staticmethod
    def _verification_input(task: Any, result: Any, evidences: tuple[Any, ...]):
        _, _, TaskVerificationInput = MissionExecutionService._contracts()
        return TaskVerificationInput(task=task, result=result, evidences=evidences,
            agent_capabilities=tuple(task.required_capabilities),
            authorized_permissions=(SYNTHETIC_PERMISSION,),
            required_permissions=(SYNTHETIC_PERMISSION,))

    @staticmethod
    def _default_verifier():
        from control_plane.result_verification import ResultVerifier
        return ResultVerifier()

    @staticmethod
    def _record(state: Any, task_id: str, **values: Any) -> None:
        records = getattr(state, "execution_records", None)
        if not isinstance(records, dict):
            records = {}
            state.execution_records = records
        current = dict(records.get(task_id, {}))
        old_status = current.get("status")
        new_status = values.get("status", old_status)
        if new_status == "verified":
            raise MissionExecutionError("Structural assessment is not external verification; use completed status.")
        allowed = {
            None: {"planned", "blocked", "authorized", "authorization_issued", "denied"},
            "blocked": {"blocked", "authorized", "authorization_issued", "running"},
            "planned": {"planned", "authorized", "authorization_issued", "blocked", "denied"},
            "authorized": {"authorized", "authorization_issued", "running", "denied", "blocked", "failed"},
            "authorization_issued": {"authorization_issued", "running", "failed", "completed", "cancelled", "timed_out", "interrupted"},
            "denied": {"denied"},
            "running": {"running", "completed", "failed", "cancelled", "timed_out", "interrupted"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
            "timed_out": {"timed_out"},
            "denied": {"denied"},
            "interrupted": {"interrupted"},
        }
        if new_status not in allowed.get(old_status, set()):
            if not (old_status == "blocked" and new_status == "authorization_issued"):
                raise MissionExecutionError("Invalid or non-monotonic execution state transition.")
        if old_status == "authorization_issued" and new_status in {"running", "completed", "failed", "cancelled", "timed_out", "interrupted"}:
            if not (current.get("authorization_id") and current.get("authorization_binding")
                    and current.get("request_digest") and current.get("authorization_expiry")):
                raise MissionExecutionError("Persisted execution approval binding is required for this transition.")
        if old_status == "blocked" and new_status == "authorization_issued" and not current.get("authorization_id"):
            # A failed Brain projection may leave a previously issued, unconsumed
            # authorization in the issuer's in-memory registry; a later explicit
            # approval can replace it without any admission or replay.
            pass
        if old_status in {"interrupted", "denied"} and values.get("status") != old_status:
            raise MissionExecutionError("A terminal execution state is immutable.")
        if old_status in {"failed", "cancelled", "timed_out"} and new_status != old_status:
            raise MissionExecutionError("A terminal execution state is immutable.")
        if old_status == "completed" and new_status != "completed":
            raise MissionExecutionError("Engine-recorded completion is immutable.")
        values = dict(values)
        values.pop("authorization_ref", None)
        if old_status in {"authorization_issued", "running", "interrupted", "completed", "failed", "cancelled", "timed_out"}:
            for field in ("authorization_binding", "request_digest", "authorization_expiry",
                          "execution_approval", "authorization_id"):
                if field not in values and field in current:
                    values[field] = current[field]
            if new_status == "authorization_issued" and "authorization_id" not in values and "authorization_id" in current:
                values["authorization_id"] = current["authorization_id"]
        if new_status == "authorization_issued":
            for field in ("authorization_binding", "request_digest", "authorization_expiry",
                          "execution_approval", "authorization_id"):
                if field not in values and field in current:
                    values[field] = current[field]
        if old_status in {"authorization_issued", "running"}:
            values.pop("outcome", None)
            values.pop("task_assessment", None)
        current.pop("authorization_ref", None)
        current.update(values)
        current["task_id"] = task_id
        records[task_id] = current
        intent = getattr(state, "execution_intents", {}).get(task_id)
        if isinstance(intent, dict) and new_status in {
                "running", "completed", "failed", "cancelled", "timed_out", "interrupted"}:
            intent["status"] = new_status
