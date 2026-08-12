"""Unit tests for the production control-plane campaign lifecycle."""

import unittest
from copy import deepcopy

from control_plane.state_machine import (
    ApprovalError,
    CampaignRecord,
    CampaignState,
    InvalidTransition,
    SpecError,
    spec_digest,
)


def valid_spec():
    return {
        "schema_version": 1,
        "campaign_id": "campaign-001",
        "target": {
            "kind": "loopback_service",
            "identifier": "mock-target",
            "host": "127.0.0.1",
            "port": 3000,
        },
        "limits": {
            "max_seconds": 60,
            "max_executions": 10000,
            "max_input_bytes": 4096,
            "memory_mb": 512,
        },
        "strategy": {"seed": 1337, "mutation_profile": "adaptive"},
        "metadata": {"purpose": "authorized local regression campaign"},
    }


class DigestTests(unittest.TestCase):
    def test_digest_is_key_order_independent(self):
        left = valid_spec()
        right = {key: left[key] for key in reversed(list(left.keys()))}
        self.assertEqual(spec_digest(left), spec_digest(right))

    def test_digest_changes_when_material_spec_changes(self):
        changed = valid_spec()
        changed["limits"]["max_seconds"] = 61
        self.assertNotEqual(spec_digest(valid_spec()), spec_digest(changed))


class CampaignLifecycleTests(unittest.TestCase):
    def approved_campaign(self):
        campaign = CampaignRecord(valid_spec())
        campaign.validate()
        campaign.request_approval()
        campaign.approve(operator_id="operator@example", supplied_digest=campaign.digest)
        return campaign

    def test_happy_path(self):
        campaign = self.approved_campaign()
        campaign.queue()
        campaign.start(worker_id="worker-01")
        campaign.finish(
            CampaignState.SUCCEEDED,
            actor="worker-01",
            reason="bounded campaign completed",
        )
        self.assertEqual(campaign.state, CampaignState.SUCCEEDED)
        self.assertTrue(campaign.is_terminal)
        self.assertEqual(len(campaign.events), 6)

    def test_cannot_skip_approval(self):
        campaign = CampaignRecord(valid_spec())
        with self.assertRaises(InvalidTransition):
            campaign.queue()

    def test_wrong_approval_digest_fails_closed(self):
        campaign = CampaignRecord(valid_spec())
        campaign.validate()
        campaign.request_approval()
        with self.assertRaises(ApprovalError):
            campaign.approve(operator_id="operator@example", supplied_digest="sha256:deadbeef")
        self.assertEqual(campaign.state, CampaignState.AWAITING_APPROVAL)

    def test_mutating_original_input_does_not_change_record(self):
        source = valid_spec()
        campaign = CampaignRecord(source)
        original_digest = campaign.digest
        source["limits"]["max_seconds"] = 999
        self.assertEqual(campaign.digest, original_digest)
        self.assertEqual(campaign.spec["limits"]["max_seconds"], 60)

    def test_internal_spec_tampering_invalidates_approval(self):
        campaign = self.approved_campaign()
        campaign.spec["limits"]["max_seconds"] = 999
        with self.assertRaises(ApprovalError):
            campaign.queue()
        self.assertEqual(campaign.state, CampaignState.APPROVED)

    def test_terminal_state_cannot_restart(self):
        campaign = self.approved_campaign()
        campaign.queue()
        campaign.start(worker_id="worker-01")
        campaign.finish(CampaignState.FAILED, actor="worker-01", reason="worker error")
        with self.assertRaises(InvalidTransition):
            campaign.start(worker_id="worker-02")

    def test_non_loopback_target_is_rejected(self):
        spec = valid_spec()
        spec["target"]["host"] = "192.0.2.10"
        campaign = CampaignRecord(spec)
        with self.assertRaises(SpecError):
            campaign.validate()
        self.assertEqual(campaign.state, CampaignState.DRAFT)

    def test_host_port_for_local_binary_is_rejected(self):
        spec = deepcopy(valid_spec())
        spec["target"] = {
            "kind": "local_binary",
            "identifier": "./sandbox/target",
            "host": "127.0.0.1",
            "port": 3000,
        }
        campaign = CampaignRecord(spec)
        with self.assertRaises(SpecError):
            campaign.validate()


if __name__ == "__main__":
    unittest.main()
