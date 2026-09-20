"""评估流水线与治理行为的单元测试。"""

import json
import unittest
from datetime import date, datetime, timezone

from governance import (
    CATEGORY_INSUFFICIENT,
    Store,
    apply_birthday_correction,
    apply_unit_correction,
    assert_family_safe,
    ingest_event,
    parent_view,
    quality_stats,
    rule_from_dict,
    set_gray_consent,
    withdraw_rule,
)
from governance.models import MeasurementEvent
from governance.safety import FORBIDDEN_PATTERNS

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

APPROVALS = [
    {"role": "medical", "approver": "dr", "approved_at": "2024-12-20T10:00:00+00:00"},
    {"role": "privacy", "approver": "pr", "approved_at": "2024-12-21T10:00:00+00:00"},
    {"role": "copywriting", "approver": "cw", "approved_at": "2024-12-22T10:00:00+00:00"},
]

COPY = {
    "below_reference": "本次测量结果低于参考范围。",
    "within_reference": "本次测量结果在参考范围内。",
    "above_reference": "本次测量结果高于参考范围。",
}

BANDS_13_15 = [
    {"category": "below_reference", "max": 15.5},
    {"category": "within_reference", "min": 15.5, "max": 23.0},
    {"category": "above_reference", "min": 23.0},
]


def make_rule(rule_id="bmi-for-age", version=1, *, gray=False, valid_from="2025-01-01",
              valid_to="2026-12-31", bands=None, copy=None, approvals=None):
    return rule_from_dict({
        "rule_id": rule_id,
        "version": version,
        "metric": "bmi_for_age",
        "gray": gray,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "reference": "测试参考标准（非医学建议）",
        "approvals": APPROVALS if approvals is None else approvals,
        "bands_by_age": bands if bands is not None else {"13-15": BANDS_13_15},
        "copy": COPY if copy is None else copy,
    })


def make_store(with_gray=True):
    store = Store()
    store.devices["scale-s2"] = {"model": "scale-s2", "calibrated_metrics": ["height", "weight"]}
    store.devices["watch-w2"] = {"model": "watch-w2", "calibrated_metrics": []}
    store.add_rule(make_rule(version=1))
    if with_gray:
        gray_bands = [
            {"category": "below_reference", "max": 15.5},
            {"category": "within_reference", "min": 15.5, "max": 18.0},
            {"category": "above_reference", "min": 18.0},
        ]
        store.add_rule(make_rule(version=2, gray=True, valid_from="2026-06-01", bands={"13-15": gray_bands}))
    return store


def make_event(event_id="e1", child_id="c1", **overrides):
    params = {
        "event_id": event_id,
        "child_id": child_id,
        "device_model": "scale-s2",
        "measured_at": datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        "received_at": datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
        "posture": "standing_still",
        "height_value": 167.0,
        "height_unit": "cm",
        "weight_value": 50.5,
        "weight_unit": "kg",
        "reported_age_band": "13-15",
    }
    params.update(overrides)
    return MeasurementEvent(**params)


def assert_family_facing_safe(testcase, payload):
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    for pattern in FORBIDDEN_PATTERNS:
        testcase.assertNotIn(pattern, text)


class CredibilityGateTest(unittest.TestCase):
    def test_incident_scenario_unexplainable_conclusion_is_stopped(self):
        """事故复现：手表上报 167cm/50.5kg，设备未校准 -> 不下结论，只给中性建议。"""
        store = make_store()
        conclusion = ingest_event(store, make_event(device_model="watch-w2"), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)
        self.assertIsNone(conclusion.rule_id)
        self.assertIn("重新测量", conclusion.advice)
        self.assertIn("咨询专业人员", conclusion.advice)
        assert_family_facing_safe(self, conclusion.category_text)
        assert_family_facing_safe(self, conclusion.advice)

    def test_calibrated_device_gives_neutral_conclusion(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(), now=NOW)
        self.assertEqual(conclusion.category, "within_reference")
        self.assertEqual(conclusion.rule_version, 1)
        self.assertAlmostEqual(conclusion.metric_value, 18.1, places=1)
        assert_family_facing_safe(self, conclusion.category_text + conclusion.advice)

    def test_unregistered_device_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(device_model="unknown-x"), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_bad_posture_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(posture="sitting"), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_unknown_unit_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(weight_unit="stone"), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_implausible_value_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(height_value=300.0), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_missing_measurement_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(weight_value=None, weight_unit=None), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_unknown_age_band_is_insufficient(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(reported_age_band=None), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)


