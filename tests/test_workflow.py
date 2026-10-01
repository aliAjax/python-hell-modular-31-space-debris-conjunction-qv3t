import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_conjunction_workflow(self):
        item = self.service.create_item({
            "primary_object_id": "SAT-1",
            "secondary_object_id": "DEB-9",
            "tca": "2026-09-28T12:00:00+00:00",
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["level"], "high")
        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5,
            "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
        }, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-7"}, "operator-1", "operator", item["version"])
        item = self.service.act(item["id"], "resolve", {"report_ref": "RPT-7"}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 5)


if __name__ == "__main__":
    unittest.main()
