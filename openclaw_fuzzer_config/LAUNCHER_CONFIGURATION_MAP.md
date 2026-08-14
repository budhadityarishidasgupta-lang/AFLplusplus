# Conversational launcher configuration map

```text
Administrator terminal
  └─ conversational_launcher.py             asynchronous console owner
       ├─ campaign_scope.json                atomic 0600 unified scope
       ├─ campaign_session_manager.py        default subprocess preflight
       ├─ enterprise_stealth_range.py        loopback/schema invariants
       ├─ /dev/shm/session_core/             volatile unlinked session leases
       └─ CLIENT_REPORT.md                   parameter-redacted denial report
```

The console accepts `test URL`, optionally followed by a complete `user=...`
and `pass=...` pair. Shell-style quoting is supported for values containing
spaces. Only literal `127.0.0.1` HTTP(S) targets pass the shared campaign
validator. An active campaign freezes its target scope; only `status`, `pause`,
`resume`, and `cancel` remain available.

## Worker process contract

By default, the launcher executes `campaign_session_manager.py --inspect` as a
safe preflight subprocess. Deployments supply the concrete, previously reviewed
browser worker after `--worker-command`. The command receives the scope through
the configured file rather than command-line credentials and must preserve the
loopback and `/dev/shm` policies.

Worker standard output is relayed with a `[worker]` prefix. A line beginning
`SUPERVISOR_EVENT:` requests an immediate process-group `SIGSTOP` and approval
checkpoint. Approval writes `VERIFY_READ_ONLY` followed by `RESUME` to worker
standard input and sends `SIGCONT`. Denial terminates the process group, removes
only an empty session cache directory, and writes a report containing a target
hash rather than the URL or credentials.
