"""Provider-neutral autonomous cognitive learning primitives.

This subsystem owns curiosity, task choice, cognitive-cycle orchestration and
its durable learning ledger. Resource and inference implementations are injected;
no direct provider adapter or claim of model-weight training lives here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Protocol

from . import studio


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class LearningQuestion:
    question: str
    origin: str
    related_concepts: tuple[str, ...] = ()
    priority: float = 0.5
    novelty: float = 1.0
    current_understanding: str = ""
    unresolved_uncertainty: tuple[str, ...] = ()
    suggested_activity: str = "research"
    status: str = "open"
    question_id: str = ""


@dataclass(frozen=True)
class LearningActivity:
    activity_id: str
    kind: str
    topic: str
    rationale: str
    question_id: str | None = None


@dataclass(frozen=True)
class Evidence:
    content: str
    source_type: str
    source_id: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5


@dataclass(frozen=True)
class CognitiveAnalysis:
    interpretation: str
    assumptions: tuple[str, ...] = ()
    questions: tuple[LearningQuestion, ...] = ()
    alternatives: tuple[str, ...] = ()
    reflection: str = ""
    candidate_knowledge: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    application: str = ""
    evaluation: dict[str, Any] = field(default_factory=dict)


class LearningSource(Protocol):
    """A source may acquire material; it must label its provenance honestly."""
    name: str

    def available(self) -> bool: ...
    def acquire(self, activity: LearningActivity) -> Iterable[Evidence]: ...


class CognitiveAnalyzer(Protocol):
    """Optional inference contract supplied by the provider integration layer."""
    def available(self) -> bool: ...
    def analyze(self, activity: LearningActivity, evidence: list[Evidence], context: list[dict[str, Any]]) -> CognitiveAnalysis: ...


class LearningMemory:
    """Persistent, revision-aware ledger in an isolated SQLite database."""
    CATEGORIES = {"semantic", "emotional", "experience", "skill", "reflection", "mistake", "curiosity", "evaluation", "personality"}

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS curiosity (
              question_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, question TEXT NOT NULL,
              origin TEXT NOT NULL, related_concepts TEXT NOT NULL, priority REAL NOT NULL,
              novelty REAL NOT NULL, current_understanding TEXT NOT NULL, uncertainty TEXT NOT NULL,
              suggested_activity TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_activities (
              activity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, topic TEXT NOT NULL,
              rationale TEXT NOT NULL, question_id TEXT, status TEXT NOT NULL,
              checkpoint TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge (
              knowledge_id TEXT PRIMARY KEY, category TEXT NOT NULL, statement TEXT NOT NULL,
              confidence REAL NOT NULL, uncertainty TEXT NOT NULL, provenance TEXT NOT NULL,
              status TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS knowledge_revisions (
              revision_id INTEGER PRIMARY KEY AUTOINCREMENT, knowledge_id TEXT NOT NULL,
              revision INTEGER NOT NULL, statement TEXT NOT NULL, confidence REAL NOT NULL,
              uncertainty TEXT NOT NULL, provenance TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_usage (
              usage_id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id TEXT NOT NULL,
              resource TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category, status);
            CREATE INDEX IF NOT EXISTS idx_activity_status ON learning_activities(status, updated);
            """)

    def _connect(self):
        from contextlib import contextmanager
        @contextmanager
        def connection():
            with self._lock:
                db = sqlite3.connect(self.db_path, timeout=10)
                db.row_factory = sqlite3.Row
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    yield db
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
        return connection()

    @staticmethod
    def _fingerprint(question: str) -> str:
        normalized = " ".join(question.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def add_question(self, question: LearningQuestion) -> LearningQuestion:
        text = question.question.strip()
        if not text:
            raise ValueError("Curiosity question cannot be empty")
        fingerprint = self._fingerprint(text)
        with self._connect() as db:
            existing = db.execute("SELECT * FROM curiosity WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing:
                if existing["status"] == "resolved":
                    return self._question(existing)
                db.execute("UPDATE curiosity SET priority=max(priority,?), novelty=min(novelty,?), updated=? WHERE fingerprint=?",
                           (question.priority, question.novelty, _now(), fingerprint))
                row = db.execute("SELECT * FROM curiosity WHERE fingerprint=?", (fingerprint,)).fetchone()
                return self._question(row)
            qid = question.question_id or str(uuid.uuid4())
            db.execute("INSERT INTO curiosity VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                qid, fingerprint, text, question.origin, _json(question.related_concepts),
                max(0, min(1, question.priority)), max(0, min(1, question.novelty)),
                question.current_understanding, _json(question.unresolved_uncertainty),
                question.suggested_activity, question.status, _now(), _now()))
            row = db.execute("SELECT * FROM curiosity WHERE question_id=?", (qid,)).fetchone()
            return self._question(row)

    @staticmethod
    def _question(row: sqlite3.Row) -> LearningQuestion:
        return LearningQuestion(row["question"], row["origin"], tuple(json.loads(row["related_concepts"])),
            row["priority"], row["novelty"], row["current_understanding"],
            tuple(json.loads(row["uncertainty"])), row["suggested_activity"], row["status"], row["question_id"])

    def questions(self, status: str | None = "open", limit: int = 100) -> list[LearningQuestion]:
        with self._connect() as db:
            if status is None:
                rows = db.execute("SELECT * FROM curiosity ORDER BY priority DESC,updated DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM curiosity WHERE status=? ORDER BY priority DESC,updated DESC LIMIT ?", (status, limit)).fetchall()
        return [self._question(row) for row in rows]

    def resolve_question(self, question_id: str, understanding: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE curiosity SET status='resolved',current_understanding=?,uncertainty='[]',updated=? WHERE question_id=?",
                       (understanding, _now(), question_id))

    def add_knowledge(self, statement: str, *, category: str = "semantic", confidence: float = 0.5,
                      uncertainty: Iterable[str] = (), provenance: dict[str, Any] | None = None,
                      status: str = "provisional", reason: str = "initial consolidation") -> str:
        if category not in self.CATEGORIES:
            raise ValueError(f"Unsupported learning category: {category}")
        statement = statement.strip()
        if not statement:
            raise ValueError("Knowledge statement cannot be empty")
        confidence = max(0.0, min(1.0, float(confidence)))
        provenance = provenance or {}
        kid = str(uuid.uuid4())
        with self._connect() as db:
            prior = db.execute("SELECT * FROM knowledge WHERE lower(statement)=lower(?) AND status!='superseded' ORDER BY updated DESC LIMIT 1", (statement,)).fetchone()
            revision = (prior["revision"] + 1) if prior else 1
            if prior:
                kid = prior["knowledge_id"]
                db.execute("UPDATE knowledge SET statement=?,confidence=?,uncertainty=?,provenance=?,status='provisional',revision=?,updated=? WHERE knowledge_id=?",
                           (statement, confidence, _json(list(uncertainty)), _json(provenance), revision, _now(), kid))
            else:
                db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    kid, category, statement, confidence, _json(list(uncertainty)), _json(provenance), "provisional", revision, _now(), _now()))
            db.execute("INSERT INTO knowledge_revisions(knowledge_id,revision,statement,confidence,uncertainty,provenance,reason,created) VALUES(?,?,?,?,?,?,?,?)",
                       (kid, revision, statement, confidence, _json(list(uncertainty)), _json(provenance), reason, _now()))
        return kid

    def revise_knowledge(self, knowledge_id: str, statement: str, *, confidence: float,
                         uncertainty: Iterable[str], provenance: dict[str, Any], reason: str) -> int:
        with self._connect() as db:
            row = db.execute("SELECT revision,category FROM knowledge WHERE knowledge_id=?", (knowledge_id,)).fetchone()
            if not row:
                raise KeyError(knowledge_id)
            revision = row["revision"] + 1
            db.execute("UPDATE knowledge SET statement=?,confidence=?,uncertainty=?,provenance=?,status='provisional',revision=?,updated=? WHERE knowledge_id=?",
                       (statement, max(0, min(1, confidence)), _json(list(uncertainty)), _json(provenance), revision, _now(), knowledge_id))
            db.execute("INSERT INTO knowledge_revisions(knowledge_id,revision,statement,confidence,uncertainty,provenance,reason,created) VALUES(?,?,?,?,?,?,?,?)",
                       (knowledge_id, revision, statement, max(0, min(1, confidence)), _json(list(uncertainty)), _json(provenance), reason, _now()))
            return revision

    def consolidate(self, knowledge_id: str, *, support_count: int, contradiction: bool = False) -> bool:
        """Consolidate only when evidence supports it and no contradiction remains."""
        if support_count < 2 or contradiction:
            return False
        with self._connect() as db:
            changed = db.execute("UPDATE knowledge SET status='supported',updated=? WHERE knowledge_id=? AND status='provisional'",
                                 (_now(), knowledge_id)).rowcount
        return bool(changed)

    def knowledge(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        tokens = set(studio.relevance_tokens(query))
        with self._connect() as db:
            rows = db.execute("SELECT * FROM knowledge WHERE status!='superseded' ORDER BY updated DESC LIMIT 250").fetchall()
        values = [dict(row) for row in rows]
        if tokens:
            values = sorted(((len(tokens & studio.relevance_tokens(v["statement"])), v) for v in values), key=lambda p: p[0], reverse=True)
            values = [v for score, v in values if score > 0]
        result = []
        for item in values[:limit]:
            for key in ("uncertainty", "provenance"):
                item[key] = json.loads(item[key])
            result.append(item)
        return result

    def save_activity(self, activity: LearningActivity, status: str, checkpoint: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO learning_activities VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(activity_id) DO UPDATE SET status=excluded.status,checkpoint=excluded.checkpoint,updated=excluded.updated",
                (activity.activity_id, activity.kind, activity.topic, activity.rationale, activity.question_id, status, _json(checkpoint), _now(), _now()))

    def pending_activities(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            values = [dict(row) for row in db.execute("SELECT * FROM learning_activities WHERE status IN ('queued','paused','running') ORDER BY created")]
        for value in values:
            value["checkpoint"] = json.loads(value["checkpoint"])
        return values

    def resumable_activities(self) -> list[dict[str, Any]]:
        """Return queued work and mark a prior interrupted running task resumable."""
        with self._connect() as db:
            db.execute("UPDATE learning_activities SET status='queued',updated=? WHERE status='running'", (_now(),))
            values = [dict(row) for row in db.execute("SELECT * FROM learning_activities WHERE status IN ('queued','paused') ORDER BY created")]
        for value in values:
            value["checkpoint"] = json.loads(value["checkpoint"])
        return values

    def log_usage(self, activity_id: str, resource: str, status: str, details: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO learning_usage(activity_id,resource,status,details,created) VALUES(?,?,?,?,?)",
                       (activity_id, resource, status, _json(details), _now()))


class CuriosityEngine:
    def __init__(self, memory: LearningMemory):
        self.memory = memory

    def generate(self, *, origin: str, concepts: Iterable[str] = (), uncertainty: Iterable[str] = (),
                 mistakes: Iterable[str] = (), contradictions: Iterable[str] = (), interests: Iterable[str] = (),
                 evaluation_weaknesses: Iterable[str] = (), existing_questions: Iterable[str] = ()) -> list[LearningQuestion]:
        generated: list[LearningQuestion] = []
        concepts = tuple(dict.fromkeys(c.strip() for c in concepts if c.strip()))
        signals = [(f"What evidence would resolve this uncertainty: {item}?", "uncertainty", (item,)) for item in uncertainty]
        signals += [(f"What assumption led to this mistake, and how can it be checked next time: {item}?", "mistake", (item,)) for item in mistakes]
        signals += [(f"Which explanation best resolves this contradiction: {item}?", "contradiction", (item,)) for item in contradictions]
        signals += [(f"How does {item} work, and what would demonstrate understanding?", "interest", (item,)) for item in interests]
        signals += [(f"What practice would improve this evaluation weakness: {item}?", "evaluation", (item,)) for item in evaluation_weaknesses]
        signals += [(f"What important gap remains in {item}?", "knowledge_gap", (item,)) for item in concepts]
        known = {" ".join(q.casefold().split()) for q in existing_questions}
        known.update(" ".join(q.question.casefold().split()) for q in self.memory.questions(status=None, limit=500))
        for text, source, related in signals:
            if " ".join(text.casefold().split()) in known:
                continue
            novelty = 1.0 / (1 + sum(1 for old in known if set(text.casefold().split()) & set(old.split())))
            question = self.memory.add_question(LearningQuestion(text, f"{origin}:{source}", related, 0.8 if source in {"contradiction", "mistake", "uncertainty"} else 0.55, novelty, "", (source,), "research"))
            generated.append(question)
            known.add(" ".join(text.casefold().split()))
        return generated


class AutonomousCurriculum:
    def __init__(self, memory: LearningMemory):
        self.memory = memory

    def select(self, *, resources: Iterable[LearningSource] = (), emotional_signals: Iterable[str] = (),
               evaluation_weaknesses: Iterable[str] = (), local_reasoning_available: bool = True) -> LearningActivity:
        available = [source for source in resources if source.available()]
        questions = self.memory.questions("open", 100)
        if questions:
            question = max(questions, key=lambda q: (q.priority, q.novelty))
            kind = question.suggested_activity
            return LearningActivity(str(uuid.uuid4()), kind, question.question,
                                    f"Open curiosity ({question.origin}); priority={question.priority:.2f}", question.question_id)
        if evaluation_weaknesses:
            return LearningActivity(str(uuid.uuid4()), "practice", next(iter(evaluation_weaknesses)), "Evaluation identified an improvement opportunity")
        if emotional_signals:
            return LearningActivity(str(uuid.uuid4()), "reflect", next(iter(emotional_signals)), "Relevant experience is available for reflection")
        if available:
            return LearningActivity(str(uuid.uuid4()), "explore", "Explore a useful new concept", "No higher-priority open curiosity; select a supported resource")
        if local_reasoning_available:
            return LearningActivity(str(uuid.uuid4()), "reflect", "Review recent supported knowledge", "Use independent local reasoning and identify remaining gaps")
        return LearningActivity(str(uuid.uuid4()), "ingest", "Process eligible local documents", "Inference unavailable; continue non-inference ingestion")


class CognitiveLearningCycle:
    def __init__(self, memory: LearningMemory, sources: Iterable[LearningSource], analyzer: CognitiveAnalyzer | None = None):
        self.memory = memory
        self.sources = list(sources)
        self.analyzer = analyzer
        self.curiosity = CuriosityEngine(memory)
        self.curriculum = AutonomousCurriculum(memory)

    def run_once(self, activity: LearningActivity | None = None) -> dict[str, Any]:
        activity = activity or self.curriculum.select(resources=self.sources, local_reasoning_available=bool(self.analyzer and self.analyzer.available()))
        existing = next((item for item in self.memory.resumable_activities() if item["activity_id"] == activity.activity_id), None)
        self.memory.save_activity(activity, "running", existing["checkpoint"] if existing else {"stage": "select"})
        if existing and existing["checkpoint"].get("stage") == "queued_analysis":
            evidence = [Evidence(**item) for item in existing["checkpoint"].get("evidence", [])]
            if not self.analyzer or not self.analyzer.available():
                self.memory.save_activity(activity, "queued", existing["checkpoint"])
                return {"activity": asdict(activity), "status": "queued_analysis", "evidence_count": len(evidence), "analysis": None}
            return self._analyze(activity, evidence)
        evidence: list[Evidence] = []
        for source in self.sources:
            try:
                if not source.available():
                    self.memory.log_usage(activity.activity_id, source.name, "unavailable", {})
                    continue
                values = list(source.acquire(activity))
                evidence.extend(values)
                self.memory.log_usage(activity.activity_id, source.name, "acquired", {"count": len(values), "simulated": bool(getattr(source, "simulated", False))})
            except Exception as exc:
                self.memory.log_usage(activity.activity_id, source.name, "error", {"error": str(exc)[:500]})
        if not self.analyzer or not self.analyzer.available():
            checkpoint = {"stage": "queued_analysis", "evidence_count": len(evidence), "evidence": [asdict(item) for item in evidence], "source_types": [e.source_type for e in evidence]}
            self.memory.save_activity(activity, "queued", checkpoint)
            return {"activity": asdict(activity), "status": "queued_analysis" if evidence else "ingestion_only", "evidence_count": len(evidence), "analysis": None}
        return self._analyze(activity, evidence)

    def _analyze(self, activity: LearningActivity, evidence: list[Evidence]) -> dict[str, Any]:
        context = self.memory.knowledge(activity.topic, limit=5)
        analysis = self.analyzer.analyze(activity, evidence, context)
        # Keep acquired claims provisional. Consolidation needs independent supporting evidence.
        ids = []
        for claim in analysis.candidate_knowledge:
            claim_evidence = [item for item in evidence if claim.casefold() in item.content.casefold()]
            provenance = {"activity_id": activity.activity_id, "sources": [asdict(e) for e in claim_evidence], "analysis": "injected cognitive analyzer", "simulated": bool(getattr(self.analyzer, "simulated", False))}
            kid = self.memory.add_knowledge(claim, confidence=min([e.confidence for e in claim_evidence], default=0.35),
                uncertainty=analysis.uncertainty or (("No independent source explicitly supports this claim",) if not claim_evidence else ()), provenance=provenance)
            supported_sources = {(e.source_type, e.source_id or e.content[:80]) for e in claim_evidence}
            consolidated = self.memory.consolidate(kid, support_count=len(supported_sources), contradiction=any(claim.casefold() in x.casefold() for x in analysis.uncertainty))
            ids.append({"knowledge_id": kid, "consolidated": consolidated})
        questions = list(analysis.questions)
        generated = self.curiosity.generate(origin=activity.activity_id, concepts=(activity.topic,), uncertainty=analysis.uncertainty,
            contradictions=[x for x in analysis.uncertainty if "contradict" in x.casefold()],
            evaluation_weaknesses=analysis.evaluation.get("weaknesses", []))
        for question in questions:
            self.memory.add_question(question)
        self.memory.save_activity(activity, "completed", {"stage": "evaluate", "knowledge": ids, "question_count": len(questions) + len(generated), "evidence_count": len(evidence)})
        return {"activity": asdict(activity), "status": "completed", "evidence_count": len(evidence),
                "interpretation": analysis.interpretation, "assumptions": analysis.assumptions,
                "alternatives": analysis.alternatives, "reflection": analysis.reflection,
                "application": analysis.application, "evaluation": analysis.evaluation,
                "knowledge": ids, "questions": len(questions) + len(generated)}


class LearningWorker:
    """Sequential standalone backend worker with pause/resume/stop checkpoints."""
    def __init__(self, cycle: CognitiveLearningCycle, *, idle_seconds: float = 1.0):
        self.cycle = cycle
        self.idle_seconds = max(0.0, idle_seconds)
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] | None = None

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def run(self, cycles: int | None = 1) -> list[dict[str, Any]]:
        results = []
        completed = 0
        memory = getattr(self.cycle, "memory", None)
        pending = {item["activity_id"]: item for item in memory.resumable_activities()} if memory else {}
        while not self._stop.is_set() and (cycles is None or completed < cycles):
            if self._pause.is_set():
                time.sleep(min(self.idle_seconds or 0.05, 0.2))
                continue
            if pending:
                stored = next(iter(pending.values()))
                activity = LearningActivity(stored["activity_id"], stored["kind"], stored["topic"], stored["rationale"], stored["question_id"])
                result = self.cycle.run_once(activity)
                pending.pop(activity.activity_id, None)
            else:
                result = self.cycle.run_once()
            self.last_result = result
            results.append(result)
            completed += 1
            if result["status"] == "ingestion_only":
                break
            if cycles is None and self.idle_seconds:
                time.sleep(self.idle_seconds)
        return results

    def start(self, cycles: int | None = None) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Learning worker is already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, args=(cycles,), name="sovereign-learning-worker", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)


# Deterministic fixtures are explicitly simulations, never live integrations.
class SyntheticSource:
    name = "synthetic_fixture"
    simulated = True
    def __init__(self, evidence: Iterable[Evidence] = (), is_available: bool = True):
        self.evidence = list(evidence)
        self.is_available = is_available
        self.activities: list[LearningActivity] = []
    def available(self) -> bool:
        return self.is_available
    def acquire(self, activity: LearningActivity) -> Iterable[Evidence]:
        self.activities.append(activity)
        return self.evidence


class SyntheticAnalyzer:
    simulated = True
    def __init__(self, analysis: CognitiveAnalysis | None = None, is_available: bool = True):
        self.analysis = analysis or CognitiveAnalysis("Fixture interpretation", candidate_knowledge=("Water freezes at 0 degrees Celsius at standard pressure.",), reflection="The source supports a limited, contextual conclusion.")
        self.is_available_value = is_available
        self.calls = 0
    def available(self) -> bool:
        return self.is_available_value
    def analyze(self, activity: LearningActivity, evidence: list[Evidence], context: list[dict[str, Any]]) -> CognitiveAnalysis:
        self.calls += 1
        return self.analysis
