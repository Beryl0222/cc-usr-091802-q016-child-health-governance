"""面向家庭文案的安全红线。

任何展示给家长/孩子的文字（结论用语、建议、通知）都不得：
- 构成诊断或暗示疾病；
- 使用羞辱性标签（如“偏重”“肥胖”）；
- 借焦虑推销付费内容。

规则自带的文案在每次评估时都会经过这里的检查；触碰红线则
本次结论降级为“证据不足”，绝不放行。
"""

from __future__ import annotations

FORBIDDEN_PATTERNS = (
    # 诊断暗示
    "诊断",
    "确诊",
    "疾病",
    "病症",
    # 羞辱性标签
    "肥胖",
    "超重",
    "偏胖",
    "偏重",
    "偏瘦",
    "瘦小",
    "减肥",
    "减重",
    "瘦身",
    # 焦虑营销 / 付费导流
    "付费",
    "会员",
    "VIP",
    "vip",
    "课程",
    "购买",
    "限时",
    "优惠",
    "抢购",
    "立即咨询",
    # 英文等价词
    "overweight",
    "obese",
    "obesity",
    "diagnosis",
)


class SafetyViolation(ValueError):
    """文案触碰安全红线。"""


def find_violation(text: str) -> str | None:
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in text:
            return pattern
    return None


def assert_family_safe(text: str) -> None:
    violation = find_violation(text)
    if violation is not None:
        raise SafetyViolation(f"文案包含禁用内容: {violation!r}")
