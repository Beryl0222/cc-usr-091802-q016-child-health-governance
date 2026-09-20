"""产品侧群体质量统计：最小群体门槛与维度白名单。"""

import unittest

from governance.stats import ALLOWED_DIMENSIONS, QualityEvent, StatsCollector

MIN_COHORT = 50


def event(i, *, model="watch-k1", band="13-15", sex="female",
          verdict="within_range", level="normal", canary=False, hard=False):
    return QualityEvent(
        model_id=model, age_band=band, sex=sex, verdict=verdict,
        evidence_level=level, rule_canary=canary, has_hard_problems=hard,
        record_version=f"rec-{i}#v1",
    )


class KAnonymityTest(unittest.TestCase):
    def test_small_groups_are_suppressed(self):
        stats = StatsCollector()
        for i in range(49):
            stats.record(event(i))
        report = stats.quality_report(["model_id", "verdict"])
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["suppressed_group_count"], 1)
        # 总数也不能暴露精确小样本
        self.assertEqual(report["total_events_in_report"], "0+")

    def test_group_at_threshold_is_reported(self):
        stats = StatsCollector()
        for i in range(MIN_COHORT):
            stats.record(event(i))
        report = stats.quality_report(["model_id", "verdict"])
        self.assertEqual(len(report["groups"]), 1)
        g = report["groups"][0]
        self.assertEqual(g["event_count"], 50)
        self.assertEqual(g["hard_problem_rate"], 0.0)

    def test_mixed_population_only_reports_large_buckets(self):
        stats = StatsCollector()
        # 80 个范围内 + 30 个证据不足（后者应被抑制）
        for i in range(80):
            stats.record(event(i, verdict="within_range"))
        for i in range(80, 110):
            stats.record(event(i, verdict="insufficient_evidence", level="weak", hard=True))
        report = stats.quality_report(["verdict"])
        verdicts = {g["verdict"] for g in report["groups"]}
        self.assertEqual(verdicts, {"within_range"})
        self.assertEqual(report["suppressed_group_count"], 1)
        # 80 向下取整到 50 的倍数
        self.assertEqual(report["total_events_in_report"], "50+")

    def test_dimension_whitelist(self):
        stats = StatsCollector()
        with self.assertRaises(ValueError):
            stats.quality_report(["person_id"])
        with self.assertRaises(ValueError):
            stats.quality_report(["record_version"])
        # 白名单维度可用
        for d in ALLOWED_DIMENSIONS:
            self.assertIn(d, ["model_id", "age_band", "sex", "verdict",
                              "evidence_level", "rule_canary", "has_hard_problems"])

    def test_revision_replaces_event_instead_of_double_counting(self):
        stats = StatsCollector()
        stats.record(event(1, verdict="needs_attention"))
        stats.record(event(1, verdict="within_range"))  # 同记录新版本
        report = stats.quality_report(["verdict"])
        # 两条都不足 50，但可通过内部计数确认只有 1 个事件
        self.assertEqual(len(stats), 1)

    def test_rates_computed(self):
        stats = StatsCollector()
        for i in range(60):
            stats.record(event(i))
        for i in range(60, 110):
            stats.record(event(i, verdict="insufficient_evidence",
                               level="weak", hard=True))
        report = stats.quality_report(["verdict"])
        groups = {g["verdict"]: g for g in report["groups"]}
        self.assertEqual(groups["within_range"]["event_count"], 60)
        self.assertEqual(groups["insufficient_evidence"]["event_count"], 50)
        self.assertEqual(groups["insufficient_evidence"]["hard_problem_rate"], 1.0)
        self.assertEqual(groups["insufficient_evidence"]["weak_evidence_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
