from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus
from control_plane.execution_durability import DurableExecutionError, SQLiteExecutionStore
from control_plane.execution_engine import (
    ExecutionAuthorizationIssuer, ExecutionDenied, ExecutionEngine, ExecutionError, ExecutionRequest, ExecutionState,
)


class DurableEcho:
    agent_id = "synthetic.durable"
    capabilities = ("echo",)
    permissions = ("read-fixture",)
    synthetic = True

    def __init__(self, *, gate: threading.Event | None = None) -> None:
        self.calls = 0
        self.gate = gate

    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(2)
        return {"echo": input_data["value"]}


class DurableExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "execution.sqlite3"
        self.store = SQLiteExecutionStore(self.db_path)
        self.registry = CapabilityRegistry()
        self.registry.register(AgentContract(
            agent_id="synthetic.durable", purpose="Durability fixture", version="1.0.0",
            capabilities=("echo",), supported_tasks=("echo",), inputs={"value": "fixture"},
            outputs={"echo": "fixture"}, permissions=("read-fixture",),
        ), CapabilityStatus.VERIFIED)
        self.issuer = ExecutionAuthorizationIssuer()
        self.engine = ExecutionEngine(self.registry, self.issuer, durable_store=self.store)
        self.adapter = DurableEcho()
        self.engine.register_adapter(self.adapter)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def request(self, task_id: str = "task-durable") -> ExecutionRequest:
        return ExecutionRequest(task_id, "mission-durable", "synthetic.durable", "echo",
                               {"value": "synthetic"}, ("read-fixture",))

    def authorize(self, request: ExecutionRequest, *, now: datetime | None = None,
                  validity: timedelta = timedelta(minutes=5)):
        return self.issuer.authorize(request, approved_by="human-reviewer",
                                     permissions=request.permissions, now=now, validity=validity)

    def test_authorized_execution_claim_is_durable_before_adapter_invocation(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        result = self.engine.submit(request, authorization)
        self.assertEqual(result.state, ExecutionState.COMPLETED)
        claim = self.store.get_claim(request.task_id)
        self.assertEqual(claim["state"], "COMPLETED")
        self.assertEqual(claim["authorization_id"], authorization.authorization_id)
        self.assertEqual(self.adapter.calls, 1)

    def test_duplicate_concurrent_submissions_are_at_most_once(self) -> None:
        gate = threading.Event()
        self.adapter = DurableEcho(gate=gate)
        self.engine = ExecutionEngine(self.registry, self.issuer, durable_store=self.store)
        self.engine.register_adapter(self.adapter)
        request = self.request()
        authorization = self.authorize(request)
        result_box: list[Any] = []
        thread = threading.Thread(target=lambda: result_box.append(self.engine.submit(request, authorization)))
        thread.start()
        deadline = time.monotonic() + 2
        while (self.store.get_claim(request.task_id) is None
               or self.store.get_claim(request.task_id)["state"] != "INVOKING") and time.monotonic() < deadline:
            time.sleep(0.005)
        with self.assertRaises(ExecutionError):
            self.engine.submit(request, authorization)
        gate.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.adapter.calls, 1)

    def test_restart_before_invocation_marks_claim_uncertain_and_never_replays(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.store.claim(request, authorization)
        reopened = SQLiteExecutionStore(self.db_path)
        self.assertEqual(reopened.recover_inflight(), (request.task_id,))
        self.assertEqual(reopened.get_claim(request.task_id)["state"], "RECONCILIATION_REQUIRED")
        restarted_issuer = ExecutionAuthorizationIssuer()
        restarted_engine = ExecutionEngine(self.registry, restarted_issuer, durable_store=reopened)
        adapter = DurableEcho()
        restarted_engine.register_adapter(adapter)
        with self.assertRaises(ExecutionError):
            restarted_engine.submit(request, authorization)
        self.assertEqual(adapter.calls, 0)

    def test_restart_during_invocation_marks_claim_uncertain(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.store.claim(request, authorization)
        self.store.transition(request.task_id, "AUTHORIZED")
        self.store.transition(request.task_id, "INVOKING")
        restarted = SQLiteExecutionStore(self.db_path)
        self.assertEqual(restarted.recover_inflight(), (request.task_id,))
        self.assertEqual(restarted.get_claim(request.task_id)["state"], "RECONCILIATION_REQUIRED")

    def test_crash_after_invoke_before_result_is_not_inferred_as_success(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.store.claim(request, authorization)
        self.store.transition(request.task_id, "AUTHORIZED")
        self.store.transition(request.task_id, "INVOKING")
        self.adapter.invoke("echo", request.input_data, threading.Event())
        restarted = SQLiteExecutionStore(self.db_path)
        self.assertEqual(restarted.recover_inflight(), (request.task_id,))
        claim = restarted.get_claim(request.task_id)
        self.assertEqual(claim["state"], "RECONCILIATION_REQUIRED")
        self.assertIsNone(claim["result_json"])
        self.assertEqual(self.adapter.calls, 1)

    def test_consumed_authorization_cannot_be_reused(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.engine.submit(request, authorization)
        another = self.request("task-durable-reuse")
        with self.assertRaises(ExecutionError):
            self.engine.submit(another, authorization)

    def test_expired_and_mismatched_authorization_fail_closed_before_invoke(self) -> None:
        request = self.request("task-expired-durable")
        expired = self.authorize(request, now=datetime.now(timezone.utc) - timedelta(minutes=5),
                                 validity=timedelta(minutes=1))
        with self.assertRaises(ExecutionDenied):
            self.engine.submit(request, expired)
        self.assertEqual(self.adapter.calls, 0)
        mismatch_request = self.request("task-mismatch-durable")
        wrong = self.request("task-other")
        wrong_auth = self.authorize(wrong)
        with self.assertRaises(ExecutionDenied):
            self.engine.submit(mismatch_request, wrong_auth)
        self.assertEqual(self.adapter.calls, 0)

    def test_manual_reconciliation_requires_evidence_and_never_retries(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.store.claim(request, authorization)
        self.store.transition(request.task_id, "AUTHORIZED")
        self.store.transition(request.task_id, "INVOKING")
        restarted = SQLiteExecutionStore(self.db_path)
        self.assertEqual(restarted.recover_inflight(), (request.task_id,))
        with self.assertRaises(DurableExecutionError):
            self.store.reconcile(request.task_id, outcome="COMPLETED", evidence=" ", result={"echo": "x"})
        with self.assertRaises(DurableExecutionError):
            self.store.reconcile(request.task_id, outcome="COMPLETED", evidence="unverified claim",
                                 result={"echo": "x"})
        self.store.reconcile(request.task_id, outcome="COMPLETED", evidence="operator verified fixture result",
                             result={"echo": "synthetic"}, verified=True)
        claim = self.store.get_claim(request.task_id)
        self.assertEqual(claim["state"], "COMPLETED")
        self.assertIn("reconciliation_evidence_digest", claim["result_json"])
        self.assertEqual(self.adapter.calls, 0)

    def test_duplicate_authorization_and_task_claim_reject_atomically(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.store.claim(request, authorization)
        with self.assertRaises(DurableExecutionError):
            self.store.claim(request, authorization)
        self.assertEqual(self.store.get_claim(request.task_id)["state"], "CLAIMED")
        self.assertEqual(self.store.get_claim(request.task_id)["authorization_id"], authorization.authorization_id)

    def test_existing_approval_and_authorization_binding_contracts_remain_separate(self) -> None:
        request = self.request()
        authorization = self.authorize(request)
        self.assertEqual(authorization.permissions, request.permissions)
        self.assertEqual(self.engine.submit(request, authorization).state, ExecutionState.COMPLETED)
        self.assertFalse(hasattr(authorization, "execution_permitted"))


if __name__ == "__main__":
    unittest.main()
