# AFL++ Production Control Plane

This directory defines the bounded control surface between orchestration agents and the AFL++ execution plane.

## Production boundary

Agents MUST NOT invoke `afl-fuzz`, shells, Docker, or arbitrary processes directly. Agents submit typed campaign requests to the control plane. Only a separately deployed worker may translate an approved campaign into AFL++ runtime configuration.

```text
Agent / workflow
      |
      v
Fuzzer Control API
      |
      +-- validate target and policy
      +-- authorize campaign
      +-- persist immutable campaign spec
      +-- enqueue bounded worker job
      |
      v
AFL++ worker
      |
      v
sandboxed target
      |
      v
normalized telemetry / findings
```

## Required API capabilities

The production service should expose only bounded operations:

- `create_campaign(spec)` - validate and persist an immutable campaign specification.
- `approve_campaign(id, approval)` - record explicit operator authorization.
- `start_campaign(id)` - enqueue an approved campaign; never accept a shell command.
- `stop_campaign(id)` - request graceful cancellation.
- `get_campaign(id)` - return normalized state and policy status.
- `get_coverage(id)` - return aggregate coverage telemetry.
- `get_corpus_stats(id)` - return counts and hashes, not arbitrary corpus execution.
- `get_findings(id)` - return normalized crash/finding metadata.
- `adjust_strategy(id, policy)` - accept only schema-validated mutation-policy changes within configured bounds.

## Non-negotiable invariants

1. No API field may contain an executable command.
2. Target identity is represented as structured data and checked against an authorization policy before execution.
3. Every campaign has execution, time, memory, input-size, and generation limits.
4. Approval is bound to the immutable campaign-spec digest. A changed spec requires new approval.
5. Workers run with least privilege and no ambient credentials.
6. Network access is deny-by-default and separately allowlisted by deployment policy.
7. Workspace paths are canonicalized and confined to a campaign-specific root.
8. Telemetry is append-only/auditable and includes campaign ID, spec digest, worker identity, timestamps, policy decisions, and termination reason.
9. Findings are treated as untrusted data and never interpolated into commands.
10. LLM/agent output is advisory input; deterministic policy enforcement remains outside the model.

## State machine

```text
DRAFT -> VALIDATED -> AWAITING_APPROVAL -> APPROVED -> QUEUED -> RUNNING
                                                       |          |
                                                       |          +-> SUCCEEDED
                                                       |          +-> EXHAUSTED
                                                       |          +-> CANCELLED
                                                       |          +-> FAILED
                                                       |          +-> BLOCKED
                                                       +------------> BLOCKED
```

Invalid transitions fail closed.

## Separation of responsibilities

### Agent layer

May analyze metadata, propose campaign parameters, interpret normalized telemetry, and recommend bounded strategy changes.

### Control plane

Owns schema validation, authentication/authorization, approvals, state transitions, rate limits, audit records, idempotency, and job dispatch.

### Worker

Owns AFL++ process lifecycle, resource isolation, instrumentation adapters, corpus storage, and normalized telemetry extraction. It consumes an immutable approved specification rather than natural-language instructions.

## Next implementation steps

1. Add versioned campaign/request/response schemas.
2. Implement the state machine and immutable spec digest.
3. Add a worker adapter for AFL++ with no arbitrary command input.
4. Add persistent campaign/audit storage.
5. Add authentication, authorization, idempotency, quotas, and rate limiting.
6. Add container/process isolation and deployment profiles.
7. Add integration tests proving policy bypasses fail closed.

This boundary is intentionally framework-independent: OpenClaw, Lobster, or another agent orchestrator can sit above it without gaining direct execution authority.