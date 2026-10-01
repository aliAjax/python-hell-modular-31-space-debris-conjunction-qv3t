from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    # ------------------------------------------------------------------
    # 身份与授权
    # ------------------------------------------------------------------

    def _require_identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def _deny(self, item_id, action, actor, role, reason):
        # 越权变更直接拒绝并留下审计记录（不推进业务版本）
        self.repository.record_denial(item_id, action, actor, role, reason)

    # ------------------------------------------------------------------
    # 接近事件
    # ------------------------------------------------------------------

    def create_item(self, payload, actor, role, region=None):
        self._require_identity(actor, role)
        if role not in rules.CREATE_ROLES:
            self._deny(None, "create_item", actor, role, "role_not_allowed")
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_conjunction(stable_key, normalized, actor, role)

    def add_source(self, item_id, payload, actor, role, region=None):
        self._require_identity(actor, role)
        if role not in rules.SOURCE_ROLES:
            self._deny(item_id, "add_source", actor, role, "role_not_allowed")
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            self._deny(item_id, "add_source", actor, role, "region_mismatch")
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    # ------------------------------------------------------------------
    # 规避方案调度（批准 / 执行 / 轨道修订走事务性台账路径）
    # ------------------------------------------------------------------

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        self._require_identity(actor, role)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            self._deny(item_id, action, actor, role, "role_not_allowed")
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                self._deny(item_id, action, actor, role, "region_mismatch")
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        if action == "approve":
            self.repository.approve_plan(item_id, payload, actor, role, expected_version)
            return self.get_item(item_id)
        if action == "execute":
            self.repository.execute_plan(item_id, payload, actor, role, expected_version)
            return self.get_item(item_id)
        if action == "report_revision":
            self.repository.revise_track(item_id, payload, actor, role, expected_version)
            return self.get_item(item_id)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    # ------------------------------------------------------------------
    # 卫星燃料台账
    # ------------------------------------------------------------------

    def create_satellite(self, payload, actor, role):
        self._require_identity(actor, role)
        if role not in rules.SATELLITE_ROLES:
            self._deny(None, "create_satellite", actor, role, "role_not_allowed")
            raise DomainError("forbidden", "当前角色不能登记卫星燃料台账", 403)
        satellite_id = domain.require_text(payload, "satellite_id")
        fuel_budget = domain.number(payload, "fuel_budget_m_s", 0)
        return self.repository.create_satellite(satellite_id, fuel_budget, actor, role)

    def list_satellites(self):
        result = []
        for sat in self.repository.list_satellites():
            balance = self.repository.fuel_balance(sat["payload"]["satellite_id"])
            result.append({"satellite": sat, "fuel": balance})
        return result

    # ------------------------------------------------------------------
    # 调度账：排队 / 已执行 / 已作废 / 未排 + 卫星燃料
    # ------------------------------------------------------------------

    def schedule(self):
        plans = self.repository.list_plans()
        queue = rules.order_queue([p for p in plans if p["status"] == "queued"])
        executed = [p for p in plans if p["status"] == "executed"]
        voided = [p for p in plans if p["status"] == "voided"]
        rejected = [p for p in plans if p["status"] == "rejected"]
        return {
            "queue": queue,
            "executed": executed,
            "voided": voided,
            "rejected": rejected,
            "satellites": self.list_satellites(),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        if item["entity_type"] == rules.ENTITY_TYPE:
            item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
