"""Streamlit-independent mission routing and state management."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from . import mission_conversation


class MissionStateRepository(Protocol):
    def load(self, conversation_id: str) -> tuple[Any, str] | None: ...
    def create_once(self, conversation_id: str, state: Any, request_key: str): ...
    def persist(self, conversation_id: str, state: Any, request_key: str) -> bool: ...


@dataclass(frozen=True)
class MissionRequest:
    text: str
    conversation_id: str
    intent: str = "mission"
    confidence: str = "explicit"
    inference_mode: str | None = None


@dataclass(frozen=True)
class MissionControllerResult:
    handled: bool
    created: bool = False
    state: Any = None
    summary: str | None = None
    reason: str | None = None


class ConversationMissionRepository:
    """Adapter for the existing conversation SQLite repository interface."""
    def __init__(self, backend, *, data_dir=None, db_path=None, synthetic_fixture_discovery=None):
        self.backend = backend
        self.data_dir = data_dir
        self.db_path = db_path
        self.synthetic_fixture_discovery = synthetic_fixture_discovery
        from . import owner_simulation
        owner_context = getattr(synthetic_fixture_discovery, "_trusted_owner_simulation_context", None)
        self._owner_simulation_authority = (owner_context() if callable(owner_context)
                                            and owner_simulation.validate_owner_authority(
                                                owner_context(), "synthetic.brain-owner-simulation")
                                            else None)
        if synthetic_fixture_discovery is not None:
            stores = getattr(mission_conversation, "_acceptance_execution_stores", None)
            if isinstance(stores, dict):
                for store in stores.values():
                    if callable(getattr(store, "get_claim", None)):
                        backend_cache = getattr(self.backend, "_brain_execution_store_cache", None)
                        if not isinstance(backend_cache, dict):
                            backend_cache = {}
                            self.backend._brain_execution_store_cache = backend_cache
                        backend_cache[str(store.path)] = store
                        self.backend._brain_execution_store_cache[str(self._execution_path())] = store

    def persist(self, conversation_id, state, request_key):
        serialized = state.to_dict()
        owner_callback = getattr(self.synthetic_fixture_discovery, "_trusted_owner_simulation_context", None)
        if callable(owner_callback):
            authority = owner_callback()
            from . import owner_simulation
            if owner_simulation.validate_owner_authority(authority, "synthetic.brain-owner-simulation"):
                self._owner_simulation_authority = authority
                state._owner_authority = authority
                state._trusted_owner_simulation = True
        if self._is_trusted_test_context():
            serialized["_trusted_acceptance_harness"] = True
        if self._is_trusted_owner_context():
            serialized["_trusted_owner_simulation"] = True
        if getattr(state, "synthetic_acceptance", False):
            serialized["synthetic_acceptance"] = True
        if getattr(state, "synthetic_acceptance", False):
            serialized["plan"]["synthetic_acceptance"] = True
            serialized["plan"]["mission_ref"] = serialized["mission"]["mission_id"]
            serialized["_trusted_acceptance_harness"] = self._is_trusted_test_context()
            serialized["_trusted_owner_simulation"] = self._is_trusted_owner_context()
            if self._is_trusted_owner_context():
                owner_callback = getattr(self.synthetic_fixture_discovery, "_trusted_owner_simulation_context")
                self._owner_simulation_authority = owner_callback()
                state._owner_authority = self._owner_simulation_authority
        execution_statuses = {'authorization_issued', 'running', 'completed', 'failed', 'cancelled',
                              'timed_out', 'interrupted'}
        has_execution_state = any(record.get('status') in execution_statuses
                                  for record in getattr(state, "execution_records", {}).values())
        if not getattr(state, "synthetic_acceptance", False) and not has_execution_state:
            result = self.backend.save_mission_state(
                conversation_id, serialized, request_key,
                data_dir=self.data_dir, db_path=self.db_path)
            return result[1] == request_key and result[0] == serialized
        if has_execution_state:
            discovery = self.synthetic_fixture_discovery
            if discovery is None or getattr(discovery, "_acceptance_service", None) is None:
                return False
            test_context = self._is_trusted_test_context()
            owner_trusted = self._is_trusted_owner_context()
            if not test_context and not owner_trusted:
                return False
            if test_context:
                serialized["_trusted_acceptance_harness"] = True
            result = self.backend.save_mission_state(
                conversation_id, serialized, request_key,
                data_dir=self.data_dir, db_path=self.db_path)
            confirmed = self._confirm_execution_persist(result, serialized, request_key)
            if not confirmed:
                return False
            return self.backend.save_mission_state(
                conversation_id, serialized, request_key,
                data_dir=self.data_dir, db_path=self.db_path)[1] == request_key
        if state.approvals and not has_execution_state:
            if getattr(state, "synthetic_acceptance", False):
                if self._is_trusted_owner_context():
                    state._trusted_owner_simulation = True
                    state._owner_authority = self._owner_simulation_authority
                if not self._trusted_synthetic_context():
                    return False
                serialized["_trusted_acceptance_harness"] = self._is_trusted_test_context()
                serialized["_trusted_owner_simulation"] = self._is_trusted_owner_context()
                return self.backend.persist_planning_approval(
                    conversation_id, serialized, request_key,
                    data_dir=self.data_dir, db_path=self.db_path)
            payload, key, _ = self.backend.save_mission_state(
                conversation_id, serialized, request_key,
                data_dir=self.data_dir, db_path=self.db_path)
            return key == request_key and payload.get('approvals') == serialized.get('approvals')
        payload, key, _ = self.backend.save_mission_state(
            conversation_id, serialized, request_key,
            data_dir=self.data_dir, db_path=self.db_path)
        return key == request_key and payload == serialized

    def _is_trusted_test_context(self):
        from .mission_execution import _TRUSTED_ACCEPTANCE_HARNESS
        discovery = self.synthetic_fixture_discovery
        return (discovery is not None
                and callable(getattr(discovery, "_trusted_acceptance_context", None))
                and discovery._trusted_acceptance_context() is _TRUSTED_ACCEPTANCE_HARNESS)

    def _is_trusted_owner_context(self):
        from . import owner_simulation
        discovery = self.synthetic_fixture_discovery
        context = getattr(discovery, "_trusted_owner_simulation_context", None)
        return bool(callable(context) and owner_simulation.validate_owner_authority(
            context(), "synthetic.brain-owner-simulation"))

    def _trusted_synthetic_context(self):
        return self._is_trusted_test_context() or self._is_trusted_owner_context()

    @staticmethod
    def _confirm_execution_persist(result, expected, request_key):
        if not isinstance(result, tuple) or len(result) != 3:
            return False
        payload, key, changed = result
        return (key == request_key and isinstance(payload, dict)
                and payload.get('execution_records') == expected.get('execution_records')
                and payload.get('execution_intents') == expected.get('execution_intents'))

    def load(self, conversation_id):
        row = self.backend.load_mission_state(
            conversation_id, data_dir=self.data_dir, db_path=self.db_path)
        if row is None:
            return None
        payload, key = row
        payload_task_ids = {
            str(item.get("task_id", "")).rsplit(".", 1)[-1]: item.get("task_id")
            for item in (payload.get("plan", {}).get("tasks") or []) if isinstance(item, dict)}
        # Only explicit trusted constructor injection can supply fixture discovery;
        # session and persisted mission fields are not consulted for authority.
        discovery = self.synthetic_fixture_discovery
        owner_trusted = self._is_trusted_owner_context()
        persisted_synthetic = (payload.get("synthetic_acceptance") is True
                              or payload.get("plan", {}).get("synthetic_acceptance") is True)
        if persisted_synthetic and discovery is None:
            # Keep persisted proposals readable without treating their marker as
            # authority; strip synthetic claims before normal state validation.
            payload = dict(payload)
            payload["synthetic_acceptance"] = False
            payload_plan = dict(payload.get("plan", {}))
            payload_plan["synthetic_acceptance"] = False
            payload["plan"] = payload_plan
            discovery = None
        state = mission_conversation.state_from_dict(payload, capability_discovery=discovery)
        test_trusted = self._is_trusted_test_context()
        state._trusted_acceptance_harness = test_trusted
        state._trusted_owner_simulation = owner_trusted
        state._owner_authority = self._owner_simulation_authority if owner_trusted else None
        state.synthetic_acceptance = discovery is not None and (owner_trusted or test_trusted)
        if state.synthetic_acceptance and (payload.get("synthetic_acceptance") is True or payload.get("plan", {}).get("synthetic_acceptance") is True):
            from dataclasses import replace
            state.plan = replace(state.plan, mission_id=state.mission.mission_id,
                tasks=tuple(replace(task, task_id=payload_task_ids.get(
                    task.task_id.rsplit(".", 1)[-1], task.task_id)) for task in state.plan.tasks))
        state.execution_intents = payload.get("execution_intents", {})
        if state.synthetic_acceptance:
            canonical_by_suffix = {str(item).rsplit(".", 1)[-1]: item
                                   for item in payload_task_ids.values()}
            from . import mission_conversation as mc
            from control_plane.delegation_authorization import (
                ApprovalDecision, ApprovalRequest, ApprovalStatus, DelegationAuthorizer)
            authorizer = DelegationAuthorizer(discovery)
            restored = {}
            for task_id, item in payload.get("approvals", {}).items():
                canonical_id = canonical_by_suffix.get(str(task_id).rsplit(".", 1)[-1])
                if canonical_id not in {task.task_id for task in state.plan.tasks}:
                    continue
                req = item.get("request", {})
                request = ApprovalRequest(
                    request_id=req["request_id"], mission_ref=req["mission_ref"],
                    task_ref=req["task_ref"], proposed_agent=req["proposed_agent"],
                    capability=req["capability"], requested_action=req["requested_action"],
                    expected_outcome=req["expected_outcome"],
                    requested_permissions=tuple(req["requested_permissions"]),
                    authorized_scope=tuple(req["authorized_scope"]), risks=tuple(req["risks"]),
                    created_at=mc.datetime.fromisoformat(req["created_at"]),
                    expires_at=mc.datetime.fromisoformat(req["expires_at"]),
                    request_digest=req["request_digest"])
                decision_data = item.get("decision")
                decision = (ApprovalDecision(
                    request_id=decision_data["request_id"],
                    request_digest=decision_data["request_digest"],
                    mission_ref=decision_data["mission_ref"], task_ref=decision_data["task_ref"],
                    status=ApprovalStatus(decision_data["status"]),
                    human_identity=decision_data["human_identity"],
                    decided_at=mc.datetime.fromisoformat(decision_data["decided_at"]),
                    reason=decision_data["reason"]) if decision_data else None)
                restored[canonical_id] = (request, decision, authorizer)
            state.approvals = restored
            state._owner_simulation_context = owner_trusted
        state.execution_records = {task_id: dict(record) for task_id, record in payload.get("execution_records", {}).items()}
        state.execution_intents = {task_id: dict(intent) for task_id, intent in payload.get("execution_intents", {}).items()}
        for task_id, intent in state.execution_intents.items():
            state.execution_records.setdefault(task_id, {"task_id": task_id}).update(intent)
        state.synthetic_acceptance = discovery is not None and (owner_trusted or test_trusted)
        if owner_trusted:
            state._owner_authority = self._owner_simulation_authority
        if state.execution_records and not state.synthetic_acceptance:
            raise mission_conversation.MissionConversationError(
                "Synthetic execution records require trusted authority injection.")
        if state.execution_records:
            store_cache = getattr(mission_conversation, "_acceptance_execution_stores", None)
            execution_store = store_cache.get(str(self._execution_path())) if isinstance(store_cache, dict) else None
            service_cache = getattr(execution_store, "_brain_execution_service_cache", {}) if execution_store else {}
            service = service_cache.get("service")
            if service is None:
                raise mission_conversation.MissionConversationError(
                    "Durable execution authority is unavailable; reconciliation remains fail-closed.")
            for task_id, record in state.execution_records.items():
                if record.get("status") == "authorization_issued":
                    intent = state.execution_intents.get(task_id, {})
                    durable_auth = execution_store.authorization(intent.get("authorization_id", ""))
                    if (not intent.get("authorization_id") or durable_auth is None
                            or durable_auth.get("consumed")):
                        record["status"] = "interrupted"
                        record["reason"] = "durable authorization unavailable or consumed; fail closed and prohibit replay"
                    continue
            for task_id in tuple(state.execution_records):
                if state.execution_records[task_id].get("status") == "authorization_issued":
                    continue
                try:
                    service.lookup(state, task_id)
                except Exception:
                    record = state.execution_records.setdefault(task_id, {"task_id": task_id})
                    record["status"] = "interrupted"
                    record["reason"] = "durable claim unavailable; fail closed and prohibit replay"
        state.synthetic_acceptance = discovery is not None and (owner_trusted or test_trusted)
        return state, key

    def _execution_path(self):
        from pathlib import Path
        if self.data_dir is not None:
            return Path(self.data_dir) / 'mission-execution.sqlite3'
        if self.db_path is not None:
            return Path(self.db_path).with_name('mission-execution.sqlite3')
        from . import studio
        return Path(studio.DATA) / 'mission-execution.sqlite3'

    def save(self, conversation_id, state, request_key):
        payload, key, _ = self.backend.save_mission_state(
            conversation_id, state.to_dict(), request_key,
            data_dir=self.data_dir, db_path=self.db_path)
        return key == request_key

    def create_once(self, conversation_id, state, request_key):
        serialized = state.to_dict()
        synthetic_fixture_discovery = self.synthetic_fixture_discovery
        if synthetic_fixture_discovery is None and getattr(state, "capability_discovery", None) is not None:
            state.capability_discovery = None
            state.synthetic_acceptance = False
            serialized = state.to_dict()
        if synthetic_fixture_discovery is not None:
            service = getattr(synthetic_fixture_discovery, "_acceptance_service", None)
            from .mission_execution import _TRUSTED_ACCEPTANCE_HARNESS
            trusted_test = (callable(getattr(synthetic_fixture_discovery, "_trusted_acceptance_context", None))
                            and synthetic_fixture_discovery._trusted_acceptance_context() is _TRUSTED_ACCEPTANCE_HARNESS
                            and getattr(service, "_MissionExecutionService__trusted_acceptance_context", None)
                            is _TRUSTED_ACCEPTANCE_HARNESS)
            from . import owner_simulation
            owner_callback = getattr(synthetic_fixture_discovery, "_trusted_owner_simulation_context", None)
            owner_authority = owner_callback() if callable(owner_callback) else None
            trusted_owner = (owner_simulation.validate_owner_authority(
                owner_authority, "synthetic.brain-owner-simulation")
                and getattr(service, "_MissionExecutionService__owner_simulation_context", False))
            if not trusted_test and not trusted_owner:
                raise mission_conversation.MissionConversationError(
                    "Mission repository received invalid simulation authority.")
            if trusted_test:
                state._trusted_acceptance_harness = True
                serialized["_trusted_acceptance_harness"] = True
            if trusted_owner:
                context = getattr(synthetic_fixture_discovery, "_trusted_owner_simulation_context", None)
                self._owner_simulation_authority = context() if callable(context) else None
                state._trusted_owner_simulation = True
                state._owner_authority = self._owner_simulation_authority
                serialized["_trusted_owner_simulation"] = True
        if synthetic_fixture_discovery is not None:
            state.synthetic_acceptance = True
        if synthetic_fixture_discovery is not None and getattr(state, "synthetic_acceptance", False):
            self.synthetic_fixture_discovery = synthetic_fixture_discovery
            serialized["plan"]["mission_ref"] = state.plan.mission_id
            serialized["synthetic_acceptance"] = True
            serialized["plan"]["synthetic_acceptance"] = True
            serialized["plan"]["mission_ref"] = state.mission.mission_id
            task_field = ("tasks" if serialized["plan"].get("tasks")
                          else "ordered_tasks")
            for task in serialized["plan"].get(task_field, []):
                task["task_id"] = task["task_id"].replace(
                    state.plan.mission_id, state.mission.mission_id, 1)
        payload, key, inserted = self.backend.save_mission_state(
            conversation_id, serialized, request_key,
            data_dir=self.data_dir, db_path=self.db_path)
        state = mission_conversation.state_from_dict(
            payload, capability_discovery=self.synthetic_fixture_discovery if getattr(
                self.synthetic_fixture_discovery, "_acceptance_service", None) is not None else None)
        state._trusted_acceptance_harness = self._is_trusted_test_context()
        state._trusted_owner_simulation = self._is_trusted_owner_context()
        state._owner_simulation_context = state._trusted_owner_simulation
        if state._trusted_owner_simulation:
            state._owner_authority = self._owner_simulation_authority
        if payload.get("synthetic_acceptance") is True or payload.get("plan", {}).get("synthetic_acceptance") is True:
            from dataclasses import replace
            stored_tasks = payload.get("plan", {}).get("tasks", [])
            task_ids = {item.get("task_id", "").rsplit(".", 1)[-1]: item.get("task_id")
                        for item in stored_tasks if isinstance(item, dict)}
            state.plan = replace(state.plan, mission_id=state.mission.mission_id,
                tasks=tuple(replace(task, task_id=task_ids.get(
                    task.task_id.rsplit(".", 1)[-1], task.task_id)) for task in state.plan.tasks))
        state.execution_records = {task_id: dict(record) for task_id, record in payload.get("execution_records", {}).items()}
        state.execution_intents = {task_id: dict(intent) for task_id, intent in payload.get("execution_intents", {}).items()}
        for task_id, intent in state.execution_intents.items():
            state.execution_records.setdefault(task_id, {"task_id": task_id}).update(intent)
        state.synthetic_acceptance = bool(self.synthetic_fixture_discovery is not None and (
            state._trusted_acceptance_harness or state._trusted_owner_simulation))
        if state.synthetic_acceptance:
            state.capability_discovery = self.synthetic_fixture_discovery
        return state, key, inserted


class MissionController:
    def __init__(self, *, builder: Callable[..., Any], repository: MissionStateRepository,
                 capability_provider: Callable[[], tuple[Mapping[str, Any], str]],
                 summarizer: Callable[[Any], str] = mission_conversation.describe_state):
        self.builder = builder
        self.repository = repository
        self.capability_provider = capability_provider
        self.summarizer = summarizer
        self.synthetic_fixture_discovery = None

    def handle(self, request: MissionRequest) -> MissionControllerResult:
        if (not isinstance(request, MissionRequest) or not isinstance(request.text, str)
                or not request.text.strip() or request.intent != "mission"
                or request.confidence != "explicit"):
            return MissionControllerResult(False, reason="not_applicable")
        if not request.conversation_id or not request.conversation_id.strip():
            return MissionControllerResult(False, reason="missing_conversation")
        if request.inference_mode == "Offline demonstration":
            return MissionControllerResult(True, reason="unavailable_offline")

        request_key = request.text.strip()
        existing = self.repository.load(request.conversation_id)
        if existing is not None:
            state, stored_key = existing
            if stored_key == request_key:
                return MissionControllerResult(True, state=state,
                                               summary=self.summarizer(state), reason="duplicate")
            return MissionControllerResult(False, state=state,
                                           reason="conversation_already_bound")

        report, fingerprint = self.capability_provider()
        try:
            built = self.builder(request.text, report, report_fingerprint=fingerprint,
                                 conversation_id=request.conversation_id)
        except TypeError:
            # SB-35's canonical contract is the deterministic define_mission signature.
            from . import mission_intelligence
            built = mission_intelligence.define_mission(request.text, capability_report=report)
        discovery = self.synthetic_fixture_discovery
        if built is not None and discovery is not None:
            built.capability_discovery = discovery
        if built is None:
            return MissionControllerResult(False, reason="not_a_mission")
        state, stored_key, created = self.repository.create_once(
            request.conversation_id, built, request_key)
        if stored_key != request_key:
            return MissionControllerResult(False, state=state, reason="conversation_already_bound")
        return MissionControllerResult(True, created=created, state=state,
                                       summary=self.summarizer(state),
                                       reason="created" if created else "duplicate")
