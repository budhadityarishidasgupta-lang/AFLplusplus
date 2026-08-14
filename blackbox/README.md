# Isolated black-box staging harness

This harness gives the worker only `TARGET_SCOPE_URL` and runs it beside the target on an internal Docker network. No ports are published and the network is marked `internal`, so the compose stack has no intended public egress path.

## Run

From this directory:

```bash
TARGET_SCOPE_URL=http://target:3000 docker compose up --abort-on-container-exit blackbox-worker
```

Accepted examples are `http://target:3000`, `http://mock-streaming:3000`, `http://localhost:3000`, `http://127.0.0.1:3000`, or an approved `*.local` HTTP host. HTTPS, credentials in URLs, external domains, arbitrary ports, paths, queries, and fragments are rejected.

The initial worker is a **passive initialization scaffold**. It validates the scope and writes `/reports/scope.json`; it does not perform arbitrary port scanning, credential guessing, exploit generation, or destructive account changes. Those operations require an explicitly approved test plan and must remain bounded, resettable, and evidence-first.

The `internal: true` Docker network is a defense-in-depth boundary, not a substitute for cloud firewall rules. Deploy the stack only in a dedicated staging VPC/project with no route to production data or credentials.
