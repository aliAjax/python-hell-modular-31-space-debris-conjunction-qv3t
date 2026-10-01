"""调度账的 SQLite 存储：事务、乐观版本、燃料流水与哈希审计链。"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from ..audit import audit_hash, canonical_json
from .models import (
    Actor,
    ConflictError,
    Conjunction,
    DomainError,
    NotFoundError,
    Plan,
    PlanState,
    RiskLevel,
    Role,
    Satellite,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class LedgerRepository:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def initialize(self):
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_actors (
                    actor_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    satellites TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_satellites (
                    satellite_id TEXT PRIMARY KEY,
                    fuel_capacity REAL NOT NULL,
                    fuel_consumed REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ledger_conjunctions (
                    conjunction_id TEXT PRIMARY KEY,
                    satellite_id TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_score REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    revision_history TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_plans (
                    plan_id TEXT PRIMARY KEY,
                    conjunction_id TEXT NOT NULL,
                    satellite_id TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    fuel_required REAL NOT NULL,
                    risk_level TEXT NOT NULL,
                    tca TEXT NOT NULL,
                    based_on_revision INTEGER NOT NULL,
                    maneuver_window TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    approved_by TEXT NOT NULL DEFAULT '',
                    reject_reason TEXT NOT NULL DEFAULT '',
                    command_ref TEXT NOT NULL DEFAULT '',
                    executed_at TEXT NOT NULL DEFAULT '',
                    reservation TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS ledger_fuel_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    satellite_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    delta REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    result TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---------- 序列化 ----------
    @staticmethod
    def _plan_from_row(row):
        return Plan(
            plan_id=row["plan_id"],
            conjunction_id=row["conjunction_id"],
            satellite_id=row["satellite_id"],
            proposed_by=row["proposed_by"],
            fuel_required=row["fuel_required"],
            risk_level=RiskLevel(row["risk_level"]),
            tca=row["tca"],
            based_on_revision=row["based_on_revision"],
            maneuver_window=row["maneuver_window"],
            state=PlanState(row["state"]),
            version=row["version"],
            approved_by=row["approved_by"],
            reject_reason=row["reject_reason"],
            command_ref=row["command_ref"],
            executed_at=row["executed_at"],
            reservation=row["reservation"],
        )

    @staticmethod
    def _conjunction_from_row(row):
        return Conjunction(
            conjunction_id=row["conjunction_id"],
            satellite_id=row["satellite_id"],
            tca=row["tca"],
            risk_level=RiskLevel(row["risk_level"]),
            risk_score=row["risk_score"],
            revision=row["revision"],
            revision_history=json.loads(row["revision_history"]),
        )

    # ---------- actor / satellite ----------
    def upsert_actor(self, actor):
        self.conn.execute(
            "INSERT INTO ledger_actors(actor_id,role,satellites) VALUES(?,?,?) "
            "ON CONFLICT(actor_id) DO UPDATE SET role=excluded.role, satellites=excluded.satellites",
            (actor.actor_id, actor.role.value, canonical_json(actor.satellites)),
        )

    def get_actor(self, actor_id):
        row = self.conn.execute("SELECT * FROM ledger_actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("actor_not_found", "人员不存在: %s" % actor_id)
        return Actor(row["actor_id"], Role(row["role"]), json.loads(row["satellites"]))

    def upsert_satellite(self, satellite):
        self.conn.execute(
            "INSERT INTO ledger_satellites(satellite_id,fuel_capacity,fuel_consumed) VALUES(?,?,?) "
            "ON CONFLICT(satellite_id) DO UPDATE SET fuel_capacity=excluded.fuel_capacity",
            (satellite.satellite_id, satellite.fuel_capacity, satellite.fuel_consumed),
        )

    def get_satellite(self, satellite_id):
        row = self.conn.execute("SELECT * FROM ledger_satellites WHERE satellite_id=?", (satellite_id,)).fetchone()
        if row is None:
            raise NotFoundError("satellite_not_found", "卫星不存在: %s" % satellite_id)
        return Satellite(row["satellite_id"], row["fuel_capacity"], row["fuel_consumed"])

    # ---------- conjunction ----------
    def insert_conjunction(self, conjunction):
        try:
            self.conn.execute(
                "INSERT INTO ledger_conjunctions(conjunction_id,satellite_id,tca,risk_level,risk_score,revision,revision_history) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    conjunction.conjunction_id,
                    conjunction.satellite_id,
                    conjunction.tca,
                    conjunction.risk_level.value,
                    conjunction.risk_score,
                    conjunction.revision,
                    canonical_json(conjunction.revision_history),
                ),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("duplicate_conjunction", "接近事件已存在: %s" % conjunction.conjunction_id)

    def update_conjunction(self, conjunction):
        self.conn.execute(
            "UPDATE ledger_conjunctions SET tca=?,risk_level=?,risk_score=?,revision=?,revision_history=? WHERE conjunction_id=?",
            (
                conjunction.tca,
                conjunction.risk_level.value,
                conjunction.risk_score,
                conjunction.revision,
                canonical_json(conjunction.revision_history),
                conjunction.conjunction_id,
            ),
        )

    def get_conjunction(self, conjunction_id):
        row = self.conn.execute("SELECT * FROM ledger_conjunctions WHERE conjunction_id=?", (conjunction_id,)).fetchone()
        if row is None:
            raise NotFoundError("conjunction_not_found", "接近事件不存在: %s" % conjunction_id)
        return self._conjunction_from_row(row)

    def list_conjunctions(self):
        return [self._conjunction_from_row(row)
                for row in self.conn.execute("SELECT * FROM ledger_conjunctions ORDER BY tca")]

    # ---------- plan ----------
    def insert_plan(self, plan):
        try:
            self.conn.execute(
                "INSERT INTO ledger_plans(plan_id,conjunction_id,satellite_id,proposed_by,fuel_required,risk_level,tca,"
                "based_on_revision,maneuver_window,state,version,approved_by,reject_reason,command_ref,executed_at,reservation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.plan_id, plan.conjunction_id, plan.satellite_id, plan.proposed_by, plan.fuel_required,
                    plan.risk_level.value, plan.tca, plan.based_on_revision, plan.maneuver_window,
                    plan.state.value, plan.version, plan.approved_by, plan.reject_reason,
                    plan.command_ref, plan.executed_at, plan.reservation,
                ),
            )
        except sqlite3.IntegrityError:
            raise ConflictError("duplicate_plan", "方案号已存在: %s" % plan.plan_id)

    def update_plan(self, plan):
        self.conn.execute(
            "UPDATE ledger_plans SET state=?,version=?,approved_by=?,reject_reason=?,command_ref=?,executed_at=?,"
            "reservation=?,fuel_required=?,risk_level=?,tca=?,based_on_revision=?,maneuver_window=? WHERE plan_id=?",
            (
                plan.state.value, plan.version, plan.approved_by, plan.reject_reason, plan.command_ref,
                plan.executed_at, plan.reservation, plan.fuel_required, plan.risk_level.value, plan.tca,
                plan.based_on_revision, plan.maneuver_window, plan.plan_id,
            ),
        )

    def get_plan_row(self, plan_id):
        row = self.conn.execute("SELECT * FROM ledger_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("plan_not_found", "规避方案不存在: %s" % plan_id)
        return row

    def get_plan(self, plan_id):
        return self._plan_from_row(self.get_plan_row(plan_id))

    def list_plans(self, satellite_id=None, state=None):
        sql = "SELECT * FROM ledger_plans"
        clauses, params = [], []
        if satellite_id:
            clauses.append("satellite_id=?")
            params.append(satellite_id)
        if state:
            clauses.append("state=?")
            params.append(state.value if isinstance(state, PlanState) else state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY tca, plan_id"
        return [self._plan_from_row(row) for row in self.conn.execute(sql, params)]

    # ---------- 燃料 ----------
    def add_fuel_movement(self, satellite_id, plan_id, kind, delta, reason):
        self.conn.execute(
            "INSERT INTO ledger_fuel_movements(satellite_id,plan_id,kind,delta,reason,created_at) VALUES(?,?,?,?,?,?)",
            (satellite_id, plan_id, kind, delta, reason, now_iso()),
        )

    def fuel_movements(self, satellite_id=None):
        if satellite_id:
            rows = self.conn.execute(
                "SELECT * FROM ledger_fuel_movements WHERE satellite_id=? ORDER BY id", (satellite_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ledger_fuel_movements ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def consume_fuel(self, satellite_id, amount):
        self.conn.execute(
            "UPDATE ledger_satellites SET fuel_consumed = fuel_consumed + ? WHERE satellite_id=?",
            (amount, satellite_id),
        )

    # ---------- 审计 ----------
    def append_audit(self, event_type, actor, role, result, payload):
        row = self.conn.execute("SELECT event_hash FROM ledger_audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["event_hash"] if row else "GENESIS"
        event = {
            "event_type": event_type,
            "actor": actor,
            "role": role.value if hasattr(role, "value") else role,
            "result": result,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        self.conn.execute(
            "INSERT INTO ledger_audit_events(event_type,actor,role,result,payload,previous_hash,event_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                event["event_type"], event["actor"], event["role"], result, canonical_json(payload),
                previous, event_hash, event["created_at"],
            ),
        )
        return {"event_type": event["event_type"], "result": result, **event}

    def audit_trail(self):
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "role": row["role"],
                "result": row["result"],
                "payload": json.loads(row["payload"]),
                "previous_hash": row["previous_hash"],
                "event_hash": row["event_hash"],
                "created_at": row["created_at"],
            }
            for row in self.conn.execute("SELECT * FROM ledger_audit_events ORDER BY id")
        ]

    def verify_audit_chain(self):
        previous = "GENESIS"
        for entry in self.audit_trail():
            event = {
                "event_type": entry["event_type"],
                "actor": entry["actor"],
                "role": entry["role"],
                "result": entry["result"],
                "payload": entry["payload"],
                "created_at": entry["created_at"],
            }
            if entry["previous_hash"] != previous:
                raise DomainError("audit_chain_broken", "审计链在事件 %s 处断裂" % entry["id"])
            if audit_hash(previous, event) != entry["event_hash"]:
                raise DomainError("audit_chain_broken", "审计哈希在事件 %s 处不匹配" % entry["id"])
            previous = entry["event_hash"]
        return True

    # ---------- 事务包裹 ----------
    def transaction(self):
        return _Transaction(self)

    def record_denied(self, event_type, actor, role, reason, payload=None):
        """被拒绝的变更不产生业务痕迹，但必须独立提交一条 denied 审计。"""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                entry = self.append_audit(event_type, actor, role, "denied",
                                          {"reason": reason, **(payload or {})})
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return entry


class _Transaction:
    """with 块：全局串行锁 + 整笔提交/回滚。"""

    def __init__(self, repo):
        self.repo = repo

    def __enter__(self):
        self.repo.lock.acquire()
        self.repo.conn.execute("BEGIN IMMEDIATE")
        return self.repo

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.repo.conn.commit()
        else:
            self.repo.conn.rollback()
        self.repo.lock.release()
        return False
