# Workflow status ledger

This append-only ledger coordinates deterministic hand-offs. Agents must not
edit or delete earlier entries. The Gateway/lead operator owns state
transitions; workers may only propose their next state.

## Allowed states

`QUEUED → ANALYZING → PLAN_READY → FUZZING → FEEDBACK_READY → COMPLETE`

`BLOCKED` may be entered from any state when validation or sandbox policy fails.

## Entry format

```markdown
### YYYY-MM-DDTHH:MM:SSZ | cycle_id=<UUID> | agent=<agent-id>
- State: <allowed-state>
- Input: <workspace-relative path or none>
- Output: <workspace-relative path or none>
- Seed: <unsigned integer or none>
- Summary: <single-line, no raw payload bytes>
- Next agent: <agent-id or none>
```

## Activity

<!-- Agents append new entries below this line. -->

