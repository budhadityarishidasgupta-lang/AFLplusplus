#!/usr/bin/env python3
"""Foundational architecture for a local, two-agent fuzzing system.

This module deliberately uses a simulated target.  It performs no network I/O
and is intended to demonstrate typed plans, agent communication, corpus state,
mutation channels, and coverage feedback rather than attack real services.
"""

from __future__ import annotations

import argparse
import ast
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TypedDict

LOGGER: Final[logging.Logger] = logging.getLogger("multi_agent_fuzzer")


@dataclass(frozen=True)
class TargetConfiguration:
    """A local target descriptor; network fields are metadata only."""

    mock_module_name: str
    source_path: Path
    ip: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class PayloadConstraints:
    """Limits that the planner communicates to the evolutionary agent."""

    minimum_length: int
    maximum_length: int
    encoding: str
    protocol_flags: tuple[str, ...] = ()
    preferred_tokens: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class SuccessCriteria:
    """Local execution signals that make a generated input interesting."""

    minimum_branch_coverage: float
    retain_new_paths: bool = True
    stop_on_simulated_crash: bool = True


@dataclass(frozen=True)
class TargetMetadata:
    """Structural facts obtained without executing the inspected source."""

    function_names: tuple[str, ...]
    conditional_count: int
    input_length_hint: int
    encoding: str
    protocol_flags: tuple[str, ...]


@dataclass(frozen=True)
class AttackPlan:
    """Structured hand-off produced by reconnaissance."""

    targets: tuple[TargetConfiguration, ...]
    payload_constraints: PayloadConstraints
    criteria: SuccessCriteria
    directives: str
    metadata: TargetMetadata


@dataclass
class PayloadRecord:
    """Ledger entry for one active evolutionary payload."""

    payload: bytes
    fitness_history: list[float] = field(default_factory=list)
    execution_paths: set[str] = field(default_factory=set)
    crashed: bool = False


@dataclass
class EvolutionaryQueue:
    """Mutable corpus ledger shared through :class:`AgentWorkspace`."""

    active_payloads: list[PayloadRecord] = field(default_factory=list)
    total_executions: int = 0
    crash_states: dict[bytes, str] = field(default_factory=dict)

    def record_feedback(
        self, record: PayloadRecord, fitness: float, response: TargetResponse
    ) -> None:
        """Atomically update the ledger after a mocked target execution."""
        record.fitness_history.append(fitness)
        record.execution_paths.update(response["execution_path"])
        record.crashed = response["crashed"]
        self.total_executions += 1
        if record.crashed:
            self.crash_states[record.payload] = response["status"]
        LOGGER.info(
            "feedback transferred to workspace payload=%r fitness=%.2f crashed=%s",
            record.payload,
            fitness,
            record.crashed,
        )


@dataclass
class AgentWorkspace:
    """Typed message bus shared by the planner and exploit developer."""

    target_configuration: TargetConfiguration
    attack_plan: AttackPlan | None = None
    evolutionary_queue: EvolutionaryQueue = field(default_factory=EvolutionaryQueue)

    def publish_attack_plan(self, plan: AttackPlan) -> None:
        """Publish and log a reconnaissance-to-development hand-off."""
        self.attack_plan = plan
        LOGGER.info(
            "attack plan transferred planner->developer target=%s constraints=%s",
            plan.targets[0].mock_module_name,
            plan.payload_constraints,
        )


class TargetResponse(TypedDict):
    """Instrumentation contract returned by local target implementations."""

    execution_path: tuple[str, ...]
    branches_covered: int
    total_branches: int
    branch_coverage: float
    crashed: bool
    status: str


class AbstractReconAgent(ABC):
    """Interface for agents that translate instructions into attack plans."""

    def __init__(self, workspace: AgentWorkspace) -> None:
        self.workspace = workspace

    @abstractmethod
    def process_directives(self, human_instructions: str) -> AttackPlan:
        """Analyze local context and return a structured plan."""

    @abstractmethod
    def inspect_local_target(self, source_path: Path) -> TargetMetadata:
        """Statically inspect a local target file without importing it."""


