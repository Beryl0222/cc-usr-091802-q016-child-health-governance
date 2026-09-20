"""领域核心：时间、身份与三方会签的安全文案。

本模块不做任何网络或持久化操作，所有判断都可在测试中复现。
时间统一使用服务端 UTC ISO-8601（带 Z 后缀）；设备时钟不可信，
采集时点以服务端接收时记录的 ``observed_at`` 为准（见 engine 模块）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

# 三个相互独立的批准角色：医学、隐私、文案。任一缺失即未会签。
APPROVAL_ROLES = ("medical", "privacy", "copy")
ROLE_LABELS_ZH = {
    "medical": "医学负责人",
    "privacy": "隐私负责人",
    "copy": "文案负责人",
}

# 文案红线：诊断性、羞辱性、付费引导。命中任意一条，规则不得入库生效。
FORBIDDEN_PATTERNS = (
    r"确诊",
    r"诊断(?!性)",  # “诊断书”等名词允许，但“诊断为肥胖”不允许
    r"患有",
    r"疾病",
    r"肥胖症",
    r"你(太|很|真|好)?胖",
    r"胖子",
    r"肥仔",
    r"减肥(产品|课|营|药|服务|中心)",
    r"付费|收费|订阅|下单|购买|优惠|促销|会员|链接购买",
    r"再不.{0,6}就",
    r"丢人|羞耻|难看|没人喜欢|嘲笑|笑话|被同学笑",
)
_FORBIDDEN_REGEXES = tuple(re.compile(p) for p in FORBIDDEN_PATTERNS)

# 结论只允许落在这几个中性分类上。
ALLOWED_VERDICTS = (
    "within_range",        # 在参考范围内
    "needs_attention",     # 建议关注（中性，不等于疾病）
    "insufficient_evidence",  # 证据不足，不出具分类结论
)

# 允许的年龄区间（整岁，闭区间）。年龄必须来自监护人维护的生日，而非设备自报。
ALLOWED_AGE_BANDS = ("0-2", "3-5", "6-9", "10-12", "13-15", "16-17")


def utcnow() -> datetime:
    """服务端当前 UTC 时间。测试可通过引擎注入时钟替换。"""
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    """规范化为 ...Z 形式，避免 +00:00 与 Z 混用导致字符串比对歧义。"""
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def canonical_json(payload: Any) -> str:
    """以稳定键序序列化，作为指纹输入。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_fingerprint(payload: Mapping[str, Any]) -> str:
    """规则逻辑与文案的 SHA-256 指纹；内容变更必产生新指纹。"""
    material = canonical_json(payload)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    """单个角色的批准记录。姓名留痕但不展示给家长。"""

    role: str
    approver_id: str
    approved_at: str
    content_fingerprint: str  # 批准时看到的内容指纹，防止批准后被篡改
    note: str = ""

    def __post_init__(self):
        if self.role not in APPROVAL_ROLES:
            raise ValueError(f"未知批准角色: {self.role}")
        if not self.approver_id:
            raise ValueError("批准人不能为空")
        parse_iso(self.approved_at)


@dataclass(frozen=True)
class SafetyPack:
    """随每条结论一起下发的安全文案与建议，本身也须通过红线检查。"""

    headline: str                 # 一句话中性标题
    explanation: str              # 判断理由（人话）
    recommendation: str           # 行动建议
    evidence_level: str = "normal"  # normal | weak -> 建议复测/咨询

    def as_dict(self) -> dict[str, str]:
        return {
            "headline": self.headline,
            "explanation": self.explanation,
            "recommendation": self.recommendation,
            "evidence_level": self.evidence_level,
        }


class ContentPolicyError(ValueError):
    """文案或结构违反安全策略。"""


def assert_safe_text(text: str, *, where: str) -> None:
    """红线词检查。诊断与羞辱、付费引导一律拒绝。"""
    for rx in _FORBIDDEN_REGEXES:
        m = rx.search(text)
        if m:
            raise ContentPolicyError(f"{where} 命中禁用表达: {m.group(0)!r}")


def validate_safety_pack(pack: Mapping[str, Any]) -> SafetyPack:
    fields_required = ("headline", "explanation", "recommendation")
    for f in fields_required:
        if not isinstance(pack.get(f), str) or not pack[f].strip():
            raise ContentPolicyError(f"安全文案缺少字段: {f}")
        assert_safe_text(pack[f], where=f"安全文案[{f}]")
    level = pack.get("evidence_level", "normal")
    if level not in ("normal", "weak"):
        raise ContentPolicyError(f"未知证据等级: {level}")
    return SafetyPack(
        headline=pack["headline"].strip(),
        explanation=pack["explanation"].strip(),
        recommendation=pack["recommendation"].strip(),
        evidence_level=level,
    )


@dataclass(frozen=True)
class Person:
    """被测量儿童：年龄来自监护人维护的生日（设备自报年龄不被采信）。"""

    person_id: str
    birth_date: str  # YYYY-MM-DD

    def age_years_on(self, on_date: str) -> int:
        """以采集日（UTC 日期）计算整岁。"""
        y, m, d = (int(x) for x in on_date[:10].split("-"))
        by, bm, bd = (int(x) for x in self.birth_date.split("-"))
        age = y - by - ((m, d) < (bm, bd))
        if age < 0 or age > 120:
            raise ValueError("生日不合理")
        return age

    def age_band_on(self, on_date: str) -> str:
        age = self.age_years_on(on_date)
        for band in ALLOWED_AGE_BANDS:
            lo, hi = (int(x) for x in band.split("-"))
            if lo <= age <= hi:
                return band
        raise ContentPolicyError(f"年龄 {age} 不在任何已批准适用区间内")


@dataclass(frozen=True)
class DeviceModel:
    """设备型号及其校准参数（由质量流程维护，不由请求方传入）。"""

    model_id: str
    display_name: str
    measurement_modes: frozenset[str] = field(default_factory=lambda: frozenset({"manual", "auto"}))
    # 测量类型 -> 姿态 -> 采集方式 -> (乘数, 加量)
    # calibrated_si = raw_si * mul + add；缺省 ("all") 为不修正。
    # 校准必须同时区分测量类型、姿态与采集方式，避免把某传感器在某模式下
    # 的偏差错误地施加到其它读数上（这正是误标“偏重”类事故的常见来源）。
    calibration: Mapping[str, Mapping[str, Mapping[str, tuple[float, float]]]] = field(
        default_factory=dict
    )
    retired: bool = False
