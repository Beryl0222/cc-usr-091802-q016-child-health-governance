"""规则的三方会签、指纹绑定、有效期与红线文案。"""

import unittest
from datetime import datetime, timezone

from governance.models import (
    Approval,
    ContentPolicyError,
    SafetyPack,
    content_fingerprint,
)
from governance.rules import Rule, RuleBook

APPROVED_AT = "2026-01-10T00:00:00Z"
FROM = "2026-02-01T00:00:00Z"

PACKS = {
    "within_range": SafetyPack("范围内", "处于参考区间内。", "保持日常活动。"),
    "needs_attention": SafetyPack("建议关注", "落在参考区间外。", "可复测或咨询专业人员。"),
}


def make_rule(
    rule_id="r1", version=1, *, packs=PACKS, thresholds=None,
    canary=False, effective_from=FROM, effective_to=None, approvals=None,
    sex_groups=frozenset({"any"}),
):
    thresholds = thresholds or {"10-12": {"any": [14.0, 24.0]}}
    rule = Rule(
        rule_id=rule_id, version=version, indicator="bmi",
        thresholds=thresholds, packs=packs,
        effective_from=effective_from, effective_to=effective_to,
        approvals=(), canary=canary, sex_groups=sex_groups,
    )
    if approvals is None:
        fp = rule.fingerprint
        approvals = tuple(
            Approval(role=r, approver_id=f"person-{r}",
                     approved_at=APPROVED_AT, content_fingerprint=fp)
            for r in ("medical", "privacy", "copy")
        )
    rule.approvals = approvals
    return rule


class RuleSigningTest(unittest.TestCase):
    def test_fully_signed_rule_publishes(self):
        book = RuleBook()
        book.add_rule(make_rule())
        self.assertEqual(len(book.history), 1)

    def test_missing_role_rejected(self):
        for missing in ("medical", "privacy", "copy"):
            rule = make_rule(rule_id=f"miss-{missing}")
            rule.approvals = tuple(
                Approval(role=r, approver_id=f"p-{r}",
                         approved_at=APPROVED_AT, content_fingerprint=rule.fingerprint)
                for r in ("medical", "privacy", "copy") if r != missing
            )
            with self.assertRaises(ContentPolicyError) as ctx:
                RuleBook().add_rule(rule)
            self.assertIn("会签", str(ctx.exception))

    def test_tampering_after_approval_detected(self):
        rule = make_rule()
        good_fp = rule.fingerprint
        # 有人在批准后改了阈值；规则对象指纹随之变化，批准仍绑旧指纹
        rule.thresholds = {"10-12": {"any": [14.0, 30.0]}}
        self.assertNotEqual(rule.fingerprint, good_fp)
        with self.assertRaises(ContentPolicyError) as ctx:
            RuleBook().add_rule(rule)
        self.assertIn("指纹", str(ctx.exception))

    def test_duplicate_rule_id_rejected(self):
        book = RuleBook()
        book.add_rule(make_rule(version=1))
        with self.assertRaises(ValueError):
            book.add_rule(make_rule(version=2))  # 内容变更必须换新 rule_id

    def test_forbidden_copy_rejected(self):
        cases = {
            "diagnosis": SafetyPack("建议关注", "已诊断为超重。", "复测。"),
            "shame": SafetyPack("建议关注", "这样下去会被同学嘲笑。", "复测。"),
            "paid": SafetyPack("建议关注", "超出区间。", "立即购买我们的减肥课。"),
        }
        for name, pack in cases.items():
            with self.subTest(name=name):
                rule = make_rule(
                    rule_id=f"bad-{name}",
                    packs={"within_range": PACKS["within_range"],
                           "needs_attention": pack},
                )
                with self.assertRaises(ContentPolicyError):
                    RuleBook().add_rule(rule)

    def test_classify_boundaries(self):
        rule = make_rule(thresholds={"10-12": {"any": [14.0, 24.0]}})
        self.assertEqual(rule.classify(23.9, "10-12", "any"), "within_range")
        self.assertEqual(rule.classify(14.0, "10-12", "any"), "within_range")
        self.assertEqual(rule.classify(24.0, "10-12", "any"), "needs_attention")
        self.assertIsNone(rule.classify(20.0, "13-15", "any"))

    def test_validity_window(self):
        rule = make_rule(effective_from=FROM, effective_to="2026-06-01T00:00:00Z")
        self.assertTrue(rule.active_at(datetime(2026, 5, 1, tzinfo=timezone.utc)))
        self.assertFalse(rule.active_at(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertFalse(rule.active_at(datetime(2026, 6, 1, tzinfo=timezone.utc)))

    def test_canary_requires_consent(self):
        book = RuleBook()
        book.add_rule(make_rule(rule_id="stable", version=1))
        book.add_rule(make_rule(rule_id="gray", version=2, canary=True,
                                thresholds={"10-12": {"any": [14.0, 25.0]}}))
        at = datetime(2026, 3, 1, tzinfo=timezone.utc)
        no_consent = book.select(indicator="bmi", age_band="10-12", sex="any",
                                 model_id="m1", at=at, canary_consent=False)
        self.assertEqual(no_consent.rule_id, "stable")
        consented = book.select(indicator="bmi", age_band="10-12", sex="any",
                                model_id="m1", at=at, canary_consent=True)
        self.assertEqual(consented.rule_id, "gray")

    def test_withdrawn_rule_invisible_even_for_past_dates(self):
        book = RuleBook()
        book.add_rule(make_rule())
        book.withdraw("r1", "2026-05-01T00:00:00Z", "阈值有误")
        # 即使采集时点早于撤回，被撤回规则也不得用于补传解释
        picked = book.select(indicator="bmi", age_band="10-12", sex="any",
                             model_id="m1",
                             at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                             canary_consent=False)
        self.assertIsNone(picked)

    def test_threshold_structure_validated(self):
        rule = make_rule(rule_id="rev", thresholds={"10-12": {"any": [24.0, 14.0]}})
        with self.assertRaises(ContentPolicyError):
            RuleBook().add_rule(rule)

        equal = make_rule(rule_id="eq", thresholds={"10-12": {"any": [24.0, 24.0]}})
        with self.assertRaises(ContentPolicyError):
            RuleBook().add_rule(equal)


if __name__ == "__main__":
    unittest.main()
