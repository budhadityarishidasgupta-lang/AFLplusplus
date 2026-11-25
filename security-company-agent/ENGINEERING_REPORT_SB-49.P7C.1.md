# SB-49.P7C.1 Engineering Completion Report

**Date:** 2026-11-25
**Disposition:** P7C.1 provider configuration/credential delivery and P7D operating modes implemented in the isolated worktree. Offline regression complete; live UAT authorization was not granted and no live Studio/search UAT occurred. Production: HOLD.

## Scope and changes

This isolated worktree adds provider-scoped configuration and runtime credential delivery, hardens the Conversation research adapter boundary, and implements the P7D Controlled/Free Learning mode selection. `brain_core/provider_config.py` validates allowlisted provider settings without network access, redacts secrets, and prevents generic secret retrieval. `brain_core/runtime_launcher.py` supports masked SearXNG token entry and builds a fresh child environment containing only required runtime values, local loopback model settings, SearXNG configuration, and explicit blanking for known unrelated provider aliases. It launches only loopback Studio with an owned process handle; it does not manage Ollama. The launcher/environment design is configuration isolation, **not** an OS sandbox.

Controlled research requires validated, explicitly enabled SearXNG configuration, per-request approval, and a durable one-time authorization claim. Free Learning requires explicit session selection and permits bounded informational research without per-query approval; it does not authorize cloud model inference or autonomous actions. A test search runner is available only behind an explicit test-only flag and is rejected in isolated runtime mode. An injected test runner now requires `SOVEREIGNBRAIN_ALLOW_TEST_ADAPTERS=1`, and is rejected in isolated runtime mode. Added tests cover secret redaction, child environment scope, lifecycle, research grants, adapter boundaries, and UI authorization. `tests/test_candidate_learning_ui.py` now sets up the repository import path and fixes the affected AppTest session-state seam. Architecture and controlled-UAT guidance are in `SECURE_PROVIDER_CONFIGURATION.md`.

## Regression evidence

- The target `test_modify_action_marks_candidate_for_re_review` passed three stability runs; its containing candidate UI module passed **9 tests** in the final isolated run.
- Final required ordered offline run passed **46 tests**, **1 skipped**, in **1.611s**: provider selection, OpenAI readiness, local readiness, fallback state, and local inference. Local inference has 1 intentional skip: a legacy-shaped/pending-approval case is covered by schema-valid tests in decision-quality tests.
- The final P7D operating-mode, Free Learning, Conversation research UI, provider configuration, runtime launcher, Studio mode selector, and research-coordinator suite passed **90 tests** (27.898s). A newly added coordinator validation test caught an undefined test fixture before the final successful run. After correcting the Controlled-mode coordinator's runner selection, the 90-test focused suite passed again. The 46-test regression subset above was rerun successfully before this final one-line Studio routing fix.
- Streamlit bare-mode `ScriptRunContext` warnings and test fixture `ResourceWarning`s (temporary directories and an unclosed SQLite connection in existing test activity) appeared. They did not fail the suite. Existing Streamlit `use_container_width` deprecation messages also appeared.

## Safety, scope, and provenance

- Only synthetic credential strings were used. Harmless subprocess tests invoked Python stdlib code; they performed no network requests and did not launch Studio.
- No real credentials or source `.env` values were read. The masked SearXNG credential prompt timed out before entry, so Studio was not launched and live research was not attempted. No new inference was run after available RAM fell below the configured safety guard; a prior local-model inference result remains historical test evidence only. Ollama was not probed, modified, started, or stopped.
- `git diff --check` and targeted `compileall` passed after the latest source edits. No push or deployment occurred. The implementation remains a local, uncommitted candidate pending final review/commit; the separate SB-T01 training worktree was not modified.

## Remaining limitations / readiness

The runtime passes the current offline regression suites for the implemented P7C.1 and P7D scope. Credential delivery through a child process environment still exposes the token to same-user process inspection; Python cannot guarantee secure memory erasure, and this is not OS-level isolation. Windows Credential Manager or another reviewed secret-store integration and any true OS sandboxing remain future work. The full suite provides regression evidence, not a production security certification.

P7D adds a user-selected Free Learning mode with session-scoped standing authorization for informational research. This mode does not authorize external model inference or autonomous actions. **Controlled mode remains the default.**
