# SB-50.CORE autonomous cognitive learning

This workstream adds a provider-neutral curiosity/curriculum/cognitive-cycle ledger and standalone worker. It does not replace the existing emotional-learning trainer, provider routing, cloud runtime, personality profile, research service, or approved-lesson system.

## Boundaries and contracts

### Provider integration (SB-50.PROVIDER)

`LearningSource` is an injected resource contract:

```python
class LearningSource(Protocol):
    name: str
    def available(self) -> bool: ...
    def acquire(self, activity: LearningActivity) -> Iterable[Evidence]: ...
```

`Evidence` requires content, source type/id, provenance, and confidence. Implementations can wrap existing Brain memory/documents, public research, external AI explanations, human experiences, and human feedback. This core creates no provider adapters and labels fixture usage separately. Provider adapters should return evidence with source-specific provenance and honor their native availability, rate limits and credits. Research acquisition alone must not imply that the Brain cognitively understood the content.

`CognitiveAnalyzer` is the injected inference boundary:

```python
class CognitiveAnalyzer(Protocol):
    def available(self) -> bool: ...
    def analyze(self, activity, evidence, relevant_memory) -> CognitiveAnalysis: ...
```

The analyzer should expose interpretation, assumptions, questions, alternatives, reflection, candidate claims, uncertainty, application and evaluation. A missing analyzer queues cognitive analysis after eligible acquisition/indexing; it does not generate fake model understanding.

### SB-T01.P2.1 emotional learning

Do not duplicate episode selection, emotional checkpoints/resume, emotional CLI or trainer tests. Future integration may provide emotional episodes/signals as curriculum inputs and contribute emotional-learning evidence through `LearningSource`. Keep affective labels/provenance separate from semantic claims. `CognitiveAnalysis` must not mutate established personality or emotional state.

### Shared memory and identity

`LearningMemory` is an additive, provider-independent SQLite ledger with categories `semantic`, `emotional`, `experience`, `skill`, `reflection`, `mistake`, `curiosity`, `evaluation`, and `personality`. It stores uncertainty, provenance and every knowledge revision. Candidates remain provisional; `consolidate` requires at least two independently identified supporting sources and no contradiction. The store is separate by default from `brain.sqlite3`; integration can share a database path only after schema/recovery/privacy review. Use retrieval by topic (`knowledge(query, limit)`) rather than injecting the full ledger. Personality adaptation is intentionally not performed; integration should call the existing personality architecture only for separately justified, small reversible simulated adaptations, never overwrite identity/history.

No private database, private experiences, or credentials are committed. `SyntheticSource` and `SyntheticAnalyzer` are deterministic development fixtures, not live integrations.

## Worker

From `security-company-agent` with the project virtual environment:

```powershell
python scripts/learning_worker.py --once
python scripts/learning_worker.py --cycles 10
python scripts/learning_worker.py --continuous
python scripts/learning_worker.py --status
python scripts/learning_worker.py --continuous --sources-factory my_integration.sources:build --analyzer-factory my_integration.inference:build
```

Default CLI factories are absent, so the worker will only perform the explicit no-resource ingestion-only/checkpoint path. For a useful learning run, inject factories. `LearningWorker` offers `pause()`, `resume()`, and `stop()` for an embedding backend; Ctrl+C/SIGTERM gracefully stop the CLI. Sequential task execution is used. Checkpoints/status are in `brain_core/studio_data/autonomous-learning.sqlite3` unless `--db` overrides it. Interrupted/running activities remain inspectable and eligible for recovery; a future integration runner should resubmit queued checkpoints to its analyzer.

Run focused deterministic tests:

```powershell
python -m pytest tests/test_autonomous_learning.py -q
```

## Known integration dependencies

- Live provider/resource factory and provider usage metadata from SB-50.PROVIDER.
- Emotional episode/experience interface and checkpoint coordination from SB-T01.P2.1.
- A privacy-approved shared-memory path/retrieval policy and any migration strategy.
- A production inference runner that can recover `queued_analysis` checkpoints and record provider/local-model execution class.
- Evaluation harness with external criteria; current `evaluation` payload is recorded, not a claim of measured improvement.
- Personality interface integration remains intentionally separate from knowledge consolidation.
