# OpenClaw local fuzzer cluster configuration brief

This package is a declarative workspace blueprint for a two-agent, local-only
fuzzing playground. It contains no runtime implementation, exploit payload, or
third-party endpoint integration. The runtime administrator remains responsible
for validating the configuration against the installed OpenClaw release and
enforcing its declared filesystem/network controls at the OS sandbox layer.

## Storage matrix

All paths in `openclaw.json` are relative to `openclaw_project/`:

```text
openclaw_fuzzer_config/
├── README.md
└── openclaw_project/                    # OpenClaw daemon project root
    ├── openclaw.json                    # engines, agents, tools, routing, sandbox
    ├── agents/
    │   ├── recon_planner/               # workspace: recon_planner_01
    │   │   ├── SOUL.md                  # analytical defensive persona
    │   │   └── AGENTS.md                # file and delegation boundaries
    │   └── exploit_developer/           # workspace: exploit_dev_01
    │       ├── SOUL.md                  # bounded evolutionary worker persona
    │       └── AGENTS.md                # Bash and artifact boundaries
    ├── shared_data/                     # shared orchestration folder
    │   ├── blueprint.json               # recon strategy → worker parameters
    │   ├── WORKFLOW_STATUS.md           # append-only coordination ledger
    │   └── crashes/                     # structured simulated findings only
    │       └── .gitkeep
    └── sandbox/                         # isolated local playground
        ├── targets/                     # mocked target assets
        │   └── .gitkeep
        ├── corpus/                      # generated local corpus
        │   └── .gitkeep
        └── runtime/                     # bounded task PID/state/output files
            └── .gitkeep
```

## Configuration map

- `recon_planner_01` owns orchestration, uses the `gpt-4o` profile, reads shared
  mock metadata, and writes the strategy and status artifacts. It has no Bash or
  network permission.
- `exploit_dev_01` uses `gpt-4o-mini`. Its Bash working directory is
  `sandbox/runtime`, writable roots are limited to `sandbox` and `shared_data`,
  and core paths including `/etc` and `/var` are explicitly denied.
- The only worker network destination is the mock target at
  `127.0.0.1:3000`; all other destinations are denied. Gateway/monitoring binds
  to loopback at `127.0.0.1:8080`.
- The shared artifacts are `shared_data/blueprint.json` and
  `shared_data/WORKFLOW_STATUS.md`. Simulated finding metadata is isolated under
  `shared_data/crashes/`.

## Context and handoff sequence

```text
Local operator
  → Gateway 127.0.0.1:8080 assigns cycle_id
  → recon_planner_01 receives its SOUL/AGENTS context + shared metadata
  → recon writes shared_data/blueprint.json and PLAN_READY ledger state
  → Gateway validates cycle_id, safety flags, and artifact path
  → exploit_dev_01 receives blueprint path (not an unrestricted transcript)
  → worker runs one bounded mock-target cycle against 127.0.0.1:3000
  → worker appends coverage outcome to WORKFLOW_STATUS.md
  → Gateway returns normalized feedback to recon for re-plan or completion
```

Invalid paths, stale cycle identifiers, disabled safety flags, or destinations
other than the loopback allowlist must transition the ledger to `BLOCKED` and
must not reach the Bash-capable worker.
