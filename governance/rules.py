"""规则登记与选择。

硬性约束（对应“只能采用已共同批准且在有效期内的规则”）：
1. 医学、隐私、文案三方批准齐全，且批准时间不晚于有效期开始；
2. 采集时间（measured_at，而非上传时间）落在有效期内；
3. 未撤回；
4. 覆盖孩子所在年龄区间；
5. 灰度规则仅在监护人明确同意且未退出时可选。
"""

from __future__ import annotations

from datetime import datetime

from .models import Approval, Band, RuleVersion, parse_boundary, parse_dt
from .store import Store


def select_rule(
    store: Store,
    *,
    metric: str,
    age_band: str,
    measured_at: datetime,
    gray_allowed: bool,
) -> RuleVersion | None:
    """按采集时间选择可用的最高版本规则；无匹配则返回 None（证据不足）。"""
    best: RuleVersion | None = None
    for rule in store.rules.values():
        if rule.metric != metric or rule.withdrawn:
            continue
        if approval_issues(rule):
            continue
        if not rule.valid_at(measured_at):
            continue
        if age_band not in rule.bands_by_age:
            continue
        if rule.gray and not gray_allowed:
            continue
        if best is None or rule.version > best.version:
            best = rule
    return best


def rule_from_dict(data: dict) -> RuleVersion:
    """从 JSON 字典构造规则版本（种子文件与创建接口共用）。"""
    approvals = {}
    for item in data.get("approvals", []):
        approval = Approval(
            role=item["role"],
            approver=item["approver"],
            approved_at=parse_dt(item["approved_at"]),
        )
        approvals[approval.role] = approval
    bands_by_age = {
        age_band: [
            Band(
                category=band["category"],
                min_value=band.get("min"),
                max_value=band.get("max"),
            )
            for band in bands
        ]
        for age_band, bands in data["bands_by_age"].items()
    }
    valid_to = data.get("valid_to")
    return RuleVersion(
        rule_id=data["rule_id"],
        version=int(data["version"]),
        metric=data["metric"],
        valid_from=parse_boundary(data["valid_from"], end_of_day=False),
        valid_to=parse_boundary(valid_to, end_of_day=True) if valid_to else None,
        gray=bool(data.get("gray", False)),
        reference=data["reference"],
        approvals=approvals,
        bands_by_age=bands_by_age,
        copy=data.get("copy", {}),
    )


def approval_issues(rule: RuleVersion) -> list[str]:
    """返回规则暂不可用的原因列表（空列表表示可用）。"""
    issues = [f"缺少{role}批准" for role in rule.missing_roles()]
    for role, approval in rule.approvals.items():
        if approval.approved_at > rule.valid_from:
            issues.append(f"{role}批准时间晚于有效期开始")
    return issues
