#!/usr/bin/env python3
"""Disposable local streaming authorization fixture; standard library only."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREATOR_ID = "creator_test_99"
SETTINGS = {"stream_enabled": True, "visibility": "private"}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockStreaming/0.1"

    def _json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _role(self) -> str | None:
        token = self.headers.get("Authorization", "")
        return {"Bearer admin-test-token": "admin", "Bearer creator-test-token": "creator"}.get(token)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            return self._json(200, {"status": "ok", "local_only": True})
        if self.path == "/admin/auth-check":
            role = self._role()
            return self._json(200 if role == "admin" else 403, {"authenticated": role == "admin", "role": role})
        if self.path == f"/api/v1/creators/{CREATOR_ID}/settings":
            role = self._role()
            return self._json(200 if role in {"admin", "creator"} else 401, {"creator_id": CREATOR_ID, "settings": SETTINGS})
        return self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != f"/api/v1/creators/{CREATOR_ID}/settings":
            return self._json(404, {"error": "not_found"})
        role = self._role()
        if role != "admin":
            return self._json(403, {"error": "authorization_denied", "state_changed": False})
        length = int(self.headers.get("Content-Length", "0"))
        patch = json.loads(self.rfile.read(length) or b"{}")
        dry_run = self.headers.get("X-Dry-Run", "true").lower() == "true"
        if not dry_run:
            SETTINGS.update(patch)
        return self._json(200, {"status": "validated", "state_changed": not dry_run, "dry_run": dry_run})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[mock-streaming] {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("refusing non-loopback host")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
