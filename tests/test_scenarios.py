"""端到端治理场景：从事故复盘到可持续治理。

重点用例：身高 167cm、体重 50.5kg 的 13 岁女孩（BMI≈18.1）绝不能被
标成“偏重”；任何无法解释、证据不足的情况必须回退到中性建议。
"""

import unittest
from datetime import datetime, timedelta, timezone

from governance.app import Application
from governance.service import GovernanceError


def fixed_clock(year=2026, month=9, day=20, hour=10):
    state = {"t": datetime(year, month, day, hour, tzinfo=timezone.utc)}

    def clock():
        return state["t"]

    def advance(**kwargs):
        state["t"] += timedelta(**kwargs)

    clock.advance = advance
    clock.set = lambda **kw: state.__setitem__("t", state["t"].replace(**kw))
    return clock


def payload(person="mei", model="watch-k1", *, height=167, height_unit="cm",
            weight=50.5, weight_unit="kg", posture="standing", mode="manual",
            record_id=None, backfill=False, collected_at=None):
    body = {
        "person_id": person,
        "model_id": model,
        "readings": [
            {"measurement": "height", "value": height, "unit": height_unit,
             "posture": posture, "capture_mode": mode},
            {"measurement": "weight", "value": weight, "unit": weight_unit,
             "posture": posture, "capture_mode": mode},
        ],
    }
    if record_id:
        body["record_id"] = record_id
    if backfill:
        body["backfill"] = True
        body["collected_at"] = collected_at
        for r in body["readings"]:
            r["measured_at"] = collected_at
    return body


class IncidentRegressionTest(unittest.TestCase):
    def setUp(self):
        self.clock = fixed_clock()
        self.app = Application(clock=self.clock)
        self.svc = self.app.service
        # 2013-03-10 出生，2026-09-20 为 13 岁女孩
        self.svc.register_person("mei", "2013-03-10", "female")
        self.principal = {"role": "guardian", "person_id": "mei"}

    def test_167cm_505kg_girl_is_not_labelled_overweight(self):
        c = self.app.submit_measurement(self.principal, payload())
        self.assertEqual(c.verdict, "within_range")
        self.assertIsNotNone(c.rule_id)
        self.assertAlmostEqual(c.indicator_value, 18.1, places=1)
        self.assertNotIn("偏重", c.headline + c.explanation + c.recommendation)
        self.assertNotIn("胖", c.headline + c.explanation + c.recommendation)
        self.assertEqual(c.applicability["age_band"], "13-15")
        self.assertEqual(c.applicability["sex"], "female")

    def test_conclusion_explains_rule_version_and_fingerprint(self):
        c = self.app.submit_measurement(self.principal, payload(record_id="rec-1"))
        self.assertIsNotNone(c.rule_fingerprint)
        self.assertTrue(c.rule_fingerprint.startswith("sha256:"))
        self.assertEqual(c.rule_version, 2)  # 女性分组适用 v2
        view = self.svc.parent_record_view("rec-1")
        self.assertEqual(view["applicability"]["rule_id"], c.rule_id)
        self.assertIn("reference_range", view["applicability"])
        self.assertIn("raw_readings", view["data_source"])
        self.assertIn("not_a_diagnosis", view)

    def test_insufficient_evidence_is_neutral(self):
        # 单位无法识别 → 不得硬算，不得给分类
        bad = payload(model="watch-k2", weight_unit="stone", record_id="rec-bad")
        c = self.app.submit_measurement(self.principal, bad)
        self.assertEqual(c.verdict, "insufficient_evidence")
        self.assertIsNone(c.rule_id)
        self.assertIn("重新测量", c.recommendation)
        self.assertIn("专业人员", c.recommendation)
        text = c.headline + c.explanation + c.recommendation
        for banned in ("诊断", "胖", "肥", "丢人", "付费", "购买"):
            self.assertNotIn(banned, text)

    def test_wrong_posture_for_age_falls_back(self):
        # 13 岁卧位“身高”不合理
        bad = payload(posture="lying", record_id="rec-posture", model="watch-k2")
        c = self.app.submit_measurement(self.principal, bad)
        self.assertEqual(c.verdict, "insufficient_evidence")
        self.assertTrue(any("姿态" in p for p in c.reliability["hard_problems"]))

    def test_no_applicable_rule_for_age_band(self):
        # 2 岁孩子没有任何已批准 BMI 规则
        self.svc.register_person("toddler", "2025-06-01", "female")
        body = payload(person="toddler", posture="lying",
                       height=0.85, height_unit="m", weight=12)
        body["readings"][1]["posture"] = "held"
        c = self.app.submit_measurement({"role": "guardian", "person_id": "toddler"}, body)
        self.assertEqual(c.verdict, "insufficient_evidence")
        self.assertTrue(any("规则" in p for p in c.reliability["hard_problems"]))


