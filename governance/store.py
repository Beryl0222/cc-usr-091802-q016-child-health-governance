"""内存仓储。

当前进程内保存全部状态，便于测试与演示；生产部署时可替换为
持久化实现，领域逻辑（pipeline/explain/analytics）不依赖本模块细节。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    SCOPE_GRAY_RULES,
    ChildProfile,
    Conclusion,
    ConsentRecord,
    MeasurementEvent,
    Notification,
    RuleVersion,
)


@dataclass
class Store:
    rules: dict[tuple[str, int], RuleVersion] = field(default_factory=dict)
    events: dict[str, MeasurementEvent] = field(default_factory=dict)
    children: dict[str, ChildProfile] = field(default_factory=dict)
    consents: dict[tuple[str, str], ConsentRecord] = field(default_factory=dict)
    conclusions: dict[str, list[Conclusion]] = field(default_factory=dict)  # event_id -> 版本序列
    notifications: dict[str, list[Notification]] = field(default_factory=dict)  # child_id -> 列表
    devices: dict[str, dict] = field(default_factory=dict)  # 设备型号 -> 校准登记信息

    # ---- 规则 ----

    def add_rule(self, rule: RuleVersion) -> None:
        key = (rule.rule_id, rule.version)
        if key in self.rules:
            raise ValueError(f"规则版本已存在: {rule.rule_id} v{rule.version}")
        self.rules[key] = rule

    # ---- 儿童档案 ----

    def ensure_child(self, child_id: str) -> ChildProfile:
        profile = self.children.get(child_id)
        if profile is None:
            profile = ChildProfile(child_id=child_id)
            self.children[child_id] = profile
        return profile

    # ---- 结论版本 ----

    def add_conclusion(self, conclusion: Conclusion) -> None:
        versions = self.conclusions.setdefault(conclusion.event_id, [])
        for old in versions:
            if old.status == "current":
                old.status = "superseded"
        versions.append(conclusion)

    def current_conclusion(self, event_id: str) -> Conclusion | None:
        for conclusion in reversed(self.conclusions.get(event_id, [])):
            if conclusion.status == "current":
                return conclusion
        return None

    def events_of_child(self, child_id: str) -> list[MeasurementEvent]:
        return [event for event in self.events.values() if event.child_id == child_id]

    # ---- 监护人同意 ----

    def gray_consent_granted(self, child_id: str) -> bool:
        record = self.consents.get((child_id, SCOPE_GRAY_RULES))
        return bool(record and record.status == "granted")

    # ---- 家庭通知 ----

    def add_notification(self, notification: Notification) -> None:
        self.notifications.setdefault(notification.child_id, []).append(notification)
