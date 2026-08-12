#!/usr/bin/env python3
"""Small, dependency-free evolutionary grey-box fuzzing demonstration.

The program deliberately contains a mock bug.  It is intended for local
education: it does not launch processes, open files, or test external targets.
"""

from __future__ import annotations

import argparse
import random
import time
from dataclasses import dataclass


class SimulatedTargetCrash(IndexError):
    """The intentional fault used to distinguish a finding from fuzzer bugs."""


def target_application(user_input_bytes: bytes, trace: set[str] | None = None) -> set[str]:
    """Parse *user_input_bytes* and return the blocks visited by this run.

    ``trace`` is injectable instrumentation, similar in spirit to the shared
    coverage bitmap used by AFL++.  Length checks make normal short inputs safe;
    only the deeply nested ``ABX`` path deliberately raises an IndexError.
    """
    blocks = trace if trace is not None else set()
    blocks.add("entry")

    if len(user_input_bytes) < 1:
        blocks.add("empty")
        return blocks
    blocks.add("has_byte_0")

    if user_input_bytes[0] == ord("A"):
        blocks.add("matched_A")
        if len(user_input_bytes) >= 2 and user_input_bytes[1] == ord("B"):
            blocks.add("matched_AB")
            if len(user_input_bytes) >= 3 and user_input_bytes[2] == ord("X"):
                blocks.add("matched_ABX")
                # Deliberate mock vulnerability: model an unsafe parser access.
                raise SimulatedTargetCrash("simulated out-of-bounds access")
            blocks.add("rejected_third_byte")
        else:
            blocks.add("rejected_second_byte")
    else:
        blocks.add("rejected_first_byte")

    return blocks


DEPTH_BY_BLOCK = {
    "entry": 0,
    "has_byte_0": 1,
    "matched_A": 2,
    "matched_AB": 3,
    "matched_ABX": 4,
}


@dataclass(frozen=True)
class Candidate:
    payload: bytes
    coverage: frozenset[str]
    depth: int
    fitness: int


def run_target(payload: bytes, global_coverage: set[str]) -> tuple[Candidate, bool]:
    """Execute one input and score depth plus newly discovered coverage."""
    trace: set[str] = set()
    crashed = False
    try:
        target_application(payload, trace)
    except SimulatedTargetCrash:
        crashed = True

    depth = max((DEPTH_BY_BLOCK.get(block, 0) for block in trace), default=0)
    new_blocks = trace - global_coverage
    # Depth dominates the score, while novelty rewards alternate/new branches.
    fitness = depth * 100 + len(trace) + len(new_blocks) * 20
    global_coverage.update(trace)
    return Candidate(payload, frozenset(trace), depth, fitness), crashed


INTERESTING_BYTES = b"ABX!0123456789abcdefghijklmnopqrstuvwxyz"


def mutate(parent: bytes, rng: random.Random, max_length: int = 32) -> bytes:
    """Apply one to four AFL-style bit-flip, replacement, or append mutations."""
    data = bytearray(parent)
    for _ in range(rng.randint(1, 4)):  # a small "havoc" stack
        operation = rng.choice(("bit_flip", "replace", "append"))
        if operation == "bit_flip" and data:
            index = rng.randrange(len(data))
            data[index] ^= 1 << rng.randrange(8)
        elif operation == "replace" and data:
            index = rng.randrange(len(data))
            data[index] = rng.choice(INTERESTING_BYTES) if rng.random() < 0.7 else rng.randrange(256)
        elif len(data) < max_length:
            data.append(rng.choice(INTERESTING_BYTES) if rng.random() < 0.7 else rng.randrange(256))
    return bytes(data)


def fuzz(max_generations: int = 1_000, population_size: int = 160, seed: int = 1337) -> bool:
    """Evolve inputs, print a report on the first crash, and return success."""
    rng = random.Random(seed)
    global_coverage: set[str] = set()
    executions = 0
    started = time.perf_counter()

    queue: list[Candidate] = []
    for _ in range(24):
        payload = bytes(rng.randrange(256) for _ in range(4))
        candidate, crashed = run_target(payload, global_coverage)
        executions += 1
        queue.append(candidate)
        if crashed:  # Practically impossible, but keeps all executions correct.
            return report_crash(candidate.payload, 0, executions, started)

    for generation in range(1, max_generations + 1):
        # Favor deep and high-scoring seeds, retaining diversity by coverage.
        parents = sorted(queue, key=lambda item: (item.depth, item.fitness), reverse=True)[:12]
        best_depth = max(item.depth for item in queue)
        for _ in range(population_size):
            parent = rng.choice(parents)
            payload = mutate(parent.payload, rng)
            candidate, crashed = run_target(payload, global_coverage)
            executions += 1
            if crashed:
                return report_crash(payload, generation, executions, started)
            if candidate.depth > best_depth or not any(
                candidate.coverage <= old.coverage for old in queue
            ):
                queue.append(candidate)
                best_depth = max(best_depth, candidate.depth)

        # Bound the favored queue while keeping the best representative of each
        # coverage signature, just as corpus minimization avoids redundant seeds.
        unique: dict[frozenset[str], Candidate] = {}
        for item in sorted(queue, key=lambda value: value.fitness, reverse=True):
            unique.setdefault(item.coverage, item)
        queue = sorted(unique.values(), key=lambda item: item.fitness, reverse=True)[:64]

    elapsed = max(time.perf_counter() - started, 1e-9)
    print(f"No crash after {executions:,} executions ({executions / elapsed:,.0f} exec/s).")
    print(f"Maximum path depth reached: {max(item.depth for item in queue)}")
    return False


def report_crash(payload: bytes, generation: int, executions: int, started: float) -> bool:
    elapsed = max(time.perf_counter() - started, 1e-9)
    print("\n=== Simulated crash discovered ===")
    print(f"Payload (bytes): {payload!r}")
    print(f"Payload (escaped): {payload.decode('ascii', errors='backslashreplace')!r}")
    print(f"Generation: {generation}")
    print(f"Executions: {executions:,}")
    print(f"Executions/second: {executions / elapsed:,.0f}")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generations", type=int, default=1_000)
    parser.add_argument("--population", type=int, default=160)
    parser.add_argument("--seed", type=int, default=1337, help="PRNG seed for reproducible runs")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(0 if fuzz(args.generations, args.population, args.seed) else 1)
