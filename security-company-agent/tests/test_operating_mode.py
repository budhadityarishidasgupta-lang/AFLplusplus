import unittest
from brain_core.operating_mode import OperatingMode, coerce_mode, permits_scoped_research


class OperatingModeTests(unittest.TestCase):
    def test_modes_and_reserved_agent_fail_closed(self):
        self.assertEqual(coerce_mode('CONTROLLED'), OperatingMode.CONTROLLED)
        self.assertEqual(coerce_mode('FREE_LEARNING'), OperatingMode.FREE_LEARNING)
        self.assertEqual(coerce_mode('AUTONOMOUS_AGENT'), OperatingMode.CONTROLLED)
        self.assertEqual(coerce_mode('unknown'), OperatingMode.CONTROLLED)

    def test_standing_scoped_research_only_in_free_learning(self):
        self.assertFalse(permits_scoped_research(OperatingMode.CONTROLLED))
        self.assertTrue(permits_scoped_research(OperatingMode.FREE_LEARNING))
        self.assertFalse(permits_scoped_research(OperatingMode.AUTONOMOUS_AGENT))


if __name__ == '__main__':
    unittest.main()
