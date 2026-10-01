import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.ledger.repository import LedgerRepository
from src.ledger.service import LedgerService
from src.ledger.models import DomainError, RiskLevel

NOW = "2026-10-01T00:00:00+00:00"


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = LedgerRepository(self.tmp.name)
        self.repo.initialize()
        self.svc = LedgerService(self.repo)
        self._bootstrap()

    def tearDown(self):
        self.repo.close()
        os.unlink(self.tmp.name)

    def _bootstrap(self):
        s = self.svc
        s.register_actor("admin", "admin")
        s.register_actor("ana", "analyst")
        s.register_actor("dutyA", "duty_officer")
        s.register_actor("dutyB", "duty_officer")
        # disp1 授权 SAT-1，disp2 只授权别的卫星
        s.register_actor("disp1", "dispatcher", satellites=["SAT-1"])
        s.register_actor("disp2", "dispatcher", satellites=["SAT-9"])
        s.register_satellite("SAT-1", 10.0, "admin")

    def cj(self, cid, tca, distance=10, cov=100, sat="SAT-1"):
        return self.svc.record_conjunction(sat, tca, distance, cov, "ana",
                                           conjunction_id=cid, now=NOW)

    def submit(self, cid, fuel, pid, who="dutyA", window="w"):
        return self.svc.submit_plan(cid, fuel, window, who, plan_id=pid)

    def approve(self, pid, who="dutyA", version=None):
        version = self.svc.get_plan(pid)["version"] if version is None else version
        return self.svc.approve_plan(pid, who, version)

    # 1. 风险等级 + 交会时刻排队；余量不足拒绝/挂起且说明原因；燃料绝不为负
    def test_queue_priority_and_fuel_never_negative(self):
        # 三个高风险：TCA 早->晚；再加一个低风险
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")          # high
        self.cj("CJ-2", "2026-10-01T09:00:00+00:00")          # high
        self.cj("CJ-3", "2026-10-01T03:00:00+00:00", 2000)    # low
        p1 = self.submit("CJ-1", 6.0, "P1")
        p2 = self.submit("CJ-2", 5.0, "P2", who="dutyB")
        p3 = self.submit("CJ-3", 4.0, "P3")

        r1 = self.approve("P1")
        self.assertEqual(r1["decision"], "queued")
        # P2 与 P1 同为高风险，但 P1 TCA 更早先占 6；P2 需 5，仅剩 4 => 挂起
        r2 = self.approve("P2", who="dutyB")
        self.assertEqual(r2["decision"], "deferred")
        self.assertEqual(r2["state"], "approved")
        self.assertIn("排队未排上", r2["reason"])
        # 低风险 P3 在 P1 之后（风险低排后），但 4 恰好够 => 排队
        r3 = self.approve("P3")
        self.assertEqual(r3["decision"], "queued")
        self.assertEqual(r3["queue"]["queued"], ["P1", "P3"])
        # 余量被卡住，不出现负数
        self.assertEqual(r3["queue"]["fuel_available_after_queue"], 0.0)
        self.assertGreaterEqual(self.svc.fuel_ledger("SAT-1")["fuel_remaining"], 0.0)

        # 超单星总容量的方案直接拒绝并说明
        self.cj("CJ-4", "2026-10-01T02:00:00+00:00")
        self.submit("CJ-4", 11.0, "P4")
        r4 = self.approve("P4")
        self.assertEqual(r4["decision"], "rejected")
        self.assertEqual(r4["state"], "rejected")
        self.assertIn("燃料余量不足", r4["reason"])

    def test_execution_consumes_and_promotes_waiting(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        self.cj("CJ-2", "2026-10-01T09:00:00+00:00")
        p1 = self.submit("CJ-1", 6.0, "P1")
        p2 = self.submit("CJ-2", 5.0, "P2", who="dutyB")
        self.approve("P1")
        self.approve("P2", who="dutyB")  # deferred
        # 授权调度员代办执行 P1：真实扣 6，重算后 P2(需5) 用剩余 4 仍不够
        ex = self.svc.execute_plan("P1", "CMD-1", "disp1", p1["version"] + 1)
        self.assertEqual(ex["state"], "executed")
        self.assertEqual(ex["queue"]["fuel_consumed"], 6.0)
        self.assertEqual(ex["queue"]["queued"], [])
        self.assertGreaterEqual(ex["queue"]["fuel_available_after_queue"], 0.0)

    # 2. 同一方案两人并发提交：晚到版本过期被拒，先到保留
    def test_optimistic_concurrent_submit(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        p = self.submit("CJ-1", 3.0, "P1")
        self.assertEqual(p["version"], 1)
        first = self.svc.submit_plan("CJ-1", 3.0, "first-edit", "dutyA",
                                     plan_id="P1", expected_version=1)
        self.assertEqual(first["version"], 2)
        # 第二个人仍拿着 v1
        with self.assertRaises(DomainError) as ctx:
            self.svc.submit_plan("CJ-1", 4.0, "late-edit", "dutyB",
                                 plan_id="P1", expected_version=1)
        self.assertEqual(ctx.exception.code, "version_conflict")
        kept = self.svc.get_plan("P1")
        self.assertEqual(kept["version"], 2)
        self.assertEqual(kept["maneuver_window"], "first-edit")

    def test_approve_requires_version_and_blocks_double_approve(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        self.submit("CJ-1", 2.0, "P1")
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_plan("P1", "dutyA", None)
        self.assertEqual(ctx.exception.code, "expected_version_required")
        r = self.approve("P1")
        # 已排队后第二个值班员拿同样的版本号重复批准 => 状态不允许 + 留痕
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_plan("P1", "dutyB", 1)
        self.assertEqual(ctx.exception.code, "invalid_state")

    # 3. 轨道修订：旧批准/占用立即失效重算；已执行保留；未执行重新评估
    def test_orbit_revision_invalidates_live_plans_but_keeps_executed(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00", distance=10)   # high
        p1 = self.submit("CJ-1", 4.0, "P1")
        r1 = self.approve("P1")
        self.assertEqual(r1["decision"], "queued")
        self.svc.execute_plan("P1", "CMD-1", "disp1", r1["version"])

        # 新的接近事件，批准但还没执行
        self.cj("CJ-2", "2026-10-01T09:00:00+00:00", distance=10)
        p2 = self.submit("CJ-2", 3.0, "P2", who="dutyB")
        r2 = self.approve("P2", who="dutyB")
        self.assertEqual(r2["decision"], "queued")
        self.assertEqual(self.svc.fuel_ledger("SAT-1")["fuel_reserved"], 3.0)

        # 轨道修订：脱靶量变大 -> 风险下降；P2 未执行必须失效并释放，P1 已执行保留
        report = self.svc.revise_orbit("CJ-2", 2000, 100, "ana")
        self.assertEqual(report["conjunction"]["revision"], 2)
        self.assertEqual(report["stale_pending_reevaluation"], ["P2"])
        self.assertEqual(report["executed_kept"], [])  # CJ-2 本身没有已执行方案
        self.assertEqual(self.svc.get_plan("P2")["state"], "stale")
        # 属于另一事件 CJ-1 的已执行指令 P1 原样保留
        self.assertEqual(self.svc.get_plan("P1")["state"], "executed")
        self.assertEqual(self.svc.get_plan("P1")["command_ref"], "CMD-1")
        # 占用已释放，账上余量恢复（已执行消耗仍在）
        self.assertEqual(self.svc.fuel_ledger("SAT-1")["fuel_reserved"], 0.0)
        self.assertEqual(self.svc.fuel_ledger("SAT-1")["fuel_consumed"], 4.0)

        # 重新评估：挂回最新数据（low, r2），回到待批准
        ree = self.svc.reevaluate_plan("P2", "dutyB")
        self.assertEqual(ree["state"], "draft")
        self.assertEqual(ree["risk_level"], "low")
        self.assertEqual(ree["based_on_revision"], 2)
        # 重新批准后按新风险参与排队
        again = self.approve("P2", who="dutyB")
        self.assertEqual(again["decision"], "queued")

    def test_approve_against_stale_revision_is_refused(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        p = self.submit("CJ-1", 2.0, "P1")
        # 修订发生在批准之前（草稿不会被级联置 stale）
        self.svc.revise_orbit("CJ-1", 10, 100, "ana")
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_plan("P1", "dutyA", p["version"])
        self.assertEqual(ctx.exception.code, "stale_track")
        self.assertEqual(self.svc.get_plan("P1")["state"], "stale")

    # 4. 授权范围内代办，越权直接拒绝并留审计
    def test_delegation_scope_enforced_and_audited(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        p = self.submit("CJ-1", 2.0, "P1")
        # 角色越权：analyst 不能批准
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_plan("P1", "ana", p["version"])
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "forbidden")
        # 范围越权：disp2 只有 SAT-9
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_plan("P1", "disp2", p["version"])
        self.assertEqual(ctx.exception.code, "out_of_scope")
        # 业务数据未受影响
        self.assertEqual(self.svc.get_plan("P1")["state"], "draft")
        denied = [e for e in self.svc.audit_trail() if e["result"] == "denied"]
        codes = {(e["event_type"], e["actor"]) for e in denied}
        self.assertIn(("approve_plan", "ana"), codes)
        self.assertIn(("approve_plan", "disp2"), codes)

    def test_denied_attempts_leave_audit_but_no_state_change(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        self.submit("CJ-1", 2.0, "P1")
        with self.assertRaises(DomainError):
            self.svc.approve_plan("P1", "ana", 1)
        # 哈希审计链完整
        self.assertTrue(self.svc.verify_audit_chain())
        denied = [e for e in self.svc.audit_trail() if e["result"] == "denied"]
        self.assertTrue(denied)

    def test_cancel_releases_reservation_and_recomputes(self):
        self.cj("CJ-1", "2026-10-01T06:00:00+00:00")
        self.cj("CJ-2", "2026-10-01T09:00:00+00:00")
        p1 = self.submit("CJ-1", 6.0, "P1")
        p2 = self.submit("CJ-2", 4.0, "P2", who="dutyB")
        self.approve("P1")
        self.approve("P2", who="dutyB")  # 4 恰好够 => queued
        self.assertEqual(self.svc.fuel_ledger("SAT-1")["fuel_reserved"], 10.0)
        v = self.svc.get_plan("P1")["version"]
        res = self.svc.cancel_plan("P1", "不再需要", "dutyA", v)
        self.assertEqual(res["state"], "cancelled")
        self.assertEqual(res["queue"]["queued"], ["P2"])
        self.assertEqual(res["queue"]["fuel_reserved"], 4.0)


if __name__ == "__main__":
    unittest.main()
