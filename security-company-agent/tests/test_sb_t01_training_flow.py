import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain_core.emotional_learning import (
    AnalysisAdapter, Episode, ExperienceMemory, TrainingCheckpoint,
    TrainingPaths, TrainingRunner,
)
from sb_t01_train import _load_seed


RESULT = {
    "what_happened": "Synthetic people discuss an unclear plan and a request for time.",
    "trigger": "A change in the plan.",
    "explicit_communication": ["A asks what changed."],
    "explicit_emotions": ["uncertainty"],
    "inferred_hypotheses": ["A may want clarity."],
    "alternatives": ["There may be an unrelated constraint."],
    "context": ["The outcome is not known."],
    "emotion_behaviour_links": ["Uncertainty prompts a clarification request."],
    "conflicting_needs": ["Clarity and time."],
    "observed_outcomes": [],
    "missing_evidence": ["What happened afterward."],
    "candidate_learning": ["Ask what changed before inferring intent."],
    "generalization": ["Check missing context in other ambiguous situations."],
    "uncertainty": "The later outcome is unknown.",
}


def seed_row(episode_id):
    return {
        "id": episode_id, "schema_version": "1.0", "source": "synthetic-source",
        "title": "Synthetic plan change",
        "observed_sequence": "A asks for clarity after a plan changes; what happened afterward is unknown.",
        "emotion_hypotheses": {"A": "uncertainty"},
        "annotation_status": "provisional", "lesson_candidate": "Ask for context before assuming intent.",
        "transfer_question": "What other context might explain a change?", "learning_scope": "synthetic",
        "outcome_status": "not_observed",
    }


def make_episode(episode_id="SB-EM-001"):
    return Episode(episode_id, [{"source_id": "synthetic", "record_id": episode_id}], [], None, None, [],
                   narrative_experience={
                       "experience_type": "narrative_observation", "title": "synthetic",
                       "observed_sequence": "A asks for clarity after a plan changes; what happened afterward is unknown.",
                       "emotion_hypotheses": {"A": "uncertainty"},
                       "lesson_candidate": "Ask for context before assuming intent.",
                       "transfer_question": "What other context might explain a change?",
                       "learning_scope": "synthetic", "outcome_status": "not_observed",
                   })


def runner_for(paths, callback, validate=True):
    import brain_core.model_api as model_api
    model_api.available_memory_gib = lambda: 8.0
    adapter = AnalysisAdapter(provider="ollama", authorized=True)
    def analyze(record):
        adapter.calls += 1
        result = callback(record)
        if validate:
            from brain_core.emotional_learning import validate_analysis
            return validate_analysis(result)
        return result
    adapter.analyze = analyze
    return TrainingRunner(paths, mode="local", adapter=adapter, authorized=True)


def test_selected_seed_ids_and_narrative_normalization(tmp_path):
    paths = TrainingPaths(tmp_path)
    paths.path("input").mkdir()
    records = [seed_row(f"SB-EM-{number:03d}") for number in range(1, 12)]
    (paths.path("input") / "seed.jsonl").write_text("\n".join(map(json.dumps, records)), encoding="utf-8")
    episodes, annotations = _load_seed(paths, "seed.jsonl", 3, {"SB-EM-001", "SB-EM-004", "SB-EM-010"})
    assert [episode.episode_id for episode in episodes] == ["SB-EM-001", "SB-EM-004", "SB-EM-010"]
    assert all(episode.narrative_experience["observed_sequence"] for episode in episodes)
    assert set(annotations) == {"SB-EM-001", "SB-EM-004", "SB-EM-010"}


def test_complete_learning_loop_and_retrieval(tmp_path):
    paths = TrainingPaths(tmp_path)
    episode = make_episode()
    runner = runner_for(paths, lambda _: RESULT)
    report = runner.run_local([episode], {episode.episode_id: RESULT}, batch_id="synthetic-loop", max_episodes=1)
    assert report["outcome"] == "COMPLETED"
    assert report["episodes_analyzed"] == 1
    assert report["inference_counts"]["baseline"]["successful"] == 1
    assert report["inference_counts"]["reflection"]["successful"] == 1
    assert report["inference_counts"]["post_training_evaluation"]["successful"] == 6
    assert report["candidate_lessons"] >= 1
    assert report["candidate_lessons"] == 1
    assert ExperienceMemory(paths).retrieve("uncertainty") == []
    stored = ExperienceMemory(paths).list_records()
    assert stored and stored[0]["candidate_generalized_lessons"]
    checkpoint = TrainingCheckpoint.load(report["checkpoint"], paths)
    assert checkpoint.completed_stages[episode.episode_id]
    resumed = runner.run_local([episode], {episode.episode_id: RESULT}, batch_id="synthetic-loop", max_episodes=1)
    assert resumed["outcome"] == "RESUMED_COMPLETE"
    assert resumed["episodes_analyzed"] == 1
    assert resumed["inference_calls"] == 0


def test_legacy_attempt_checkpoint_is_not_trusted_and_runner_runs(tmp_path):
    paths = TrainingPaths(tmp_path)
    checkpoint_dir = paths.path("checkpoints")
    checkpoint_dir.mkdir(parents=True)
    checkpoint_path = checkpoint_dir / "synthetic-resume.json"
    checkpoint_path.write_text(json.dumps({"version": 1, "batch_id": "synthetic-resume",
                                          "phase": "evaluation_complete",
                                          "completed_episode_ids": ["SB-EM-001"],
                                          "failed_episode_ids": [], "updated_at": "2026-01-01T00:00:00+00:00"}))
    runner = runner_for(paths, lambda _: RESULT)
    episode = make_episode("SB-EM-001")
    episode.messages = [{"speaker_id": "speaker_synthetic", "timestamp": None,
                         "text": "A asks for clarity after a plan changes.", "reactions": [],
                         "edited": False, "has_attachment": False, "attachment_missing": False}]
    report = runner.run_local([episode], {"SB-EM-001": RESULT}, batch_id="synthetic-resume", max_episodes=1)
    assert report["outcome"] == "COMPLETED"
    assert report["episodes_analyzed"] == 1


def test_zero_and_ineligible_episodes_report_no_training(tmp_path):
    paths = TrainingPaths(tmp_path)
    paths.prepare_layout()
    runner = runner_for(paths, lambda _: RESULT)
    assert runner.run_local([], {}, batch_id="empty-run")["outcome"] == "NO_TRAINING"
    empty = Episode("empty", [], [], None, None, [])
    report = runner.run_local([empty], {}, batch_id="ineligible-run", max_episodes=1)
    assert report["outcome"] == "NO_TRAINING"
    assert report["episodes_analyzed"] == 0
    assert report["skipped_episodes"] == [{"episode_id": "empty", "reason": "empty_episode"}]


def test_failed_model_output_is_reported_and_not_checkpointed_complete(tmp_path):
    paths = TrainingPaths(tmp_path)
    runner = runner_for(paths, lambda _: {"invalid": "output"}, validate=False)
    report = runner.run_local([make_episode()], {"SB-EM-001": RESULT}, batch_id="bad-output", max_episodes=1)
    assert report["outcome"] == "FAILED"
    assert report["episodes_analyzed"] == 0
    assert report["failed_episodes"][0]["reason"].startswith("baseline_failed:")
    assert report["inference_counts"]["baseline"]["validation_failures"] == 1
    checkpoint = TrainingCheckpoint.load(report["checkpoint"], paths)
    assert checkpoint.completed_episode_ids == []
