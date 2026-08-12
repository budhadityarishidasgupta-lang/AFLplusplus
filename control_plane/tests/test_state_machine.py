from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError
from types import MappingProxyType

from control_plane.state_machine import (
    ApprovalError,
    Campaign,
    CampaignError,
    CampaignState,
    InvalidTransition,
    SpecificationError,
    specification_digest,
    validate_bounded_spec,
)


def valid_spec() -> dict[str, object]:
    return {
        "target": {"kind": "mock_http", "host": "127.0.0.1", "port": 3000},
        "limits": {"max_executions": 1000, "max_seconds": 30, "max_input_bytes": 4096},
        "strategy": {"mutation_profile": "balanced", "seed": 1337},
        "metadata": {"owner": "security-team"},
    }


class CampaignTests(unittest.TestCase):
    def campaign(self) -> Campaign:
        return Campaign("campaign-01", valid_spec())

    def approved(self) -> Campaign:
        campaign = self.campaign()
        campaign.request_approval("submitter")
        campaign.approve(campaign.campaign_id, campaign.digest, "human-operator")
        return campaign

    def running(self) -> Campaign:
        campaign = self.approved()
        campaign.queue("scheduler")
        campaign.start("worker-01")
        return campaign

    def test_draft_cannot_transition_directly_to_approved(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.campaign().approve("campaign-01", "0" * 64, "operator")

    def test_draft_cannot_transition_directly_to_running(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.campaign().start("worker")

    def test_approved_cannot_transition_directly_to_running(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.approved().start("worker")

    def test_queue_requires_valid_approval(self) -> None:
        campaign = self.campaign()
        object.__setattr__(campaign, "state", CampaignState.APPROVED)
        with self.assertRaises(ApprovalError):
            campaign.queue("scheduler")

    def test_wrong_campaign_id_approval_is_rejected(self) -> None:
        campaign = self.campaign()
        campaign.request_approval("submitter")
        with self.assertRaises(ApprovalError):
            campaign.approve("other-campaign", campaign.digest, "operator")

    def test_wrong_digest_is_rejected(self) -> None:
        campaign = self.campaign()
        campaign.request_approval("submitter")
        with self.assertRaises(ApprovalError):
            campaign.approve(campaign.campaign_id, "0" * 64, "operator")

    def test_nested_spec_is_immutable(self) -> None:
        campaign = self.campaign()
        self.assertIsInstance(campaign.spec, MappingProxyType)
        with self.assertRaises(TypeError):
            campaign.spec["target"]["port"] = 4000  # type: ignore[index]
        with self.assertRaises(TypeError):
            campaign.spec["limits"]["max_seconds"] = 1  # type: ignore[index]
        with self.assertRaises(TypeError):
            campaign.spec["strategy"]["seed"] = 2  # type: ignore[index]
        with self.assertRaises(TypeError):
            campaign.spec["metadata"]["owner"] = "attacker"  # type: ignore[index]

    def test_original_input_mutation_does_not_change_campaign(self) -> None:
        source = valid_spec()
        campaign = Campaign("campaign-01", source)
        source["target"]["port"] = 4000  # type: ignore[index]
        self.assertEqual(campaign.spec["target"]["port"], 3000)  # type: ignore[index]

    def test_tampering_after_approval_is_detected(self) -> None:
        campaign = self.approved()
        object.__setattr__(campaign, "spec", MappingProxyType(valid_spec() | {"metadata": {"owner": "changed"}}))
        with self.assertRaises(ApprovalError):
            campaign.queue("scheduler")

    def test_approval_is_frozen_and_exactly_bound(self) -> None:
        campaign = self.approved()
        self.assertEqual(campaign.approval.specification_digest, specification_digest(campaign.spec))  # type: ignore[union-attr]
        with self.assertRaises(FrozenInstanceError):
            campaign.approval.operator = "other"  # type: ignore[misc,union-attr]

    def test_terminal_campaigns_cannot_transition(self) -> None:
        for terminal in CampaignState.BLOCKED, CampaignState.CANCELLED:
            campaign = self.campaign()
            getattr(campaign, "block" if terminal is CampaignState.BLOCKED else "cancel")("actor", "reason")
            with self.assertRaises(InvalidTransition):
                campaign.cancel("actor", "again")
        for terminal in CampaignState.FAILED, CampaignState.EXHAUSTED, CampaignState.SUCCEEDED:
            campaign = self.running()
            campaign.finish(terminal, "worker", "bounded outcome")
            with self.assertRaises(InvalidTransition):
                campaign.cancel("actor", "again")

    def test_empty_operator_identity_is_rejected(self) -> None:
        campaign = self.campaign()
        campaign.request_approval("submitter")
        with self.assertRaises(CampaignError):
            campaign.approve(campaign.campaign_id, campaign.digest, "  ")

    def test_empty_worker_identity_is_rejected(self) -> None:
        campaign = self.approved()
        campaign.queue("scheduler")
        with self.assertRaises(CampaignError):
            campaign.start("")

    def test_invalid_campaign_ids(self) -> None:
        for value in ("", "UPPER", "-start", "end-", "contains space", "a" * 65):
            with self.subTest(value=value), self.assertRaises(SpecificationError):
                Campaign(value, valid_spec())

    def test_invalid_mutation_profiles(self) -> None:
        spec = valid_spec()
        spec["strategy"]["mutation_profile"] = "unrestricted"  # type: ignore[index]
        with self.assertRaises(SpecificationError):
            validate_bounded_spec(spec)

    def test_resource_limits_outside_bounds(self) -> None:
        bounds = {
            "max_executions": (0, 1_000_001),
            "max_seconds": (0, 86_401),
            "max_input_bytes": (0, 1_048_577),
        }
        for key, values in bounds.items():
            for value in values:
                spec = valid_spec()
                spec["limits"][key] = value  # type: ignore[index]
                with self.subTest(key=key, value=value), self.assertRaises(SpecificationError):
                    validate_bounded_spec(spec)

    def test_external_hosts_are_rejected(self) -> None:
        for host in ("localhost", "127.0.0.2", "::1", "example.com"):
            spec = valid_spec()
            spec["target"]["host"] = host  # type: ignore[index]
            with self.subTest(host=host), self.assertRaises(SpecificationError):
                validate_bounded_spec(spec)

    def test_invalid_loopback_ports_are_rejected(self) -> None:
        for port in (0, 65536, -1, True, "3000"):
            spec = valid_spec()
            spec["target"]["port"] = port  # type: ignore[index]
            with self.subTest(port=port), self.assertRaises(SpecificationError):
                validate_bounded_spec(spec)

    def test_unsupported_target_kinds_are_rejected(self) -> None:
        spec = valid_spec()
        spec["target"]["kind"] = "process"  # type: ignore[index]
        with self.assertRaises(SpecificationError):
            validate_bounded_spec(spec)

    def test_floats_are_rejected_at_every_depth(self) -> None:
        spec = valid_spec()
        spec["metadata"] = {"nested": [1.0]}
        with self.assertRaises(SpecificationError):
            specification_digest(spec)

    def test_digest_is_deterministic_across_key_order(self) -> None:
        first = valid_spec()
        second = {key: first[key] for key in reversed(first)}
        self.assertEqual(specification_digest(first), specification_digest(second))

    def test_all_successful_transitions_are_auditable(self) -> None:
        campaign = self.running()
        event = campaign.finish(CampaignState.SUCCEEDED, "worker", "target criteria met")
        self.assertEqual(event.previous_state, CampaignState.RUNNING)
        self.assertEqual(event.new_state, CampaignState.SUCCEEDED)
        self.assertEqual(event.actor, "worker")
        self.assertEqual(event.reason, "target criteria met")
        self.assertIsNotNone(event.timestamp.tzinfo)
        self.assertEqual(len(campaign.events), 5)

    def test_failed_transition_emits_no_event(self) -> None:
        campaign = self.campaign()
        before = copy.copy(campaign.events)
        with self.assertRaises(InvalidTransition):
            campaign.start("worker")
        self.assertEqual(campaign.events, before)


if __name__ == "__main__":
    unittest.main()
