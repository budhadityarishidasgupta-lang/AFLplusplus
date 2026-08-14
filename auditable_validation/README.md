# Auditable authorization validation

This package runs a finite, read-only authorization assertion matrix against the approved staging range `http://localhost:3000`. It does not guess credentials, mutate creator settings, inject code, or alter server logs.

Configured paths:

```text
TARGET_SCOPE_URL=http://localhost:3000
ADMIN_LOGIN_PATH=/admin/login
CREATOR_SETTINGS_PATH=/api/v1/creators/creator_test_99/settings
TEST_ACCOUNT_ID=creator_test_99
RUNTIME_DIR=/dev/shm/fuzzer_runtime
AUDIT_PATH=./logs/audit_trail.csv
REPORT_PATH=./logs/PATCH_ADVISORY.md
```

## Run

```bash
mkdir -p audit reports
TARGET_SCOPE_URL=http://target:3000 CAMPAIGN_ID=validation-01 \
  docker compose up --abort-on-container-exit authorization-worker
```

The worker uses `/dev/shm/fuzzer_runtime` as a volatile tmpfs workspace. Campaign IDs, execution hashes, assertion outcomes, and response fingerprints are appended to `./logs/audit_trail.csv`, which is outside the volatile workspace. The developer-facing report is written to `./logs/PATCH_ADVISORY.md`. The Compose network is internal and publishes no ports.

The adapter sends only unauthenticated `GET` assertions to `/admin/login` and `/api/v1/creators/creator_test_99/settings`. Expected `401`/`403` responses pass; `200`/`204` is reported as a finding; other responses are errors requiring review. No account-setting mutation is performed.

## Redacted export

```bash
python3 report_export.py PATCH_ADVISORY.raw.md reports/PATCH_ADVISORY.md
```

The exporter creates a redacted developer copy while preserving the original audit evidence under access control. It masks bearer tokens, cookies, passwords, secrets, and token-like key/value fields. It does not erase server logs or shell history.
