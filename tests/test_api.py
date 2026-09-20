"""HTTP 接口端到端测试：以种子数据启动真实服务进程内实例。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from service import create_server

INCIDENT_EVENT = {
    "event_id": "evt-incident",
    "child_id": "child-girl",
    "device_model": "watch-w2",
    "measured_at": "2026-09-01T08:00:00+08:00",
    "posture": "standing_still",
    "height": {"value": 167, "unit": "cm"},
    "weight": {"value": 50.5, "unit": "kg"},
    "reported_age_band": "13-15",
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.server = create_server(0)  # 随机端口 + 种子规则/设备，每用例独立
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "child-health-governance")

    def test_incident_event_is_stopped_and_explainable(self):
        """事故输入走完整接口：不再出现“偏重”，只有中性建议与完整解释。"""
        status, view = self.call("POST", "/v1/events", INCIDENT_EVENT)
        self.assertEqual(status, 201)
        self.assertEqual(view["current"]["category"], "insufficient_evidence")
        self.assertIsNone(view["current"]["scope"])
        self.assertIn("重新测量", view["current"]["advice"])
        text = json.dumps(view, ensure_ascii=False)
        for banned in ("偏重", "肥胖", "诊断", "付费", "课程"):
            self.assertNotIn(banned, text)

        status, fetched = self.call("GET", "/v1/children/child-girl/records/evt-incident")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["current"]["category"], "insufficient_evidence")
        checks = {c["name"]: c["passed"] for c in fetched["current"]["reasoning"]["checks"]}
        self.assertFalse(checks["device"])  # 手表未做身高体重校准登记

    def test_duplicate_event_rejected(self):
        self.call("POST", "/v1/events", INCIDENT_EVENT)
        status, body = self.call("POST", "/v1/events", INCIDENT_EVENT)
        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_calibrated_event_full_lifecycle(self):
        event = dict(INCIDENT_EVENT, event_id="evt-ok", child_id="child-ok", device_model="scale-s2")
        status, view = self.call("POST", "/v1/events", event)
        self.assertEqual(status, 201)
        self.assertEqual(view["current"]["category"], "within_reference")
        self.assertEqual(view["current"]["scope"]["rule_version"], 1)

        # 灰度：未同意时仍用稳定版；监护人同意后切到灰度 v2；退出后回滚。
        status, body = self.call(
            "POST", "/v1/consents",
            {"child_id": "child-ok", "guardian_id": "guardian-1", "action": "grant"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["changed_events"], ["evt-ok"])
        status, view = self.call("GET", "/v1/children/child-ok/records/evt-ok")
        self.assertEqual(view["current"]["scope"]["rule_version"], 2)
        self.assertTrue(view["current"]["scope"]["gray"])
        self.assertEqual(view["current"]["category"], "above_reference")

        status, body = self.call(
            "POST", "/v1/consents",
            {"child_id": "child-ok", "guardian_id": "guardian-1", "action": "revoke"},
        )
        self.assertEqual(body["changed_events"], ["evt-ok"])
        status, view = self.call("GET", "/v1/children/child-ok/records/evt-ok")
        self.assertEqual(view["current"]["scope"]["rule_version"], 1)
        self.assertEqual(len(view["history"]), 3)

        # 家庭通知可追溯
        status, notes = self.call("GET", "/v1/children/child-ok/notifications")
        self.assertEqual(len(notes["notifications"]), 2)

    def test_unit_correction_flow(self):
        event = dict(
            INCIDENT_EVENT,
            event_id="evt-unit",
            child_id="child-unit",
            device_model="scale-s2",
            weight={"value": 101, "unit": "jin"},
        )
        status, view = self.call("POST", "/v1/events", event)
        self.assertEqual(view["current"]["category"], "within_reference")  # 101 斤 = 50.5 kg

        status, body = self.call(
            "POST", "/v1/corrections/unit", {"event_id": "evt-unit", "weight_unit": "kg"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["updated"])
        self.assertEqual(body["record"]["current"]["category"], "above_reference")
        self.assertEqual(len(body["record"]["history"]), 2)
        history_text = json.dumps(body["record"]["history"], ensure_ascii=False)
        self.assertNotIn("参考范围", history_text)  # 旧结果内容不再展示

    def test_birthday_correction_flow(self):
        event = dict(
            INCIDENT_EVENT,
            event_id="evt-bday",
            child_id="child-bday",
            device_model="scale-s2",
            weight={"value": 60, "unit": "kg"},
            birth_date="2014-05-01",
        )
        status, view = self.call("POST", "/v1/events", event)
        self.assertEqual(view["current"]["category"], "above_reference")  # 10-12 区间

        status, body = self.call(
            "POST", "/v1/corrections/birthday",
            {"child_id": "child-bday", "birth_date": "2012-05-01"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["updated_events"], ["evt-bday"])
        self.assertTrue(body["notified"])
        status, view = self.call("GET", "/v1/children/child-bday/records/evt-bday")
        self.assertEqual(view["current"]["category"], "within_reference")  # 13-15 区间

    def test_rule_withdrawal_flow(self):
        event = dict(INCIDENT_EVENT, event_id="evt-wd", child_id="child-wd", device_model="scale-s2")
        self.call("POST", "/v1/events", event)
        status, body = self.call("POST", "/v1/rules/withdraw", {"rule_id": "bmi-for-age", "version": 1})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(body["affected_children"], 1)
        status, view = self.call("GET", "/v1/children/child-wd/records/evt-wd")
        self.assertEqual(view["current"]["category"], "insufficient_evidence")
        self.assertIsNone(view["current"]["scope"])
        status, notes = self.call("GET", "/v1/children/child-wd/notifications")
        self.assertEqual(notes["notifications"][0]["kind"], "rule_withdrawal")

    def test_create_rule_reports_missing_approvals(self):
        payload = {
            "rule_id": "bmi-for-age",
            "version": 9,
            "metric": "bmi_for_age",
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
            "reference": "测试",
            "approvals": [
                {"role": "medical", "approver": "dr", "approved_at": "2025-12-01T00:00:00+00:00"}
            ],
            "bands_by_age": {"13-15": [{"category": "within_reference", "min": 0}]},
        }
        status, body = self.call("POST", "/v1/rules", payload)
        self.assertEqual(status, 201)
        self.assertFalse(body["usable"])
        self.assertTrue(any("privacy" in issue for issue in body["issues"]))
        self.assertTrue(any("copywriting" in issue for issue in body["issues"]))

    def test_analytics_threshold_and_individual_rejection(self):
        self.call("POST", "/v1/events", INCIDENT_EVENT)
        self.call("POST", "/v1/events", dict(INCIDENT_EVENT, event_id="evt-2", child_id="child-2"))
        status, body = self.call("GET", "/v1/analytics/quality?dims=device_model")
        self.assertEqual(status, 200)
        self.assertEqual(body["min_cohort"], 50)
        self.assertEqual(len(body["cells"]), 1)
        self.assertTrue(all(cell["suppressed"] for cell in body["cells"]))
        self.assertNotIn("child-girl", json.dumps(body, ensure_ascii=False))

        status, body = self.call("GET", "/v1/analytics/quality?dims=child_id")
        self.assertEqual(status, 400)
        status, body = self.call("GET", "/v1/analytics/quality?child_id=child-girl")
        self.assertEqual(status, 400)

    def test_unknown_routes(self):
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/v1/nope", {})
        self.assertEqual(status, 404)
        status, _ = self.call("GET", "/v1/children/nobody/records/nothing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
