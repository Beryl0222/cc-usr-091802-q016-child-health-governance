"""领域模型：测量事件、规则版本、结论版本、同意记录与家庭通知。

所有时间统一为带时区的 datetime（UTC 归一化），有效期边界按天解释。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

# 规则生效前必须集齐的三方批准角色。
ROLES = ("medical", "privacy", "copywriting")

# 中性结论类别。禁止出现“偏重/肥胖”等标签性词汇，文案见 safety.py。
CATEGORY_BELOW = "below_reference"
CATEGORY_WITHIN = "within_reference"
CATEGORY_ABOVE = "above_reference"
CATEGORY_INSUFFICIENT = "insufficient_evidence"

# 结论版本的生成原因。
REASON_INITIAL = "initial"
REASON_UNIT_CORRECTION = "unit_correction"
REASON_BIRTHDAY_CORRECTION = "birthday_correction"
REASON_RULE_WITHDRAWAL = "rule_withdrawal"
REASON_GRAY_OPT_IN = "gray_opt_in"
REASON_GRAY_OPT_OUT = "gray_opt_out"

SCOPE_GRAY_RULES = "gray_rules"


def new_id() -> str:
    return uuid.uuid4().hex


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    """解析 ISO 时间；缺时区按 UTC 处理。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_boundary(value: str, *, end_of_day: bool) -> datetime:
    """解析有效期边界；纯日期按当天开始/结束（含当天）。"""
    if "T" in value:
        return parse_dt(value)
    day = date.fromisoformat(value)
    moment = time.max if end_of_day else time.min
    return datetime.combine(day, moment, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Approval:
    role: str
    approver: str
    approved_at: datetime


@dataclass
class Band:
    """某年龄区间内一个参考分段，[min_value, max_value)，None 表示开口。"""

    category: str
    min_value: float | None = None
    max_value: float | None = None

    def contains(self, value: float) -> bool:
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value >= self.max_value:
            return False
        return True


@dataclass
class RuleVersion:
    """一条规则的一个版本。只有三方批准齐全且在有效期内才可被采用。"""

    rule_id: str
    version: int
    metric: str
    valid_from: datetime
    valid_to: datetime | None  # 含边界当天；None 表示长期有效
    gray: bool
    reference: str
    approvals: dict[str, Approval]
    bands_by_age: dict[str, list[Band]]
    copy: dict[str, str]  # category -> 经文案批准的展示用语
    withdrawn: bool = False

    def approvals_complete(self) -> bool:
        return all(role in self.approvals for role in ROLES)

    def missing_roles(self) -> list[str]:
        return [role for role in ROLES if role not in self.approvals]

    def valid_at(self, moment: datetime) -> bool:
        if moment < self.valid_from:
            return False
        if self.valid_to is not None and moment > self.valid_to:
            return False
        return True


@dataclass
class MeasurementEvent:
    """一次设备测量。measured_at 是采集时间，离线补传时早于 received_at。"""

    event_id: str
    child_id: str
    device_model: str
    measured_at: datetime
    received_at: datetime
    posture: str
    height_value: float | None = None
    height_unit: str | None = None
    weight_value: float | None = None
    weight_unit: str | None = None
    reported_age_band: str | None = None


@dataclass
class ChildProfile:
    child_id: str
    birth_date: date | None = None
    guardian_id: str | None = None


@dataclass
class ConsentRecord:
    child_id: str
    scope: str
    status: str  # granted / revoked
    guardian_id: str
    updated_at: datetime


@dataclass
class Check:
    """一项校准/可信度检查的结果，面向家长可读。"""

    name: str
    passed: bool
    detail: str


@dataclass
class Conclusion:
    """一次测量的一条结论版本。旧版本永不删除，只被取代并停止展示。"""

    conclusion_id: str
    event_id: str
    child_id: str
    version_no: int
    created_at: datetime
    reason: str
    category: str
    category_text: str
    advice: str
    checks: list[Check]
    input_snapshot: dict
    age_band: str | None = None
    age_band_source: str = "unknown"
    rule_id: str | None = None
    rule_version: int | None = None
    gray_used: bool = False
    metric_value: float | None = None
    band: dict | None = None
    copy_blocked: bool = False  # 规则文案触碰安全红线，已拦截
    status: str = "current"  # current / superseded


@dataclass
class Notification:
    notification_id: str
    child_id: str
    kind: str
    message: str
    event_ids: list[str]
    created_at: datetime
