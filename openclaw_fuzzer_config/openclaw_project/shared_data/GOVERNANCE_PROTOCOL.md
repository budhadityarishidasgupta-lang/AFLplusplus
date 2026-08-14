# Hierarchical governance protocol

## Roles

| Role | Authority |
|---|---|
| `recon_planner_01` | Read-only planning and redacted evidence analysis. |
| `exploit_developer_01` | Bounded staging observation and approved test execution. No protected mutation authority. |
| `system_architect_01` | Intercepts protected requests, validates structure, and presents the operator gate. Cannot self-approve. |
| Human Operator | Explicit Yes/No decision for the exact summarized change. |

## Protected request lifecycle

```text
REQUESTED
  -> HALTED_FOR_OPERATOR
  -> APPROVED or REJECTED
  -> VALIDATING
  -> COMMITTED or BLOCKED
```

A request must include a unique request ID, campaign ID, exact paths/actions, change summary, risk, tests, rollback, and redaction plan. The operator approval must bind to a canonical digest of that request. Any change to paths, content, target, or action requires a new request and a new Yes/No decision.

## Enforcement rules

Protected operations include file generation/editing, configuration and dependency changes, target-state mutation, and all Git add/commit/push/merge/rebase/reset actions. Lower agents may read approved inputs, perform bounded non-mutating tests, and append redacted run logs only when the campaign policy permits it.

The Architect must reject requests that add secrets, raw tokens, personal data, exploit-ready payloads, unapproved hosts/routes, destructive actions, or missing rollback/reset evidence. Rejected or blocked requests cannot be retried through another agent.

## Operator prompt

> CRITICAL SYSTEM MODIFICATION DETECTED. Do you authorize consulting Codex/Manus to build and commit this code change? [Yes/No]

Record the response, timestamp, operator identity, request digest, validation result, and commit SHA in the append-only governance log.
