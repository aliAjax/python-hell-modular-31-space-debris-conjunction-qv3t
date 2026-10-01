import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def _create_item(self, conn, entity_type, stable_key, initial_status, payload, actor, role):
        try:
            conn.execute(
                "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    entity_type,
                    stable_key,
                    initial_status,
                    1,
                    canonical_json(payload),
                    actor,
                    role,
                    now_iso(),
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("duplicate_item", "同一业务实体已经存在")
        item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
        return item_id

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._create_item(conn, entity_type, stable_key, initial_status, payload, actor, role)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def create_conjunction(self, stable_key, payload, actor, role):
        """创建接近事件，并在同一事务内为目标卫星开立燃料台账（若尚不存在）。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_id = self._create_item(conn, rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, payload, actor, role)
            satellite_id = payload.get("primary_object_id")
            fuel_budget = float(payload.get("fuel_budget_m_s", 0) or 0)
            if satellite_id:
                self._ensure_satellite_account(conn, satellite_id, fuel_budget, actor, role)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def create_satellite(self, satellite_id, fuel_budget, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            payload = {"satellite_id": satellite_id, "fuel_budget_m_s": float(fuel_budget)}
            self._create_item(conn, rules.SATELLITE_ENTITY_TYPE, satellite_id, rules.SATELLITE_STATUS, payload, actor, role)
            conn.execute("COMMIT")
            return self.get_item_by_stable(rules.SATELLITE_ENTITY_TYPE, satellite_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _ensure_satellite_account(self, conn, satellite_id, fuel_budget, actor, role):
        row = conn.execute(
            "SELECT id FROM items WHERE entity_type=? AND stable_key=?",
            (rules.SATELLITE_ENTITY_TYPE, satellite_id),
        ).fetchone()
        if row is not None:
            return row["id"]
        payload = {"satellite_id": satellite_id, "fuel_budget_m_s": float(fuel_budget)}
        return self._create_item(conn, rules.SATELLITE_ENTITY_TYPE, satellite_id, rules.SATELLITE_STATUS, payload, actor, role)

    def get_item_by_stable(self, entity_type, stable_key):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND stable_key=?",
                (entity_type, stable_key),
            ).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 调度账：卫星燃料台账与规避方案
    # ------------------------------------------------------------------

    def list_satellites(self):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM items WHERE entity_type=? ORDER BY id",
                (rules.SATELLITE_ENTITY_TYPE,),
            ).fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def list_plans(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM items WHERE entity_type=? AND status=? ORDER BY id",
                    (rules.PLAN_ENTITY_TYPE, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM items WHERE entity_type=? ORDER BY id",
                    (rules.PLAN_ENTITY_TYPE,),
                ).fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def _find_satellite(self, conn, satellite_id):
        row = conn.execute(
            "SELECT * FROM items WHERE entity_type=? AND stable_key=?",
            (rules.SATELLITE_ENTITY_TYPE, satellite_id),
        ).fetchone()
        return self._row_to_item(row)

    def _plans_for_satellite(self, conn, satellite_id):
        rows = conn.execute(
            "SELECT * FROM items WHERE entity_type=? AND status IN (?, ?) ORDER BY id",
            (rules.PLAN_ENTITY_TYPE, "queued", "executed"),
        ).fetchall()
        result = []
        for row in rows:
            item = self._row_to_item(row)
            if item["payload"].get("satellite_id") == satellite_id:
                result.append(item)
        return result

    def _insert_plan(self, conn, conjunction_id, plan_payload, actor, role):
        stable_key = "plan:%s:%s" % (conjunction_id, uuid.uuid4().hex[:12])
        conn.execute(
            "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rules.PLAN_ENTITY_TYPE,
                stable_key,
                plan_payload["status"],
                1,
                canonical_json(plan_payload),
                actor,
                role,
                now_iso(),
                now_iso(),
            ),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def fuel_balance(self, satellite_id):
        """卫星燃料台账：预算、已占用（排队）、已消耗（已执行）、可用余量。"""
        conn = self.connect()
        try:
            sat = self._find_satellite(conn, satellite_id)
            if sat is None:
                return None
            budget = float(sat["payload"].get("fuel_budget_m_s", 0) or 0)
            occupied = 0.0
            spent = 0.0
            for plan in self._plans_for_satellite(conn, satellite_id):
                cost = float(plan["payload"].get("fuel_cost_m_s", 0) or 0)
                if plan["status"] == "queued":
                    occupied += cost
                elif plan["status"] == "executed":
                    spent += cost
            return {
                "satellite_id": satellite_id,
                "fuel_budget_m_s": budget,
                "occupied_m_s": round(occupied, 3),
                "spent_m_s": round(spent, 3),
                "available_m_s": round(budget - occupied - spent, 3),
            }
        finally:
            conn.close()

    def _queue_snapshot(self, conn, satellite_id):
        plans = self._plans_for_satellite(conn, satellite_id)
        ordered = rules.order_queue(plans)
        snapshot = []
        for plan in ordered:
            payload = plan["payload"]
            snapshot.append(
                {
                    "plan_id": plan["id"],
                    "status": plan["status"],
                    "risk_level": payload.get("risk_level"),
                    "risk_score": payload.get("risk_score"),
                    "tca": payload.get("tca"),
                    "fuel_cost_m_s": payload.get("fuel_cost_m_s"),
                }
            )
        return snapshot

    def approve_plan(self, conjunction_id, payload, actor, role, expected_version):
        """批准规避方案：乐观锁校验版本，按风险/交会时刻排队并占用卫星燃料。

        余量不足时落一条 rejected 方案记录（含未排原因）并提交，再抛出
        fuel_budget_exceeded；先到的占用不会被晚到的变更冲掉。
        """
        conn = self.connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (conjunction_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            new_status, new_payload, event_payload = rules.apply_action(item, "approve", payload, actor, role)

            satellite_id = new_payload.get("primary_object_id")
            per_event_budget = float(new_payload.get("fuel_budget_m_s", 0) or 0)
            fuel_cost = float(payload.get("fuel_cost_m_s", 0) or 0)

            sat = self._find_satellite(conn, satellite_id)
            if sat is None:
                self._ensure_satellite_account(conn, satellite_id, per_event_budget, actor, role)
                sat_budget = per_event_budget
            else:
                sat_budget = float(sat["payload"].get("fuel_budget_m_s", 0) or 0)

            plans = self._plans_for_satellite(conn, satellite_id)
            occupied = sum(float(p["payload"].get("fuel_cost_m_s", 0) or 0) for p in plans if p["status"] == "queued")
            spent = sum(float(p["payload"].get("fuel_cost_m_s", 0) or 0) for p in plans if p["status"] == "executed")
            available = sat_budget - occupied - spent

            if fuel_cost > available:
                reason = {
                    "code": "fuel_budget_exceeded",
                    "message": "卫星燃料余量不足，方案未排入",
                    "fuel_cost_m_s": fuel_cost,
                    "satellite_budget_m_s": sat_budget,
                    "occupied_m_s": round(occupied, 3),
                    "spent_m_s": round(spent, 3),
                    "available_m_s": round(available, 3),
                    "queue": self._queue_snapshot(conn, satellite_id),
                }
                plan_payload = {
                    "conjunction_id": conjunction_id,
                    "satellite_id": satellite_id,
                    "fuel_cost_m_s": fuel_cost,
                    "maneuver_window": payload.get("maneuver_window"),
                    "risk_level": (new_payload.get("assessment") or {}).get("level"),
                    "risk_score": (new_payload.get("assessment") or {}).get("score"),
                    "tca": new_payload.get("tca"),
                    "status": "rejected",
                    "queued_by": actor,
                    "queued_at": now_iso(),
                    "reject_reason": reason,
                }
                plan_id = self._insert_plan(conn, conjunction_id, plan_payload, actor, role)
                self.append_audit(conn, conjunction_id, "plan_rejected", actor, role, reason)
                self.append_audit(conn, plan_id, "plan_rejected", actor, role, reason)
                conn.execute("COMMIT")
                committed = True
                raise DomainError(
                    "fuel_budget_exceeded",
                    "卫星燃料余量不足（可用 %.2f m/s，本次需求 %.2f m/s），方案未排入" % (round(available, 3), fuel_cost),
                    409,
                )

            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), conjunction_id),
            )
            plan_payload = {
                "conjunction_id": conjunction_id,
                "satellite_id": satellite_id,
                "fuel_cost_m_s": fuel_cost,
                "maneuver_window": payload.get("maneuver_window"),
                "risk_level": (new_payload.get("assessment") or {}).get("level"),
                "risk_score": (new_payload.get("assessment") or {}).get("score"),
                "tca": new_payload.get("tca"),
                "status": "queued",
                "queued_by": actor,
                "queued_at": now_iso(),
                "executed_at": None,
                "command_ref": None,
                "void_reason": None,
                "voided_at": None,
                "reject_reason": None,
            }
            plan_id = self._insert_plan(conn, conjunction_id, plan_payload, actor, role)
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (conjunction_id, "approve", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, conjunction_id, "approve", actor, role, event_payload)
            self.append_audit(
                conn,
                plan_id,
                "plan_queued",
                actor,
                role,
                {
                    "conjunction_id": conjunction_id,
                    "satellite_id": satellite_id,
                    "fuel_cost_m_s": fuel_cost,
                    "risk_level": plan_payload["risk_level"],
                    "tca": plan_payload["tca"],
                },
            )
            conn.execute("COMMIT")
            committed = True
            return self.get_item(conjunction_id)
        except Exception:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()

    def execute_plan(self, conjunction_id, payload, actor, role, expected_version):
        """执行已排队方案：占用转为消耗，已执行指令保留原记录。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (conjunction_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            new_status, new_payload, event_payload = rules.apply_action(item, "execute", payload, actor, role)

            plan_row = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND status=? ORDER BY id DESC LIMIT 1",
                (rules.PLAN_ENTITY_TYPE, "queued"),
            ).fetchall()
            active = None
            for candidate in plan_row:
                candidate_item = self._row_to_item(candidate)
                if candidate_item["payload"].get("conjunction_id") == conjunction_id:
                    active = candidate_item
                    break
            if active is None:
                raise DomainError("no_active_plan", "没有已排队的规避方案可执行")

            plan_payload = active["payload"]
            plan_payload["status"] = "executed"
            plan_payload["executed_at"] = now_iso()
            plan_payload["command_ref"] = payload.get("command_ref")

            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), conjunction_id),
            )
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                ("executed", int(active["version"]) + 1, canonical_json(plan_payload), now_iso(), active["id"]),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (conjunction_id, "execute", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, conjunction_id, "execute", actor, role, event_payload)
            self.append_audit(
                conn,
                active["id"],
                "plan_executed",
                actor,
                role,
                {"conjunction_id": conjunction_id, "command_ref": payload.get("command_ref"), "fuel_cost_m_s": plan_payload.get("fuel_cost_m_s")},
            )
            conn.execute("COMMIT")
            return self.get_item(conjunction_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def revise_track(self, conjunction_id, payload, actor, role, expected_version):
        """轨道修订：作废依赖旧数据的未执行方案（释放燃料），已执行指令保留原记录。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (conjunction_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            item = self._row_to_item(row)
            previous_status = item["status"]
            new_status, new_payload, event_payload = rules.apply_action(item, "report_revision", payload, actor, role)

            voided = []
            plan_rows = conn.execute(
                "SELECT * FROM items WHERE entity_type=? AND status=? ORDER BY id",
                (rules.PLAN_ENTITY_TYPE, "queued"),
            ).fetchall()
            for plan_row in plan_rows:
                plan = self._row_to_item(plan_row)
                if plan["payload"].get("conjunction_id") != conjunction_id:
                    continue
                plan_payload = plan["payload"]
                plan_payload["status"] = "voided"
                plan_payload["void_reason"] = "orbit_revised"
                plan_payload["voided_at"] = now_iso()
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    ("voided", int(plan["version"]) + 1, canonical_json(plan_payload), now_iso(), plan["id"]),
                )
                self.append_audit(
                    conn,
                    plan["id"],
                    "plan_voided",
                    actor,
                    role,
                    {"conjunction_id": conjunction_id, "reason": "orbit_revised", "revision": event_payload.get("revision")},
                )
                voided.append(plan["id"])

            if previous_status == "coordinating":
                # 未执行方案作废：批准失效，回到待评估状态重新排队
                new_status = "assessed"
                new_payload["approved_maneuver"] = None
            # executing/resolved：已执行指令保留原记录，状态与 command_ref 不动

            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), conjunction_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (conjunction_id, "orbit_revised", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(
                conn,
                conjunction_id,
                "orbit_revised",
                actor,
                role,
                {"revision": event_payload.get("revision"), "voided_plans": voided, "reassessment": new_payload.get("assessment")},
            )
            conn.execute("COMMIT")
            return self.get_item(conjunction_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def record_denial(self, item_id, action, actor, role, reason):
        """越权变更直接拒绝并留下审计记录（不改变业务版本）。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.append_audit(
                conn,
                item_id,
                "authorization_denied",
                actor,
                role,
                {"action": action, "reason": reason},
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
