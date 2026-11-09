"""End-to-end offline Streamlit chat coverage for capability notices."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import requests
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from brain_core import capability_communication as communication
from brain_core import conversation
from capability_report_fixtures import valid_report


def report_file(tmp_path: Path, *, changed: bool = False, invalid: bool = False) -> Path:
    report = json.loads(json.dumps(valid_report()))
    if changed:
        target = report["capability_states"][0]
        agent = next(item for item in report["agents"] if item["identity"] == target["agent"])
        capability = next(item for item in agent["capabilities"]
                          if item["capability"] == target["capability"])
        target["state"] = "verified"
        target["reason"] = "Static metadata evidence verified; execution remains unavailable."
        capability["status"] = "verified"
        capability["uncertainty_reason"] = None
    if invalid:
        report["execution_verified"] = True
    path = tmp_path / ("changed.json" if changed else "invalid.json" if invalid else "baseline.json")
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def app_source(tmp_path: Path, capability_report: Path) -> str:
    source = (ROOT / "brain_studio.py").read_text(encoding="utf-8")
    observe = source.index("def _observe_capability_update_for_chat():")
    render = source.index("def render_conversation(", observe)
    admin = source.index("def render_admin(", render)
    save_response = source.index("\ndef _save_response(", observe, render) + 1
    handler = source.index("\ndef _handle_message(", save_response) + 1
    handler_end = source.index("\ndef render_conversation(", handler) + 1
    render_source = source[render:admin]
    selector = "key='conversation_selector', on_change=select_conversation)"
    render_source = render_source.replace(
        selector, selector + "\n    if False:\n        pass")
    render_source = render_source.replace("if st.button('＋ New conversation'):", "if False:")
    production_functions = (source[observe:save_response]
                            + source[save_response:handler_end]
                            + render_source)
    startup = f'''import json, os, re, uuid
from pathlib import Path
import streamlit as st
from brain_core import conversation, lessons, studio, trainer, personality, service
from brain_core.appraisal import NEUTRAL
from brain_core.model_api import ModelConfig, ModelError, ModelOutputError
from brain_core.runtime_guard import available_memory_gib
from brain_core.decision import emotional_transition

REPORT = {str(capability_report)!r}
DATA = Path({str(tmp_path / "data")!r})
DB = Path({str(tmp_path / "brain.sqlite3")!r})
studio.DATA = DATA
studio.DB = DB
os.environ['SOVEREIGN_BRAIN_CAPABILITY_REPORT'] = REPORT
st.session_state.setdefault('studio_inference', 'Offline demonstration')
profile = dict(personality.DEFAULT)
model_config = None
configuration_error = None
ready = False

def _run_general_answer(*args, **kwargs):
    st.info('Offline general response fixture; no model was called.')

def _render_cloud_approval(*args, **kwargs): return None
def _render_candidate_learning_review(*args, **kwargs): return None
def _render_conversation_research_approval(*args, **kwargs): return None
def _show_research_citations(*args, **kwargs): return None
def _research_relevant(*args, **kwargs): return False
def _prepare_conversation_research(*args, **kwargs): return None

'''
    end = "\nrender_conversation(profile, ready, model_config, configuration_error)\n"
    return startup + production_functions + end


def run_app(tmp_path: Path, report: Path, *, session: AppTest | None = None) -> AppTest:
    app = tmp_path / "offline_chat_app.py"
    app.write_text(app_source(tmp_path, report), encoding="utf-8")
    return (session or AppTest.from_file(str(app), default_timeout=20)).run()


def messages(app: AppTest) -> list[str]:
    return [item.value for item in app.markdown]


def text_elements(app: AppTest) -> list[str]:
    return [str(item.value) for item in app.caption] + messages(app)


def conversation_history(data_dir: Path, db_path: Path) -> list[dict]:
    sessions = conversation.list_sessions(data_dir=data_dir, db_path=db_path)
    assert sessions
    return conversation.load_messages(sessions[0]["conversation_id"], data_dir=data_dir, db_path=db_path)


def establish_distinct_baseline(tmp_path: Path, changed_report: dict) -> Path:
    baseline = json.loads(json.dumps(changed_report))
    changed_state = baseline["capability_states"][0]
    agent = next(item for item in baseline["agents"] if item["identity"] == changed_state["agent"])
    capability = next(item for item in agent["capabilities"]
                      if item["capability"] == changed_state["capability"])
    changed_state["state"] = "understood"
    changed_state["reason"] = "Static metadata is understood; execution remains unavailable."
    capability["status"] = "understood"
    capability["uncertainty_reason"] = "Execution remains unavailable."
    path = tmp_path / "baseline-chat.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return path


def test_capability_change_is_announced_in_offline_chat_and_not_duplicated(tmp_path):
    changed = report_file(tmp_path, changed=True)
    baseline = establish_distinct_baseline(tmp_path, json.loads(changed.read_text()))
    app = run_app(tmp_path, baseline)
    assert not app.exception
    store = communication.CapabilityUpdateStore(communication.store_path(tmp_path / "data"))
    assert store.baseline() is not None
    app = run_app(tmp_path, changed, session=app)
    assert not app.exception
    app.chat_input[0].set_value("Hello, how are you?").run()
    assert not app.exception
    assert app.session_state["studio_inference"] == "Offline demonstration"
    announced = [item for item in messages(app) if "Capability awareness update:" in item]
    assert len(announced) == 2
    assert announced[0] == announced[1]
    assert "does not establish execution readiness" in announced[0]
    assert store.latest()["status"] == "communicated"
    history = conversation_history(tmp_path / "data", tmp_path / "brain.sqlite3")
    assert len([item for item in history if item["message_type"] == "capability_update_notice"]) == 1
    app.run()
    assert len([item for item in messages(app) if "Capability awareness update:" in item]) == 2
    assert app.session_state["_capability_notices_rendered"]


def test_restart_preserves_notice_and_explicit_acknowledgement_updates_state(tmp_path):
    changed = report_file(tmp_path, changed=True)
    baseline = establish_distinct_baseline(tmp_path, json.loads(changed.read_text()))
    app = run_app(tmp_path, baseline)
    app = run_app(tmp_path, changed, session=app)
    app.chat_input[0].set_value("Hello").run()
    assert not app.exception
    store = communication.CapabilityUpdateStore(communication.store_path(tmp_path / "data"))
    notice = store.latest()
    assert notice and notice["status"] == "communicated"
    restarted = run_app(tmp_path, changed, session=app)
    assert not restarted.exception
    assert notice["message"] in messages(restarted)
    assert store.latest()["status"] == "communicated"
    assert conversation.classify_intent("I acknowledge the capability update").intent == "capability_acknowledgement"
    restarted.chat_input[0].set_value("I acknowledge the capability update").run()
    assert not restarted.exception
    assert store.latest()["status"] == "acknowledged"
    assert any(item["message_type"] == "capability_update_acknowledged"
               and "Acknowledged." in item["content"]
               for item in conversation_history(tmp_path / "data", tmp_path / "brain.sqlite3"))


def test_untrusted_report_creates_no_notice_and_chat_still_loads(tmp_path):
    invalid = report_file(tmp_path, invalid=True)
    app = run_app(tmp_path, invalid)
    assert not app.exception
    store = communication.CapabilityUpdateStore(communication.store_path(tmp_path / "data"))
    assert store.pending() is None
    assert store.baseline() is None
    assert not any("Capability awareness update:" in item for item in messages(app))


def test_initial_baseline_does_not_announce_discovery(tmp_path):
    app = run_app(tmp_path, report_file(tmp_path))
    assert not app.exception
    store = communication.CapabilityUpdateStore(communication.store_path(tmp_path / "data"))
    assert store.latest() is None
    assert store.baseline() is not None
    assert not any("Capability awareness update:" in item for item in messages(app))


def test_routing_and_external_side_effect_guards(tmp_path):
    changed = report_file(tmp_path, changed=True)
    baseline = establish_distinct_baseline(tmp_path, json.loads(changed.read_text()))
    protected = [ROOT / "brain_core" / "personality.json", ROOT / "brain_core" / "personality.identity"]
    before = {path: path.read_bytes() if path.exists() else None for path in protected}
    with patch.object(requests, "post", side_effect=AssertionError("network prohibited")), \
            patch.object(requests, "get", side_effect=AssertionError("network prohibited")):
        app = run_app(tmp_path, baseline)
        app = run_app(tmp_path, changed, session=app)
        app.chat_input[0].set_value("What agents do you know?").run()
        assert not app.exception
        assert any("Capability awareness update:" in item for item in messages(app))
        assert any("specialist agents" in item or "declared capabilities" in item for item in messages(app))
    assert before == {path: path.read_bytes() if path.exists() else None for path in protected}
    assert not list((tmp_path / "data").glob("*.json"))
    history = conversation_history(tmp_path / "data", tmp_path / "brain.sqlite3")
    assert any(item["message_type"] == "agent_awareness" for item in history)
    assert not any(item["message_type"] == "mission" for item in history)


def test_published_but_undeployed_description_and_initial_report_contract(tmp_path):
    from test_capability_communication import normalize_test_report
    report = normalize_test_report(valid_report())
    runtime = {
        "assessments": [{"agent_id": "agent.one", "capability": "do-task", "state": "NOT_DEPLOYED",
            "published": True, "installed": False, "loaded": False, "verified": False,
            "execution_eligible": False, "required_permissions": [], "runtime_permissions": [],
            "reasons": ["not installed"], "verification_required": [], "published_revision": "a" * 40,
            "installed_revision": None, "software_revision": None, "execution_authorized": False}],
        "summary": "Published, not installed", "execution_authorized": False,
        "runtime_available_claimed": False,
    }
    assert communication.meaningful_change(None, report) is None
    prior = {"_accepted_report": report, "_runtime_evidence": {
        **runtime, "assessments": [{**runtime["assessments"][0], "state": "UNKNOWN",
                                     "published": False, "published_revision": None}]}}
    message = communication.meaningful_change(prior, report, runtime_evidence=runtime)
    assert message and "0 capabilities verified available" in message
    assert "permission" in message
