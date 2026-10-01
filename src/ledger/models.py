"""调度账的领域类型、角色与风险规则（纯函数，不依赖存储）。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


class RiskLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self):
        # 数值越小优先级越高
        return {"high": 0, "medium": 1, "low": 2}[self.value]


class PlanState(str, Enum):
    DRAFT = "draft"                # 已提交，待批准
    APPROVED = "approved"          # 已批准，在排队账上占用燃料（或因余量不足被挂起）
    QUEUED = "queued"              # 已批准且已在账上排到燃料
    REJECTED = "rejected"          # 批准被拒（余量不足等），附带原因
    STALE = "stale"                # 依赖的轨道数据过期，批准与占用失效，待重评
    EXECUTED = "executed"          # 指令已执行，记录永久保留
    CANCELLED = "cancelled"


# 角色与权限
class Role(str, Enum):
    ANALYST = "analyst"            # 轨道分析员：录入接近事件、轨道修订
    DUTY_OFFICER = "duty_officer"  # 值班员：批准规避方案
    DISPATCHER = "dispatcher"      # 调度员：授权范围内代办提交/执行
    ADMIN = "admin"                # 注册卫星/人员等台账维护

    @classmethod
    def parse(cls, value):
        try:
            return cls(value)
        except ValueError:
            raise DomainError("unknown_role", "未知角色: %s" % value)


# 每个动作允许的角色
ACTION_ROLES = {
    "register_satellite": {Role.ADMIN},
    "register_actor": {Role.ADMIN},
    "record_conjunction": {Role.ANALYST, Role.DUTY_OFFICER, Role.DISPATCHER},
    "revise_orbit": {Role.ANALYST},
    "submit_plan": {Role.DUTY_OFFICER, Role.DISPATCHER},
    "approve_plan": {Role.DUTY_OFFICER, Role.DISPATCHER},
    "execute_plan": {Role.DUTY_OFFICER, Role.DISPATCHER},
    "cancel_plan": {Role.DUTY_OFFICER, Role.DISPATCHER},
}

WILDCARD = "*"


@dataclass
class Actor:
    actor_id: str
    role: Role
    # 可操作的卫星范围；WILDCARD 表示全部
    satellites: list = field(default_factory=lambda: [WILDCARD])

    def can_touch(self, satellite_id):
        return WILDCARD in self.satellites or satellite_id in self.satellites


@dataclass
class Satellite:
    satellite_id: str
    fuel_capacity: float
    # 已被执行指令真实消耗的燃料
    fuel_consumed: float = 0.0

    @property
    def available(self):
        return round(self.fuel_capacity - self.fuel_consumed, 9)


@dataclass
class Conjunction:
    conjunction_id: str
    satellite_id: str
    tca: str                     # 交会时刻 ISO
    risk_level: RiskLevel
    risk_score: float
    revision: int = 1
    # 每个修订版本的留痕：{revision, miss_distance_m, covariance_m, observed_at, risk_level}
    revision_history: list = field(default_factory=list)


@dataclass
class Plan:
    plan_id: str
    conjunction_id: str
    satellite_id: str
    proposed_by: str
    fuel_required: float
    risk_level: RiskLevel
    tca: str
    based_on_revision: int
    maneuver_window: str = ""
    state: PlanState = PlanState.DRAFT
    version: int = 1             # 乐观锁版本
    approved_by: str = ""
    reject_reason: str = ""
    command_ref: str = ""
    executed_at: str = ""
    # 排队结果：queued / deferred（余量不足挂起）
    reservation: str = ""

    def is_live(self):
        """是否仍是未执行、可参与重算的活动方案。"""
        return self.state in (PlanState.APPROVED, PlanState.QUEUED)


def parse_iso(value):
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise DomainError("invalid_timestamp", "时间必须是 ISO 格式: %s" % value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def assess_risk(miss_distance_m, covariance_m, hours_to_tca=None):
    """由脱靶量/协方差（和距交会时间）评定风险等级。演示模型，可替换为真实碰撞概率。"""
    if covariance_m <= 0:
        raise DomainError("invalid_covariance", "协方差必须大于零")
    ratio = float(miss_distance_m) / float(covariance_m)
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    score = severity
    if hours_to_tca is not None:
        urgency = max(0.0, min(20.0, (24.0 - float(hours_to_tca)) * 0.8))
        score += urgency
    score = round(min(100.0, score), 2)
    if score >= 80:
        level = RiskLevel.HIGH
    elif score >= 50:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW
    return {"score": score, "level": level, "ratio": round(ratio, 3)}


def queue_order(plan):
    """排队顺序：风险等级高者优先，同级交会时刻早者优先，再以方案号兜底保序。"""
    return (plan.risk_level.rank, parse_iso(plan.tca), plan.plan_id)
