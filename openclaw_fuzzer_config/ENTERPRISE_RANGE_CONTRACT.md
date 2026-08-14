# Bounded enterprise browser QA contract

`enterprise_stealth_range.py` is a historical compatibility name, not a claim
of covert operation. The implementation is restricted to literal
`127.0.0.1`, discloses automation, and rejects fingerprint spoofing and remote
proxy gateways. It must not be adapted to bypass bot controls or access gates.

## Campaign shapes and routing

`campaign_scope.json` is either the exact zero-knowledge shape containing only
`target_scope_url`, or the exact credentialed shape adding `username`,
`password`, `auth_entry_route`, and `role_profile`. Partial credentials and
unknown keys are rejected. URLs must remain on one loopback origin.

The zero-knowledge route performs login-gate UI analysis. The credentialed
route performs ordinary authorization regression checks for `standard_user` or
`admin_test`; it does not fuzz privilege escalation. Credentials are never
logged, and session state belongs to the concrete browser's memory context.

## Runtime safety

At 500 attempts the browser is stopped, the append-only CSV is flushed, a
`TIMEOUT_ALERT.md` explanation is written, and `BudgetExhaustedError` is raised.
An unexpected boundary condition stops browser activity before recording a
SHA-256 classification and short secret-redacted fingerprint. The pipeline may
resume only after an explicit terminal Yes decision.

The asynchronous live channel accepts newline-delimited JSON for `set_path`,
bounded `set_pacing_ms`, and `snapshot`. Every accepted or rejected command is
audited. Commands cannot change host, credentials, network policy, or safety
limits.
