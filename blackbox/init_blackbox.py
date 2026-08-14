#!/usr/bin/env python3
"""Initialize a bounded black-box staging run.

The worker receives only TARGET_SCOPE_URL. It is intentionally limited to an
allowlisted local/container host and does not perform arbitrary port scanning,
credential guessing, or exploit generation.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOST = re.compile(r"^(localhost|127\.0\.0\.1|target|mock-streaming|[a-z0-9-]+\.local)$")
ALLOWED_SCHEME = {"http"}
ALLOWED_PORTS = {80, 3000, 8000, 8080}


@dataclass(frozen=True)
class Scope:
    url: str
    scheme: str
    host: str
    port: int


def validate_scope(raw: str) -> Scope:
    if not raw or len(raw) > 255:
        raise ValueError("TARGET_SCOPE_URL is required and must be <=255 characters")
    if any(char in raw for char in "\r\n\t @"):
        raise ValueError("TARGET_SCOPE_URL contains forbidden characters")
    parsed = urlparse(raw)
    if parsed.scheme not in ALLOWED_SCHEME or parsed.username or parsed.password:
        raise ValueError("TARGET_SCOPE_URL must be plain HTTP without credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("TARGET_SCOPE_URL must contain only scheme, host, and optional port")
    host = (parsed.hostname or "").lower()
    if not ALLOWED_HOST.fullmatch(host):
        raise ValueError("target host is outside the local/container allowlist")
    port = parsed.port or 80
    if port not in ALLOWED_PORTS:
        raise ValueError("target port is outside the approved bounded set")
    return Scope(raw.rstrip("/"), parsed.scheme, host, port)


def main() -> int:
    try:
        scope = validate_scope(os.environ.get("TARGET_SCOPE_URL", ""))
    except ValueError as error:
        print(f"SCOPE_REJECTED: {error}", file=sys.stderr)
        return 2
    output = Path(os.environ.get("RUN_DIR", "/reports"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "scope.json").write_text(json.dumps({"scope": asdict(scope), "max_attempts": 50, "mode": "passive-black-box"}, indent=2) + "\n", encoding="utf-8")
    print(f"SCOPE_ACCEPTED: {scope.url}")
    print("MODE: passive-black-box; arbitrary port scanning and credential guessing disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
