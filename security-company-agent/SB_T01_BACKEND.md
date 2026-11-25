# SB-T01 isolated emotional-learning backend

## Status and scope

SB-T01 is an isolated, local training workspace. The implementation supports synthetic dry runs, structured experience records, blind-first analysis, reflection, checkpoints, rollback, and a private SQLite experience store. It is not a production-memory updater. The gated local inference command exists but is not ready for genuine private-data processing until the evaluation and privacy limitations below are resolved. No complete archive processing is authorized by this document.

## Private paths and filesystem assumptions

The backend requires an existing absolute local private root. `TrainingPaths` checks canonical containment, rejects known symlinks and reparse points (including junctions), rejects traversal and cloud roots configured through OneDrive environment variables, and confines input, checkpoint, report, and output destinations. Windows junction smoke tests use only synthetic temporary data. The designated root is `C:\Users\bda50\Documents\SovereignBrain\private-training\sb-t01`, outside the Git worktrees and configured OneDrive roots observed during setup. It is separate from production memory and the SB-49 infrastructure worktree.

These controls are path validation and accident prevention, not an OS sandbox. SQLite uses path-based file access and sidecar journal files; a malicious concurrent process running as the same user can race path checks or replace files/directories. Atomic file writes and immediate revalidation reduce ordinary risk but do not eliminate that race. Operational assumption: trusted operator-controlled machine, verified private root, no concurrent filesystem manipulation, no elevated untrusted code, and local-only inference. Do not run this against a directory being changed by another process. Do not claim protection against a hostile administrator or same-user process.

The backend does not change Windows ACLs. No `.env` is read/copied. Reserved legacy personality/profile paths are not used; training records are explicitly non-autobiographical and never written to production memory.

## Training and privacy architecture

- `InstagramHTMLImporter` parses inert local HTML; scripts, links and attachments are not executed or fetched. File import must be restricted to the approved input directory.
- `PrivacyProcessor` pseudonymizes supplied participant names and minimizes contact/address/financial/link/intimate material. It is not anonymization: rare facts, writing style, dates, and context can identify people. Seed importer requires pseudonymous `speaker_N` IDs and structurally valid annotations; the privacy regex is not a de-identification guarantee. Records with unresolved identifying detail, intimate/sensitive content, or insufficient de-identification must be excluded or quarantined before inference. Do not print raw input or put it in engineering reports or cloud coding-agent context.
- `EpisodeGenerator` emits heuristic episode candidates and bounded context windows; lexical signals are not psychological findings.
- Versioned records separate explicit emotion statements from hypotheses, evidence, alternatives, unknowns, behaviour, outcomes, and candidate lessons. Several emotions can coexist. Inferred human emotions are uncertain hypotheses, never verified facts.
- `AnalysisAdapter` permits only mock or HTTP loopback Ollama on port 11434. Local mode pins `qwen3:0.6b`, checks RAM before requests, and uses structured output. A bounded, authorized local runner is prepared, but no genuine inference was executed in this work session.
- The reflection path analyzes before revealing provisional teaching annotations and permits disagreement. The present runner flags candidate lessons for autonomous handling but does not truly validate lesson evidence or robustly detect privacy-sensitive content. Regex-based uncertainty checks are only a weak heuristic; do not interpret `consolidated_lessons` as psychologically validated. Sensitive-content quarantine and evidence validation require improvement before genuine private inference.
- `ExperienceMemory` is a separate training DB with provenance, source deletion, batch rollback, idempotent insertion, and retrieval. Human-reviewed controls remain available; the local runner's autonomous mode applies only to its bounded isolated batch. No production personality or memory mutation occurs.
- Evaluation split validation prevents shared episode/source IDs. The 12-dimension rubric distinguishes recognition, empathy, uncertainty and decision quality; scores require an independent human evaluator and evidence. No before/after results exist.

## Pilot prerequisites and sequence

**Evaluation readiness limitation:** the present runner records a blind analysis and a post-consolidation retrieval probe, but it does not produce a separate baseline-before-annotations assessment, compare decisions on a matched unseen scenario, conduct an application assessment, or independently score decision quality. The retrieval check is not evidence of improved emotional competence or retention. The first run should therefore be treated as an inference/analysis smoke pilot, not a successful learning evaluation. Do not claim improvement; implement the independent held-out assessment before any expansion claim.

The operator confirmed authorization to use the curated de-identified seed dataset. No seed JSONL file was found in the designated private `input` directory, so no dataset was opened and no inference was run. Place only the intended curated experiences there and verify participant permissions/de-identification before using the command below. No full-archive processing is authorized.

Before inference is enabled, implement and validate: file schema and privacy quarantine; blind baseline analysis with annotations withheld; independent unseen scenarios with references hidden from the model; explicit scoring for recognition, empathy, perspective-taking, uncertainty, decision quality and principle application; checkpointed reload/retention testing; and evidence-based lesson validation. Then confirm the loopback Ollama endpoint, exact model `qwen3:0.6b`, RAM availability, and no competing resource-intensive inference. Process sequentially, no more than three episodes, and stop for human review. Do not process the full archive or write production memory.

## Commands

From `security-company-agent`, offline validation:

```powershell
python -m pytest tests/test_emotional_learning.py -q
python -m py_compile brain_core/emotional_learning.py sb_t01_train.py tests/test_emotional_learning.py
python sb_t01_train.py --training-root 'C:\Users\bda50\Documents\SovereignBrain\private-training\sb-t01' --mode dry-run
```

The prepared local inference command is gated by explicit authorization, exact `qwen3:0.6b`, Ollama loopback, a RAM check, and a maximum of three episodes. **It is not ready for genuine private-data inference** until de-identification/quarantine and independent blind baseline/holdout evaluation are implemented and reviewed. It does not process the full archive or update production memory. The operator previously said “No, stop here”; do not run it without new authorization.

```powershell
python sb_t01_train.py --training-root 'C:\Users\bda50\Documents\SovereignBrain\private-training\sb-t01' --mode local --authorize-local-pilot --seed-file 'curated_emotional_seed.jsonl' --max-episodes 3 --batch-id 'sb-t01-seed-pilot-01' --timeout 60
```

Do not run in parallel with resource-intensive SB-49 work. Stop for human review after the three-episode evaluation; resume only with the same dataset/batch and a reviewed checkpoint.

## Pause, resume, rollback

Synthetic/local runner checkpoints belong only under private `checkpoints`; resume only after verifying batch ID and dataset identity. Stop on missing model, inadequate/unverifiable RAM, malformed output, privacy concern, or unexpected provider/model. Use private DB batch rollback or source deletion to remove training artifacts. Restore only from private checkpoints. These operations do not affect production memory.

## Evidence limits

No real conversation contents were accessed, no seed file was found in the private input directory, no genuine inference occurred, no external provider was called, no Streamlit or Ollama service was started/stopped, no production memory was modified, and no Git commit/push/merge/deploy occurred in this work session. Full offline regression is run after the final changes; results are recorded in the engineering response, not here, to keep this file evergreen.
