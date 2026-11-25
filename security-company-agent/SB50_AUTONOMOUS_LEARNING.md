# SB-50 Autonomous Learning Vertical Slice

SB-50 introduces a local, provider-independent curiosity queue and bounded learning worker. Learning tasks, provenance, outcomes, worker controls, and provider attribution live in their own SQLite store; they do not depend on a model conversation ID.

## Backend API

`brain_core.sb50_learning` provides:

- `LearningStore`: persistent deduplicated curiosity, priority/novelty scheduling, task state, learned records, events, and pause/resume/stop configuration.
- `build_inference_request`: constructs a structured request using the existing `InferenceRequest` contract.
- `AutonomousLearningWorker`: sequential bounded cycles with injected provider callbacks returning existing `InferenceResult` values, failure recovery, provider attribution, confidence-gated completed vs provisional memory, and follow-up curiosity.
- `LearningMode`: `LOCAL_ONLY`, `HYBRID`, and `COLLABORATIVE` mode labels. Provider availability/routing and credentials remain supplied by the caller; this module never discovers credentials or invokes a provider by itself.

Default storage is `%SOVEREIGNBRAIN_RUNTIME_DIR%\autonomous_learning.sqlite3` when the runtime variable is set, otherwise `%USERPROFILE%\.sovereignbrain\autonomous_learning.sqlite3`. Pass `--db` to use another local store.

## CLI

Run from `security-company-agent` (or use `Launch Autonomous Learning.cmd`):

```powershell
python .\sb50_learn.py enqueue "How should uncertainty affect an emotional interpretation?" --topic psychology --priority 0.7 --novelty 0.8
python .\sb50_learn.py configure --mode HYBRID --state running
python .\sb50_learn.py once
python .\sb50_learn.py run --cycles 3
python .\sb50_learn.py run --continuous --max-cycles 20 --pace 2
python .\sb50_learn.py pause
python .\sb50_learn.py resume
python .\sb50_learn.py stop
python .\sb50_learn.py status
python .\sb50_learn.py learned
```

The CLI manages persistent tasks and controls but deliberately does not silently make genuine inference requests. For genuine execution, a host application must construct an authorized configured adapter and pass callbacks to `AutonomousLearningWorker`. This keeps credentials, privacy eligibility, provider budgets, and readiness decisions at the existing provider-configuration boundary. When the CLI finds queued work but no attached provider, it reports `pending_provider` and exits with code 3; it does not pretend to have learned.

## Validation status

Automated tests use deterministic in-process callbacks and are not evidence of genuine inference, learning improvement, neural fine-tuning, local-model independence, or cognitive capacity. The current implementation stores provisional knowledge with provider/model attribution and preserves confidence/uncertainty. It does not yet add a Studio panel, automated SearXNG/document ingestion loop, provider-specific adapter factory, budget accounting, or live resource-based routing.

Emotional seed processing remains available through `sb_t01_train.py` and uses its existing isolated private training store. It is intentionally not auto-enqueued or executed by this worker yet: the host must choose an eligible experience and pass an authorized, privacy-appropriate abstraction to its configured provider adapter.
