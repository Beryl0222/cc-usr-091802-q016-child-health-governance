"""儿童健康结论治理的服务入口。

GET  /health                                   服务身份
POST /v1/rules                                 登记规则版本（三方批准信息随附）
POST /v1/rules/withdraw                        撤回规则版本 -> 重估受影响结论并通知家庭
POST /v1/consents                              监护人授予/退出灰度同意
POST /v1/events                                上报测量（含离线补传）-> 评估并生成结论
POST /v1/corrections/unit                      单位修正 -> 新结论版本
POST /v1/corrections/birthday                  生日修正 -> 新结论版本
GET  /v1/children/{cid}/records/{event_id}     家长视图（来源/范围/理由/修订经过）
GET  /v1/children/{cid}/notifications          家庭通知
GET  /v1/analytics/quality?dims=device_model   群体质量统计（最小群体门槛保护）
"""

import argparse
import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from governance import (
    MeasurementEvent,
    Store,
    apply_birthday_correction,
    apply_unit_correction,
    approval_issues,
    assert_family_safe,
    ingest_event,
    now_utc,
    parent_view,
    parse_dt,
    quality_stats,
    rule_from_dict,
    set_gray_consent,
    withdraw_rule,
)
from governance.pipeline import ConflictError, NotFoundError

SERVICE_ID = "child-health-governance"
SERVICE_NAME = "儿童健康结论治理"

BASE_DIR = Path(__file__).resolve().parent


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def load_seed(store: Store) -> None:
    """加载演示用规则与设备校准登记（均为可公开样例数据）。"""
    rules = json.loads((BASE_DIR / "fixtures/rules.json").read_text(encoding="utf-8"))
    for item in rules["rules"]:
        store.add_rule(rule_from_dict(item))
    devices = json.loads((BASE_DIR / "fixtures/devices.json").read_text(encoding="utf-8"))
    for device in devices["devices"]:
        store.devices[device["model"]] = device


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _require(payload: dict, *fields: str) -> None:
    missing = [field for field in fields if payload.get(field) in (None, "")]
    if missing:
        raise ApiError(400, "缺少必填字段: " + ", ".join(missing))


# ---------------------------------------------------------------- 各接口实现


def api_create_rule(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "rule_id", "version", "metric", "valid_from", "reference", "bands_by_age")
    rule = rule_from_dict(payload)
    try:
        store.add_rule(rule)
    except ValueError as exc:
        raise ApiError(409, str(exc))
    issues = approval_issues(rule)
    return 201, {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "usable": not issues,
        "issues": issues,
    }


def api_withdraw_rule(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "rule_id", "version")
    changed = withdraw_rule(store, payload["rule_id"], int(payload["version"]), now=now_utc())
    return 200, {
        "withdrawn": True,
        "affected_children": len(changed),
        "affected_events": sum(len(ids) for ids in changed.values()),
    }


def api_consent(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "child_id", "guardian_id", "action")
    try:
        changed = set_gray_consent(
            store,
            payload["child_id"],
            guardian_id=payload["guardian_id"],
            action=payload["action"],
            now=now_utc(),
        )
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return 200, {"status": payload["action"], "changed_events": changed}


def api_ingest_event(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "event_id", "child_id", "device_model", "measured_at", "posture")
    if payload["event_id"] in store.events:
        raise ApiError(409, f"测量事件已存在: {payload['event_id']}")
    height = payload.get("height") or {}
    weight = payload.get("weight") or {}
    event = MeasurementEvent(
        event_id=payload["event_id"],
        child_id=payload["child_id"],
        device_model=payload["device_model"],
        measured_at=parse_dt(payload["measured_at"]),
        received_at=parse_dt(payload["received_at"]) if payload.get("received_at") else now_utc(),
        posture=payload["posture"],
        height_value=height.get("value"),
        height_unit=height.get("unit"),
        weight_value=weight.get("value"),
        weight_unit=weight.get("unit"),
        reported_age_band=payload.get("reported_age_band"),
    )
    if payload.get("birth_date"):
        child = store.ensure_child(event.child_id)
        child.birth_date = date.fromisoformat(payload["birth_date"])
    try:
        ingest_event(store, event, now=now_utc())
    except ConflictError as exc:
        raise ApiError(409, str(exc))
    return 201, parent_view(store, event.event_id)


