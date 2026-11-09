"""Streamlit-independent mission routing and state management."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from . import mission_conversation


class MissionStateRepository(Protocol):
    def load(self, conversation_id: str) -> tuple[dict[str, Any], str] | None: ...
    def create_once(self, conversation_id: str, state: dict[str, Any], request_key: str): ...


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
    def __init__(self, backend, *, data_dir=None, db_path=None):
        self.backend = backend
        self.data_dir = data_dir
        self.db_path = db_path

    def load(self, conversation_id):
        row = self.backend.load_mission_state(
            conversation_id, data_dir=self.data_dir, db_path=self.db_path)
        if row is None:
            return None
        payload, key = row
        return mission_conversation.state_from_dict(payload), key

    def create_once(self, conversation_id, state, request_key):
        payload, key, inserted = self.backend.save_mission_state(
            conversation_id, state.to_dict(), request_key,
            data_dir=self.data_dir, db_path=self.db_path)
        return mission_conversation.state_from_dict(payload), key, inserted


class MissionController:
    def __init__(self, *, builder: Callable[..., Any], repository: MissionStateRepository,
                 capability_provider: Callable[[], tuple[Mapping[str, Any], str]],
                 summarizer: Callable[[Any], str] = mission_conversation.describe_state):
        self.builder = builder
        self.repository = repository
        self.capability_provider = capability_provider
        self.summarizer = summarizer

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
        if built is None:
            return MissionControllerResult(False, reason="not_a_mission")
        state, stored_key, created = self.repository.create_once(
            request.conversation_id, built, request_key)
        if stored_key != request_key:
            return MissionControllerResult(False, state=state, reason="conversation_already_bound")
        return MissionControllerResult(True, created=created, state=state,
                                       summary=self.summarizer(state),
                                       reason="created" if created else "duplicate")
