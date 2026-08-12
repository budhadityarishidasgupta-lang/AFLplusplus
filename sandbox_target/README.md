# Disposable local streaming target

This fixture is intentionally local-only and resettable. It exposes a health endpoint, an admin authentication check, and a designated test creator settings route for validation.

## Start the fixture

```bash
docker compose up mock-streaming
```

Or without Docker:

```bash
python3 mock_streaming_service.py --host 127.0.0.1 --port 3000
```

The service refuses non-loopback binding. It accepts only the two synthetic tokens in the source. Creator sessions must not modify creator settings; admin requests default to dry-run unless explicitly changed by a future approved adapter.

## Passive ZAP plan

The `zap-passive.yaml` plan is scoped to `http://127.0.0.1:3000`, runs a short spider, waits for passive processing, and writes a report. It does not enable active scanning.

```bash
docker run --rm --network host \
  -v "$PWD/sandbox_target:/zap/wrk:ro" \
  -v "$PWD/reports:/zap/wrk/reports" \
  ghcr.io/zaproxy/zaproxy:stable \
  zap.sh -cmd -autorun /zap/wrk/zap-passive.yaml
```

Run only against this disposable fixture. The host-network command is deliberately limited to loopback targets by the plan; do not reuse it with external URLs.
