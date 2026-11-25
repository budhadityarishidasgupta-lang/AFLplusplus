"""Deterministic tests for SB-50 autonomous cognitive learning core."""
import threading
import time

import pytest

from brain_core.autonomous_learning import (
    CognitiveAnalysis, CognitiveLearningCycle, CuriosityEngine, Evidence,
    LearningMemory, LearningWorker, SyntheticAnalyzer, SyntheticSource,
)


@pytest.fixture
def memory(tmp_path):
    store = LearningMemory(str(tmp_path / "learning.sqlite3"))
    store.initialize()
    return store


def test_curiosity_persists_deduplicates_and_resolved_questions_do_not_recur(memory):
    engine = CuriosityEngine(memory)
    first = engine.generate(origin="cycle-a", uncertainty=["the evidence boundary"])
    second = engine.generate(origin="cycle-b", uncertainty=["the evidence boundary"])
    assert len(first) == 1
    assert second == []
    reloaded = LearningMemory(memory.db_path)
    assert reloaded.questions()[0].question_id == first[0].question_id
    reloaded.resolve_question(first[0].question_id, "Two independent sources agree.")
    assert engine.generate(origin="cycle-c", uncertainty=["the evidence boundary"]) == []


def test_autonomous_selector_chooses_open_curiosity_without_supplied_topic(memory):
    question = CuriosityEngine(memory).generate(origin="reflection", interests=["glacier dynamics"])[0]
    activity = CognitiveLearningCycle(memory, []).curriculum.select()
    assert activity.topic == question.question
    assert activity.question_id == question.question_id


def test_cycle_acquires_interprets_questions_applies_and_keeps_provisional(memory):
    evidence = Evidence("Water freezes at 0 degrees Celsius at standard pressure.", "document", "fixture-1", {"fixture": True}, .9)
    source = SyntheticSource([evidence])
    analyzer = SyntheticAnalyzer(CognitiveAnalysis(
        "Water's freezing point is context dependent.", assumptions=("standard pressure",),
        alternatives=("impurities can change the freezing point",), reflection="Avoid generalizing beyond stated conditions.",
        candidate_knowledge=(evidence.content,), application="Check pressure before applying the value.",
        evaluation={"method": "fixture", "weaknesses": ["phase diagrams"]}, uncertainty=("Only one evidence source",)))
    cycle = CognitiveLearningCycle(memory, [source], analyzer)
    result = cycle.run_once()
    assert result["status"] == "completed"
    assert result["assumptions"] == ("standard pressure",)
    assert result["application"].startswith("Check pressure")
    knowledge = memory.knowledge("freezes Celsius")
    assert knowledge[0]["status"] == "provisional"
    assert knowledge[0]["provenance"]["simulated"] is True
    assert memory.questions()


def test_consolidation_requires_independent_support_and_no_contradiction(memory):
    kid = memory.add_knowledge("A supported-looking claim", provenance={"fixture": True})
    assert not memory.consolidate(kid, support_count=1)
    assert not memory.consolidate(kid, support_count=3, contradiction=True)
    assert memory.consolidate(kid, support_count=2)
    assert memory.knowledge()[0]["status"] == "supported"


def test_revision_retains_history_and_provisional_state(memory):
    kid = memory.add_knowledge("Initial interpretation", provenance={"source": "a"})
    assert memory.revise_knowledge(kid, "Corrected interpretation", confidence=.8,
                                   uncertainty=["one caveat"], provenance={"source": "b"}, reason="new evidence") == 2
    current = memory.knowledge()[0]
    assert current["statement"] == "Corrected interpretation"
    assert current["status"] == "provisional"
    import sqlite3
    with sqlite3.connect(memory.db_path) as db:
        assert db.execute("select count(*) from knowledge_revisions where knowledge_id=?", (kid,)).fetchone()[0] == 2


def test_unavailable_inference_continues_ingestion_and_queues_analysis(memory):
    source = SyntheticSource([Evidence("A fixture document", "document", "d1")])
    cycle = CognitiveLearningCycle(memory, [source], SyntheticAnalyzer(is_available=False))
    result = cycle.run_once()
    assert result["status"] == "queued_analysis"
    assert memory.pending_activities()[0]["status"] == "queued"
    assert source.activities


def test_all_resources_unavailable_still_checkpoints_without_fabricating_understanding(memory):
    source = SyntheticSource(is_available=False)
    analyzer = SyntheticAnalyzer(is_available=False)
    result = CognitiveLearningCycle(memory, [source], analyzer).run_once()
    assert result["status"] == "ingestion_only"
    assert result["analysis"] is None
    assert not memory.knowledge()


def test_worker_pause_resume_and_graceful_stop(memory):
    class BlockingCycle:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0
        def run_once(self):
            self.calls += 1
            self.started.set()
            self.release.wait(2)
            return {"status": "completed"}
    cycle = BlockingCycle()
    worker = LearningWorker(cycle, idle_seconds=.01)
    worker.start(cycles=None)
    assert cycle.started.wait(1)
    worker.pause()
    cycle.release.set()
    time.sleep(.05)
    calls = cycle.calls
    time.sleep(.05)
    assert cycle.calls == calls
    worker.resume()
    deadline = time.time() + 1
    while cycle.calls == calls and time.time() < deadline:
        time.sleep(.01)
    assert cycle.calls > calls
    worker.stop()
    worker.join(1)
    assert not worker._thread.is_alive()


def test_worker_one_cycle_and_recovery_checkpoint(memory):
    source = SyntheticSource([Evidence("source text", "document", "doc")])
    cycle = CognitiveLearningCycle(memory, [source], SyntheticAnalyzer(is_available=False))
    results = LearningWorker(cycle).run(cycles=1)
    assert len(results) == 1
    assert memory.pending_activities()[0]["checkpoint"]["stage"] == "queued_analysis"


def test_supported_memory_reloads_with_provenance(memory):
    kid = memory.add_knowledge("A retrievable semantic concept", provenance={"source_id": "test"}, category="semantic")
    reloaded = LearningMemory(memory.db_path)
    result = reloaded.knowledge("retrievable concept")
    assert result[0]["knowledge_id"] == kid
    assert result[0]["provenance"] == {"source_id": "test"}


def test_contradiction_generates_curiosity_and_blocks_consolidation(memory):
    engine = CuriosityEngine(memory)
    questions = engine.generate(origin="comparison", contradictions=["sources disagree on the mechanism"])
    assert questions and "contradiction" in questions[0].origin
    kid = memory.add_knowledge("The mechanism is X", provenance={"sources": ["a", "b"]})
    assert not memory.consolidate(kid, support_count=2, contradiction=True)
    assert memory.knowledge()[0]["status"] == "provisional"
