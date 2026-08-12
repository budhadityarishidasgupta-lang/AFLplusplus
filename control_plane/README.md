# Campaign state machine and immutable approval binding

Phase 1 is a dependency-free deterministic domain model. It performs no worker,
AFL++, shell, network, persistence, LLM, or OpenClaw operations.

## Authoritative lifecycle

```text
DRAFT → PENDING_APPROVAL → APPROVED → QUEUED → RUNNING
  │             │             │          │          ├→ SUCCEEDED
  ├─────────────┼─────────────┼──────────┼──────────├→ FAILED
  │             │             │          │          └→ EXHAUSTED
  ├─────────────┴─────────────┴──────────┴───────────→ BLOCKED
  └─────────────┴─────────────┴──────────┴───────────→ CANCELLED
```

Terminal states never transition. `RUNNING` is reachable only from `QUEUED`,
and `QUEUED` only from an approval bound to the campaign ID and the SHA-256 of
the exact canonical specification.

## Security invariants

- Campaign IDs use 1–64 lowercase letters, digits, `_`, or `-`, with an
  alphanumeric first and last character.
- Targets are only the literal loopback address `127.0.0.1`, ports 1–65535, and
  target kind `mock_http`.
- Resource budgets and mutation profiles are closed, bounded sets.
- Specifications are copied into recursively immutable mapping proxies/tuples.
- Canonical JSON uses UTF-8, sorted keys, compact separators, and rejects every
  floating-point value and non-JSON type before hashing.
- Approval records contain campaign ID, exact specification digest, non-empty
  human operator identity, and an aware UTC timestamp.
- Digest binding is rechecked before both queue and start, detecting stale or
  illicitly replaced specifications.
- A worker identity is required only when entering `RUNNING`; an operator—not an
  agent—is responsible for calling `approve`.

`campaign.schema.json` mirrors the deterministic validator for interchange, but
the Python validator remains authoritative at the security boundary.

## Audit and concurrency boundaries

Every successful transition emits a `StateEvent` containing previous/new state,
aware UTC timestamp, actor, and reason. Persistence remains outside the domain
model. Failed transitions produce no event.

`Campaign` is intentionally not thread-safe. A future repository/service layer
must serialize commands per campaign (and use optimistic versioning across
processes) before persisting each event. Sharing a mutable instance concurrently
without that boundary is unsupported.

## Architectural review note

The referenced `control_plane/` implementation was absent from the supplied
branch at the start of this hardening task, and the requested remote branch was
not available in the local clone. Therefore no pre-existing implementation
could be reviewed in place. This minimal phase-1 model documents the lifecycle
and constraints it implements; this absence should be reconciled against the
intended upstream design before Phase 2 begins.

