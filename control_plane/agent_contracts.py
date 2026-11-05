"""Offline, declarative contracts for discovering specialist agents.

Nothing in this module imports or executes legacy agents. Registry membership is
metadata only: it grants neither permission nor readiness to execute.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

_IDENTIFIER = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9._:-]{0,126}[a-zA-Z0-9])?$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class ContractError(ValueError):
    """Base class for deterministic contract and registry errors."""


class ValidationError(ContractError):
    """A declarative contract is malformed or internally inconsistent."""


class DuplicateAgentError(ContractError):
    """An agent identifier is already registered."""


class AgentNotFoundError(ContractError):
    """An operation references an agent that is not registered."""


class CapabilityNotFoundError(ContractError):
    """No registered agent advertises the requested capability."""


class CapabilityStatus(str, Enum):
    """Knowledge/evidence status, deliberately separate from authorization."""

    DISCOVERED = "discovered"
    UNDERSTOOD = "understood"
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: str, label: str) -> str:
    value = _required_text(value, label)
    if len(value) > 128 or not _IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{label} must be a 1-128 character URL-safe identifier")
    return value


def _text_tuple(values: tuple[str, ...], label: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list, frozenset)):
        raise ValidationError(f"{label} must be a collection of strings")
    clean = tuple(
        _identifier(value, f"{label} item") if identifiers else _required_text(value, f"{label} item")
        for value in values
    )
    if len(set(clean)) != len(clean):
        raise ValidationError(f"{label} must not contain duplicates")
    return tuple(sorted(clean))


def _frozen_mapping(value: Mapping[str, str], label: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        key = _identifier(key, f"{label} key")
        result[key] = _required_text(item, f"{label}[{key}]")
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True)
class AgentContract:
    """Validated, immutable description of an agent; not an executable agent."""

    agent_id: str
    purpose: str
    version: str
    capabilities: tuple[str, ...]
    supported_tasks: tuple[str, ...]
    inputs: Mapping[str, str]
    outputs: Mapping[str, str]
    permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _identifier(self.agent_id, "agent_id"))
        object.__setattr__(self, "purpose", _required_text(self.purpose, "purpose"))
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ValidationError("version must be a semantic version (major.minor.patch)")
        for name in ("capabilities", "supported_tasks", "permissions"):
            object.__setattr__(
                self, name, _text_tuple(getattr(self, name), name, identifiers=True)
            )
        if not self.capabilities:
            raise ValidationError("capabilities must contain at least one capability")
        object.__setattr__(self, "inputs", _frozen_mapping(self.inputs, "inputs"))
        object.__setattr__(self, "outputs", _frozen_mapping(self.outputs, "outputs"))


@dataclass(frozen=True)
class TaskContract:
    """Declarative requested work and its authorization/output requirements."""

    task_id: str
    objective: str
    requested_capability: str
    constraints: Mapping[str, str] = field(default_factory=dict)
    authorization_requirements: tuple[str, ...] = ()
    expected_output: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        object.__setattr__(self, "objective", _required_text(self.objective, "objective"))
        object.__setattr__(
            self, "requested_capability", _identifier(self.requested_capability, "requested_capability")
        )
        object.__setattr__(self, "constraints", _frozen_mapping(self.constraints, "constraints"))
        object.__setattr__(
            self,
            "authorization_requirements",
            _text_tuple(self.authorization_requirements, "authorization_requirements", identifiers=True),
        )
        object.__setattr__(self, "expected_output", _required_text(self.expected_output, "expected_output"))


@runtime_checkable
class AgentAdapter(Protocol):
    """Future integration seam; intentionally defines no execution method."""

    @property
    def contract(self) -> AgentContract:
        """Return declarative metadata for the adapted agent."""


@dataclass(frozen=True)
class AgentRegistration:
    """Registry view separating discovery status from readiness/authorization."""

    contract: AgentContract
    status: CapabilityStatus = CapabilityStatus.DISCOVERED
    execution_ready: bool = False
    authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.contract, AgentContract):
            raise ValidationError("contract must be an AgentContract")
        if not isinstance(self.status, CapabilityStatus):
            raise ValidationError("status must be a CapabilityStatus")
        if not isinstance(self.execution_ready, bool) or not isinstance(self.authorized, bool):
            raise ValidationError("execution_ready and authorized must be booleans")
        if self.execution_ready or self.authorized:
            raise ValidationError("registration cannot grant execution readiness or authorization")


class CapabilityRegistry:
    """In-memory metadata registry with deterministic operations and no imports."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentRegistration] = {}

    def register(
        self, contract: AgentContract, status: CapabilityStatus = CapabilityStatus.DISCOVERED
    ) -> AgentRegistration:
        if not isinstance(contract, AgentContract):
            raise ValidationError("contract must be an AgentContract")
        if not isinstance(status, CapabilityStatus):
            raise ValidationError("status must be a CapabilityStatus")
        if contract.agent_id in self._agents:
            raise DuplicateAgentError(f"agent already registered: {contract.agent_id}")
        registration = AgentRegistration(contract, status)
        self._agents[contract.agent_id] = registration
        return registration

    def discover(self, agent_id: str) -> AgentRegistration:
        agent_id = _identifier(agent_id, "agent_id")
        try:
            return self._agents[agent_id]
        except KeyError as error:
            raise AgentNotFoundError(f"agent not registered: {agent_id}") from error

    def query(self, capability: str) -> tuple[AgentRegistration, ...]:
        capability = _identifier(capability, "capability")
        matches = tuple(
            self._agents[agent_id]
            for agent_id in sorted(self._agents)
            if capability in self._agents[agent_id].contract.capabilities
        )
        if not matches:
            raise CapabilityNotFoundError(f"no registered agent provides capability: {capability}")
        return matches

    def list_agents(self) -> tuple[AgentRegistration, ...]:
        return tuple(self._agents[agent_id] for agent_id in sorted(self._agents))

    def unregister(self, agent_id: str) -> AgentRegistration:
        agent_id = _identifier(agent_id, "agent_id")
        try:
            return self._agents.pop(agent_id)
        except KeyError as error:
            raise AgentNotFoundError(f"agent not registered: {agent_id}") from error

    def set_status(self, agent_id: str, status: CapabilityStatus) -> AgentRegistration:
        """Record explicit knowledge changes; never grants readiness or access."""
        current = self.discover(agent_id)
        if not isinstance(status, CapabilityStatus):
            raise ValidationError("status must be a CapabilityStatus")
        updated = AgentRegistration(current.contract, status)
        self._agents[current.contract.agent_id] = updated
        return updated