class RuleGatingTest(unittest.TestCase):
    def test_rule_missing_any_approval_is_not_used(self):
        store = Store()
        store.devices["scale-s2"] = {"model": "scale-s2", "calibrated_metrics": ["height", "weight"]}
        store.add_rule(make_rule(approvals=APPROVALS[:2]))  # 缺文案批准
        conclusion = ingest_event(store, make_event(), now=NOW)
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)
        self.assertIsNone(conclusion.rule_id)

    def test_expired_rule_is_not_used(self):
        store = make_store()
        conclusion = ingest_event(
            store, make_event(measured_at=datetime(2027, 1, 5, tzinfo=timezone.utc)), now=NOW
        )
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)

    def test_offline_backfill_uses_collection_time_rule(self):
        """补传数据按采集时（而非上传时/评估时）的规则解释。"""
        store = Store()
        store.devices["scale-s2"] = {"model": "scale-s2", "calibrated_metrics": ["height", "weight"]}
        tight = [  # 旧规则：18.1 -> above
            {"category": "within_reference", "min": 15.5, "max": 18.0},
            {"category": "above_reference", "min": 18.0},
        ]
        loose = BANDS_13_15  # 新规则：18.1 -> within
        store.add_rule(make_rule(version=1, valid_from="2025-01-01", valid_to="2025-06-30", bands={"13-15": tight}))
        store.add_rule(make_rule(version=2, valid_from="2025-07-01", valid_to="2026-12-31", bands={"13-15": loose}))
        event = make_event(
            measured_at=datetime(2025, 5, 10, 8, tzinfo=timezone.utc),
            received_at=datetime(2025, 8, 1, 8, tzinfo=timezone.utc),  # 离线补传
        )
        conclusion = ingest_event(store, event, now=NOW)
        self.assertEqual(conclusion.rule_version, 1)
        self.assertEqual(conclusion.category, "above_reference")

    def test_shaming_copy_is_blocked(self):
        """规则文案触碰含羞辱/营销内容时被安全红线拦截，降级为证据不足。"""
        store = Store()
        store.devices["scale-s2"] = {"model": "scale-s2", "calibrated_metrics": ["height", "weight"]}
        bad_copy = dict(COPY, above_reference="偏重，建议购买减重课程")
        store.add_rule(make_rule(copy=bad_copy))
        conclusion = ingest_event(store, make_event(weight_value=90.0), now=NOW)  # BMI 32.3 -> above
        self.assertEqual(conclusion.category, CATEGORY_INSUFFICIENT)
        self.assertTrue(conclusion.copy_blocked)
        assert_family_facing_safe(self, conclusion.category_text + conclusion.advice)


class GrayConsentTest(unittest.TestCase):
    def test_gray_rule_requires_guardian_consent(self):
        store = make_store()
        conclusion = ingest_event(store, make_event(), now=NOW)
        self.assertEqual((conclusion.rule_version, conclusion.gray_used), (1, False))
        self.assertEqual(conclusion.category, "within_reference")

    def test_opt_in_uses_gray_rule_and_opt_out_rolls_back(self):
        store = make_store()
        ingest_event(store, make_event(), now=NOW)
        changed = set_gray_consent(store, "c1", guardian_id="g1", action="grant", now=NOW)
        self.assertEqual(changed, ["e1"])
        current = store.current_conclusion("e1")
        self.assertEqual((current.rule_version, current.gray_used), (2, True))
        self.assertEqual(current.category, "above_reference")  # 灰度规则阈值更严
        self.assertEqual(len(store.notifications["c1"]), 1)

        changed = set_gray_consent(store, "c1", guardian_id="g1", action="revoke", now=NOW)
        self.assertEqual(changed, ["e1"])
        current = store.current_conclusion("e1")
        self.assertEqual((current.rule_version, current.gray_used), (1, False))
        self.assertEqual(current.category, "within_reference")
        self.assertEqual(current.version_no, 3)
        self.assertEqual(len(store.notifications["c1"]), 2)


