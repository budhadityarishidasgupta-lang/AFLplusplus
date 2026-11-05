import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain_core import agent_awareness, conversation
from control_plane.agent_discovery import AgentDiscovery
from control_plane.capability_verification import verification_report


def valid_report():
    discovery = AgentDiscovery()
    report = verification_report(discovery, workspace=ROOT.parent.parent)
    for state in report['capability_states']:
        if state['reason'] is None:
            state['reason'] = 'Static metadata evidence verified; execution integration remains unavailable.'
    state_lookup = {(state['agent'], state['capability']): state['state'] for state in report['capability_states']}
    for item in report['agents']:
        for capability in item['capabilities']:
            capability['status'] = state_lookup[(item['identity'], capability['capability'])]
        item['purpose'] = {
            'multi-agent.local-recon': 'Inspect local Python source and create a fuzzing plan.',
            'multi-agent.evolutionary-exploit-dev': 'Generate and mutate payloads in a local simulated workflow.',
            'enterprise-range.system-architect': 'Coordinate bounded campaign state and approval gates.',
        }[item['identity']]
    verified_state = next((state for state in report['capability_states'] if state['state'] == 'verified'), None)
    if verified_state:
        verified_state['reason'] = 'Static metadata evidence verified; execution integration remains unavailable.'
    report['capability_states'] = [
        {**state, 'readiness': False} for state in report['capability_states']
    ]
    return report


def test_reads_valid_capability_report_mapping_and_file(tmp_path):
    source = valid_report()
    normalized = agent_awareness.read_capability_report(source)
    path = tmp_path / 'capabilities.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    assert agent_awareness.read_capability_report(path) == normalized
    assert len(normalized['agents']) == 3
    assert all(not agent['ready'] for agent in normalized['agents'])


@pytest.mark.parametrize('mutate', [
    lambda report: report.update(schema_version=9),
    lambda report: report.update(execution_verified=True),
    lambda report: report.update(permissions_granted=True),
    lambda report: report.update(unexpected='data'),
    lambda report: report.update(agents=[{'identity': 'unsafe', 'source_hash': 'secret'}]),
])
def test_rejects_malformed_or_untrusted_report(mutate):
    report = valid_report()
    mutate(report)
    with pytest.raises(agent_awareness.CapabilityReportError):
        agent_awareness.read_capability_report(report)


def test_rejects_command_readiness_claim_even_with_static_verified_status():
    report = valid_report()
    report['agents'][0]['execution_readiness']['ready'] = True
    with pytest.raises(agent_awareness.CapabilityReportError, match='command readiness'):
        agent_awareness.read_capability_report(report)
    report = valid_report()
    report['capability_states'][0]['readiness'] = True
    with pytest.raises(agent_awareness.CapabilityReportError, match='command readiness'):
        agent_awareness.read_capability_report(report)


@pytest.mark.parametrize('question', [
    'Which agents do you know?',
    'What can my agents do?',
    'Can you command the security agents?',
    'Which capabilities are missing?',
    'What have you learned about your agents?',
])
def test_awareness_questions_route_to_read_only_intent(question):
    assert conversation.classify_intent(question).intent == 'agent_awareness'


def test_natural_question_variants_route_to_agent_awareness():
    questions = (
        'What have you learned about your agents?',
        'What have you learned about my agents?',
        'Tell me what agents you know.',
        'What capabilities are still missing?',
    )
    assert all(conversation.classify_intent(question).intent == 'agent_awareness' for question in questions)


def test_answers_use_verified_data_without_internal_source_details():
    report = valid_report()
    response = agent_awareness.answer_agent_question('What can my agents do?', report)
    assert 'declared capabilities' in response
    assert 'static verification' in response
    assert "haven't verified that I can command them" in response
    assert 'SHA-256' not in response
    assert 'multi_agent_system' not in response


def test_known_agent_answer_is_grounded_and_not_a_claim_of_authority():
    response = agent_awareness.answer_agent_question('Which agents do you know?', valid_report())
    assert '3 specialist agents' in response
    assert 'local Python source' in response
    command = agent_awareness.answer_agent_question('Can you command the security agents?', valid_report())
    assert command.startswith('No.')
    assert 'does not grant permission' in command


def test_missing_capabilities_are_reported_conservatively():
    report = valid_report()
    report['unsupported_capabilities'] = [{
        'capability': 'remote-shell-control', 'status': 'unknown',
        'message': 'Capability support cannot be determined from inspected source.',
    }]
    response = agent_awareness.answer_agent_question('Which capabilities are missing?', report)
    assert 'remote shell control' in response


def test_change_detection_distinguishes_discovery_from_static_verification():
    original = valid_report()
    first = agent_awareness.capability_change(None, original)
    assert first and 'identified' in first and 'static evidence only' in first
    assert agent_awareness.capability_change(original, original) is None
    changed = json.loads(json.dumps(original))
    changed_state = next(state for state in changed['capability_states'] if state['state'] != 'verified')
    changed_state['state'] = 'verified'
    changed_state['reason'] = 'Static metadata evidence verified; execution integration remains unavailable.'
    state = changed_state
    for agent in changed['agents']:
        if agent['identity'] == state['agent']:
            for capability in agent['capabilities']:
                if capability['capability'] == state['capability']:
                    capability['status'] = 'unknown'
    update = agent_awareness.capability_change(original, changed)
    assert update and 'static checks now verify' in update
    assert 'does not establish execution readiness' in update


def test_duplicate_announcement_fingerprint_stable_across_serialization():
    first = valid_report()
    second = json.loads(json.dumps(first))
    assert agent_awareness.report_fingerprint(first) == agent_awareness.report_fingerprint(second)


def test_awareness_does_not_touch_private_memory_or_execute_agents(tmp_path, monkeypatch):
    database = tmp_path / 'brain.sqlite3'
    database.write_bytes(b'private-memory-sentinel')
    before = database.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError('agent execution or memory writes are forbidden')

    monkeypatch.setattr(conversation, 'save_message', forbidden)
    monkeypatch.setattr(conversation, 'save_lesson_state', forbidden)
    monkeypatch.setattr(conversation, 'delete_session', forbidden)
    monkeypatch.setattr(agent_awareness, 'execute_agent', forbidden, raising=False)
    response = conversation.respond_agent_awareness('Can you command the security agents?', valid_report())
    assert response.startswith('No.')
    assert database.read_bytes() == before


def test_report_file_errors_fail_closed(tmp_path):
    with pytest.raises(agent_awareness.CapabilityReportError):
        agent_awareness.read_capability_report(tmp_path / 'absent.json')
    oversized = tmp_path / 'large.json'
    oversized.write_bytes(b' ' * (1_000_001))
    with pytest.raises(agent_awareness.CapabilityReportError):
        agent_awareness.read_capability_report(oversized)


def test_existing_conversation_intents_and_general_responses_compatible():
    assert conversation.classify_intent('Hello, how are you?').intent == 'general'
    assert conversation.classify_intent('Show my approved lessons.').intent == 'memory_recall'
    assert conversation.classify_intent('A friend asked what I learned about them.').intent == 'emotional_reasoning'
    assert conversation.classify_intent('What have you learned about your agents?').intent == 'agent_awareness'
    assert conversation.classify_intent('Which agents or capabilities are missing?').intent == 'agent_awareness'
    reply = conversation.respond_general('Hello', {'identity_name': 'Sovereign Brain'})
    assert reply.startswith('Hello. I am Sovereign Brain.')
