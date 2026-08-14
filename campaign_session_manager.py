"""Dual-mode session ingestion for the bounded loopback validation range.

Authenticated session material is placed in an unlinked file backed by the
existing ``/dev/shm`` tmpfs.  The open descriptor is the only reference, so the
kernel releases the pages on normal cleanup *and* abrupt process termination.
This module never attempts to unmount the host's shared tmpfs.
"""

from __future__ import annotations

import json
import mmap
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from enterprise_stealth_range import (
    AuthorizationScenarioAdapter,
    CampaignScope,
    ConfigurationError,
)

SESSION_CORE = Path("/dev/shm/session_core")
MAX_SESSION_BYTES = 1_048_576


class ExecutionMode(str, Enum):
    EXTERNAL_PERIMETER_AUDIT = "EXTERNAL_PERIMETER_AUDIT"
    AUTHENTICATED_SESSION = "AUTHENTICATED_SESSION"


class SessionBrowserRunner(Protocol):
    """Minimal browser boundary; concrete implementations remain dependency-free."""

    async def run_unauthenticated(self, target_url: str) -> None: ...

    async def authenticate(
        self, auth_url: str, username: str, password: str
    ) -> Mapping[str, Any]: ...


@dataclass
class VolatileSessionLease:
    """Owner of an anonymous mmap containing serialized session metadata."""

    _mapping: mmap.mmap
    _descriptor: int
    byte_length: int
    _closed: bool = False

    def read(self) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("session lease is closed")
        self._mapping.seek(0)
        return json.loads(self._mapping.read(self.byte_length))

    def close(self) -> None:
        if self._closed:
            return
        self._mapping.close()
        os.close(self._descriptor)
        self._closed = True

    def __enter__(self) -> VolatileSessionLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class VolatileSessionStore:
    """Create anonymous, process-bound session storage on a tmpfs directory."""

    def __init__(self, root: Path = SESSION_CORE, *, require_shm: bool = True) -> None:
        self.root = root
        if require_shm and root.resolve() != SESSION_CORE:
            raise ConfigurationError("session storage must be /dev/shm/session_core")

    def _prepare_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ConfigurationError("session_core must be a real directory")
        self.root.chmod(0o700)
        mode = self.root.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigurationError("session_core permissions must be 0700")

    def save(self, artifacts: Mapping[str, Any]) -> VolatileSessionLease:
        payload = self._serialize(artifacts)
        self._prepare_root()
        descriptor, name = tempfile.mkstemp(prefix="lease-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            os.unlink(name)  # no pathname survives; kernel owns lifetime
            os.ftruncate(descriptor, len(payload))
            mapping = mmap.mmap(descriptor, len(payload), access=mmap.ACCESS_WRITE)
            mapping.write(payload)
            mapping.flush()
            return VolatileSessionLease(mapping, descriptor, len(payload))
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _serialize(artifacts: Mapping[str, Any]) -> bytes:
        if not isinstance(artifacts, Mapping):
            raise ConfigurationError("browser session artifacts must be an object")
        allowed = {"cookies", "tokens", "verification"}
        if not set(artifacts) <= allowed:
            raise ConfigurationError("browser returned unsupported session artifact fields")
        try:
            payload = json.dumps(
                artifacts, separators=(",", ":"), sort_keys=True, allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ConfigurationError("session artifacts must be JSON serializable") from error
        if not payload or len(payload) > MAX_SESSION_BYTES:
            raise ConfigurationError("session artifacts exceed the volatile storage budget")
        return payload

    def cleanup(self) -> None:
        """Remove only this module's empty directory, never unmount shared tmpfs."""
        try:
            self.root.rmdir()
        except FileNotFoundError:
            return
        except OSError as error:
            raise RuntimeError("session_core is not empty; refusing recursive deletion") from error


@dataclass(frozen=True)
class SessionIngestionResult:
    mode: ExecutionMode
    target_url: str
    session: VolatileSessionLease | None = None


class CampaignSessionManager:
    """Load a unified schema and dispatch one bounded browser entry mode."""

    KNOWN_KEYS = {
        "target_scope_url",
        "username",
        "password",
        "auth_entry_route",
        "role_profile",
    }

    def __init__(
        self,
        runner: SessionBrowserRunner,
        store: VolatileSessionStore | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.runner = runner
        self.store = store or VolatileSessionStore()
        self.logger = logger or print

    @classmethod
    def load_scope(cls, path: str | Path = "campaign_scope.json") -> CampaignScope:
        with Path(path).open(encoding="utf-8") as source:
            payload = json.load(source)
        if not isinstance(payload, dict) or set(payload) != cls.KNOWN_KEYS:
            raise ConfigurationError("campaign_scope.json must contain the unified schema")

        username, password = payload["username"], payload["password"]
        if username is None and password is None:
            if payload["auth_entry_route"] is not None or payload["role_profile"] is not None:
                raise ConfigurationError("unauthenticated mode cannot define auth parameters")
            return AuthorizationScenarioAdapter.from_payload(
                {"target_scope_url": payload["target_scope_url"]}
            )
        if username is None or password is None:
            raise ConfigurationError("username and password must both be null or both be set")
        return AuthorizationScenarioAdapter.from_payload(payload)

    async def ingest(self, scope: CampaignScope) -> SessionIngestionResult:
        if scope.username is None:
            self.logger("EXECUTION_MODE: EXTERNAL_PERIMETER_AUDIT")
            await self.runner.run_unauthenticated(scope.target_scope_url)
            return SessionIngestionResult(
                ExecutionMode.EXTERNAL_PERIMETER_AUDIT, scope.target_scope_url
            )

        self.logger("EXECUTION_MODE: AUTHENTICATED_SESSION")
        assert scope.auth_entry_route is not None and scope.password is not None
        artifacts = await self.runner.authenticate(
            scope.auth_entry_route, scope.username, scope.password
        )
        lease = self.store.save(artifacts)
        return SessionIngestionResult(
            ExecutionMode.AUTHENTICATED_SESSION, scope.target_scope_url, lease
        )
