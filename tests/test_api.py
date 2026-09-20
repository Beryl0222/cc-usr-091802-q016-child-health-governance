"""HTTP API 契约：鉴权、角色边界、端到端流程、持久化。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from governance.api import make_server
from governance.app import Application


def fixed_now():
    return datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = str(Path(self.tmp.name) / "state.json")
        self.app = Application(clock=fixed_now, data_path=self.data_path)
        self.server: ThreadingHTTPServer = make_server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None, expect_error=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode())
            if expect_error:
                return e.code, payload
            raise AssertionError(f"{method} {path} -> {e.code}: {payload}")

    def token(self, role, person_id=None):
        _, resp = self.call("POST", "/v1/bootstrap-token",
                            {"role": role, "person_id": person_id})
        return resp["token"]


class HttpFlowTest(ApiTestBase):
    def _register(self, person_id="mei", birth="2013-03-10", sex="female"):
        # 演示引导流程：admin 建档 -> 发给绑定该儿童的监护人令牌
        admin = self.token("admin")
        self.call("POST", "/v1/persons",
                  {"person_id": person_id, "birth_date": birth, "sex": sex}, admin)
        return self.token("guardian", person_id)

    def test_health_anonymous(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "child-health-governance")

    def test_full_family_flow(self):
        token = self._register()
        status, resp = self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k1",
            "readings": [
                {"measurement": "height", "value": 167, "unit": "cm", "posture": "standing"},
                {"measurement": "weight", "value": 50.5, "unit": "kg", "posture": "standing"},
            ],
        }, token)
        self.assertEqual(status, 201)
        c = resp["conclusion"]
        self.assertEqual(c["verdict"], "within_range")
        self.assertAlmostEqual(c["indicator_value"], 18.1, places=1)

        record_id = c["record_id"]
        status, view = self.call("GET", f"/v1/records/{record_id}", token=token)
        self.assertEqual(status, 200)
        self.assertIn("data_source", view)
        self.assertIn("applicability", view)
        self.assertIn("revision_history", view)
        self.assertEqual(view["data_source"]["raw_readings"][0]["value"], 167)

        status, notes = self.call("GET", "/v1/persons/mei/notifications", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(notes["notifications"][0]["kind"], "first_result")

    def test_canary_consent_requires_confirm_flag(self):
        token = self._register()
        code, resp = self.call("POST", "/v1/canary-consent",
                               {"person_id": "mei", "enable": True}, token,
                               expect_error=True)
        self.assertEqual(code, 400)
        self.assertIn("confirm", resp["message"])

    def test_canary_grant_and_optout_over_http(self):
        token = self._register("mei", birth="2012-06-01")
        # 先产生一条记录
        self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k1", "record_id": "r1",
            "readings": [
                {"measurement": "height", "value": 1.60, "unit": "m", "posture": "standing"},
                {"measurement": "weight", "value": 62, "unit": "kg", "posture": "standing"},
            ],
        }, token)
        code, resp = self.call("POST", "/v1/canary-consent",
                               {"person_id": "mei", "enable": True, "confirm": True}, token)
        self.assertEqual(code, 200)
        self.assertTrue(resp["canary_enabled"])
        _, sub = self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k1", "record_id": "r2",
            "readings": [
                {"measurement": "height", "value": 1.60, "unit": "m", "posture": "standing"},
                {"measurement": "weight", "value": 62, "unit": "kg", "posture": "standing"},
            ],
        }, token)
        self.assertEqual(sub["conclusion"]["rule_id"], "bmi-ref-puberty-canary-v3")
        _, out = self.call("POST", "/v1/canary-consent",
                           {"person_id": "mei", "enable": False, "confirm": True}, token)
        self.assertEqual(out["reprocessed_records"], ["r2"])

    def test_unit_correction_over_http(self):
        token = self._register()
        _, sub = self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k2", "record_id": "r1",
            "readings": [
                {"measurement": "weight", "value": 111, "unit": "kg", "posture": "standing"},
                {"measurement": "height", "value": 1.60, "unit": "m", "posture": "standing"},
            ],
        }, token)
        self.assertEqual(sub["conclusion"]["verdict"], "needs_attention")
        code, resp = self.call("POST", "/v1/records/r1/correct-units", {
            "readings": [
                {"measurement": "weight", "value": 111, "unit": "lb", "posture": "standing"},
                {"measurement": "height", "value": 1.60, "unit": "m", "posture": "standing"},
            ],
        }, token)
        self.assertEqual(code, 200)
        self.assertEqual(resp["conclusion"]["version"], 2)
        self.assertEqual(resp["conclusion"]["verdict"], "within_range")

    def test_backfill_over_http(self):
        token = self._register()
        code, resp = self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k2", "backfill": True,
            "collected_at": "2026-09-01T08:00:00Z",
            "readings": [
                {"measurement": "height", "value": 1.5, "unit": "m", "posture": "standing",
                 "measured_at": "2026-09-01T08:00:00Z"},
                {"measurement": "weight", "value": 45, "unit": "kg", "posture": "standing",
                 "measured_at": "2026-09-01T08:00:00Z"},
            ],
        }, token)
        self.assertEqual(resp["conclusion"]["observed_at"], "2026-09-01T08:00:00Z")
        self.assertEqual(resp["conclusion"]["rule_id"], "bmi-ref-sexspecific-v2")

    def test_auth_required(self):
        code, _ = self.call("GET", "/v1/records/nope", expect_error=True)
        self.assertEqual(code, 401)

    def test_guardian_cannot_access_other_child(self):
        t1 = self._register("mei")
        t2 = self._register("ling", birth="2012-01-01", sex="male")
        self.call("POST", "/v1/measurements", {
            "person_id": "ling", "model_id": "watch-k1", "record_id": "secret",
            "readings": [
                {"measurement": "height", "value": 1.5, "unit": "m", "posture": "standing"},
                {"measurement": "weight", "value": 45, "unit": "kg", "posture": "standing"},
            ],
        }, t2)
        code, _ = self.call("GET", "/v1/records/secret", token=t1, expect_error=True)
        self.assertEqual(code, 403)

    def test_product_role_only_sees_aggregates(self):
        product = self.token("product")
        code, resp = self.call("GET", "/v1/quality?group_by=model_id,verdict",
                               token=product)
        self.assertEqual(code, 200)
        self.assertEqual(resp["groups"], [])  # 无数据
        # 产品令牌不能取单条儿童记录，也不能提交测量
        code, _ = self.call("GET", "/v1/records/x", token=product, expect_error=True)
        self.assertEqual(code, 403)
        code, _ = self.call("POST", "/v1/measurements",
                            {"person_id": "x"}, product, expect_error=True)
        self.assertEqual(code, 403)

    def test_rules_listing_shows_approvals(self):
        product = self.token("product")
        _, resp = self.call("GET", "/v1/rules", token=product)
        ids = {r["rule_id"]: r for r in resp["rules"]}
        self.assertIn("bmi-ref-sexspecific-v2", ids)
        self.assertEqual(
            sorted(ids["bmi-ref-sexspecific-v2"]["approvals"]),
            ["copy", "medical", "privacy"],
        )
        self.assertTrue(ids["bmi-ref-sexspecific-v2"]["fingerprint"].startswith("sha256:"))

    def test_admin_withdraw_flow_and_family_notification(self):
        guardian = self._register()
        admin = self.token("admin")
        self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k1", "record_id": "r1",
            "readings": [
                {"measurement": "height", "value": 167, "unit": "cm", "posture": "standing"},
                {"measurement": "weight", "value": 50.5, "unit": "kg", "posture": "standing"},
            ],
        }, guardian)
        # 监护人不能撤回规则
        code, _ = self.call("POST", "/v1/rules/bmi-ref-sexspecific-v2/withdraw",
                            {"reason": "x"}, guardian, expect_error=True)
        self.assertEqual(code, 403)
        code, resp = self.call("POST", "/v1/rules/bmi-ref-sexspecific-v2/withdraw",
                               {"reason": "阈值需复核"}, admin)
        self.assertEqual(code, 200)
        self.assertEqual(resp["family_notifications_queued"], 1)
        # 仅撤回 v2 时由 v1 兜底，通知类型为 revised
        _, notes = self.call("GET", "/v1/persons/mei/notifications", token=guardian)
        self.assertEqual(notes["notifications"][-1]["kind"], "revised")
        # 再撤回唯一兜底规则 v1，旧结果被 withhold
        self.call("POST", "/v1/rules/bmi-ref-sexneutral-v1/withdraw",
                  {"reason": "阈值需复核"}, admin)
        _, notes = self.call("GET", "/v1/persons/mei/notifications", token=guardian)
        self.assertEqual(notes["notifications"][-1]["kind"], "withheld")

    def test_state_persists_across_restart(self):
        guardian = self._register()
        self.call("POST", "/v1/measurements", {
            "person_id": "mei", "model_id": "watch-k1", "record_id": "persist-1",
            "readings": [
                {"measurement": "height", "value": 167, "unit": "cm", "posture": "standing"},
                {"measurement": "weight", "value": 50.5, "unit": "kg", "posture": "standing"},
            ],
        }, guardian)

        # 用同一数据文件启动新应用实例
        app2 = Application(clock=fixed_now, data_path=self.data_path)
        self.assertTrue(app2.load())
        server2 = make_server("127.0.0.1", 0, app2)
        port2 = server2.server_address[1]
        t = threading.Thread(target=server2.serve_forever, daemon=True)
        t.start()
        try:
            cur = app2.service.active_conclusion("persist-1")
            self.assertIsNotNone(cur)
            self.assertAlmostEqual(cur.indicator_value, 18.1, places=1)
            self.assertEqual(cur.rule_id, "bmi-ref-sexspecific-v2")
            # 原始读数与校准来源可还原
            view = app2.service.parent_record_view("persist-1")
            self.assertEqual(view["data_source"]["raw_readings"][0]["unit"], "cm")
            self.assertEqual(view["revision_history"][0]["status"], "active")
        finally:
            server2.shutdown()
            server2.server_close()


if __name__ == "__main__":
    unittest.main()
