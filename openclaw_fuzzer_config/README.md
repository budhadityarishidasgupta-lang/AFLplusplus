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
├── openclaw_home/                       # materialize as ~/.openclaw
│   └── skills/
│       ├── recon-analyzer/
│       │   ├── SKILL.md
│       │   ├── memory/                  # append-only reviewed path rules
│       │   └── plugins/drafts/          # declarative parser drafts
│       └── evolutionary-fuzzer/
│           └── SKILL.md
└── openclaw_project/                    # OpenClaw daemon project root
    ├── openclaw.json                    # engines, agents, tools, routing, sandbox
    ├── fuzzer_flow.lobster              # typed, approval-gated DAG
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

## Browser QA range

`openclaw_project/enterprise_range.yaml` and
`ENTERPRISE_RANGE_CONTRACT.md` define the optional dual-mode browser QA
contract. It accepts zero-knowledge and credentialed loopback campaigns while
explicitly forbidding automation concealment, fingerprint spoofing, remote
proxies, and privilege-escalation fuzzing. The Python reference implementation
is `../enterprise_stealth_range.py` from this directory. Unified campaign
loading and process-bound `/dev/shm` session leases are implemented by
`../campaign_session_manager.py`. `../system_supervisor.py` supplies the
loopback live-command listener and asynchronous manual-approval gate. The
administrator entry point and worker protocol are documented in
`LAUNCHER_CONFIGURATION_MAP.md` and implemented by
`../conversational_launcher.py`.

## Skill placement

The repository stores installable skill templates under `openclaw_home/skills/`
so their final mapping is explicit:

```text
openclaw_fuzzer_config/openclaw_home/skills/recon-analyzer/SKILL.md
  → ~/.openclaw/skills/recon-analyzer/SKILL.md

openclaw_fuzzer_config/openclaw_home/skills/evolutionary-fuzzer/SKILL.md
  → ~/.openclaw/skills/evolutionary-fuzzer/SKILL.md
```

The recon skill's `memory/` and `plugins/drafts/` directories remain local to
that skill. Parser drafts require a later-cycle human approval and contain
specifications/pseudocode only; the skill cannot generate and immediately run
new executable capability.

## Lobster flow

`fuzzer_flow.lobster` declares typed target arguments and JSON state, invokes
recon, pauses at an approval node that returns a `resumeToken`, invokes the
developer, runs a bounded assessment sub-workflow while fitness is below 100,
and routes threshold success to defensive finding and patch documentation. A
separate terminal ledger step records budget exhaustion, so every approved run
terminates deterministically.

Lobster schemas can vary between OpenClaw releases. The file is a complete
declarative object specification; validate its `approval`, `subworkflow`, and
expression keys with the schema shipped by the exact daemon release before
materializing it in an operational workspace.