def api_unit_correction(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "event_id")
    if not payload.get("height_unit") and not payload.get("weight_unit"):
        raise ApiError(400, "至少提供 height_unit 或 weight_unit")
    try:
        conclusion = apply_unit_correction(
            store,
            payload["event_id"],
            height_unit=payload.get("height_unit"),
            weight_unit=payload.get("weight_unit"),
            now=now_utc(),
        )
    except NotFoundError:
        raise ApiError(404, "测量事件不存在")
    return 200, {
        "updated": conclusion is not None,
        "record": parent_view(store, payload["event_id"]),
    }


def api_birthday_correction(store: Store, payload: dict) -> tuple[int, dict]:
    _require(payload, "child_id", "birth_date")
    changed = apply_birthday_correction(
        store,
        payload["child_id"],
        birth_date=date.fromisoformat(payload["birth_date"]),
        now=now_utc(),
    )
    return 200, {"updated_events": changed, "notified": bool(changed)}


def api_analytics(store: Store, query: dict) -> tuple[int, dict]:
    for forbidden in ("child_id", "event_id", "guardian_id", "family_id"):
        if forbidden in query:
            raise ApiError(400, "不允许按个体维度查询")
    dims = query.get("dims", ["device_model"])[0]
    try:
        stats = quality_stats(store, [dim for dim in dims.split(",") if dim])
    except ValueError as exc:
        raise ApiError(400, str(exc))
    return 200, stats


# ---------------------------------------------------------------- HTTP 接线

POST_ROUTES = {
    "/v1/rules": api_create_rule,
    "/v1/rules/withdraw": api_withdraw_rule,
    "/v1/consents": api_consent,
    "/v1/events": api_ingest_event,
    "/v1/corrections/unit": api_unit_correction,
    "/v1/corrections/birthday": api_birthday_correction,
}

RECORD_RE = re.compile(r"^/v1/children/([^/]+)/records/([^/]+)$")
NOTIFICATIONS_RE = re.compile(r"^/v1/children/([^/]+)/notifications$")


def make_handler(store: Store):
    class GovernanceHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "请求体不是合法 JSON")
            if not isinstance(payload, dict):
                raise ApiError(400, "请求体必须是 JSON 对象")
            return payload

        def _dispatch(self, handler):
            try:
                status, obj = handler()
            except ApiError as exc:
                status, obj = exc.status, {"error": exc.message}
            except NotFoundError as exc:
                status, obj = 404, {"error": f"对象不存在: {exc}"}
            except ValueError as exc:
                status, obj = 400, {"error": str(exc)}
            self._send(status, obj)

        def do_GET(self):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send(200, health_payload())
                return
            if path == "/v1/analytics/quality":
                self._dispatch(lambda: api_analytics(store, query))
                return
            match = RECORD_RE.match(path)
            if match:
                child_id, event_id = match.groups()

                def handle():
                    view = parent_view(store, event_id)
                    if view is None or view["child_id"] != child_id:
                        raise ApiError(404, "记录不存在")
                    return 200, view

                self._dispatch(handle)
                return
            match = NOTIFICATIONS_RE.match(path)
            if match:
                child_id = match.group(1)

                def handle():
                    notes = [
                        {
                            "notification_id": n.notification_id,
                            "kind": n.kind,
                            "message": n.message,
                            "event_ids": n.event_ids,
                            "created_at": n.created_at.isoformat(),
                        }
                        for n in store.notifications.get(child_id, [])
                    ]
                    return 200, {"notifications": notes}

                self._dispatch(handle)
                return
            self._send(404, {"error": "未知路径"})

        def do_POST(self):
            path = urlparse(self.path).path
            handler = POST_ROUTES.get(path)
            if handler is None:
                self._send(404, {"error": "未知路径"})
                return
            self._dispatch(lambda: handler(store, self._read_json()))

        def log_message(self, *_args):
            return

    return GovernanceHandler


def create_server(port: int, store: Store | None = None) -> ThreadingHTTPServer:
    if store is None:
        store = Store()
        load_seed(store)
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(store))


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        store = Store()
        load_seed(store)
        assert store.rules, "种子规则加载失败"
        assert_family_safe("自检文案")
        print("基础检查通过")
        return
    create_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