class VersioningTest(unittest.TestCase):
    def test_unit_correction_creates_version_and_hides_old(self):
        """101 斤 == 50.5 kg；把单位从“斤”修正为“kg”后结论必须翻新。"""
        store = make_store()
        ingest_event(store, make_event(weight_value=101.0, weight_unit="jin"), now=NOW)
        self.assertEqual(store.current_conclusion("e1").category, "within_reference")

        conclusion = apply_unit_correction(store, "e1", weight_unit="kg", now=NOW)
        self.assertIsNotNone(conclusion)
        self.assertEqual(conclusion.version_no, 2)
        self.assertEqual(conclusion.category, "above_reference")  # 101kg -> BMI 36.2

        view = parent_view(store, "e1")
        self.assertEqual(view["current"]["category"], "above_reference")
        self.assertEqual(len(view["history"]), 2)
        for entry in view["history"]:
            self.assertNotIn("category", entry)  # 旧结果不再展示
            self.assertNotIn("advice", entry)
        self.assertEqual(len(store.notifications["c1"]), 1)
        assert_family_facing_safe(self, view)

    def test_noop_correction_does_not_create_version(self):
        store = make_store()
        ingest_event(store, make_event(), now=NOW)
        self.assertIsNone(apply_unit_correction(store, "e1", weight_unit="kg", now=NOW))
        self.assertEqual(store.current_conclusion("e1").version_no, 1)

    def test_birthday_correction_reevaluates_all_events(self):
        bands = {
            "10-12": [
                {"category": "within_reference", "min": 14.5, "max": 21.0},
                {"category": "above_reference", "min": 21.0},
            ],
            "13-15": BANDS_13_15,
        }
        store = make_store()
        store.rules.clear()
        store.add_rule(make_rule(bands=bands))
        event = make_event(weight_value=60.0)  # BMI 21.5
        store.ensure_child("c1").birth_date = date(2014, 5, 1)  # 12 岁 -> 10-12 -> above
        ingest_event(store, event, now=NOW)
        self.assertEqual(store.current_conclusion("e1").category, "above_reference")

        changed = apply_birthday_correction(store, "c1", birth_date=date(2012, 5, 1), now=NOW)
        self.assertEqual(changed, ["e1"])
        current = store.current_conclusion("e1")
        self.assertEqual(current.age_band, "13-15")
        self.assertEqual(current.category, "within_reference")
        self.assertEqual(len(store.notifications["c1"]), 1)

    def test_rule_withdrawal_relabels_and_notifies(self):
        store = make_store()
        ingest_event(store, make_event("e1", "c1"), now=NOW)
        ingest_event(store, make_event("e2", "c2"), now=NOW)
        set_gray_consent(store, "c3", guardian_id="g3", action="grant", now=NOW)
        ingest_event(store, make_event("e3", "c3"), now=NOW)  # 走灰度 v2，不受 v1 撤回影响

        changed = withdraw_rule(store, "bmi-for-age", 1, now=NOW)
        self.assertEqual(set(changed), {"c1", "c2"})
        for event_id in ("e1", "e2"):
            current = store.current_conclusion(event_id)
            self.assertEqual(current.category, CATEGORY_INSUFFICIENT)  # 无稳定规则可用
            self.assertIsNone(current.rule_id)
            self.assertEqual(current.version_no, 2)
        self.assertEqual(store.current_conclusion("e3").rule_version, 2)
        self.assertEqual(len(store.notifications["c1"]), 1)
        self.assertNotIn("c3", changed)

        view = parent_view(store, "e1")
        self.assertIsNone(view["current"]["scope"])
        self.assertIn("没有适用于本次测量的已批准规则", view["current"]["scope_note"])
        assert_family_facing_safe(self, view)


class ParentViewTest(unittest.TestCase):
    def test_view_contains_source_scope_reasoning_history(self):
        store = make_store()
        ingest_event(store, make_event(), now=NOW)
        view = parent_view(store, "e1")
        self.assertEqual(
            set(view), {"event_id", "child_id", "data_source", "current", "history", "disclaimer"}
        )
        self.assertEqual(view["data_source"]["device_model"], "scale-s2")
        self.assertFalse(view["data_source"]["offline_backfill"])
        scope = view["current"]["scope"]
        self.assertEqual(scope["rule_id"], "bmi-for-age")
        self.assertEqual(sorted(scope["approvals"]), ["copywriting", "medical", "privacy"])
        reasoning = view["current"]["reasoning"]
        self.assertEqual(reasoning["value"], 18.1)
        self.assertTrue(all(check["passed"] for check in reasoning["checks"]))
        self.assertEqual(view["history"][0]["reason"], "首次评估")
        assert_family_facing_safe(self, view)

    def test_offline_backfill_flag_visible(self):
        store = make_store()
        event = make_event(
            measured_at=datetime(2026, 8, 1, 8, tzinfo=timezone.utc),
            received_at=datetime(2026, 8, 5, 8, tzinfo=timezone.utc),
        )
        ingest_event(store, event, now=NOW)
        view = parent_view(store, "e1")
        self.assertTrue(view["data_source"]["offline_backfill"])


class AnalyticsTest(unittest.TestCase):
    def test_below_minimum_cohort_is_suppressed(self):
        store = make_store()
        for i in range(3):
            ingest_event(store, make_event(f"e{i}", f"c{i}"), now=NOW)
        stats = quality_stats(store, ["device_model"])
        self.assertEqual(stats["min_cohort"], 50)
        self.assertTrue(stats["cells"][0]["suppressed"])
        self.assertNotIn("count", stats["cells"][0])

    def test_cohort_meeting_threshold_reports_counts(self):
        store = make_store()
        for i in range(5):
            ingest_event(store, make_event(f"e{i}", f"c{i}"), now=NOW)
        stats = quality_stats(store, ["device_model", "category"], min_cohort=5)
        cell = stats["cells"][0]
        self.assertFalse(cell["suppressed"])
        self.assertEqual(cell["count"], 5)
        self.assertEqual(cell["insufficient_rate"], 0.0)

    def test_analytics_never_exposes_individuals(self):
        store = make_store()
        ingest_event(store, make_event("evt-secret", "child-secret"), now=NOW)
        stats = quality_stats(store, ["device_model", "age_band", "category"], min_cohort=1)
        payload = json.dumps(stats, ensure_ascii=False)
        self.assertNotIn("child-secret", payload)
        self.assertNotIn("evt-secret", payload)
        self.assertNotIn("child_id", payload)

    def test_individual_dims_are_rejected(self):
        store = make_store()
        for dim in ("child_id", "event_id", "guardian_id", "family_id", "unknown_dim"):
            with self.assertRaises(ValueError):
                quality_stats(store, [dim])


if __name__ == "__main__":
    unittest.main()
