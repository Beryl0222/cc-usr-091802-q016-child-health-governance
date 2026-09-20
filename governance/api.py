"""HTTP API：仅标准库依赖。

鉴权：``Authorization: Bearer <token>``。监护人令牌绑定单一 person_id，
只能访问自己孩子的数据；产品令牌只能看群体质量统计；规则撤回等治理
操作需要 admin 令牌。

注意：``POST /v1/bootstrap-token`` 仅用于演示/测试环境的引导发证，
生产环境必须替换为带身份核验的监护关系认证流程。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .app import Application
from .service import (
    AuthenticationError,
    AuthorizationError,
    GovernanceError,
)

MAX_BODY = 64 * 1024


class ApiHandler(BaseHTTPRequestHandler):
    app: Application = None  # 由 make_server 注入到类上

    # ---------- 基础收发 ----------
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise GovernanceError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GovernanceError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise GovernanceError("请求体必须是 JSON 对象")
        return data

    def _principal(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise AuthenticationError("缺少 Bearer 令牌")
        return self.app.service.authenticate(auth[7:].strip())

    def _require_role(self, principal, *roles: str) -> None:
        if principal["role"] not in roles:
            raise AuthorizationError(f"该操作需要角色: {', '.join(roles)}")

    def _require_owner(self, principal, person_id: str) -> None:
        self._require_role(principal, "guardian", "admin")
        if principal["role"] == "guardian" and principal["person_id"] != person_id:
            raise AuthorizationError("监护人令牌只能访问本人绑定儿童的数据")

    def log_message(self, *_args):
        return

    # ---------- 路由 ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return self._send(200, {
                    "status": "ok",
                    "service": "child-health-governance",
                    "name": "儿童健康结论治理",
                })
            if method == "GET" and path == "/v1/rules":
                principal = self._principal()
                self._require_role(principal, "product", "guardian", "admin")
                return self._list_rules()
            if method == "GET" and path == "/v1/quality":
                principal = self._principal()
                self._require_role(principal, "product", "admin")
                dims = query.get("group_by", ["model_id,verdict"])[0].split(",")
                dims = [d.strip() for d in dims if d.strip()]
                return self._send(200, self.app.quality_report(principal, dims))

            if path.startswith("/v1/records/"):
                rest = path[len("/v1/records/"):]
                parts = rest.split("/")
                if method == "GET" and len(parts) == 1:
                    return self._get_record(parts[0])
                if method == "POST" and len(parts) == 2 and parts[1] == "correct-units":
                    return self._correct_units(parts[0])
            if path.startswith("/v1/persons/"):
                rest = path[len("/v1/persons/"):]
                parts = rest.split("/")
                if method == "GET" and len(parts) == 2 and parts[1] == "notifications":
                    return self._list_notifications(parts[0])
                if method == "POST" and len(parts) == 2 and parts[1] == "correct-birth-date":
                    return self._correct_birth_date(parts[0])

            if method == "POST":
                if path == "/v1/bootstrap-token":
                    return self._bootstrap_token()
                if path == "/v1/persons":
                    return self._register_person()
                if path == "/v1/measurements":
                    return self._submit_measurement()
                if path == "/v1/canary-consent":
                    return self._set_canary()
                if path.startswith("/v1/rules/") and path.endswith("/withdraw"):
                    rule_id = path[len("/v1/rules/"):-len("/withdraw")]
                    return self._withdraw_rule(rule_id)

            self._send(404, {"error": "not_found", "path": path})
        except AuthenticationError as exc:
            self._send(401, {"error": "unauthenticated", "message": str(exc)})
        except AuthorizationError as exc:
            self._send(403, {"error": "forbidden", "message": str(exc)})
        except GovernanceError as exc:
            self._send(400, {"error": "rejected", "message": str(exc)})
        except Exception:  # 防御性兜底：不外泄堆栈
            self._send(500, {"error": "internal", "message": "服务内部错误，请联系治理负责人"})
            raise

    # ---------- 处理函数 ----------
    def _bootstrap_token(self):
        data = self._read_json()
        role = data.get("role")
        person_id = data.get("person_id")
        token = self.app.service.issue_token(role, person_id)
        self.app.log_audit("bootstrap", "token_issued", {"role": role, "person_id": person_id})
        self._send(201, {"token": token, "role": role, "person_id": person_id,
                         "warning": "引导发证接口，仅限演示/测试环境使用"})

    def _register_person(self):
        principal = self._principal()
        self._require_role(principal, "guardian", "admin")
        data = self._read_json()
        person = self.app.service.register_person(
            person_id=data["person_id"],
            birth_date=data["birth_date"],
            sex=data["sex"],
        )
        self.app.log_audit(principal["role"], "person_registered",
                           {"person_id": person.person_id})
        self.app.save()
        self._send(201, {"person_id": person.person_id, "birth_date": person.birth_date})

    def _submit_measurement(self):
        principal = self._principal()
        data = self._read_json()
        self._require_owner(principal, data.get("person_id", ""))
        c = self.app.submit_measurement(principal, data)
        self._send(201, {"conclusion": c.as_dict()})

    def _set_canary(self):
        principal = self._principal()
        data = self._read_json()
        person_id = data.get("person_id", "")
        self._require_owner(principal, person_id)
        enable = bool(data.get("enable"))
        if data.get("confirm") is not True:
            raise GovernanceError("灰度同意必须携带 confirm=true，表示监护人已阅读并明确授权")
        result = self.app.set_canary_consent(principal, person_id, enable)
        self._send(200, {
            "canary_enabled": result["state"].canary_enabled,
            "granted_at": result["state"].canary_granted_at,
            "revoked_at": result["state"].canary_revoked_at,
            "reprocessed_records": [c.record_id for c in result["reprocessed"]],
            "new_versions": [
                {"record_id": c.record_id, "version": c.version,
                 "rule_id": c.rule_id}
                for c in result["reprocessed"]
            ],
        })

    def _correct_units(self, record_id: str):
        principal = self._principal()
        # 先校验角色，再查资源，避免向无权方泄露记录是否存在
        self._require_role(principal, "guardian", "admin")
        record = self.app.service.records.get(record_id)
        if record is None:
            raise GovernanceError("记录不存在")
        self._require_owner(principal, record.person_id)
        data = self._read_json()
        c = self.app.correct_units(principal, record_id, data["readings"])
        self._send(200, {"conclusion": c.as_dict()})

    def _correct_birth_date(self, person_id: str):
        principal = self._principal()
        self._require_owner(principal, person_id)
        data = self._read_json()
        affected = self.app.correct_birth_date(
            principal, person_id, data["birth_date"]
        )
        self._send(200, {
            "person_id": person_id,
            "birth_date": data["birth_date"],
            "revised": [
                {"record_id": c.record_id, "version": c.version,
                 "verdict": c.verdict, "rule_id": c.rule_id}
                for c in affected
            ],
        })

    def _get_record(self, record_id: str):
        principal = self._principal()
        self._require_role(principal, "guardian", "admin")
        record = self.app.service.records.get(record_id)
        if record is None:
            raise GovernanceError("记录不存在")
        self._require_owner(principal, record.person_id)
        self._send(200, self.app.service.parent_record_view(record_id))

    def _list_notifications(self, person_id: str):
        principal = self._principal()
        self._require_owner(principal, person_id)
        self._send(200, {"notifications": self.app.service.list_notifications(person_id)})

    def _withdraw_rule(self, rule_id: str):
        principal = self._principal()
        self._require_role(principal, "admin")
        data = self._read_json()
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise GovernanceError("撤回规则必须填写原因")
        affected = self.app.withdraw_rule(principal, rule_id, reason)
        self._send(200, {
            "rule_id": rule_id,
            "withdrawn": True,
            "affected_conclusions": [
                {"record_id": c.record_id, "person_id": c.person_id,
                 "version": c.version, "verdict": c.verdict}
                for c in affected
            ],
            "family_notifications_queued": len(affected),
        })

    def _list_rules(self):
        book = self.app.rulebook
        out = []
        for rule_id in sorted(book._rules):
            r = book.get(rule_id)
            now = self.app.service.now_dt()
            out.append({
                "rule_id": r.rule_id,
                "version": r.version,
                "indicator": r.indicator,
                "canary": r.canary,
                "age_bands": sorted(r.age_bands),
                "sex_groups": sorted(r.sex_groups),
                "applies_to_models": sorted(r.applies_to_models),
                "effective_from": r.effective_from,
                "effective_to": r.effective_to,
                "active_at_present": r.active_at(now) and not book.is_withdrawn(r.rule_id, now),
                "withdrawn": book.is_withdrawn(r.rule_id),
                "fingerprint": r.fingerprint,
                "approvals": sorted(a.role for a in r.approvals),
            })
        self._send(200, {"rules": out})


def make_server(host: str, port: int, app: Application) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)
