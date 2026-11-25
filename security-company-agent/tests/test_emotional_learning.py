from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain_core.emotional_learning import (
    AnalysisAdapter, EpisodeGenerator, ExperienceMemory, InstagramHTMLImporter,
    ImportedMessage, PrivacyProcessor, TrainingCheckpoint, TrainingPaths,
    TrainingRunner, explicit_operator_authorization, learning_record,
    reflection_workflow, score_evaluation, validate_analysis, validate_evaluation_split,
    validate_learning_record, EVALUATION_DIMENSIONS, Episode,
)


def synthetic_html(messages):
    parts = ['<html><script>throw new Error("must not execute")</script><body>']
    for sender, timestamp, text in messages:
        parts.append(f'<div class="pam"><div class="_3-96">{sender}</div>'
                     f'<div class="_3-96 _2pio">{timestamp}</div>'
                     f'<div class="_3-94">{text}</div></div>')
    parts.append('</body></html>')
    return ''.join(parts)


def msg(i, text, *, source='messages.html', speaker='speaker_a', at=None, edited=False):
    return ImportedMessage(f'm{i}', source, speaker, at or f'2026-01-01T00:0{i}:00+00:00', text,
                           ('liked by someone',) if i == 1 else (), edited, False, False, i)


def test_importer_extracts_orders_deduplicates_and_never_executes_markup():
    importer = InstagramHTMLImporter()
    html = synthetic_html([
        ('Alice', 'Jan 02, 2025 3:04 pm', 'I felt hurt &amp; wanted reassurance'),
        ('Bob', 'Jan 02, 2025 3:04 pm', 'I am sorry; I did not understand'),
    ])
    parsed = importer.parse_html(html, source_id='message_1(1).html')
    assert len(parsed) == 2
    assert parsed[0].timestamp == '2025-01-02T15:04:00+00:00'
    assert parsed[0].speaker_id.startswith('speaker_')
    assert 'throw new Error' not in ''.join(m.text for m in parsed)
    reversed_export = importer.parse_html(synthetic_html([
        ('Alice', 'Jan 02, 2025 3:04 pm', 'I felt hurt &amp; wanted reassurance'),
        ('Bob', 'Jan 02, 2025 3:04 pm', 'I am sorry; I did not understand'),
    ]), source_id='message_2(1).html')
    merged = importer.merge([parsed, reversed_export])
    assert len(merged) == 4
    assert sum(bool(item.duplicate_of) for item in merged) == 2
    assert merged[0].timestamp == merged[1].timestamp


def test_import_requires_operator_authorization():
    with pytest.raises(PermissionError):
        explicit_operator_authorization(False)
    with pytest.raises(PermissionError):
        InstagramHTMLImporter().parse_file('not-read.html')


def test_privacy_processor_removes_named_contacts_addresses_and_minimizes_intimate_details():
    source = msg(1, 'Alice emailed alice@example.com and called +1 212 555 0188 from 12 Main Street. We had sex. Love you.')
    cleaned = PrivacyProcessor(['Alice']).process(source)
    assert 'Alice' not in cleaned.text and 'alice@example.com' not in cleaned.text
    assert '212 555 0188' not in cleaned.text and '12 Main Street' not in cleaned.text
    assert 'had sex' not in cleaned.text and 'Participant_1' in cleaned.text
    assert cleaned.reactions == ('[REACTION]',)


def test_episode_generation_candidates_context_overlap_and_record_schema():
    messages = [msg(1, 'I care about you, but I feel hurt.', at='2026-01-01T00:00:00+00:00'),
                msg(2, 'Please do not dismiss my boundary. I need space.', at='2026-01-01T00:02:00+00:00'),
                msg(3, 'I am sorry; can we work this out?', at='2026-01-01T00:04:00+00:00')]
    episodes = EpisodeGenerator(context_budget=128).generate(messages)
    assert episodes and episodes[0].status == 'heuristic_candidate'
    assert {'care', 'distress', 'boundary', 'withdrawal', 'apology', 'reconciliation'} <= set(episodes[0].candidate_signals)
    assert episodes[0].context_window_ids
    record = learning_record(episodes[0], batch_id='synthetic', explicit_emotions=[{'emotion':'hurt', 'evidence_refs':['m1']}],
                             inferred_hypotheses=[{'hypothesis':'may feel dismissed', 'evidence_refs':['m2']}],
                             alternative_interpretations=['need for rest rather than rejection'],
                             missing_information=['what response followed'])
    assert validate_learning_record(record)['uncertainty']['level'] == 'unknown'
    assert record['autobiographical_memory'] is False
    with pytest.raises(ValueError):
        validate_learning_record({**record, 'explicit_emotions':[{'emotion':'anger','intensity':0.8}]})


