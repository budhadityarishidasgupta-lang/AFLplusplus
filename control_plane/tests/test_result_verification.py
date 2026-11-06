from __future__ import annotations

import json
import unittest
from dataclasses import replace

from control_plane.delegation_coordinator import ChildTaskStatus, DelegatedTask
from control_plane.execution_engine import ErrorCategory, ExecutionResult, ExecutionState
from control_plane.mission_planning import MissionReference, MissionTask
from control_plane.result_verification import (
    Evidence, MissionOutcomeStatus, ResultVerifier, TaskVerificationInput,
)


class ResultVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mission = MissionReference(
            "mission-41", "Verify outcome", "Outcome verified",
            ("criterion.alpha", "criterion.beta"), ("analysis-capability",),
        )
        self.task = MissionTask(
            "task-1", "Analyze", ("analysis-capability",), "analyze",
            "synthetic.agent", (), ("read-local",), (), (),
            ("deliverable.report",), True,
        )
        self.result = ExecutionResult(
            "task-1", "mission-41", "synthetic.agent", ExecutionState.COMPLETED,
            {"deliverable.report": "report", "criterion.alpha": True,
             "criterion.beta": "criterion.beta"},
        )
        self.evidence = (
            Evidence("evidence-1", "mission-41", "task-1", "criterion.alpha",
                     "synthetic fixture", True, {"finding": "alpha"}, "fixture provenance"),
            Evidence("evidence-2", "mission-41", "task-1", "criterion.beta",
                     "synthetic fixture", True, {"finding": "beta"}, "fixture provenance"),
        )
        self.input = TaskVerificationInput(
            self.task, self.result, self.evidence,
            expected_schema={"type": "object", "required": ["deliverable.report", "criterion.alpha", "criterion.beta"]},
            agent_capabilities=("analysis-capability",),
            authorized_permissions=("read-local",), required_permissions=("read-local",),
        )
        self.verifier = ResultVerifier()

    def test_verified_completion_and_brain_summary_are_serializable(self) -> None:
        outcome = self.verifier.assess_mission(self.mission, (self.input,))
        self.assertEqual(outcome.status, MissionOutcomeStatus.COMPLETED_VERIFIED)
        self.assertEqual(outcome.verified_deliverables, ("deliverable.report",))
        json.dumps(outcome.to_dict())
        summary = outcome.brain_summary()
        json.dumps(summary)
        self.assertNotIn("task-1", summary["message"])

    def test_partial_completion_and_missing_deliverable(self) -> None:
        evidence = (self.evidence[0],)
        result = replace(self.result, output={"criterion.alpha": True})
        item = replace(self.input, result=result, evidences=evidence)
        outcome = self.verifier.assess_mission(self.mission, (item,))
        self.assertEqual(outcome.status, MissionOutcomeStatus.NEEDS_MORE_WORK)
        self.assertFalse(outcome.success_criteria[1].satisfied)
        self.assertIn("deliverable.report", " ".join(outcome.uncertainties))

    def test_contradictory_evidence_never_verifies_criterion(self) -> None:
        contradiction = Evidence("evidence-3", "mission-41", "task-1", "not:criterion.alpha",
                                 "synthetic fixture", True, {"finding": "negative"}, "fixture provenance")
        item = replace(self.input, evidences=self.evidence + (contradiction,))
        assessment = self.verifier.verify_task(self.mission, item)
        self.assertFalse(assessment.criteria[0].satisfied)
        self.assertFalse(assessment.completed)

    def test_invalid_schema_and_reference_mismatch_fail_closed(self) -> None:
        invalid = replace(self.input, expected_schema={"required": ["absent"]})
        self.assertEqual(self.verifier.verify_task(self.mission, invalid).status, "INVALID_RESULT")
        mismatched = replace(self.input, result=replace(self.result, mission_id="other-mission"))
        assessment = self.verifier.verify_task(self.mission, mismatched)
        self.assertFalse(assessment.completed)
        self.assertIn("reference mismatch", " ".join(assessment.uncertainties))

    def test_timeout_and_failed_execution_are_not_success(self) -> None:
        for state, category in ((ExecutionState.TIMED_OUT, ErrorCategory.TIMEOUT),
                                (ExecutionState.FAILED, ErrorCategory.ADAPTER)):
            result = replace(self.result, state=state, error_category=category, output=None)
            self.assertFalse(self.verifier.verify_task(self.mission, replace(self.input, result=result)).completed)

    def test_unverified_or_unbound_sources_do_not_support_claims(self) -> None:
        unverified = replace(self.evidence[0], verified=False)
        unbound = replace(self.evidence[1], task_id="other-task")
        assessment = self.verifier.verify_task(self.mission,
            replace(self.input, evidences=(unverified, unbound)))
        self.assertFalse(assessment.completed)
        self.assertTrue(any("mismatch" in value for value in assessment.uncertainties))

    def test_dependency_failure_blocks_completion(self) -> None:
        item = replace(self.input, dependencies=("missing-dependency",))
        outcome = self.verifier.assess_mission(self.mission, (item,))
        self.assertEqual(outcome.status, MissionOutcomeStatus.BLOCKED)

    def test_self_reported_success_without_evidence_is_not_proof(self) -> None:
        item = replace(self.input, evidences=())
        outcome = self.verifier.assess_mission(self.mission, (item,))
        self.assertNotEqual(outcome.status, MissionOutcomeStatus.COMPLETED_VERIFIED)
        self.assertFalse(any(criterion.satisfied for criterion in outcome.success_criteria))

    def test_no_automatic_retries_execution_or_delegation(self) -> None:
        item = replace(self.input, result=None, evidences=())
        outcome = self.verifier.assess_mission(self.mission, (item,))
        self.assertTrue(outcome.recommendations)
        self.assertTrue(all("execute" not in value.lower() and "retry" not in value.lower()
                            and "delegate" not in value.lower() for value in outcome.recommendations))
        self.assertEqual(outcome.status, MissionOutcomeStatus.NEEDS_MORE_WORK)

    def test_child_task_scope_uses_declared_capability_and_authorization(self) -> None:
        delegated = DelegatedTask("task-1", "parent", "mission-41", "synthetic.agent",
                                  "analysis-capability", "Analyze", "Report",
                                  status=ChildTaskStatus.SUCCEEDED, result={"ok": True})
        bad = replace(self.input, task=delegated, agent_capabilities=("other-capability",))
        assessment = self.verifier.verify_task(self.mission, bad)
        self.assertFalse(assessment.completed)
        self.assertEqual(assessment.status, "OUT_OF_SCOPE")
        bad_permissions = replace(self.input, authorized_permissions=())
        self.assertEqual(self.verifier.verify_task(self.mission, bad_permissions).status, "OUT_OF_SCOPE")

    def test_denied_dependency_and_duplicate_evidence_fail_closed(self) -> None:
        duplicate = replace(self.input, evidences=(self.evidence[0], self.evidence[0]))
        self.assertFalse(self.verifier.verify_task(self.mission, duplicate).completed)
        first = replace(self.input, task=replace(self.task, task_id="task-first"),
                        result=replace(self.result, task_id="task-first"), evidences=(),
                        expected_schema=None)
        second = replace(self.input, dependencies=("task-first",))
        outcome = self.verifier.assess_mission(self.mission, (first, second))
        self.assertEqual(outcome.status, MissionOutcomeStatus.BLOCKED)

    def test_failed_only_mission_reports_failure_and_timeout_reports_incomplete(self) -> None:
        failed = replace(self.result, state=ExecutionState.FAILED, error_category=ErrorCategory.ADAPTER)
        outcome = self.verifier.assess_mission(self.mission, (replace(self.input, result=failed),))
        self.assertEqual(outcome.status, MissionOutcomeStatus.FAILED)
        self.assertTrue(outcome.recommendations)
        timed_out = replace(self.result, state=ExecutionState.TIMED_OUT, error_category=ErrorCategory.TIMEOUT)
        timed = self.verifier.assess_mission(self.mission, (replace(self.input, result=timed_out),))
        self.assertEqual(timed.status, MissionOutcomeStatus.NEEDS_MORE_WORK)

    def test_mission_unknown_when_no_tasks_are_supplied(self) -> None:
        outcome = self.verifier.assess_mission(self.mission, ())
        self.assertEqual(outcome.status, MissionOutcomeStatus.UNKNOWN)
        self.assertFalse(any(item.satisfied for item in outcome.success_criteria))

    def test_mismatched_delegated_task_mission_is_not_accepted(self) -> None:
        delegated = DelegatedTask("task-1", "parent", "other-mission", "synthetic.agent",
                                  "analysis-capability", "Analyze", "Report",
                                  status=ChildTaskStatus.SUCCEEDED, result={"ok": True})
        assessment = self.verifier.verify_task(self.mission, replace(self.input, task=delegated))
        self.assertFalse(assessment.completed)
        self.assertIn("mission reference mismatch", " ".join(assessment.uncertainties))


if __name__ == "__main__":
    unittest.main()
