import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_risk_score_and_level(self):
        high = assess({"miss_distance_m": 10, "covariance_m": 100, "hours_to_tca": 6})
        low = assess({"miss_distance_m": 2000, "covariance_m": 100, "hours_to_tca": 40})
        self.assertEqual(high["level"], "high")
        self.assertEqual(low["level"], "low")
        self.assertGreater(high["score"], low["score"])

    def test_stale_track_is_rejected(self):
        item = {
            "status": "pending",
            "payload": {"track_age_hours": 7, "miss_distance_m": 10, "covariance_m": 100},
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "assess", {"hours_to_tca": 5}, "a", "analyst")
        self.assertEqual(context.exception.code, "stale_track")


if __name__ == "__main__":
    unittest.main()