def test_privacy_quarantine_state_for_insufficiently_deidentified_content():
    processor = PrivacyProcessor()
    message = msg(1, 'My private medical detail about a rare condition and workplace makes me identifiable.')
    cleaned = processor.process(message)
    assert cleaned.text and 'rare condition' in cleaned.text
    assert not hasattr(cleaned, 'quarantined')


def test_analysis_schema_requires_simultaneous_emotions_and_evidence_separation():
    result = {"what_happened":"Two people discussed a boundary.", "trigger":"A request for space.",
              "explicit_communication":["I need space"], "explicit_emotions":["hurt", "care"],
              "inferred_hypotheses":["one person may feel rejected"],
              "alternatives":["the request may concern fatigue"], "context":[],
              "emotion_behaviour_links":["hurt may lead to withdrawal"], "conflicting_needs":["closeness and autonomy"],
              "observed_outcomes":[], "missing_evidence":["later response"], "candidate_learning":[],
              "generalization":[], "uncertainty":"Insufficient evidence to know motive."}
    assert validate_analysis(result)['explicit_emotions'] == ['hurt', 'care']
    with pytest.raises(ValueError):
        validate_analysis({k:v for k,v in result.items() if k != 'uncertainty'})


def test_blind_reflection_then_provisional_annotation_allows_disagreement():
    ep = EpisodeGenerator().generate([msg(1, 'I feel hurt but still care about you.')])[0]
    record = learning_record(ep, batch_id='b')
    base = {"what_happened":"A person expressed hurt and care.", "trigger":"Unknown.",
            "explicit_communication":["hurt and care"], "explicit_emotions":["hurt", "care"],
            "inferred_hypotheses":["may want reassurance"], "alternatives":["may be seeking space"],
            "context":[], "emotion_behaviour_links":[], "conflicting_needs":[], "observed_outcomes":[],
            "missing_evidence":["response"], "candidate_learning":["Ask rather than assume."],
            "generalization":["Check context before responding."], "uncertainty":"Motive unknown."}
    adapter = AnalysisAdapter(mock=lambda payload: base)
    annotation = {**base, 'inferred_hypotheses':['may be setting a boundary'],
                  'candidate_learning':['Respect a stated boundary.']}
    with pytest.raises(PermissionError):
        reflection_workflow(record, annotation, adapter, autonomous_low_risk=True)
    reflection = reflection_workflow(record, annotation, adapter)
    assert adapter.calls == 1 and reflection['annotation_is_ground_truth'] is False
    assert reflection['disagreements'] and reflection['requires_human_review'] is True


def test_paths_refuse_relative_and_symlink_escape_and_do_not_initialize_memory():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir()
        paths = TrainingPaths(root); layout = paths.prepare_layout()
        assert Path(layout['database']).is_relative_to(root) and not Path(layout['database']).exists()
        with pytest.raises(ValueError): TrainingPaths('relative-training-root')
        external = Path(temp) / 'external'; external.mkdir()
        import shutil
        shutil.rmtree(root / 'memory'); _make_junction(root / 'memory', external)
        try:
            with pytest.raises(ValueError): paths.path('database', create_parent=True)
        finally: _remove_junction(root / 'memory')


def _make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True, check=False)
    if result.returncode: pytest.skip(f'Windows junction creation unavailable: {result.stderr.strip()}')


def _remove_junction(link: Path) -> None:
    subprocess.run(['cmd.exe', '/d', '/c', 'rmdir', str(link)], capture_output=True, text=True, check=False)


def test_windows_direct_and_nested_junctions_are_rejected_for_private_targets():
    if os.name != 'nt': pytest.skip('Windows junction protection test')
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp); root = base / 'private'; outside = base / 'outside'
        root.mkdir(); outside.mkdir(); (outside / 'sentinel.txt').write_text('synthetic')
        paths = TrainingPaths(root)
        for name, path_key in [('memory', 'database'), ('processed', 'episodes'), ('checkpoints', 'checkpoints'),
                               ('evaluation', 'baseline'), ('reports', 'diagnostics'), ('input', 'input')]:
            junction = root / name; _make_junction(junction, outside)
            try:
                with pytest.raises(ValueError): paths.path(path_key, create_parent=True)
                assert (outside / 'sentinel.txt').read_text() == 'synthetic'
            finally: _remove_junction(junction)
        memory = root / 'memory'; memory.mkdir(); nested = memory / 'nested'; _make_junction(nested, outside)
        try:
            with pytest.raises(ValueError): paths.path('database', create_parent=True)
        finally: _remove_junction(nested)


