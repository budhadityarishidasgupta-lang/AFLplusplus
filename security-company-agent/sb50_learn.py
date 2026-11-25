"""Windows-friendly command line for Sovereign Brain's local learning queue.

Provider calls are deliberately not configured implicitly. Applications may
construct AutonomousLearningWorker with already-authorized adapters; this CLI
ships with an offline mode so task planning and queue maintenance work without
inference resources or network access.
"""
from __future__ import annotations

import argparse
import json

from pathlib import Path
import sys
import sqlite3

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from brain_core.sb50_learning import LearningMode, LearningStore, AutonomousLearningWorker


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sovereign Brain autonomous learning queue")
    parser.add_argument("--db", help="Learning SQLite database (defaults under SOVEREIGNBRAIN_RUNTIME_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue", help="Add a curiosity question")
    enqueue.add_argument("question")
    enqueue.add_argument("--topic", default="general")
    enqueue.add_argument("--reason", default="operator-seeded curiosity")
    enqueue.add_argument("--priority", type=float, default=0.5)
    enqueue.add_argument("--novelty", type=float, default=0.5)
    commands.add_parser("status", help="Show worker controls and queued learning tasks")
    events = commands.add_parser("events", help="Show sanitized worker activity and task attribution")
    events.add_argument("--limit", type=int, default=50)
    configure = commands.add_parser("configure", help="Configure standing learning mode and worker state")
    configure.add_argument("--mode", choices=[mode.value for mode in LearningMode], default=LearningMode.HYBRID.value)
    configure.add_argument("--state", choices=["running", "paused", "stopped"], default="running")
    commands.add_parser("learned", help="Show persistent learning records")
    for name in ("pause", "resume", "stop"):
        commands.add_parser(name, help=f"Set persistent worker state to {name}")
    once = commands.add_parser("once", help="Perform one cycle (offline CLI reports pending provider)")
    once.add_argument("--mode", choices=[mode.value for mode in LearningMode], default=LearningMode.HYBRID.value)
    run = commands.add_parser("run", help="Run a bounded number of cycles")
    run.add_argument("--cycles", type=int, default=1)
    run.add_argument("--continuous", action="store_true", help="Continue up to the required finite --max-cycles bound")
    run.add_argument("--max-cycles", type=int, default=20)
    run.add_argument("--pace", type=float, default=0.0)
    args = parser.parse_args(argv)
    store = LearningStore(args.db)

    try:
        if args.command in {"status", "events", "learned", "pause", "resume", "stop", "configure"}:
            store.recover_interrupted()
        if args.command == "enqueue":
            task, created = store.enqueue(args.question, topic=args.topic, reason=args.reason,
                                          priority=args.priority, novelty=args.novelty)
            print(json.dumps({"task": task.__dict__, "created": created}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "configure":
            print(json.dumps(store.set_control(args.state, args.mode), indent=2))
            return 0
        if args.command == "events":
            print(json.dumps(store.events(args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps({"controls": store.controls(),
                              "pending": [task.__dict__ for task in store.tasks("pending")],
                              "retry": [task.__dict__ for task in store.tasks("retry")],
                              "recent": [task.__dict__ for task in store.tasks(limit=10)]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "learned":
            print(json.dumps(store.records(), ensure_ascii=False, indent=2))
            return 0
        if args.command in {"pause", "resume", "stop"}:
            state = {"pause": "paused", "resume": "running", "stop": "stopped"}[args.command]
            print(json.dumps(store.set_control(state), indent=2))
            return 0
        if args.command == "once":
            control = store.controls()
            if control["state"] in {"paused", "stopped"}:
                print(json.dumps({"status": control["state"]}, indent=2))
                return 0
            if control["mode"] != args.mode:
                store.set_control(control["state"], args.mode)
            task = store.next_task()
            if task is None:
                print(json.dumps({"status": "idle", "detail": "No pending learning task."}, indent=2))
                return 0
            # The CLI does not silently bypass provider-specific configuration or authorization.
            print(json.dumps({"status": "pending_provider", "task": task.__dict__,
                              "detail": "Queue is ready; attach an authorized provider adapter through the worker API."},
                             ensure_ascii=False, indent=2))
            return 3
        if args.command == "run":
            control = store.controls()
            if control["state"] in {"paused", "stopped"}:
                print(json.dumps({"status": control["state"]}, indent=2))
                return 0
            if args.pace < 0:
                raise ValueError("--pace must be non-negative")
            if args.continuous:
                if type(args.max_cycles) is not int or not 1 <= args.max_cycles <= 1000:
                    raise ValueError("--continuous requires --max-cycles from 1 to 1000")
                outcomes = ["pending_provider" for _ in range(min(args.max_cycles, len(store.tasks("pending"))))]
                print(json.dumps({"status": "pending_provider" if outcomes else "idle",
                                  "cycles_available": len(outcomes),
                                  "detail": "The CLI does not enable unconfigured providers; use the worker API to run authorized inference."}, indent=2))
                return 3 if outcomes else 0
            if type(args.cycles) is not int or not 1 <= args.cycles <= 1000:
                raise ValueError("--cycles must be between 1 and 1000")
            pending = store.tasks("pending", limit=args.cycles)
            print(json.dumps({"status": "pending_provider" if pending else "idle",
                              "pending_cycles": len(pending),
                              "detail": "The CLI does not enable unconfigured providers; use the worker API to run authorized inference."}, indent=2))
            return 3 if pending else 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
