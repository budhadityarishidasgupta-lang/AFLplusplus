# Log-first local validation

This phase is intentionally dashboard-free. It produces a Markdown log and CSV event file; Excel export is optional.

## Run the safe fixture

```bash
python3 -m control_plane.log_runner --cycle-id local-demo-01 --output-dir reports
```

For Excel output:

```bash
python3 -m control_plane.log_runner --cycle-id local-demo-01 --output-dir reports --excel
```

The fixture validates one negative authorization invariant: a creator session must not change the designated test creator’s settings. It records policy acceptance, authentication, authorization result, state-unchanged evidence, and reset completion.

## Important boundary

The current runner makes **no network requests** and does not attack a real web service. It is the report/evidence contract and local fixture needed before adding a real adapter. A future adapter must be approved separately and must enforce literal loopback targeting, a disposable target, explicit route/action scope, time and execution budgets, redaction, and automatic reset.

The first production-like adapter should be a subprocess/container wrapper around a local target and ZAP, not direct LLM-controlled network access. It must emit the same event schema and refuse any target other than the approved loopback fixture.
