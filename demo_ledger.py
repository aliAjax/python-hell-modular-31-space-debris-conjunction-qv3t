"""调度账端到端演示（内存 SQLite，不写盘）。

演出五条主线：
  1) 多个接近事件同时到来，值班员各批各的，按风险+TCA 排队占用燃料，紧张余量拒绝/挂起；
  2) 同一方案两人同时提交，晚到的按版本校验拒绝，先到占用保留；
  3) 轨道数据修订后，依赖旧数据的未执行批准/占用立即失效重算，已执行指令保留；
  4) 调度员在授权卫星范围内代办执行；越权（角色/范围）直接拒绝并留审计；
  5) 全程哈希审计链，拒绝也留痕。

运行：python3 demo_ledger.py
"""

import os
import tempfile

from src.ledger.models import DomainError
from src.ledger.repository import LedgerRepository
from src.ledger.service import LedgerService

NOW = "2026-10-01T00:00:00+00:00"


def hr(title):
    print("\n" + "=" * 72)
    print(title)
    print("-" * 72)


def show_book(svc, sat="SAT-1"):
    book = svc.schedule_book(sat)["satellites"][0]
    print("燃料账  容量=%(fuel_capacity).1f 已执行=%(fuel_consumed).1f "
          "预占=%(fuel_reserved).1f 剩余=%(fuel_remaining).1f" % book)
    for p in book["queue"]:
        tag = {"queued": "已排队占用", "approved": "批准但挂起", "stale": "失效待重评"}[p["state"]]
        print("  %-4s [%-4s] 风险=%-4s TCA=%s 需燃料=%-4.1f %s"
              % (p["plan_id"], tag, p["risk_level"], p["tca"][11:16],
                 p["fuel_required"], p.get("reject_reason", "")))


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo = LedgerRepository(tmp.name)
    repo.initialize()
    svc = LedgerService(repo)

    hr("0. 台账：卫星与人员（调度员授权范围不同）")
    svc.register_actor("admin", "admin")
    svc.register_actor("ana", "analyst")
    svc.register_actor("dutyA", "duty_officer")
    svc.register_actor("dutyB", "duty_officer")
    svc.register_actor("disp1", "dispatcher", satellites=["SAT-1"])   # 授权 SAT-1
    svc.register_actor("disp2", "dispatcher", satellites=["SAT-9"])   # 只授权别的星
    svc.register_satellite("SAT-1", 10.0, "admin")
    print("卫星 SAT-1 燃料容量 10.0；disp1 管 SAT-1，disp2 只被授权 SAT-9")

    hr("1. 三个接近事件同时到来（值班员各批各的）")
    svc.record_conjunction("SAT-1", "2026-10-01T06:00:00+00:00", 10, 100, "ana", "CJ-1", now=NOW)
    svc.record_conjunction("SAT-1", "2026-10-01T09:00:00+00:00", 10, 100, "ana", "CJ-2", now=NOW)
    svc.record_conjunction("SAT-1", "2026-10-01T12:00:00+00:00", 2000, 100, "ana", "CJ-3", now=NOW)
    svc.submit_plan("CJ-1", 6.0, "窗口1", "dutyA", plan_id="P1")
    svc.submit_plan("CJ-2", 3.0, "窗口2", "dutyB", plan_id="P2")
    svc.submit_plan("CJ-3", 4.0, "窗口3", "dutyA", plan_id="P3")
    r1 = svc.approve_plan("P1", "dutyA", 1)
    r2 = svc.approve_plan("P2", "dutyB", 1)
    r3 = svc.approve_plan("P3", "dutyA", 1)
    print("P1(高/06:00,6.0) 批准 ->", r1["decision"])
    print("P2(高/09:00,3.0) 批准 ->", r2["decision"])
    print("P3(低/12:00,4.0) 批准 ->", r3["decision"], "：风险最低且额度只剩 1.0，挂起不占")
    show_book(svc)

    hr("2. 同一方案两人同时提交：晚到按版本校验拒绝，先到保留")
    # 用一个还在待批准（v1）的新方案 P4 演示排队前的并发修改
    svc.record_conjunction("SAT-1", "2026-10-01T15:00:00+00:00", 10, 100, "ana", "CJ-4", now=NOW)
    svc.submit_plan("CJ-4", 2.0, "窗口4", "dutyB", plan_id="P4")
    first = svc.submit_plan("CJ-4", 2.0, "窗口4-先改", "dutyA", plan_id="P4", expected_version=1)
    print("先到变更成功，P4 版本 v%s" % first["version"])
    try:
        svc.submit_plan("CJ-4", 8.0, "窗口4-晚到", "dutyB", plan_id="P4", expected_version=1)
    except DomainError as exc:
        print("晚到变更被拒 [%s]：%s" % (exc.code, exc))
    print("P4 保留先到结果：窗口=%s 版本=v%s（未产生任何燃料占用）"
          % (svc.get_plan("P4")["maneuver_window"], svc.get_plan("P4")["version"]))

    hr("3. 调度员授权范围内代办执行 P1；越权直接拒绝")
    svc.execute_plan("P1", "CMD-1001", "disp1", r1["version"])
    print("disp1 在 SAT-1 授权内代办执行 P1 -> CMD-1001，真实扣减 6.0")
    try:
        svc.approve_plan("P4", "disp2", svc.get_plan("P4")["version"])
    except DomainError as exc:
        print("disp2 只授权 SAT-9，代办 SAT-1 被拒 [%s]：%s" % (exc.code, exc))
    try:
        svc.execute_plan("P2", "CMD-X", "ana", svc.get_plan("P2")["version"])
    except DomainError as exc:
        print("ana 角色越权执行被拒 [%s]：%s" % (exc.code, exc))
    show_book(svc)

    hr("4. 轨道数据修订：未执行的 P2 立即失效释放；已执行 P1 原样保留")
    report = svc.revise_orbit("CJ-2", 2000, 100, "ana")
    print("CJ-2 修订 r1->r%s，风险重算为 %s（脱靶量变大）"
          % (report["conjunction"]["revision"], report["conjunction"]["risk_level"]))
    print("失效待重评：%s；已执行 P1 原样保留：state=%s 指令=%s"
          % (report["stale_pending_reevaluation"],
             svc.get_plan("P1")["state"], svc.get_plan("P1")["command_ref"]))
    show_book(svc)

    hr("5. 失效方案重新评估后再批准，按新风险重新排队")
    ree = svc.reevaluate_plan("P2", "dutyB")
    print("P2 重新挂到 r%s：状态=%s 风险=%s"
          % (ree["based_on_revision"], ree["state"], ree["risk_level"]))
    again = svc.approve_plan("P2", "dutyB", ree["version"])
    print("P2 重新批准 ->", again["decision"], "（风险已降为 low，按新优先级排队）")
    show_book(svc)

    hr("6. 审计链（含拒绝留痕）")
    svc.verify_audit_chain()
    denied = [e for e in svc.audit_trail() if e["result"] == "denied"]
    print("审计链哈希校验通过；共 %s 条事件，其中拒绝留痕 %s 条："
          % (len(svc.audit_trail()), len(denied)))
    for e in denied:
        print("  [denied] %-13s actor=%-6s %s"
              % (e["event_type"], e["actor"], e["payload"].get("reason", "")[:64]))

    repo.close()
    os.unlink(tmp.name)


if __name__ == "__main__":
    main()
