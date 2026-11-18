"""Local owner-presence authority for supervised synthetic simulation.

MVP local-development gate only, not production-grade authentication. The owner
opts in via process environment and confirms through a local terminal challenge.
"""
from __future__ import annotations

import hmac
import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable


class OwnerSimulationError(ValueError):
    """Owner simulation is unavailable or not explicitly authorized."""


@dataclass(frozen=True)
class OwnerSimulationAuthority:
    """Process-local, adapter-scoped authority returned by local confirmation."""
    adapter_ids: frozenset[str]
    _nonce: object


_AUTHORITY_NONCE = object()
_AUTHORITY_LOCK = threading.RLock()
_AUTHORITY: OwnerSimulationAuthority | None = None


def _is_local_dev(env: dict[str, str]) -> bool:
    return (env.get("SOVEREIGN_BRAIN_ENV", "").strip().casefold() == "local-development"
            and env.get("SOVEREIGN_BRAIN_ENABLE_OWNER_SIMULATION", "").strip() == "1"
            and env.get("STREAMLIT_SERVER_HEADLESS", "").strip().casefold() not in {"1", "true"})


def acquire_owner_authority(*, input_fn: Callable[[str], str] | None = None,
                            environ: dict[str, str] | None = None) -> OwnerSimulationAuthority:
    """Require local development opt-in plus direct terminal confirmation.

    Streamlit browser input and session state are not consulted; stdin must be
    attached to the local interactive process terminal.
    """
    env = os.environ if environ is None else environ
    if not _is_local_dev(env):
        raise OwnerSimulationError("Owner simulation is disabled outside an explicitly enabled local development process.")
    if not sys.stdin.isatty():
        raise OwnerSimulationError("Owner presence requires confirmation in the local interactive terminal.")
    reader = input if input_fn is None else input_fn
    challenge = "AUTHORIZE LOCAL SYNTHETIC SIMULATION"
    try:
        response = reader(f"Type exactly '{challenge}' in this local terminal to enable synthetic simulation: ")
    except (EOFError, OSError) as error:
        raise OwnerSimulationError("Local owner confirmation was unavailable.") from error
    if not isinstance(response, str) or not hmac.compare_digest(response.strip(), challenge):
        raise OwnerSimulationError("Local owner confirmation did not match.")
    authority = OwnerSimulationAuthority(frozenset({"synthetic.brain-owner-simulation"}), _AUTHORITY_NONCE)
    global _AUTHORITY
    with _AUTHORITY_LOCK:
        _AUTHORITY = authority
        object.__setattr__(authority, "_service", None)
    return authority


def validate_owner_authority(authority: object, adapter_id: str) -> bool:
    """Validate exact process-issued authority and adapter scope."""
    with _AUTHORITY_LOCK:
        return (isinstance(authority, OwnerSimulationAuthority)
                and authority is _AUTHORITY
                and authority._nonce is _AUTHORITY_NONCE
                and adapter_id in authority.adapter_ids
                and adapter_id == "synthetic.brain-owner-simulation")


def revoke_owner_authority() -> None:
    """Revoke process authority; durable claims and history are untouched."""
    global _AUTHORITY
    with _AUTHORITY_LOCK:
        _AUTHORITY = None
