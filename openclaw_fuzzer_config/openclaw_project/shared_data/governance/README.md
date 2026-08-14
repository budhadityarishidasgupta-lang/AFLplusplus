# Governance message bus

```text
agents/exploit_developer
  └─ writes only after a structural change is detected:
     shared_data/governance/requests/Modification_Request.json
          │
          ▼
agents/system_architect
  ├─ validates schema, digest, paths, risk, tests, rollback, redaction
  ├─ suspends the Lobster flow
  ├─ presents the exact human Yes/No gate
  └─ writes a redacted decision to:
     shared_data/governance/responses/<request_id>.json
          │
          ▼
workflow checkpoint
  ├─ APPROVED: builder may act only on the approved digest
  ├─ REJECTED: request is closed; lower agent uses read-only alternative
  └─ BLOCKED: execution remains suspended
```

The request directory is a controlled handoff, not a general workspace. Lower agents must not write scripts, configuration, generated code, or Git state there. Requests contain metadata and specifications only; secrets, raw tokens, personal data, and exploit-ready payloads are forbidden.

Each response must include `request_id`, `request_digest`, `operator`, `approved`, `timestamp`, `validation_status`, and a redacted comment. The workflow must verify that the response digest matches the request before resuming.