class LocalReconAgent(AbstractReconAgent):
    """AST-based reconnaissance implementation for local Python targets."""

    def inspect_local_target(self, source_path: Path) -> TargetMetadata:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        functions = tuple(
            node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        conditional_count = sum(isinstance(node, ast.If) for node in ast.walk(tree))
        metadata = TargetMetadata(
            function_names=functions,
            conditional_count=conditional_count,
            input_length_hint=4,
            encoding="bytes",
            protocol_flags=("LOCAL_ONLY", "MOCK_INSTRUMENTATION"),
        )
        LOGGER.info("recon inspected local target=%s metadata=%s", source_path, metadata)
        return metadata

    def process_directives(self, human_instructions: str) -> AttackPlan:
        if not human_instructions.strip():
            raise ValueError("human instructions must not be empty")
        target = self.workspace.target_configuration
        metadata = self.inspect_local_target(target.source_path)
        plan = AttackPlan(
            targets=(target,),
            payload_constraints=PayloadConstraints(
                minimum_length=1,
                maximum_length=max(16, metadata.input_length_hint),
                encoding=metadata.encoding,
                protocol_flags=metadata.protocol_flags,
                preferred_tokens=(b"A", b"B", b"X"),
            ),
            criteria=SuccessCriteria(minimum_branch_coverage=0.75),
            directives=human_instructions,
            metadata=metadata,
        )
        self.workspace.publish_attack_plan(plan)
        return plan


class AbstractExploitDevAgent(ABC):
    """Interface for a plan-driven evolutionary fuzzing engine."""

    def __init__(self, attack_plan: AttackPlan, *, random_seed: int = 1337) -> None:
        self.attack_plan = attack_plan
        self.random = random.Random(random_seed)

    @abstractmethod
    def initialize_seed_pool(self) -> list[bytes]:
        """Prepare the initial pseudo-random byte population."""

    @abstractmethod
    def apply_genetic_mutation(self, parent_payload: bytes) -> bytes:
        """Apply a bit flip, token deletion, or havoc insertion."""

    @abstractmethod
    def evaluate_feedback(self, target_response: TargetResponse) -> float:
        """Compute fitness from simulated target coverage."""


class EvolutionaryExploitDevAgent(AbstractExploitDevAgent):
    """Small concrete engine exposing independently extensible mutation stages."""

    def initialize_seed_pool(self) -> list[bytes]:
        constraints = self.attack_plan.payload_constraints
        pool = [
            bytes(self.random.randrange(256) for _ in range(metadata_length))
            for metadata_length in (
                self.random.randint(constraints.minimum_length, min(4, constraints.maximum_length))
                for _ in range(8)
            )
        ]
        LOGGER.info("developer initialized seed pool count=%d", len(pool))
        return pool

    def _bit_flip(self, payload: bytearray) -> None:
        if payload:
            offset = self.random.randrange(len(payload))
            payload[offset] ^= 1 << self.random.randrange(8)

    def _delete_token(self, payload: bytearray) -> None:
        if len(payload) > self.attack_plan.payload_constraints.minimum_length:
            del payload[self.random.randrange(len(payload))]

    def _havoc_insert(self, payload: bytearray) -> None:
        constraints = self.attack_plan.payload_constraints
        if len(payload) < constraints.maximum_length:
            token = self.random.choice(constraints.preferred_tokens or (b"\x00",))
            position = self.random.randrange(len(payload) + 1)
            payload[position:position] = token

    def apply_genetic_mutation(self, parent_payload: bytes) -> bytes:
        mutated = bytearray(parent_payload)
        operation = self.random.choice((self._bit_flip, self._delete_token, self._havoc_insert))
        operation(mutated)
        LOGGER.debug("mutation transferred parent=%r child=%r channel=%s", parent_payload, mutated, operation.__name__)
        return bytes(mutated)

    def evaluate_feedback(self, target_response: TargetResponse) -> float:
        depth_bonus = len(target_response["execution_path"]) * 5.0
        crash_bonus = 100.0 if target_response["crashed"] else 0.0
        fitness = target_response["branch_coverage"] * 100.0 + depth_bonus + crash_bonus
        LOGGER.info("developer evaluated target feedback fitness=%.2f", fitness)
        return fitness


class MockTargetSystem(ABC):
    """Environment interface for isolated, instrumented target execution."""

    @abstractmethod
    def receive_and_instrument(self, input_payload: bytes) -> TargetResponse:
        """Execute bytes locally and return normalized coverage feedback."""


class LocalMockTargetSystem(MockTargetSystem):
    """Deterministic target with nested paths and a simulated crash state."""

    TOTAL_BRANCHES: Final[int] = 4

    def receive_and_instrument(self, input_payload: bytes) -> TargetResponse:
        path = ["entry"]
        crashed = False
        if input_payload[:1] == b"A":
            path.append("matched_A")
            if input_payload[1:2] == b"B":
                path.append("matched_AB")
                if input_payload[2:3] == b"X":
                    path.append("matched_ABX")
                    crashed = True
        covered = len(path)
        response: TargetResponse = {
            "execution_path": tuple(path),
            "branches_covered": covered,
            "total_branches": self.TOTAL_BRANCHES,
            "branch_coverage": covered / self.TOTAL_BRANCHES,
            "crashed": crashed,
            "status": "simulated_crash" if crashed else "ok",
        }
        LOGGER.debug("mock target transferred instrumentation response=%s", response)
        return response


def run_local_demo(random_seed: int = 1337) -> AgentWorkspace:
    """Wire the components together for one harmless local demonstration pass."""
    target = TargetConfiguration("LocalMockTargetSystem", Path(__file__).resolve())
    workspace = AgentWorkspace(target)
    planner = LocalReconAgent(workspace)
    plan = planner.process_directives("Exercise only the local mocked parser")
    developer = EvolutionaryExploitDevAgent(plan, random_seed=random_seed)
    environment = LocalMockTargetSystem()

    for payload in developer.initialize_seed_pool():
        mutated = developer.apply_genetic_mutation(payload)
        record = PayloadRecord(mutated)
        workspace.evolutionary_queue.active_payloads.append(record)
        response = environment.receive_and_instrument(mutated)
        fitness = developer.evaluate_feedback(response)
        workspace.evolutionary_queue.record_feedback(record, fitness, response)
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    workspace = run_local_demo(args.seed)
    print(
        f"Local demo complete: {workspace.evolutionary_queue.total_executions} "
        f"executions, {len(workspace.evolutionary_queue.crash_states)} simulated crashes"
    )


if __name__ == "__main__":
    main()
