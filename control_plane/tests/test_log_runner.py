import csv
import tempfile
import unittest
from pathlib import Path

from control_plane.log_runner import CampaignPolicy, run_campaign, write_reports


class LogRunnerTests(unittest.TestCase):
    def test_external_target_is_rejected(self):
        with self.assertRaises(ValueError):
            CampaignPolicy("demo", target_host="example.com").validate()

    def test_campaign_denies_creator_mutation_and_resets(self):
        events = run_campaign(CampaignPolicy("demo"))
        self.assertEqual([event.status for event in events], ["PASS", "PASS", "PASS", "PASS"])
        self.assertEqual(events[2].phase, "authorization")
        self.assertTrue(events[2].evidence["state_unchanged"])

    def test_reports_are_readable(self):
        events = run_campaign(CampaignPolicy("report-test"))
        with tempfile.TemporaryDirectory() as directory:
            paths = write_reports(events, Path(directory))
            self.assertIn("Security Validation Log", paths["markdown"].read_text(encoding="utf-8"))
            with paths["csv"].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[2]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