def test_local_runner_sequential_checkpoint_and_isolated_consolidation(monkeypatch):
    import brain_core.model_api as model_api
    monkeypatch.setattr(model_api, 'available_memory_gib', lambda: 8.0)
    result = {"what_happened":"Two people discussed a request.", "trigger":"A need was raised.",
              "explicit_communication":["I need space"], "explicit_emotions":["hurt", "care"],
              "inferred_hypotheses":["may feel overwhelmed"], "alternatives":["may be tired"],
              "context":[], "emotion_behaviour_links":["stress may lead to withdrawal"],
              "conflicting_needs":["closeness and autonomy"], "observed_outcomes":[],
              "missing_evidence":["later response"], "candidate_learning":["Ask before assuming motive."],
              "generalization":["Respect stated boundaries."], "uncertainty":"Evidence is adequate for this low-risk lesson."}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root)
        adapter = AnalysisAdapter(provider='ollama', authorized=True)
        adapter.analyze = lambda record: (setattr(adapter, 'calls', adapter.calls + 1) or result)
        runner = TrainingRunner(paths, mode='local', adapter=adapter, authorized=True)
        episode = Episode('pilot-1', [{'source_id':'seed-1','message_id':'m1','speaker_id':'speaker_1'}],
                          [{'speaker_id':'speaker_1','timestamp':None,'text':'I feel hurt but care.',
                            'reactions':[],'edited':False,'has_attachment':False,'attachment_missing':False}], None, None, [])
        report = runner.run_local([episode], {'pilot-1':result}, batch_id='synthetic-pilot', max_episodes=1)
        assert report['inference_calls'] == 8  # baseline, reflection analysis, and three paired holdouts
        assert report['model'] == 'qwen3:0.6b' and report['consolidated_lessons'] == 1
        assert report['holdout_scenarios'] == 3
        assert Path(report['curiosity_artifact']).exists()
        assert ExperienceMemory(paths).retrieve('assuming motive')
        resumed = runner.run_local([episode], {'pilot-1':result}, batch_id='synthetic-pilot', max_episodes=1)
        assert resumed['episodes_analyzed'] == 1
    assert resumed['outcome'] == 'RESUMED_COMPLETE'


def test_runner_local_requires_operator_authorization_and_exact_model():
    from brain_core.model_policy import DEFAULT_LOCAL_MODEL
    assert DEFAULT_LOCAL_MODEL == 'qwen3:0.6b'
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root)
        adapter = AnalysisAdapter(provider='ollama')
        valid = learning_record(EpisodeGenerator().generate([msg(1, 'I care.')])[0], batch_id='auth-test')
        with pytest.raises(PermissionError): adapter.analyze(valid)
        adapter = AnalysisAdapter(provider='ollama', authorized=True)
        with pytest.raises(PermissionError): TrainingRunner(paths, mode='local', adapter=adapter, authorized=False)
        with pytest.raises(ValueError): AnalysisAdapter(provider='ollama', endpoint='http://example.com:11434/v1')
        with pytest.raises(ValueError): TrainingRunner(paths, mode='synthetic', adapter=adapter, authorized=True)
        assert adapter.provider == 'ollama' and adapter.endpoint == 'http://127.0.0.1:11434/v1'


def test_input_access_is_limited_to_private_input_folder():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root); paths.path('input').mkdir(parents=True)
        approved = paths.path('input') / 'synthetic.html'; approved.write_text('<html>synthetic</html>')
        assert InstagramHTMLImporter().parse_file(approved, authorized=True, paths=paths) == []
        outside = Path(temp) / 'outside.html'; outside.write_text('<html>synthetic</html>')
        with pytest.raises(ValueError): InstagramHTMLImporter().parse_file(outside, authorized=True, paths=paths)


def test_memory_approval_idempotence_rollback_and_source_deletion():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root); memory = ExperienceMemory(paths)
        ep = EpisodeGenerator().generate([msg(1, 'I feel hurt but care.')])[0]
        record = learning_record(ep, batch_id='batch-x', candidate_lessons=[{'principle':'Ask before assuming hurt means rejection.'}])
        assert memory.put(record) == ep.episode_id and memory.put(record) == ep.episode_id
        with pytest.raises(PermissionError): memory.consolidate(ep.episode_id)
        with pytest.raises(PermissionError): memory.set_state(ep.episode_id, 'approved', autonomous_training=True)
        with pytest.raises(PermissionError): memory.consolidate(ep.episode_id, autonomous_training=True)
        memory.set_state(ep.episode_id, 'approved', human_reviewed=True)
        assert memory.consolidate(ep.episode_id, human_reviewed=True) is True
        assert memory.consolidate(ep.episode_id, human_reviewed=True) is False
        assert memory.retrieve('assuming rejection')
        assert memory.rollback_batch('batch-x') == 1 and memory.retrieve('assuming rejection') == []
        assert memory.put(record) == ep.episode_id and memory.delete_source('messages.html') == 1


