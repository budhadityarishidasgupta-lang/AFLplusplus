import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain_core.providers import InferenceResult, ProviderID
from brain_core.sb50_learning import (
    AutonomousLearningWorker, LearningMode, LearningStore, build_inference_request,
)


def result(*, status="success", lesson="Ask for context before assuming intent.", supported=True,
           confidence=0.85, next_question="How can uncertainty be tested across cultures?"):
    return InferenceResult(ProviderID.LOCAL_OLLAMA, "mock-model", "answer", {
        "lesson": lesson, "supported": supported, "confidence": confidence,
        "uncertainty": "Limited example; needs broader evidence.", "next_question": next_question,
    } if status == "success" else None, status, None if status == "success" else "timeout",
        0.01, 10, 20, "request-mock", None, True, False)


def test_curiosity_queue_deduplicates_prioritizes_and_persists(tmp_path):
    db = tmp_path / "learning.sqlite3"
    store = LearningStore(db)
    low, created = store.enqueue("What is emotional ambiguity?", topic="psychology", priority=0.2, novelty=0.2)
    assert created
    same, created = store.enqueue("  what IS emotional ambiguity? ", topic="other")
    assert not created and same.task_id == low.task_id
    high, created = store.enqueue("How do people revise beliefs?", topic="critical thinking", priority=0.8, novelty=0.9)
    assert created and store.next_task().task_id == high.task_id
    reopened = LearningStore(db)
    assert {task.question for task in reopened.tasks("pending")} == {
        "What is emotional ambiguity?", "How do people revise beliefs?"}


def test_worker_persists_learning_and_brain_generated_followup(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    task, _ = store.enqueue("How can stated boundaries be interpreted?", topic="emotional intelligence",
                            reason="uncertain interpretation", origin="emotional_experience")
    request = build_inference_request(task, model="mock-model")
    assert request.model == "mock-model" and request.json_schema["additionalProperties"] is False
    calls = []
    worker = AutonomousLearningWorker(store, [lambda current: calls.append(current.task_id) or result()],
                                      provider_ids=[ProviderID.LOCAL_OLLAMA], mode=LearningMode.HYBRID)
    outcome = worker.cycle()
    assert outcome.status == "completed" and outcome.provider == "local_ollama"
    assert outcome.request_id == "request-mock" and outcome.input_tokens == 10
    assert calls == [task.task_id]
    assert len(store.records()) == 1
    assert store.records()[0]["content"] == "Ask for context before assuming intent."
    followup = store.next_task()
    assert followup and followup.origin == "learned_curiosity"
    assert followup.question == "How can uncertainty be tested across cultures?"


def test_weak_learning_stays_provisional_and_provider_failure_is_retried(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    task, _ = store.enqueue("How do hypotheses differ from evidence?", priority=0.9)
    worker = AutonomousLearningWorker(store, [lambda _: result(confidence=0.2)], mode="HYBRID")
    outcome = worker.cycle()
    assert outcome.status == "provisional"
    assert store.get_task(task.task_id).status == "provisional"
    task2, _ = store.enqueue("What causes a provider timeout?", priority=1.0, novelty=1.0)
    worker = AutonomousLearningWorker(store, [lambda _: result(status="failed")], mode="LOCAL_ONLY")
    failed = worker.cycle()
    assert failed.task_id == task2.task_id and failed.status == "retry" and failed.error_category == "timeout"
    assert store.get_task(task2.task_id).attempts == 1


def test_missing_provider_and_worker_controls_are_explicit(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    task, _ = store.enqueue("What can be learned without inference?")
    worker = AutonomousLearningWorker(store, [], mode="HYBRID")
    outcome = worker.cycle()
    assert outcome.status == "retry" and outcome.error_category == "no_provider"
    assert store.get_task(task.task_id).status == "retry"
    assert store.set_control("paused")["state"] == "paused"
    assert worker.cycle().status == "paused"
    assert store.set_control("running")["state"] == "running"
    worker.providers = [lambda _: result()]
    outcomes = worker.run(1)
    assert outcomes[0].status in {"completed", "provisional"}
    assert store.set_control("stopped")["state"] == "stopped"
    assert worker.cycle().status == "stopped"


def test_run_bounds_and_continuous_guard(tmp_path):
    worker = AutonomousLearningWorker(LearningStore(tmp_path / "learning.sqlite3"), [])
    try:
        worker.run(0)
        assert False, "zero cycles must be rejected"
    except ValueError:
        pass
    try:
        worker.run(1, continuous=True, max_cycles=2)
        assert False, "continuous runner uses a zero cycles sentinel and finite max_cycles guard"
    except ValueError:
        pass
