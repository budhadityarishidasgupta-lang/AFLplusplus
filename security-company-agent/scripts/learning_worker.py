"""Standalone Windows-compatible cognitive learning worker CLI.

The default source and analyzer are deterministic test fixtures. Supply Python
factory modules from integration deployments to enable real sources/inference.
"""
from __future__ import annotations

import argparse
import importlib
import json
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brain_core.autonomous_learning import CognitiveLearningCycle, LearningMemory, LearningWorker


def load_factory(spec: str | None):
    if not spec:
        return None
    module, separator, attribute = spec.partition(":")
    if not separator:
        raise ValueError("Factory must use module:callable syntax")
    return getattr(importlib.import_module(module), attribute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "brain_core" / "studio_data" / "autonomous-learning.sqlite3"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one autonomous learning cycle")
    mode.add_argument("--cycles", type=int, help="Run at most N cycles")
    mode.add_argument("--continuous", action="store_true", help="Run until stopped")
    parser.add_argument("--sources-factory", help="Integration factory module:callable returning LearningSource list")
    parser.add_argument("--analyzer-factory", help="Integration factory module:callable returning CognitiveAnalyzer or None")
    parser.add_argument("--idle-seconds", type=float, default=2.0)
    parser.add_argument("--status", action="store_true", help="Show pending checkpoint count and exit")
    args = parser.parse_args(argv)

    memory = LearningMemory(args.db)
    memory.initialize()
    if args.status:
        print(json.dumps({"database": args.db, "pending": memory.pending_activities()}, indent=2))
        return 0
    sources_factory = load_factory(args.sources_factory)
    analyzer_factory = load_factory(args.analyzer_factory)
    sources = list(sources_factory() if sources_factory else [])
    analyzer = analyzer_factory() if analyzer_factory else None
    cycle = CognitiveLearningCycle(memory, sources, analyzer)
    worker = LearningWorker(cycle, idle_seconds=args.idle_seconds)
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    cycles = 1 if args.once or (not args.cycles and not args.continuous) else (None if args.continuous else args.cycles)
    try:
        for result in worker.run(cycles):
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    except KeyboardInterrupt:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
