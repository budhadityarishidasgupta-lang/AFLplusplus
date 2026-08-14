# Auditable authorization validation

This package runs a finite, read-only authorization assertion matrix against an approved local staging target. It does not guess credentials, mutate creator settings, inject code, or alter server logs.

## Run

```bash
mkdir -p audit reports
TARGET_SCOPE_URL=http://target:3000 CAMPAIGN_ID=validation-01 \
  docker compose up --abort-on-container-exit authorization-worker
```

The worker uses `/runtime` as a volatile tmpfs workspace. Campaign IDs, execution hashes, assertion outcomes, and response fingerprints are appended to `audit/audit.jsonl`, which is outside the volatile workspace. The Compose network is internal and publishes no ports.

The adapter sends only unauthenticated `GET` assertions to `/admin/login` and `/api/v1/creators/creator_test_99/settings`. Expected `401`/`403` responses pass; `200`/`204` is reported as a finding; other responses are errors requiring review. No account-setting mutation is performed.

## Redacted export

```bash
python3 report_export.py PATCH_ADVISORY.raw.md reports/PATCH_ADVISORY.md
```

The exporter creates a redacted developer copy while preserving the original audit evidence under access control. It masks bearer tokens, cookies, passwords, secrets, and token-like key/value fields. It does not erase server logs or shell history.