class CanaryConsentTest(unittest.TestCase):
    def setUp(self):
        self.clock = fixed_clock()
        self.app = Application(clock=self.clock)
        self.svc = self.app.service
        self.svc.register_person("mei", "2012-06-01", "female")
        self.principal = {"role": "guardian", "person_id": "mei"}

    def _high_bmi(self, record_id):
        # 62kg / 1.60m -> BMI 24.2：v2 女性 13-15 区间 [15,26.5) 范围内，
        # 但灰度 v3 边界不同，用于区分是否命中灰度
        return payload(record_id=record_id, height=1.60, height_unit="m", weight=62)

    def test_gray_rule_not_used_without_explicit_consent(self):
        c = self.app.submit_measurement(self.principal, self._high_bmi("r1"))
        self.assertEqual(c.rule_id, "bmi-ref-sexspecific-v2")
        self.assertFalse(c.canary)

    def test_grant_then_optout_reprocesses_and_notifies(self):
        c1 = self.app.submit_measurement(self.principal, self._high_bmi("r1"))
        self.assertEqual(c1.version, 1)
        state, _ = self.svc.set_canary_consent("mei", True)
        self.assertTrue(state.canary_enabled)
        self.assertEqual(state.history[-1]["action"], "grant")
        # 新记录命中灰度
        self.clock.advance(days=1)
        c2 = self.app.submit_measurement(self.principal, self._high_bmi("r2"))
        self.assertEqual(c2.rule_id, "bmi-ref-puberty-canary-v3")
        self.assertTrue(c2.canary)
        self.assertIs(c2.canary_consent_snapshot, True)
        # 退出灰度：r2 立即用正式规则重评
        state, affected = self.svc.set_canary_consent("mei", False)
        self.assertFalse(state.canary_enabled)
        self.assertEqual(len(affected), 1)
        new = affected[0]
        self.assertEqual(new.record_id, "r2")
        self.assertEqual(new.rule_id, "bmi-ref-sexspecific-v2")
        self.assertEqual(new.revision_reason, "canary_optout")
        self.assertEqual(self.svc.conclusions["r2"][-2].status, "superseded")
        # 家庭收到修订通知
        kinds = [n.kind for n in self.svc.notifications["mei"]]
        self.assertIn("revised", kinds)
        body = " ".join(n.body for n in self.svc.notifications["mei"])
        self.assertIn("旧版本已停止展示", body)

    def test_guardian_can_only_access_own_child(self):
        self.svc.register_person("other", "2012-01-01", "male")
        with self.assertRaises(GovernanceError):
            self.app.submit_measurement(
                {"role": "guardian", "person_id": "mei"},
                payload(person="other", record_id="x"),
            )


