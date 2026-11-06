from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from control_plane.agent_contracts import AgentContract, CapabilityRegistry, CapabilityStatus
from control_plane.execution_engine import (
    ErrorCategory,
    ExecutionAuthorizationIssuer,
    ExecutionBusy,
    ExecutionEngine,
    ExecutionLimits,
    ExecutionRequest,
    ExecutionState,
)


class SyntheticEchoAdapter:
    agent_id = "synthetic.echo"
    capabilities = ("echo",)
    permissions = ("read-fixture",)
    synthetic = True

    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
        return {"echo": input_data["value"]}


class SyntheticSlowAdapter(SyntheticEchoAdapter):
    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
        deadline = time.monotonic() + input_data.get("sleep", 1)
        while time.monotonic() < deadline:
            if cancel_event.wait(0.01):
                return {"cancelled": True}
        return {"echo": "finished"}


class SyntheticFailureAdapter(SyntheticEchoAdapter):
    def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
        raise RuntimeError("private payload must not leak")


class ExecutionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = CapabilityRegistry()
        self.registry.register(AgentContract(
            agent_id="synthetic.echo", purpose="Synthetic test adapter", version="1.0.0",
            capabilities=("echo",), supported_tasks=("echo-task",),
            inputs={"value": "Synthetic value"}, outputs={"echo": "Echo result"},
            permissions=("read-fixture",),
        ), CapabilityStatus.UNDERSTOOD)
        self.issuer = ExecutionAuthorizationIssuer()
        self.engine = ExecutionEngine(self.registry, self.issuer, limits=ExecutionLimits(max_duration_seconds=2))
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine.register_adapter(SyntheticEchoAdapter())

    def request(self, task_id: str = "task-1", **kwargs: Any) -> ExecutionRequest:
        values = {"task_id": task_id, "mission_id": "mission-1", "agent_id": "synthetic.echo", "capability": "echo", "input_data": {"value": "fixture"}, "permissions": ("read-fixture",)}
        values.update(kwargs)
        return ExecutionRequest(**values)

    def authorize(self, request: ExecutionRequest, **kwargs: Any) -> Any:
        permissions = kwargs.pop("permissions", request.permissions)
        return self.issuer.authorize(request, approved_by="human-operator", permissions=permissions, **kwargs)

    def test_authorized_synthetic_execution_and_audit_trace(self) -> None:
        request = self.request()
        result = self.engine.submit(request, self.authorize(request))
        self.assertEqual(result.state, ExecutionState.COMPLETED)
        self.assertEqual(result.output, {"echo": "fixture"})
        self.assertEqual((result.task_id, result.mission_id), ("task-1", "mission-1"))
        self.assertEqual([event.state for event in self.engine.audit_events], [ExecutionState.PENDING, ExecutionState.AUTHORIZED, ExecutionState.RUNNING, ExecutionState.COMPLETED])
        self.assertEqual(self.engine.result("task-1"), result)
        self.assertNotIn("fixture", str([event.to_dict() for event in self.engine.audit_events]))
        self.assertNotIn("human-operator", str([event.to_dict() for event in self.engine.audit_events]))

    def test_revoked_authorization_fails_closed(self) -> None:
        request = self.request("task-revoked")
        authorization = self.authorize(request)
        self.assertTrue(self.issuer.revoke(authorization))
        result = self.engine.submit(request, authorization)
        self.assertEqual(result.error_category, ErrorCategory.AUTHORIZATION)

    def test_missing_expired_replayed_and_cross_task_authorization_fail_closed(self) -> None:
        request = self.request()
        missing = self.engine.submit(request, None)
        self.assertEqual(missing.error_category, ErrorCategory.AUTHORIZATION)
        self.assertNotEqual(missing.state, ExecutionState.COMPLETED)

        expired_request = self.request("task-expired")
        expired = self.authorize(expired_request, now=datetime.now(timezone.utc) - timedelta(minutes=2), validity=timedelta(minutes=1))
        self.assertEqual(self.engine.submit(expired_request, expired).error_category, ErrorCategory.AUTHORIZATION)
        with self.assertRaises(ValueError):
            self.authorize(self.request("task-escalate", permissions=()), permissions=("read-fixture",))

        cross_request = self.request("task-cross")
        cross_auth = self.authorize(cross_request)
        other_task = self.request("task-other")
        self.assertEqual(self.engine.submit(other_task, cross_auth).error_category, ErrorCategory.AUTHORIZATION)

        replay_request = self.request("task-replay")
        replay_auth = self.authorize(replay_request)
        self.assertEqual(self.engine.submit(replay_request, replay_auth).state, ExecutionState.COMPLETED)
        second_id = ExecutionRequest("task-replay-2", "mission-1", "synthetic.echo", "echo", {"value": "fixture"}, ("read-fixture",))
        self.assertEqual(self.engine.submit(second_id, replay_auth).error_category, ErrorCategory.AUTHORIZATION)

    def test_unverified_adapter_metadata_cannot_be_registered(self) -> None:
        registry = CapabilityRegistry()
        registry.register(AgentContract(
            agent_id="synthetic.unverified", purpose="Unverified test", version="1.0.0",
            capabilities=("echo",), supported_tasks=("echo-task",),
            inputs={}, outputs={}, permissions=(),
        ), CapabilityStatus.UNDERSTOOD)
        engine = ExecutionEngine(registry, self.issuer)

        class UnverifiedAdapter(SyntheticEchoAdapter):
            agent_id = "synthetic.unverified"
            permissions = ()

        with self.assertRaises(ValueError):
            engine.register_adapter(UnverifiedAdapter())

    def test_adapter_registration_requires_synthetic_and_bounded_metadata(self) -> None:
        class NonSynthetic(SyntheticEchoAdapter):
            synthetic = False

        with self.assertRaises(ValueError):
            self.engine.register_adapter(NonSynthetic())
        with self.assertRaises(ValueError):
            ExecutionLimits(max_depth=3)
        with self.assertRaises(ValueError):
            ExecutionLimits(max_concurrent_tasks=2)
        request = self.request("task-depth", depth=3)
        authorization = self.authorize(request)
        self.assertEqual(self.engine.submit(request, authorization).error_category, ErrorCategory.BUDGET)

    def test_unsupported_capability_and_unavailable_agent_do_not_invoke(self) -> None:
        bad = ExecutionRequest("task-bad", "mission-1", "synthetic.echo", "other", {"value": "x"})
        auth = self.authorize(bad)
        result = self.engine.submit(bad, auth)
        self.assertEqual(result.error_category, ErrorCategory.CAPABILITY)
        unavailable = ExecutionRequest("task-unavailable", "mission-1", "legacy.agent", "echo", {})
        authorization = self.authorize(unavailable)
        result = self.engine.submit(unavailable, authorization)
        self.assertEqual(result.state, ExecutionState.FAILED)
        self.assertEqual(result.error_category, ErrorCategory.CAPABILITY)

    def test_adapter_exception_is_sanitized(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine = ExecutionEngine(self.registry, self.issuer)
        self.engine.register_adapter(SyntheticFailureAdapter())
        request = self.request()
        result = self.engine.submit(request, self.authorize(request))
        self.assertEqual(result.error_category, ErrorCategory.ADAPTER)
        self.assertNotIn("private payload", result.error_message or "")
        self.assertNotIn("fixture", result.error_message or "")
        self.assertNotIn("fixture", str([event.to_dict() for event in self.engine.audit_events]))

    def test_cooperative_cancellation(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine = ExecutionEngine(self.registry, self.issuer, limits=ExecutionLimits(max_duration_seconds=2))
        self.engine.register_adapter(SyntheticSlowAdapter())
        request = self.request(input_data={"sleep": 1})
        authorization = self.authorize(request)
        result_box: list[Any] = []
        thread = threading.Thread(target=lambda: result_box.append(self.engine.submit(request, authorization)))
        thread.start()
        deadline = time.monotonic() + 1
        while not any(event.state is ExecutionState.RUNNING for event in self.engine.audit_events) and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.engine.cancel("task-1"))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result_box[0].state, ExecutionState.CANCELLED)

    def test_timeout_of_uncooperative_adapter_keeps_single_slot_until_thread_exits(self) -> None:
        class UncooperativeAdapter(SyntheticEchoAdapter):
            def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
                time.sleep(0.15)
                return {"echo": "finished"}

        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine = ExecutionEngine(self.registry, self.issuer, limits=ExecutionLimits(max_duration_seconds=0.02))
        self.engine.register_adapter(UncooperativeAdapter())
        request = self.request("task-uncooperative")
        result = self.engine.submit(request, self.authorize(request))
        self.assertEqual(result.state, ExecutionState.TIMED_OUT)
        with self.assertRaises(ExecutionBusy):
            next_request = self.request("task-blocked")
            self.engine.submit(next_request, self.authorize(next_request))
        worker = self.engine._workers["task-uncooperative"]
        self.assertTrue(worker.is_alive())
        worker.join(1)
        self.assertFalse(worker.is_alive())
        deadline = time.monotonic() + 1
        while self.engine._active is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(self.engine._active)

    def test_timeout_requests_cooperative_stop_and_releases_no_claim_of_hard_kill(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine = ExecutionEngine(self.registry, self.issuer, limits=ExecutionLimits(max_duration_seconds=0.05))
        self.engine.register_adapter(SyntheticSlowAdapter())
        request = self.request(input_data={"sleep": 0.3})
        result = self.engine.submit(request, self.authorize(request))
        self.assertEqual(result.state, ExecutionState.TIMED_OUT)
        self.assertIn("worker may still be running", result.error_message or "")
        self.assertIsNotNone(self.engine._active)
        with self.assertRaises(ExecutionBusy):
            second = self.request("task-after-timeout")
            self.engine.submit(second, self.authorize(second))
        worker = self.engine._workers["task-1"]
        self.assertTrue(worker.is_alive())
        worker.join(1)
        self.assertFalse(worker.is_alive())
        deadline = time.monotonic() + 1
        while self.engine._active is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(self.engine._active)

    def test_task_inputs_are_snapshotted_before_authorization(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        mutable = {"value": "before"}
        request = self.request("task-snapshot", input_data=mutable)
        mutable["value"] = "after-construction"
        authorization = self.authorize(request)
        mutable["value"] = "after-authorization"
        result = self.engine.submit(request, authorization)
        self.assertEqual(result.state, ExecutionState.COMPLETED)
        self.assertEqual(result.output, {"echo": "before"})

    def test_error_messages_sanitize_sensitive_patterns(self) -> None:
        from control_plane.execution_engine import _safe_error_message

        self.assertEqual(
            _safe_error_message(RuntimeError("credential=private"), "fallback"),
            "fallback",
        )
        self.assertEqual(
            _safe_error_message(RuntimeError("safe diagnostic"), "fallback"),
            "safe diagnostic",
        )

    def test_oversized_adapter_output_is_sanitized_and_bounded(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        class LargeOutput(SyntheticEchoAdapter):
            def invoke(self, capability: str, input_data: Mapping[str, Any], cancel_event: threading.Event) -> Any:
                return {"data": "x" * 200}

        engine = ExecutionEngine(
            self.registry, self.issuer,
            limits=ExecutionLimits(max_output_bytes=100, max_duration_seconds=2),
        )
        engine.register_adapter(LargeOutput())
        request = self.request("task-large-output")
        result = engine.submit(request, self.authorize(request))
        self.assertEqual(result.error_category, ErrorCategory.BUDGET)
        self.assertIsNone(result.output)
        self.assertNotIn("x" * 20, str([event.to_dict() for event in engine.audit_events]))

    def test_budget_exhaustion_and_invalid_limits(self) -> None:
        request = self.request(input_data={"value": "x" * 70_000})
        result = self.engine.submit(request, self.authorize(request))
        self.assertEqual(result.error_category, ErrorCategory.BUDGET)
        bounded_engine = ExecutionEngine(
            self.registry, self.issuer,
            limits=ExecutionLimits(max_input_bytes=100, max_duration_seconds=2),
        )
        bounded_engine.register_adapter(SyntheticEchoAdapter())
        bounded_request = self.request("task-bounded", input_data={"value": "x" * 200})
        bounded_result = bounded_engine.submit(bounded_request, self.authorize(bounded_request))
        self.assertEqual(bounded_result.error_category, ErrorCategory.BUDGET)
        with self.assertRaises(ValueError):
            ExecutionLimits(max_depth=3)

    def test_concurrent_execution_limit_is_one(self) -> None:
        self.registry.set_status("synthetic.echo", CapabilityStatus.VERIFIED)
        self.engine = ExecutionEngine(self.registry, self.issuer, limits=ExecutionLimits(max_duration_seconds=2))
        self.engine.register_adapter(SyntheticSlowAdapter())
        first = self.request("task-first", input_data={"sleep": 0.4})
        authorization = self.authorize(first)
        thread = threading.Thread(target=lambda: self.engine.submit(first, authorization))
        thread.start()
        deadline = time.monotonic() + 1
        while not any(event.state is ExecutionState.RUNNING for event in self.engine.audit_events) and time.monotonic() < deadline:
            time.sleep(0.005)
        second = self.request("task-second")
        second_auth = self.authorize(second)
        with self.assertRaises(ExecutionBusy):
            self.engine.submit(second, second_auth)
        self.engine.cancel("task-first")
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_planning_and_delegation_approvals_are_not_execution_authorizations(self) -> None:
        from control_plane.agent_discovery import AgentDiscovery
        from control_plane.delegation_authorization import DelegationAuthorizer, DelegationContext, DelegationDecisionState
        from control_plane.mission_planning import DelegationProposal

        discovered = AgentDiscovery()
        discovered.discover_existing()
        proposal = DelegationProposal(
            "mission-1", "task-1", "local-python-static-analysis", "test", "source summary", ()
        )
        planning_approval = DelegationAuthorizer(discovered).evaluate(
            proposal, DelegationContext("mission-1", "task-1", None, ()),
            requested_task_type="inspect-local-python-source",
        )
        request = self.request("task-planning-only")
        result = self.engine.submit(request, planning_approval)  # type: ignore[arg-type]
        self.assertEqual(result.error_category, ErrorCategory.AUTHORIZATION)
        self.assertFalse(result.state is ExecutionState.COMPLETED)
        self.assertEqual(planning_approval.state, DelegationDecisionState.APPROVABLE)

    def test_no_legacy_imports_or_execution_without_authorization(self) -> None:
        import sys
        for module in ("multi_agent_system", "conversational_launcher", "enterprise_stealth_range"):
            self.assertNotIn(module, sys.modules)
        request = self.request()
        result = self.engine.submit(request, None)
        self.assertFalse(result.state is ExecutionState.COMPLETED)
        for module in ("multi_agent_system", "conversational_launcher", "enterprise_stealth_range"):
            self.assertNotIn(module, sys.modules)


if __name__ == "__main__":
    unittest.main()
