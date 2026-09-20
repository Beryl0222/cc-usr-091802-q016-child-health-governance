"""规则的三方会签、有效期与指纹管理。

一条规则只有同时满足以下条件才可被引擎采用：
1. 医学、隐私、文案三个角色各有一条批准；
2. 每条批准绑定的内容指纹等于规则当前指纹（批准后任何改动都会失配）；
3. 处于有效期内、未被撤回；
4. 灰度规则只对已明确同意灰度的监护人生效，且可随时退出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

from .models import (
    ALLOWED_AGE_BANDS,
    ALLOWED_VERDICTS,  # noqa: F401  (供上层与测试引用)
    APPROVAL_ROLES,
    Approval,
    ContentPolicyError,
    SafetyPack,
    assert_safe_text,
    content_fingerprint,
    parse_iso,
    validate_safety_pack,
)

INDICATORS = ("bmi",)
SEX_GROUPS = ("female", "male", "any")
CLASSIFYING_VERDICTS = ("within_range", "needs_attention")


def rule_content(rule: "Rule") -> dict:
    """进入指纹的内容：逻辑与文案。批准记录与运行期状态不入指纹。"""
    return {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "indicator": rule.indicator,
        "age_bands": sorted(rule.age_bands),
        "sex_groups": sorted(rule.sex_groups),
        "applies_to_models": sorted(rule.applies_to_models),
        "thresholds": rule.thresholds,
        "packs": {k: v.as_dict() for k, v in rule.packs.items()},
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
        "canary": rule.canary,
    }


@dataclass
class Rule:
    rule_id: str
    version: int
    indicator: str
    thresholds: Mapping[str, Mapping[str, list[float]]]
    # age_band -> sex_group -> [lo, hi]，区间内为 within_range，否则 needs_attention
    packs: Mapping[str, SafetyPack]  # verdict -> SafetyPack
    effective_from: str
    approvals: tuple[Approval, ...]
    age_bands: frozenset[str] = field(default_factory=lambda: frozenset(ALLOWED_AGE_BANDS))
    sex_groups: frozenset[str] = field(default_factory=lambda: frozenset({"any"}))
    applies_to_models: frozenset[str] = field(default_factory=frozenset)
    effective_to: Optional[str] = None
    canary: bool = False
    supersedes_rule_id: Optional[str] = None

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(rule_content(self))

    def active_at(self, at: datetime) -> bool:
        start = parse_iso(self.effective_from)
        if at < start:
            return False
        if self.effective_to is not None and at >= parse_iso(self.effective_to):
            return False
        return True

    def classify(self, value: float, age_band: str, sex: str) -> Optional[str]:
        by_sex = self.thresholds.get(age_band, {})
        rng = by_sex.get(sex) or by_sex.get("any")
        if rng is None:
            return None
        lo, hi = rng
        return "within_range" if lo <= value < hi else "needs_attention"


class RuleBook:
    """装载并校验规则；引擎只通过 RuleBook 取规则。"""

    def __init__(self):
        self._rules: dict[str, Rule] = {}
        self._withdrawn: dict[str, tuple[str, str]] = {}  # rule_id -> (at, reason)
        self._history: list[dict] = []

    @property
    def history(self) -> list[dict]:
        return list(self._history)

    def add_rule(self, rule: Rule) -> Rule:
        self._validate(rule)
        if rule.rule_id in self._rules:
            raise ValueError(f"规则 {rule.rule_id} 已存在；内容变更须发新版本（新 rule_id）")
        self._rules[rule.rule_id] = rule
        self._history.append({"event": "rule_published", "rule_id": rule.rule_id,
                              "version": rule.version, "fingerprint": rule.fingerprint,
                              "canary": rule.canary, "at": rule.effective_from})
        return rule

    def withdraw(self, rule_id: str, at: str, reason: str) -> None:
        """撤回立即生效：之后的评估（含历史记录重评）不得再使用该规则。"""
        if rule_id not in self._rules:
            raise ValueError(f"未知规则: {rule_id}")
        parse_iso(at)
        if not reason.strip():
            raise ValueError("撤回必须留痕原因")
        self._withdrawn[rule_id] = (at, reason)
        self._history.append({"event": "rule_withdrawn", "rule_id": rule_id,
                              "at": at, "reason": reason})

    def is_withdrawn(self, rule_id: str, at: Optional[datetime] = None) -> bool:
        info = self._withdrawn.get(rule_id)
        if info is None:
            return False
        return at is None or at >= parse_iso(info[0])

    def get(self, rule_id: str) -> Rule:
        return self._rules[rule_id]

    def _validate(self, rule: Rule) -> None:
        if rule.indicator not in INDICATORS:
            raise ContentPolicyError(f"不支持的指标: {rule.indicator}")
        unknown_bands = set(rule.age_bands) - set(ALLOWED_AGE_BANDS)
        if unknown_bands:
            raise ContentPolicyError(f"未批准的年龄区间: {sorted(unknown_bands)}")
        unknown_sex = set(rule.sex_groups) - set(SEX_GROUPS)
        if unknown_sex:
            raise ContentPolicyError(f"未批准的性别分组: {sorted(unknown_sex)}")
        parse_iso(rule.effective_from)
        if rule.effective_to is not None:
            if parse_iso(rule.effective_to) <= parse_iso(rule.effective_from):
                raise ContentPolicyError("生效止期必须晚于起期")
        for v, pack in rule.packs.items():
            if v not in CLASSIFYING_VERDICTS:
                raise ContentPolicyError(f"规则只可为 {CLASSIFYING_VERDICTS} 配置文案，{v} 由服务统一兜底")
            if not isinstance(pack, SafetyPack):
                raise ContentPolicyError(f"文案 {v} 必须是 SafetyPack")
            for field_name, text in (
                ("headline", pack.headline),
                ("explanation", pack.explanation),
                ("recommendation", pack.recommendation),
            ):
                if not text.strip():
                    raise ContentPolicyError(f"安全文案[{v}.{field_name}]不能为空")
                assert_safe_text(text, where=f"安全文案[{v}.{field_name}]")
            if pack.evidence_level not in ("normal", "weak"):
                raise ContentPolicyError(f"文案 {v} 的证据等级非法")
        # 阈值完整性与顺序
        for band, by_sex in rule.thresholds.items():
            if band not in rule.age_bands:
                raise ContentPolicyError(f"阈值中的 {band} 不在声明年龄区间内")
            for sex, rng in by_sex.items():
                if sex not in rule.sex_groups:
                    raise ContentPolicyError(f"阈值中的性别分组 {sex} 未声明")
                if len(rng) != 2 or rng[0] >= rng[1]:
                    raise ContentPolicyError(f"{band}/{sex} 阈值必须为 [下限, 上限) 且下限<上限")
        self._validate_approvals(rule)

    def _validate_approvals(self, rule: Rule) -> None:
        by_role = {}
        for a in rule.approvals:
            if not isinstance(a, Approval):
                raise ContentPolicyError("批准记录类型错误")
            if a.role in by_role:
                raise ContentPolicyError(f"{a.role} 重复批准")
            if a.content_fingerprint != rule.fingerprint:
                raise ContentPolicyError(
                    f"{a.role} 批准的内容指纹与当前规则不一致：规则已在批准后被修改"
                )
            by_role[a.role] = a
        missing = [r for r in APPROVAL_ROLES if r not in by_role]
        if missing:
            raise ContentPolicyError(f"规则缺少会签: {missing}")
        # 批准时间不得晚于生效时间
        start = parse_iso(rule.effective_from)
        for a in rule.approvals:
            if parse_iso(a.approved_at) > start:
                raise ContentPolicyError(f"{a.role} 的批准时间晚于规则生效时间")

    def select(
        self,
        *,
        indicator: str,
        age_band: str,
        sex: str,
        model_id: str,
        at: datetime,
        canary_consent: bool,
    ) -> Optional[Rule]:
        """选择采集时点 ``at`` 适用的规则。

        非灰度规则优先；灰度规则仅在监护人明确同意时可被选中。
        命中多条时取版本号最高者。
        """
        candidates = []
        for rule in self._rules.values():
            if rule.rule_id in self._withdrawn:
                # 撤回是治理动作：对所有时点立即、永久失效（含历史补传与重评）。
                continue
            if rule.indicator != indicator or not rule.active_at(at):
                continue
            if age_band not in rule.age_bands:
                continue
            if sex not in rule.sex_groups and "any" not in rule.sex_groups:
                continue
            if rule.applies_to_models and model_id not in rule.applies_to_models:
                continue
            if rule.canary and not canary_consent:
                continue
            candidates.append(rule)
        if not candidates:
            return None
        # 灰度资格已在上面过滤；在全部合格规则中取最高版本。
        candidates.sort(key=lambda r: r.version)
        return candidates[-1]
