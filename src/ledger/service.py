"""调度账用例编排：授权代办、版本校验、风险/TCA 排队、轨道修订失效重算。

事务约定：业务变更与 accepted 审计在同一事务里原子提交；
任何校验失败（含越权、版本冲突、余量不足、过期数据）都回滚业务数据，
随后在独立事务补记一条 result=denied 的审计，做到"拒绝也留痕"。
"""

from .models import (
    ACTION_ROLES,
    Actor,
    Conjunction,
    DomainError,
    Plan,
    PlanState,
    RiskLevel,
    Role,
    Satellite,
    WILDCARD,
    assess_risk,
    parse_iso,
    queue_order,
)
from .repository import now_iso


class LedgerService:
    def __init__(self, repository):
        self.repository = repository
        self._seq = 0

    def _id(self, prefix):
        self._seq += 1
        return "%s-%d" % (prefix, self._seq)

    def _unit(self, action, actor, fn):
        """执行一个业务单元：成功整体提交；DomainError 时回滚并补记 denied 审计后抛出。
        若结果是"已落库但要通知调用方被拒/失效"，fn 返回 _CommitThenRaise：
        事务照常提交（状态与审计都保留），随后再抛出对应错误。"""
        repo = self.repository
        try:
            with repo.transaction() as tx:
                out = fn(tx)
        except DomainError as exc:
            # 业务事务已回滚；用独立事务留下拒绝记录（不覆盖越权时已记录的条目）
            if not getattr(exc, "_audited", False):
                repo.record_denied(action, actor.actor_id if actor else None,
                                   actor.role if actor else None, exc.code + ": " + str(exc),
                                   getattr(exc, "audit_payload", None))
            raise
        if isinstance(out, _CommitThenRaise):
            raise out.error
        return out

    # ------------------------------------------------------------------
    # 身份与授权
    # ------------------------------------------------------------------
    def _actor(self, actor_id):
        return self.repository.get_actor(actor_id)

    def _authorize(self, action, actor, satellite_id=None):
        """角色 + 卫星范围校验。越权直接拒绝：补记 denied 审计并抛出（数据不动）。"""
        allowed = ACTION_ROLES.get(action, set())
        if actor.role not in allowed:
            reason = "角色 %s 无权执行 %s（允许：%s）" % (
                actor.role.value, action, "、".join(sorted(r.value for r in allowed)))
            err = DomainError("forbidden", reason, 403)
            err.audit_payload = {"satellite_id": satellite_id}
            self.repository.record_denied(action, actor.actor_id, actor.role, reason,
                                          {"satellite_id": satellite_id})
            err._audited = True
            raise err
        if satellite_id is not None and not actor.can_touch(satellite_id):
            reason = "调度员 %s 未获得卫星 %s 的授权（授权范围：%s）" % (
                actor.actor_id, satellite_id, "、".join(actor.satellites))
            err = DomainError("out_of_scope", reason, 403)
            err.audit_payload = {"satellite_id": satellite_id}
            self.repository.record_denied(action, actor.actor_id, actor.role, reason,
                                          {"satellite_id": satellite_id})
            err._audited = True
            raise err

    def register_actor(self, actor_id, role, satellites=None, *, by=None):
        role = role if isinstance(role, Role) else Role.parse(role)
        actor = Actor(actor_id, role, list(satellites) if satellites else [WILDCARD])
        with self.repository.transaction() as repo:
            repo.upsert_actor(actor)
            repo.append_audit("register_actor", by or actor_id, role, "accepted",
                              {"actor_id": actor_id, "role": role.value, "satellites": actor.satellites})
        return self._actor_view(actor)

    def register_satellite(self, satellite_id, fuel_capacity, actor_id):
        actor = self._actor(actor_id)
        self._authorize("register_satellite", actor)
        if fuel_capacity <= 0:
            raise DomainError("invalid_number", "燃料容量必须大于零")
        satellite = Satellite(satellite_id, float(fuel_capacity))

        def work(tx):
            tx.upsert_satellite(satellite)
            tx.append_audit("register_satellite", actor.actor_id, actor.role, "accepted",
                            {"satellite_id": satellite_id, "fuel_capacity": fuel_capacity})
            return self._satellite_view(satellite)

        return self._unit("register_satellite", actor, work)

    # ------------------------------------------------------------------
    # 接近事件与轨道修订
    # ------------------------------------------------------------------
    def record_conjunction(self, satellite_id, tca, miss_distance_m, covariance_m,
                           actor_id, conjunction_id=None, now=None):
        actor = self._actor(actor_id)
        self._authorize("record_conjunction", actor, satellite_id)
        self.repository.get_satellite(satellite_id)  # 卫星必须存在
        tca = parse_iso(tca).isoformat()
        hours = self._hours_to_tca(tca, now)
        risk = assess_risk(miss_distance_m, covariance_m, hours)
        cid = conjunction_id or self._id("CJ")
        conjunction = Conjunction(
            conjunction_id=cid, satellite_id=satellite_id, tca=tca,
            risk_level=RiskLevel(risk["level"]), risk_score=risk["score"], revision=1,
            revision_history=[{
                "revision": 1, "observed_at": now_iso(),
                "miss_distance_m": miss_distance_m, "covariance_m": covariance_m,
                "risk_level": risk["level"], "risk_score": risk["score"],
            }],
        )

        def work(tx):
            tx.insert_conjunction(conjunction)
            tx.append_audit("record_conjunction", actor.actor_id, actor.role, "accepted",
                            {"conjunction_id": cid, "satellite_id": satellite_id,
                             "risk_level": risk["level"], "risk_score": risk["score"]})
            return self._conjunction_view(conjunction)

        return self._unit("record_conjunction", actor, work)

    def revise_orbit(self, conjunction_id, miss_distance_m, covariance_m, actor_id,
                     observed_at=None, new_tca=None):
        """轨道数据修订：新版本覆盖；依赖旧数据的未执行方案立即失效（stale）、释放占用并重算。
        已执行指令保留原记录，不受影响。"""
        actor = self._actor(actor_id)
        conjunction = self.repository.get_conjunction(conjunction_id)
        self._authorize("revise_orbit", actor, conjunction.satellite_id)
        if covariance_m <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        observed_at = parse_iso(observed_at).isoformat() if observed_at else now_iso()
        tca = parse_iso(new_tca).isoformat() if new_tca else conjunction.tca

        def work(tx):
            current = tx.get_conjunction(conjunction_id)
            old_revision = current.revision
            hours = self._hours_to_tca(tca, observed_at)
            risk = assess_risk(miss_distance_m, covariance_m, hours)
            new_revision = old_revision + 1
            current.revision = new_revision
            current.tca = tca
            current.risk_level = RiskLevel(risk["level"])
            current.risk_score = risk["score"]
            current.revision_history.append({
                "revision": new_revision, "observed_at": observed_at,
                "miss_distance_m": miss_distance_m, "covariance_m": covariance_m,
                "risk_level": risk["level"], "risk_score": risk["score"],
            })
            tx.update_conjunction(current)

            invalidated = []
            for plan in tx.list_plans():
                if plan.conjunction_id != conjunction_id:
                    continue
                # 已执行永久保留；草稿/已拒/已失效/已取消无占用
                if plan.state in (PlanState.DRAFT, PlanState.STALE, PlanState.REJECTED,
                                  PlanState.EXECUTED, PlanState.CANCELLED):
                    continue
                if plan.based_on_revision < new_revision:
                    plan.state = PlanState.STALE
                    plan.reservation = ""
                    plan.approved_by = ""
                    plan.reject_reason = ""
                    plan.version += 1
                    tx.update_plan(plan)
                    tx.add_fuel_movement(plan.satellite_id, plan.plan_id, "release",
                                         plan.fuel_required,
                                         "轨道修订 r%s->r%s，旧批准与占用失效" % (old_revision, new_revision))
                    invalidated.append(plan.plan_id)
            payload = {
                "conjunction_id": conjunction_id, "revision": new_revision,
                "old_revision": old_revision, "risk_level": risk["level"],
                "risk_score": risk["score"], "invalidated_plans": invalidated,
            }
            tx.append_audit("revise_orbit", actor.actor_id, actor.role, "accepted", payload)
            # 释放后立即按最新数据重算（stale 方案不参与，待重新评估）
            self._recompute_locked(tx, current.satellite_id)
            return self.revision_report(conjunction_id)

        return self._unit("revise_orbit", actor, work)

    # ------------------------------------------------------------------
    # 规避方案：提交（乐观版本）/ 重评 / 批准 / 执行 / 取消
    # ------------------------------------------------------------------
    def submit_plan(self, conjunction_id, fuel_required, maneuver_window, actor_id,
                    plan_id=None, expected_version=None):
        """提交方案；对已存在 plan_id 的变更必须带正确版本（乐观锁）。
        两人同时改同一方案时，晚到且版本过期的变更被拒，先到占用保留。"""
        actor = self._actor(actor_id)
        conjunction = self.repository.get_conjunction(conjunction_id)
        self._authorize("submit_plan", actor, conjunction.satellite_id)
        try:
            fuel_required = float(fuel_required)
        except (TypeError, ValueError):
            raise DomainError("invalid_number", "规避燃料必须是数字")
        if fuel_required <= 0:
            raise DomainError("invalid_number", "规避燃料必须大于零")

        def work(tx):
            current_cj = tx.get_conjunction(conjunction_id)
            existing = None
            if plan_id is not None:
                for p in tx.list_plans():
                    if p.plan_id == plan_id:
                        existing = p
                        break
            if existing is None:
                pid = plan_id or self._id("PL")
                plan = Plan(
                    plan_id=pid, conjunction_id=conjunction_id,
                    satellite_id=current_cj.satellite_id, proposed_by=actor.actor_id,
                    fuel_required=fuel_required, risk_level=current_cj.risk_level,
                    tca=current_cj.tca, based_on_revision=current_cj.revision,
                    maneuver_window=maneuver_window or "", state=PlanState.DRAFT, version=1,
                )
                tx.insert_plan(plan)
                tx.append_audit("submit_plan", actor.actor_id, actor.role, "accepted",
                                {"plan_id": pid, "conjunction_id": conjunction_id,
                                 "fuel_required": fuel_required, "mode": "create"})
                return self._plan_view(tx.get_plan(pid))

            # 变更已有方案：乐观版本校验
            if expected_version is None:
                raise _denied(DomainError("expected_version_required", "变更方案需要 expected_version"),
                              {"plan_id": plan_id, "current_version": existing.version})
            if int(expected_version) != existing.version:
                reason = "版本冲突：你基于 v%s，方案已更新到 v%s（先到的变更与占用保留）" % (
                    expected_version, existing.version)
                raise _denied(DomainError("version_conflict", reason, 409),
                              {"plan_id": plan_id, "expected_version": int(expected_version),
                               "current_version": existing.version})
            if existing.state == PlanState.EXECUTED:
                raise _denied(DomainError("invalid_state", "已执行方案记录不可变更", 409),
                              {"plan_id": plan_id})
            if existing.state in (PlanState.APPROVED, PlanState.QUEUED):
                raise _denied(DomainError("invalid_state", "已批准并占用燃料的方案不能直接修改，请先取消", 409),
                              {"plan_id": plan_id, "state": existing.state.value})
            if existing.based_on_revision < current_cj.revision:
                raise _denied(DomainError("stale_track",
                                          "方案基于过期轨道数据 r%s，请先重新评估（当前 r%s）" % (
                                              existing.based_on_revision, current_cj.revision), 409),
                              {"plan_id": plan_id, "based_on_revision": existing.based_on_revision,
                               "current_revision": current_cj.revision})
            existing.fuel_required = fuel_required
            existing.maneuver_window = maneuver_window or existing.maneuver_window
            existing.risk_level = current_cj.risk_level
            existing.tca = current_cj.tca
            existing.version += 1
            tx.update_plan(existing)
            tx.append_audit("submit_plan", actor.actor_id, actor.role, "accepted",
                            {"plan_id": plan_id, "mode": "update", "version": existing.version,
                             "fuel_required": fuel_required})
            return self._plan_view(tx.get_plan(plan_id))

        return self._unit("submit_plan", actor, work)

    def reevaluate_plan(self, plan_id, actor_id):
        """轨道修订后，把失效方案重新挂到最新数据上评估，回到待批准（draft）。"""
        actor = self._actor(actor_id)
        plan = self.repository.get_plan(plan_id)
        self._authorize("submit_plan", actor, plan.satellite_id)

        def work(tx):
            current = tx.get_plan(plan_id)
            if current.state != PlanState.STALE:
                raise _denied(DomainError("invalid_state",
                                          "仅失效（stale）方案需要重新评估，当前 %s" % current.state.value, 409),
                              {"plan_id": plan_id, "state": current.state.value})
            cj = tx.get_conjunction(current.conjunction_id)
            current.based_on_revision = cj.revision
            current.risk_level = cj.risk_level
            current.tca = cj.tca
            current.state = PlanState.DRAFT
            current.reservation = ""
            current.version += 1
            tx.update_plan(current)
            tx.append_audit("reevaluate_plan", actor.actor_id, actor.role, "accepted",
                            {"plan_id": plan_id, "revision": cj.revision,
                             "risk_level": cj.risk_level.value})
            return self._plan_view(tx.get_plan(plan_id))

        return self._unit("reevaluate_plan", actor, work)

    def approve_plan(self, plan_id, actor_id, expected_version):
        """值班员批准：进入风险+TCA 排队账占用燃料；余量不足则拒绝并说明未排原因。"""
        actor = self._actor(actor_id)
        plan = self.repository.get_plan(plan_id)
        self._authorize("approve_plan", actor, plan.satellite_id)
        if expected_version is None:
            raise DomainError("expected_version_required", "批准需要 expected_version")

        def work(tx):
            current = tx.get_plan(plan_id)
            cj = tx.get_conjunction(current.conjunction_id)
            if current.state not in (PlanState.DRAFT, PlanState.REJECTED):
                raise _denied(DomainError("invalid_state",
                                          "仅待批准方案可批准，当前 %s" % current.state.value, 409),
                              {"plan_id": plan_id, "state": current.state.value})
            if int(expected_version) != current.version:
                reason = "版本冲突：你基于 v%s 批准，方案已更新到 v%s" % (expected_version, current.version)
                raise _denied(DomainError("version_conflict", reason, 409),
                              {"plan_id": plan_id, "expected_version": int(expected_version),
                               "current_version": current.version})
            if current.based_on_revision < cj.revision:
                # 批准瞬间发现数据已过期：方案置为失效并落库（不占燃料），同时告知批准未生效
                current.state = PlanState.STALE
                current.version += 1
                tx.update_plan(current)
                reason = "方案基于过期轨道 r%s（当前 r%s），批准未生效，请重新评估" % (
                    current.based_on_revision, cj.revision)
                tx.append_audit("approve_plan", actor.actor_id, actor.role, "stale",
                                {"plan_id": plan_id, "reason": reason,
                                 "based_on_revision": current.based_on_revision,
                                 "current_revision": cj.revision})
                return _CommitThenRaise(DomainError("stale_track", reason, 409))

            satellite = tx.get_satellite(current.satellite_id)
            if current.fuel_required > satellite.available:
                reason = "燃料余量不足：需要 %.3f，可用 %.3f（容量 %.3f，已执行消耗 %.3f）" % (
                    current.fuel_required, satellite.available,
                    satellite.fuel_capacity, satellite.fuel_consumed)
                current.state = PlanState.REJECTED
                current.reject_reason = reason
                current.approved_by = actor.actor_id
                current.version += 1
                tx.update_plan(current)
                tx.append_audit("approve_plan", actor.actor_id, actor.role, "rejected",
                                {"plan_id": plan_id, "reason": reason,
                                 "fuel_required": current.fuel_required,
                                 "fuel_available": satellite.available})
                result = self._plan_view(tx.get_plan(plan_id))
                result["decision"] = "rejected"
                result["reason"] = reason
                return result

            current.state = PlanState.APPROVED
            current.approved_by = actor.actor_id
            current.reject_reason = ""
            current.version += 1
            tx.update_plan(current)
            tx.append_audit("approve_plan", actor.actor_id, actor.role, "accepted",
                            {"plan_id": plan_id, "fuel_required": current.fuel_required})
            queue = self._recompute_locked(tx, current.satellite_id)
            result = self._plan_view(tx.get_plan(plan_id))
            result["decision"] = "queued" if result["reservation"] == "queued" else "deferred"
            if result["decision"] == "deferred":
                result["reason"] = result["reject_reason"]
            result["queue"] = queue
            return result

        return self._unit("approve_plan", actor, work)

    def execute_plan(self, plan_id, command_ref, actor_id, expected_version):
        """执行已排到燃料的方案：真实扣减燃料，指令记录永久保留（执行后再修订也不撤销）。"""
        actor = self._actor(actor_id)
        plan = self.repository.get_plan(plan_id)
        self._authorize("execute_plan", actor, plan.satellite_id)
        if not command_ref or not str(command_ref).strip():
            raise DomainError("field_required", "command_ref 不能为空")

        def work(tx):
            current = tx.get_plan(plan_id)
            if expected_version is None or int(expected_version) != current.version:
                raise _denied(DomainError("version_conflict", "版本不匹配，执行被拒", 409),
                              {"plan_id": plan_id, "expected_version": expected_version,
                               "current_version": current.version})
            if current.state != PlanState.QUEUED:
                raise _denied(DomainError("invalid_state",
                                          "仅已排队（queued）方案可执行，当前 %s" % current.state.value, 409),
                              {"plan_id": plan_id, "state": current.state.value})
            satellite = tx.get_satellite(current.satellite_id)
            if current.fuel_required > satellite.available:
                raise _denied(DomainError("fuel_shortage",
                                          "执行前燃料余量不足，需 %.3f，可用 %.3f" % (
                                              current.fuel_required, satellite.available), 409),
                              {"plan_id": plan_id, "fuel_required": current.fuel_required,
                               "fuel_available": satellite.available})
            tx.consume_fuel(current.satellite_id, current.fuel_required)
            tx.add_fuel_movement(current.satellite_id, plan_id, "consume",
                                 current.fuel_required, "规避指令执行 %s" % command_ref)
            current.state = PlanState.EXECUTED
            current.command_ref = str(command_ref).strip()
            current.executed_at = now_iso()
            current.reservation = ""
            current.version += 1
            tx.update_plan(current)
            tx.append_audit("execute_plan", actor.actor_id, actor.role, "accepted",
                            {"plan_id": plan_id, "command_ref": current.command_ref,
                             "fuel_consumed": current.fuel_required})
            queue = self._recompute_locked(tx, current.satellite_id)
            result = self._plan_view(tx.get_plan(plan_id))
            result["queue"] = queue
            return result

        return self._unit("execute_plan", actor, work)

    def cancel_plan(self, plan_id, reason, actor_id, expected_version):
        actor = self._actor(actor_id)
        plan = self.repository.get_plan(plan_id)
        self._authorize("cancel_plan", actor, plan.satellite_id)

        def work(tx):
            current = tx.get_plan(plan_id)
            if expected_version is None or int(expected_version) != current.version:
                raise _denied(DomainError("version_conflict", "版本不匹配，取消被拒", 409),
                              {"plan_id": plan_id, "expected_version": expected_version,
                               "current_version": current.version})
            if current.state in (PlanState.EXECUTED, PlanState.CANCELLED):
                raise _denied(DomainError("invalid_state",
                                          "%s 方案不可取消" % current.state.value, 409),
                              {"plan_id": plan_id, "state": current.state.value})
            was_live = current.is_live()
            current.state = PlanState.CANCELLED
            current.reservation = ""
            current.reject_reason = reason or ""
            current.version += 1
            tx.update_plan(current)
            if was_live:
                tx.add_fuel_movement(current.satellite_id, plan_id, "release",
                                     current.fuel_required, "取消方案，释放预占燃料")
            tx.append_audit("cancel_plan", actor.actor_id, actor.role, "accepted",
                            {"plan_id": plan_id, "reason": reason})
            queue = self._recompute_locked(tx, current.satellite_id)
            result = self._plan_view(tx.get_plan(plan_id))
            result["queue"] = queue
            return result

        return self._unit("cancel_plan", actor, work)

    # ------------------------------------------------------------------
    # 排队重算：风险等级 -> 交会时刻；余量不足挂起并写明原因，绝不冲成负数
    # ------------------------------------------------------------------
    def _recompute_locked(self, tx, satellite_id):
        satellite = tx.get_satellite(satellite_id)
        live = [p for p in tx.list_plans(satellite_id)
                if p.state in (PlanState.APPROVED, PlanState.QUEUED)]
        live.sort(key=queue_order)
        used = 0.0
        queue, deferred = [], []
        for plan in live:  # 清掉上一轮占用标记，再按优先级重新占
            plan.reservation = ""
            plan.reject_reason = ""
        for plan in live:
            remaining = round(satellite.available - used, 9)
            if plan.fuel_required <= remaining + 1e-9:
                plan.state = PlanState.QUEUED
                plan.reservation = "queued"
                used += plan.fuel_required
                queue.append(plan.plan_id)
                tx.update_plan(plan)
            else:
                plan.state = PlanState.APPROVED
                plan.reservation = "deferred"
                plan.reject_reason = (
                    "排队未排上：风险 %s / TCA %s；需要 %.3f，重算后仅剩 %.3f"
                    "（容量 %.3f，已执行 %.3f，更高优先级已预占 %.3f）" % (
                        plan.risk_level.value, plan.tca, plan.fuel_required,
                        max(remaining, 0.0), satellite.fuel_capacity,
                        satellite.fuel_consumed, round(used, 9)))
                deferred.append({"plan_id": plan.plan_id, "reason": plan.reject_reason})
                tx.update_plan(plan)
        return {
            "satellite_id": satellite_id,
            "fuel_capacity": satellite.fuel_capacity,
            "fuel_consumed": round(satellite.fuel_consumed, 9),
            "fuel_reserved": round(used, 9),
            "fuel_available_after_queue": round(satellite.available - used, 9),
            "queued": queue,
            "deferred": deferred,
        }

    def recompute(self, satellite_id, actor_id):
        """供外部触发的人工重算。"""
        actor = self._actor(actor_id)
        self._authorize("submit_plan", actor, satellite_id)

        def work(tx):
            queue = self._recompute_locked(tx, satellite_id)
            tx.append_audit("recompute", actor.actor_id, actor.role, "accepted",
                            {"satellite_id": satellite_id, "queued": queue["queued"],
                             "deferred": [d["plan_id"] for d in queue["deferred"]]})
            return queue

        return self._unit("recompute", actor, work)

    # ------------------------------------------------------------------
    # 查询：调度账 / 燃料流水 / 修订报告 / 审计
    # ------------------------------------------------------------------
    def fuel_ledger(self, satellite_id):
        satellite = self.repository.get_satellite(satellite_id)
        reserved = sum(p.fuel_required for p in self.repository.list_plans(satellite_id)
                       if p.state == PlanState.QUEUED)
        return {
            "satellite_id": satellite_id,
            "fuel_capacity": satellite.fuel_capacity,
            "fuel_consumed": round(satellite.fuel_consumed, 9),
            "fuel_reserved": round(reserved, 9),
            "fuel_remaining": round(satellite.available - reserved, 9),
            "movements": self.repository.fuel_movements(satellite_id),
        }

    def schedule_book(self, satellite_id=None):
        """一张调度账：每颗卫星的燃料账 + 按风险/TCA 排好的方案队列。"""
        repo = self.repository
        satellites = [repo.get_satellite(satellite_id)] if satellite_id else self._all_satellites()
        book = []
        for sat in satellites:
            plans = sorted(
                [p for p in repo.list_plans(sat.satellite_id)
                 if p.state in (PlanState.QUEUED, PlanState.APPROVED, PlanState.STALE)],
                key=queue_order,
            )
            reserved = sum(p.fuel_required for p in plans if p.state == PlanState.QUEUED)
            book.append({
                "satellite_id": sat.satellite_id,
                "fuel_capacity": sat.fuel_capacity,
                "fuel_consumed": round(sat.fuel_consumed, 9),
                "fuel_reserved": round(reserved, 9),
                "fuel_remaining": round(sat.available - reserved, 9),
                "queue": [self._plan_view(p) for p in plans],
            })
        return {"satellites": book}

    def revision_report(self, conjunction_id):
        conjunction = self.repository.get_conjunction(conjunction_id)
        plans = [self._plan_view(p) for p in self.repository.list_plans()
                 if p.conjunction_id == conjunction_id]
        return {
            "conjunction": self._conjunction_view(conjunction),
            "plans": plans,
            "executed_kept": [p["plan_id"] for p in plans if p["state"] == PlanState.EXECUTED.value],
            "stale_pending_reevaluation": [p["plan_id"] for p in plans
                                           if p["state"] == PlanState.STALE.value],
        }

    def get_plan(self, plan_id):
        return self._plan_view(self.repository.get_plan(plan_id))

    def audit_trail(self):
        return self.repository.audit_trail()

    def verify_audit_chain(self):
        return self.repository.verify_audit_chain()

    # ------------------------------------------------------------------
    def _all_satellites(self):
        rows = self.repository.conn.execute(
            "SELECT * FROM ledger_satellites ORDER BY satellite_id").fetchall()
        return [Satellite(r["satellite_id"], r["fuel_capacity"], r["fuel_consumed"]) for r in rows]

    @staticmethod
    def _hours_to_tca(tca, now):
        if now is None:
            return None
        return (parse_iso(tca) - parse_iso(now)).total_seconds() / 3600.0

    @staticmethod
    def _actor_view(actor):
        return {"actor_id": actor.actor_id, "role": actor.role.value, "satellites": actor.satellites}

    @staticmethod
    def _satellite_view(satellite):
        return {
            "satellite_id": satellite.satellite_id,
            "fuel_capacity": satellite.fuel_capacity,
            "fuel_consumed": satellite.fuel_consumed,
            "fuel_available": satellite.available,
        }

    @staticmethod
    def _conjunction_view(conjunction):
        return {
            "conjunction_id": conjunction.conjunction_id,
            "satellite_id": conjunction.satellite_id,
            "tca": conjunction.tca,
            "risk_level": conjunction.risk_level.value,
            "risk_score": conjunction.risk_score,
            "revision": conjunction.revision,
            "revision_history": conjunction.revision_history,
        }

    @staticmethod
    def _plan_view(plan):
        return {
            "plan_id": plan.plan_id,
            "conjunction_id": plan.conjunction_id,
            "satellite_id": plan.satellite_id,
            "proposed_by": plan.proposed_by,
            "approved_by": plan.approved_by,
            "fuel_required": plan.fuel_required,
            "risk_level": plan.risk_level.value,
            "tca": plan.tca,
            "based_on_revision": plan.based_on_revision,
            "maneuver_window": plan.maneuver_window,
            "state": plan.state.value,
            "reservation": plan.reservation,
            "reject_reason": plan.reject_reason,
            "command_ref": plan.command_ref,
            "executed_at": plan.executed_at,
            "version": plan.version,
        }


class _denied(DomainError):
    """业务单元内部抛出的拒绝：携带审计 payload；由 _unit 回滚后补记 denied 审计。"""

    def __init__(self, error, audit_payload):
        super().__init__(error.code, str(error), error.status)
        self.audit_payload = audit_payload


class _CommitThenRaise:
    """业务结果需落库（连同审计一起提交），但仍要向调用方抛错（如批准时发现已过期）。"""

    def __init__(self, error):
        self.error = error