class BackfillAndRevisionTest(unittest.TestCase):
    def setUp(self):
        self.clock = fixed_clock()
        self.app = Application(clock=self.clock)
        self.svc = self.app.service
        self.svc.register_person("mei", "2012-06-01", "female")
        self.principal = {"role": "guardian", "person_id": "mei"}

    def test_backfill_uses_rules_effective_at_collection_time(self):
        self.clock.advance(days=0)
        # 采集于 2026-09-01（v2 已生效）
        body = payload(record_id="bf-ok", backfill=True,
                       collected_at="2026-09-01T08:00:00Z",
                       height=1.5, height_unit="m", weight=45)
        c = self.app.submit_measurement(self.principal, body)
        self.assertEqual(c.observed_at, "2026-09-01T08:00:00Z")
        self.assertEqual(c.data_source["time_source"], "device_clock_backfill")
        self.assertEqual(c.rule_id, "bmi-ref-sexspecific-v2")
        self.assertEqual(c.evidence_level, "weak")  # 补传标记弱证据

    def test_backfill_before_any_rule_is_insufficient(self):
        body = payload(backfill=True, collected_at="2025-06-01T08:00:00Z",
                       height=1.3, height_unit="m", weight=30)
        c = self.app.submit_measurement(self.principal, body)
        self.assertEqual(c.verdict, "insufficient_evidence")
        # 超出 30 天补传窗口也是硬性问题
        self.assertTrue(c.reliability["hard_problems"])

    def test_backfill_missing_timestamp_falls_back(self):
        body = payload(backfill=True, height=1.5, height_unit="m", weight=45)
        del body["collected_at"]
        for r in body["readings"]:
            r.pop("measured_at", None)
        c = self.app.submit_measurement(self.principal, body)
        self.assertEqual(c.verdict, "insufficient_evidence")

    def test_unit_correction_creates_new_version_and_chain(self):
        # 111 被错按 kg 录入，实际应为 lb（≈50.35kg）
        c1 = self.app.submit_measurement(
            self.principal,
            payload(record_id="rec-unit", height=1.60, height_unit="m", weight=111),
        )
        self.assertGreater(c1.indicator_value, 40)
        self.assertEqual(c1.verdict, "needs_attention")
        c2 = self.app.correct_units(self.principal, "rec-unit", [
            {"measurement": "weight", "value": 111, "unit": "lb", "posture": "standing"},
            {"measurement": "height", "value": 1.60, "unit": "m", "posture": "standing"},
        ])
        self.assertEqual(c2.version, 2)
        self.assertEqual(c2.supersedes_version, 1)
        self.assertEqual(c2.revision_reason, "unit_correction")
        self.assertAlmostEqual(c2.indicator_value, 19.7, places=1)
        self.assertEqual(c2.verdict, "within_range")
        view = self.svc.parent_record_view("rec-unit")
        self.assertEqual(view["current_version"], 2)
        self.assertEqual(view["revision_history"][0]["status"], "superseded")
        self.assertEqual(len(view["unit_corrections"]), 1)
        before_weight = next(
            r for r in view["unit_corrections"][0]["before"]
            if r["measurement"] == "weight"
        )
        self.assertEqual(before_weight["unit"], "kg")

    def test_birth_date_correction_reprocesses_all_records(self):
        self.app.submit_measurement(self.principal,
                                    payload(record_id="r1", weight=50, height=1.55,
                                            height_unit="m"))
        affected = self.app.correct_birth_date(self.principal, "mei", "2009-06-01")
        self.assertEqual(len(affected), 1)
        new = affected[0]
        self.assertEqual(new.revision_reason, "birth_date_correction")
        self.assertEqual(new.applicability["age_band"], "16-17")
        note = self.svc.notifications["mei"][-1]
        self.assertIn("生日", note.body)

    def test_rule_withdrawal_stops_old_result_and_notifies_family(self):
        c = self.app.submit_measurement(self.principal, payload(record_id="r1"))
        self.assertEqual(c.rule_id, "bmi-ref-sexspecific-v2")
        affected = self.app.withdraw_rule(
            {"role": "admin"}, "bmi-ref-sexspecific-v2", "青春期阈值需重新校准"
        )
        self.assertEqual(len(affected), 1)
        new = affected[0]
        # 旧版本停止展示，按仍有效的已会签规则 v1 重新解释并产生新版本
        self.assertEqual(new.version, 2)
        self.assertEqual(new.revision_reason, "rule_withdrawn")
        self.assertEqual(new.rule_id, "bmi-ref-sexneutral-v1")
        self.assertEqual(self.svc.conclusions["r1"][0].status, "withheld")
        view = self.svc.parent_record_view("r1")
        self.assertEqual(view["current_version"], 2)
        self.assertEqual(view["revision_history"][0]["rule_id"],
                         "bmi-ref-sexspecific-v2")
        note = self.svc.notifications["mei"][-1]
        self.assertIn(note.kind, ("revised", "withheld"))
        self.assertIn("规则", note.body)
        # 撤回后即使是历史时点补传，也选不到被撤回的规则
        body = payload(record_id="bf", backfill=True,
                       collected_at="2026-09-05T08:00:00Z")
        c2 = self.app.submit_measurement(self.principal, body)
        self.assertNotEqual(c2.rule_id, "bmi-ref-sexspecific-v2")

    def test_withdrawal_without_alternative_rule_withholds_result(self):
        # 把两条正式规则都撤回后，任何记录都只能得到证据不足结论
        self.app.submit_measurement(self.principal, payload(record_id="r1"))
        self.app.withdraw_rule({"role": "admin"}, "bmi-ref-sexspecific-v2", "阈值复核")
        affected2 = self.app.withdraw_rule(
            {"role": "admin"}, "bmi-ref-sexneutral-v1", "阈值复核"
        )
        new = next(c for c in affected2 if c.record_id == "r1")
        self.assertEqual(new.verdict, "insufficient_evidence")
        self.assertEqual(self.svc.active_conclusion("r1").verdict,
                         "insufficient_evidence")
        self.assertEqual(self.svc.notifications["mei"][-1].kind, "withheld")


if __name__ == "__main__":
    unittest.main()
