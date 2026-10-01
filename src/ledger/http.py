"""调度账的 HTTP 路由：挂在 /api/ledger 之下，错误处理与主服务隔离。

用法：router = LedgerRouter(ledger)；在主 handler 的 do_GET/do_POST 里先调
router.dispatch(self, method, path)，命中则返回 True 并已写回响应，否则返回 False。
"""

import json
from urllib.parse import urlparse, parse_qs

from .models import DomainError as LedgerDomainError


def _require_actor(handler):
    actor = handler.headers.get("X-User-Id", "").strip()
    if not actor:
        raise LedgerDomainError("identity_required", "需要 X-User-Id 身份头", 401)
    return actor


def _expected(body):
    value = body.get("expected_version")
    if value is None:
        raise LedgerDomainError("expected_version_required", "需要 expected_version", 400)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LedgerDomainError("invalid_version", "expected_version 必须是整数")


class LedgerRouter:
    def __init__(self, ledger):
        self.ledger = ledger

    # ---- 响应 ----
    @staticmethod
    def _send(handler, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    @staticmethod
    def _body(handler):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        try:
            return json.loads(handler.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise LedgerDomainError("invalid_json", "请求体不是有效 JSON", 400)

    # ---- 分派 ----
    def dispatch(self, handler, method, path):
        parsed = urlparse(path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        if len(parts) < 2 or parts[:2] != ["api", "ledger"]:
            return False
        rest = parts[2:]
        ledger = self.ledger
        try:
            if method == "GET":
                return self._get(handler, rest, query)
            if method == "POST":
                return self._post(handler, rest)
            self._send(handler, 405, {"error": "method_not_allowed", "message": "不支持的方法"})
            return True
        except LedgerDomainError as exc:
            self._send(handler, getattr(exc, "status", 500),
                       {"error": exc.code, "message": str(exc)})
            return True
        except KeyError as exc:
            self._send(handler, 400, {"error": "field_required",
                                      "message": "缺少字段: %s" % exc.args[0]})
            return True
        except (TypeError, ValueError) as exc:
            self._send(handler, 400, {"error": "invalid_request", "message": str(exc)})
            return True

    # ---- GET ----
    def _get(self, handler, rest, query):
        ledger = self.ledger
        if rest == ["book"]:
            self._send(handler, 200,
                       ledger.schedule_book(query.get("satellite_id", [None])[0]))
            return True
        if len(rest) == 2 and rest[0] == "fuel":
            self._send(handler, 200, ledger.fuel_ledger(rest[1]))
            return True
        if len(rest) == 2 and rest[0] == "conjunctions":
            self._send(handler, 200, ledger.revision_report(rest[1]))
            return True
        if rest == ["conjunctions"]:
            self._send(handler, 200,
                       {"conjunctions": [ledger._conjunction_view(c)
                                         for c in ledger.repository.list_conjunctions()]})
            return True
        if rest == ["audit"]:
            self._send(handler, 200, {"events": ledger.audit_trail()})
            return True
        if rest == ["audit", "verify"]:
            self._send(handler, 200, {"valid": ledger.verify_audit_chain()})
            return True
        self._send(handler, 404, {"error": "not_found", "message": "接口不存在"})
        return True

    # ---- POST ----
    def _post(self, handler, rest):
        ledger = self.ledger
        body = self._body(handler)
        actor = handler.headers.get("X-User-Id", "").strip()

        if rest == ["actors"]:
            self._send(handler, 201, ledger.register_actor(
                body["actor_id"], body.get("role", "dispatcher"),
                body.get("satellites"), by=actor or "bootstrap"))
            return True
        if rest == ["satellites"]:
            self._send(handler, 201, ledger.register_satellite(
                body["satellite_id"], float(body["fuel_capacity"]), _require_actor(handler)))
            return True
        if rest == ["conjunctions"]:
            self._send(handler, 201, ledger.record_conjunction(
                body["satellite_id"], body["tca"],
                float(body["miss_distance_m"]), float(body["covariance_m"]),
                _require_actor(handler), conjunction_id=body.get("conjunction_id"),
                now=body.get("now")))
            return True
        if len(rest) == 3 and rest[0] == "conjunctions" and rest[2] == "revisions":
            self._send(handler, 200, ledger.revise_orbit(
                rest[1], float(body["miss_distance_m"]), float(body["covariance_m"]),
                _require_actor(handler), observed_at=body.get("observed_at"),
                new_tca=body.get("new_tca")))
            return True
        if rest == ["plans"]:
            self._send(handler, 201, ledger.submit_plan(
                body["conjunction_id"], float(body["fuel_required"]),
                body.get("maneuver_window", ""), _require_actor(handler),
                plan_id=body.get("plan_id"),
                expected_version=body.get("expected_version")))
            return True
        if len(rest) == 3 and rest[0] == "plans":
            pid, op = rest[1], rest[2]
            if op == "approve":
                result = ledger.approve_plan(pid, _require_actor(handler), _expected(body))
                status = 200
            elif op == "execute":
                result = ledger.execute_plan(pid, body["command_ref"],
                                             _require_actor(handler), _expected(body))
                status = 200
            elif op == "cancel":
                result = ledger.cancel_plan(pid, body.get("reason", ""),
                                            _require_actor(handler), _expected(body))
                status = 200
            elif op == "reevaluate":
                result = ledger.reevaluate_plan(pid, _require_actor(handler))
                status = 200
            else:
                self._send(handler, 404, {"error": "not_found", "message": "接口不存在"})
                return True
            self._send(handler, status, result)
            return True
        self._send(handler, 404, {"error": "not_found", "message": "接口不存在"})
        return True