def test_checkpoint_resume_and_sequential_synthetic_runner():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root)
        report = TrainingRunner(paths, mode='synthetic').run_synthetic([msg(1,'I am sorry; I care.')], batch_id='run1')
        assert TrainingCheckpoint.load(report['checkpoint'], paths).completed_episode_ids
        assert report['inference_calls'] == 0 and not report['external_providers']
        assert not paths.path('personality').exists()


def test_evaluation_split_blocks_episode_and_source_leakage_and_requires_human_evidence():
    train = [{'episode_id':'e1','source_references':[{'source_id':'conversation-a'}]}]
    holdout = [{'episode_id':'e2','source_references':[{'source_id':'conversation-b'}]}]
    validate_evaluation_split(train, holdout)
    with pytest.raises(ValueError): validate_evaluation_split(train, [{'episode_id':'e3','source_references':[{'source_id':'conversation-a'}]}])
    scores = {name: 3 for name in EVALUATION_DIMENSIONS}
    evidence = {name:'Synthetic reviewer cites observable evidence.' for name in EVALUATION_DIMENSIONS}
    assert score_evaluation(scores, evaluator_id='human-1', evidence=evidence)['mean'] == 3
    with pytest.raises(ValueError): score_evaluation(scores, evaluator_id='', evidence=evidence)


def test_runner_and_adapter_exclude_external_providers():
    with pytest.raises(ValueError): AnalysisAdapter(provider='openai')
    with pytest.raises(ValueError): AnalysisAdapter(provider='ollama', endpoint='https://example.com/v1')
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir()
        with pytest.raises(ValueError): TrainingRunner(TrainingPaths(root), mode='synthetic', adapter=AnalysisAdapter(provider='ollama', authorized=True))


def test_seed_jsonl_import_supports_both_structured_and_narrative_schemas():
    processor = PrivacyProcessor()
    legacy = {'episode_id':'seed-1','messages':[{'speaker_id':'speaker_1','text':'I felt hurt.'}],
              'annotation':{'candidate_learning':['Ask first.']}}
    narrative = {'id':'SB-EM-001','schema_version':1,'source':{'source_id':'curated'},'title':'A narrative',
                 'observed_sequence':'A person felt uncertain when plans changed.',
                 'emotion_hypotheses':['uncertainty'], 'annotation_status':'provisional',
                 'lesson_candidate':'Ask what changed before assuming intent.',
                 'transfer_question':'How might this apply at work?', 'learning_scope':'general',
                 'outcome_status':'not_observed'}
    parsed = processor.process_seed_jsonl('\n'.join(json.dumps(x) for x in (legacy, narrative)))
    assert parsed[0]['episode_id'] == 'seed-1'
    assert parsed[1]['format'] == 'narrative_v1' and parsed[1]['observed_sequence'] == narrative['observed_sequence']
    with pytest.raises(ValueError): processor.process_seed_jsonl(json.dumps({**narrative, 'schema_version':99}))


def test_seed_loader_preserves_narrative_without_inventing_dialogue_or_outcomes():
    from sb_t01_train import _load_seed
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / 'private'; root.mkdir(); paths = TrainingPaths(root); paths.path('input').mkdir()
        item = {'id':'SB-EM-001','schema_version':1,'source':'curated-source','title':'Changed plans',
                'observed_sequence':'A person waited; plans changed without explanation.',
                'emotion_hypotheses':['uncertainty'], 'annotation_status':'provisional',
                'lesson_candidate':'Ask for context before inferring rejection.', 'transfer_question':'What other explanation fits?',
                'learning_scope':'general', 'outcome_status':'not_observed'}
        (paths.path('input')/'seed.jsonl').write_text(json.dumps(item))
        episodes, annotations = _load_seed(paths, 'seed.jsonl', 1, {'SB-EM-001'})
        assert episodes[0].episode_id == 'SB-EM-001'
        assert episodes[0].narrative_experience['observed_sequence'] == item['observed_sequence']
        assert episodes[0].narrative_experience['lesson_candidate'] == item['lesson_candidate']
        record = learning_record(episodes[0], batch_id='fixture')
        payload = json.dumps(record)
        assert record['experience_type'] == 'narrative_observation' and record['chronological_interaction'] == []
        assert item['observed_sequence'] in payload and 'outcome_status' in payload
        assert 'timestamp' not in record['narrative_observation']
        assert annotations['SB-EM-001']['observed_outcomes'] == []
