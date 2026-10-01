import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _conjunction(self, satellite, tca, miss_distance, budget):
        return self.service.create_item({
            "primary_object_id": satellite,
            "secondary_object_id": "DEB-X",
            "tca": tca,
            "miss_distance_m": miss_distance,
            "covariance_m": 100,
            "fuel_budget_m_s": budget,
            "track_age_hours": 1,
            "operating_organizations": [],
        }, "analyst-1", "analyst")

    def _assess(self, item, hours=6):
        return self.service.act(item["id"], "assess", {"hours_to_tca": hours}, "analyst-1", "analyst", item["version"])

    def _approve(self, item, cost, window, actor="coord-1"):
        return self.service.act(item["id"], "approve", {"fuel_cost_m_s": cost, "maneuver_window": window}, actor, "coordinator", item["version"])

    def test_fuel_pool_rejects_when_margin_insufficient(self):
        a = self._conjunction("SAT-1", "2026-10-10T10:00:00+00:00", 10, 10)
        b = self._conjunction("SAT-1", "2026-10-11T10:00:00+00:00", 20, 10)
        a = self._assess(a)
        b = self._assess(b)
        self._approve(a, 6, "w1")
        with self.assertRaises(DomainError) as context:
            self._approve(b, 6, "w2")
        self.assertEqual(context.exception.code, "fuel_budget_exceeded")
        self.assertEqual(context.exception.status, 409)
        # 未排原因与排队队列可查
        schedule = self.service.schedule()
        rejected = [p for p in schedule["rejected"] if p["payload"]["conjunction_id"] == b["id"]]
        self.assertEqual(len(rejected), 1)
        reason = rejected[0]["payload"]["reject_reason"]
        self.assertAlmostEqual(reason["available_m_s"], 4.0)
        self.assertEqual(len(reason["queue"]), 1)
        # 先到的占用保留
        sat = [s for s in schedule["satellites"] if s["satellite"]["stable_key"] == "SAT-1"][0]
        self.assertAlmostEqual(sat["fuel"]["occupied_m_s"], 6.0)
        self.assertAlmostEqual(sat["fuel"]["available_m_s"], 4.0)

    def test_same_plan_concurrent_submission_first_come_retained(self):
        c = self._conjunction("SAT-2", "2026-10-12T10:00:00+00:00", 10, 5)
        c = self._assess(c)
        stale_version = c["version"]
        self._approve(c, 2, "w")
        with self.assertRaises(ConflictError) as context:
            self.service.act(c["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-2", "coordinator", stale_version)
        self.assertEqual(context.exception.code, "version_conflict")
        sat = self.service.list_satellites()
        sat2 = [s for s in sat if s["satellite"]["stable_key"] == "SAT-2"][0]
        self.assertAlmostEqual(sat2["fuel"]["occupied_m_s"], 2.0)
        self.assertAlmostEqual(sat2["fuel"]["available_m_s"], 3.0)

    def test_orbit_revision_voids_queued_and_reassesses(self):
        d = self._conjunction("SAT-3", "2026-10-13T10:00:00+00:00", 10, 8)
        d = self._assess(d)
        d = self._approve(d, 3, "w")
        self.assertEqual(d["status"], "coordinating")
        d = self.service.act(d["id"], "report_revision", {
            "observed_at": "2026-10-01T00:00:00+00:00",
            "miss_distance_m": 500,
            "covariance_m": 100,
            "source": "radar",
        }, "analyst-1", "analyst", d["version"])
        self.assertEqual(d["status"], "assessed")
        self.assertIsNone(d["payload"].get("approved_maneuver"))
        schedule = self.service.schedule()
        voided = [p for p in schedule["voided"] if p["payload"]["conjunction_id"] == d["id"]]
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0]["payload"]["void_reason"], "orbit_revised")
        # 燃料释放后可重新排队
        d = self.service.get_item(d["id"])
        d = self._approve(d, 3, "w-new")
        self.assertEqual(d["status"], "coordinating")
        schedule = self.service.schedule()
        self.assertEqual(len([p for p in schedule["queue"] if p["payload"]["conjunction_id"] == d["id"]]), 1)

    def test_executed_plan_keeps_record_after_revision(self):
        e = self._conjunction("SAT-4", "2026-10-14T10:00:00+00:00", 10, 8)
        e = self._assess(e)
        e = self._approve(e, 3, "w")
        e = self.service.act(e["id"], "execute", {"command_ref": "CMD-1"}, "op-1", "operator", e["version"])
        e = self.service.act(e["id"], "report_revision", {
            "observed_at": "2026-10-01T00:00:00+00:00",
            "miss_distance_m": 999,
            "covariance_m": 100,
            "source": "radar",
        }, "analyst-1", "analyst", e["version"])
        self.assertEqual(e["status"], "executing")
        self.assertEqual(e["payload"].get("command_ref"), "CMD-1")
        schedule = self.service.schedule()
        executed = [p for p in schedule["executed"] if p["payload"]["conjunction_id"] == e["id"]]
        self.assertEqual(len(executed), 1)
        self.assertAlmostEqual(executed[0]["payload"]["fuel_cost_m_s"], 3.0)
        # 已执行燃料仍计为消耗
        sat = [s for s in schedule["satellites"] if s["satellite"]["stable_key"] == "SAT-4"][0]
        self.assertAlmostEqual(sat["fuel"]["spent_m_s"], 3.0)

    def test_unauthorized_change_is_denied_and_audited(self):
        f = self._conjunction("SAT-5", "2026-10-15T10:00:00+00:00", 10, 8)
        f = self._assess(f)
        with self.assertRaises(DomainError) as context:
            self.service.act(f["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "op-9", "operator", f["version"])
        self.assertEqual(context.exception.status, 403)
        f = self.service.get_item(f["id"])
        denials = [ev for ev in f["audit"] if ev["event_type"] == "authorization_denied"]
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0]["payload"]["action"], "approve")
        self.assertEqual(denials[0]["role"], "operator")

    def test_queue_orders_by_risk_then_tca(self):
        # high 风险、晚交会 vs low 风险、早交会：high 优先
        high = self._conjunction("SAT-6", "2026-12-01T10:00:00+00:00", 10, 8)
        low = self._conjunction("SAT-6", "2026-10-01T10:00:00+00:00", 2000, 8)
        high = self._assess(high, hours=6)
        low = self._assess(low, hours=40)
        self._approve(low, 1, "w-low")
        self._approve(high, 1, "w-high")
        schedule = self.service.schedule()
        queue = schedule["queue"]
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["payload"]["risk_level"], "high")
        self.assertEqual(queue[1]["payload"]["risk_level"], "low")


if __name__ == "__main__":
    unittest.main()
