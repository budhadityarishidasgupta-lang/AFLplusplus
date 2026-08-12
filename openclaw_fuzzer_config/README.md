# OpenClaw local fuzzer cluster brief

This directory is a deployment **blueprint**, not an exploit deployment. It
ports the planner/developer separation from `multi_agent_system.py` into two
OpenClaw workspaces while keeping execution confined to mocked targets beneath
`openclaw_project/sandbox/`.

## Design boundaries

- The gateway binds to loopback and has no webhook or remote-target binding.
- The recon planner is the lead operator. It may read the local sandbox and
  publish plans, but it cannot use shell or network tools.
- The exploit developer may use a restricted local shell for bounded fuzzing
  jobs. Prompt rules complement, but do not replace, OS/container isolation.
- JSON messages are written atomically (`*.tmp`, validate, rename). Shared log
  entries are append-only. Payloads are encoded as hex plus a SHA-256 digest;
  raw bytes are never interpolated into shell commands.
- `SOUL.md` describes role and intent; `AGENTS.md` describes workspace operating
  rules. Enforcement belongs in the gateway policy and the sandbox runtime.

## Layout

```text
openclaw_project/
├── openclaw.json
├── WORKFLOW_STATUS.md
├── agents/
│   ├── recon_planner/
│   │   ├── SOUL.md
│   │   ├── AGENTS.md
│   │   └── blueprint.json
│   └── exploit_developer/
│       ├── SOUL.md
│       └── AGENTS.md
├── logs/
│   └── crashes/
└── sandbox/
    ├── targets/
    ├── corpus/
    └── runtime/
```

Runtime directories contain `.gitkeep` placeholders only. Put copied mock
targets—not symlinks to external trees—under `sandbox/targets/`.

## Deterministic routing sequence

1. A local operator sends a task to the loopback Gateway.
2. The Gateway selects `recon_planner`, attaches only its workspace instructions
   and the shared status ledger, and assigns a correlation ID.
3. Recon statically inspects `sandbox/targets/`, atomically updates
   `agents/recon_planner/blueprint.json`, and appends a `PLAN_READY` ledger row.
4. The Gateway validates that JSON against the documented shape, then forwards
   the correlation ID and plan path—not an unconstrained transcript—to
   `exploit_developer`.
5. The developer runs one bounded local mock-target campaign, records fitness,
   and appends `FEEDBACK_READY`. A simulated crash additionally creates a
   structured record beneath `logs/crashes/`.
6. The Gateway returns the feedback path to recon. Recon revises the next plan
   or writes `COMPLETE`; retry number and seed make re-orchestration repeatable.
7. Invalid messages become `BLOCKED` ledger entries and are never routed to a
   shell-capable agent.

The exact CLI used to start a Gateway varies by installed OpenClaw release.
Validate `openclaw.json` with that release before startup; this blueprint keeps
version-sensitive policy in one file for that reason.

