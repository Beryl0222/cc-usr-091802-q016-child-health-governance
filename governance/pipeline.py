"""评估流水线：校准与可信度检查 -> 规则选择 -> 中性结论 -> 版本治理。

设计原则：
- 证据不足时只给中性建议（重新测量 / 咨询专业人员），绝不下诊断、
  绝不贴标签、绝不推销付费内容；
- 规则按采集时间（measured_at）选择，离线补传仍按采集时规则解释；
- 单位修正、生日修正、规则撤回、灰度同意变化都会生成新的结论版本，
  旧版本停止展示并通知受影响家庭。
"""

from __future__ import annotations

from datetime import date, datetime

from .models import (
    CATEGORY_ABOVE,
    CATEGORY_BELOW,
    CATEGORY_INSUFFICIENT,
    CATEGORY_WITHIN,
    REASON_BIRTHDAY_CORRECTION,
    REASON_GRAY_OPT_IN,
    REASON_GRAY_OPT_OUT,
    REASON_INITIAL,
    REASON_RULE_WITHDRAWAL,
    REASON_UNIT_CORRECTION,
    SCOPE_GRAY_RULES,
    Check,
    Conclusion,
    ConsentRecord,
    MeasurementEvent,
    Notification,
    RuleVersion,
    iso,
    new_id,
)
from .rules import select_rule
from .safety import assert_family_safe
from .store import Store

METRIC_BMI_FOR_AGE = "bmi_for_age"

# 单位归一化（含中文环境常见的“斤”）。
HEIGHT_UNIT_TO_CM = {"cm": 1.0, "m": 100.0, "mm": 0.1}
WEIGHT_UNIT_TO_KG = {"kg": 1.0, "g": 0.001, "jin": 0.5, "lb": 0.45359237}

# 采集姿态与测量项目的兼容关系。
POSTURE_COMPATIBLE = {
    "standing_still": {"height", "weight"},
    "lying": {"height"},
    "sitting": set(),
    "moving": set(),
    "worn_on_wrist": set(),
}

# 儿童生理合理范围（超出即视为误读，不用于判断）。
PLAUSIBLE_HEIGHT_CM = (40.0, 210.0)
PLAUSIBLE_WEIGHT_KG = (1.5, 200.0)
PLAUSIBLE_BMI = (8.0, 60.0)

AGE_BANDS = ("3-5", "6-9", "10-12", "13-15", "16-18")

# 中性文案模板（本服务自身输出，规则文案另经安全红线检查）。
INSUFFICIENT_TEXT = "本次数据不足以给出可靠结论。"
INSUFFICIENT_ADVICE = "建议按正确姿势重新测量；如仍有疑问，请咨询专业人员。"
WITHIN_ADVICE = "请保持规律的测量习惯。"
BAND_ADVICE = "建议择日重新测量确认；如持续如此或您有疑虑，请咨询专业人员。"

# 规则文案缺失时的兜底中性用语。
CATEGORY_TEXT_FALLBACK = {
    CATEGORY_BELOW: "本次测量结果低于参考范围。",
    CATEGORY_WITHIN: "本次测量结果在参考范围内。",
    CATEGORY_ABOVE: "本次测量结果高于参考范围。",
}

NOTIFY_MESSAGES = {
    REASON_UNIT_CORRECTION: "您孩子的{n}条测量结论因单位修正已更新，请查看最新说明。",
    REASON_BIRTHDAY_CORRECTION: "您孩子的{n}条测量结论因生日信息修正已更新，请查看最新说明。",
    REASON_RULE_WITHDRAWAL: "您孩子的{n}条测量结论所依据的规则已撤回，结论已重新评估，请查看最新说明。",
    REASON_GRAY_OPT_IN: "您孩子的{n}条测量结论已按灰度同意设置重新评估，请查看最新说明。",
    REASON_GRAY_OPT_OUT: "您孩子的{n}条测量结论已按灰度同意设置重新评估，请查看最新说明。",
}


class NotFoundError(KeyError):
    """对象不存在。"""


class ConflictError(ValueError):
    """对象冲突（如重复上报）。"""


# ---------------------------------------------------------------- 基础换算


def age_years(birth: date, at: datetime) -> int:
    day = at.date()
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def band_for_age(years: int) -> str | None:
    if 3 <= years <= 5:
        return "3-5"
    if 6 <= years <= 9:
        return "6-9"
    if 10 <= years <= 12:
        return "10-12"
    if 13 <= years <= 15:
        return "13-15"
    if 16 <= years <= 18:
        return "16-18"
    return None


def normalize_measurements(event: MeasurementEvent):
    """把上报值换算为标准单位；返回 (height_cm, weight_kg, 未知单位列表)。"""
    height_cm = weight_kg = None
    unknown: list[str] = []
    if event.height_value is not None:
        factor = HEIGHT_UNIT_TO_CM.get((event.height_unit or "").lower())
        if factor is None:
            unknown.append(f"height:{event.height_unit or '缺失'}")
        else:
            height_cm = event.height_value * factor
    if event.weight_value is not None:
        factor = WEIGHT_UNIT_TO_KG.get((event.weight_unit or "").lower())
        if factor is None:
            unknown.append(f"weight:{event.weight_unit or '缺失'}")
        else:
            weight_kg = event.weight_value * factor
    return height_cm, weight_kg, unknown


def resolve_age_band(store: Store, event: MeasurementEvent):
    """优先按生日推算年龄区间；否则采用设备上报区间。返回 (区间, 来源)。"""
    child = store.children.get(event.child_id)
    if child and child.birth_date:
        return band_for_age(age_years(child.birth_date, event.measured_at)), "birth_date"
    if event.reported_age_band in AGE_BANDS:
        return event.reported_age_band, "reported"
    return None, "unknown"


# ---------------------------------------------------------------- 可信度检查


def run_checks(store: Store, event: MeasurementEvent, height_cm, weight_kg, unknown_units) -> list[Check]:
    checks: list[Check] = []

    complete = event.height_value is not None and event.weight_value is not None
    checks.append(
        Check(
            "completeness",
            complete,
            "身高、体重数据完整" if complete else "缺少身高或体重数据",
        )
    )

    checks.append(
        Check(
            "units",
            not unknown_units,
            "单位可识别并已换算为标准单位"
            if not unknown_units
            else "存在无法识别的单位：" + "、".join(unknown_units),
        )
    )

    device = store.devices.get(event.device_model)
    calibrated = device is not None and {"height", "weight"} <= set(device.get("calibrated_metrics", []))
    checks.append(
        Check(
            "device",
            calibrated,
            "设备型号已完成身高体重校准登记"
            if calibrated
            else "设备型号未登记或未针对身高体重测量完成校准",
        )
    )

    compatible = {"height", "weight"} <= POSTURE_COMPATIBLE.get(event.posture, set())
    checks.append(
        Check(
            "posture",
            compatible,
            "采集姿态适用于本次测量"
            if compatible
            else f"采集姿态（{event.posture}）不适用于身高体重测量",
        )
    )

    plausible = True
    if height_cm is not None and not PLAUSIBLE_HEIGHT_CM[0] <= height_cm <= PLAUSIBLE_HEIGHT_CM[1]:
        plausible = False
    if weight_kg is not None and not PLAUSIBLE_WEIGHT_KG[0] <= weight_kg <= PLAUSIBLE_WEIGHT_KG[1]:
        plausible = False
    if plausible and height_cm and weight_kg:
        bmi = weight_kg / (height_cm / 100.0) ** 2
        if not PLAUSIBLE_BMI[0] <= bmi <= PLAUSIBLE_BMI[1]:
            plausible = False
    checks.append(
        Check(
            "plausibility",
            plausible,
            "测量值在生理合理范围内" if plausible else "测量值超出合理范围，可能为误读",
        )
    )
    return checks


# ---------------------------------------------------------------- 评估


def _find_band(rule: RuleVersion, age_band: str, bmi: float):
    for band in rule.bands_by_age.get(age_band, []):
        if band.contains(bmi):
            return band
    return None


def build_conclusion(
    store: Store,
    event: MeasurementEvent,
    *,
    now: datetime,
    reason: str,
    version_no: int,
) -> Conclusion:
    """对一次测量执行完整流水线，产出一条结论版本（不落库）。"""
    height_cm, weight_kg, unknown_units = normalize_measurements(event)
    checks = run_checks(store, event, height_cm, weight_kg, unknown_units)
    credible = all(check.passed for check in checks)
    age_band, age_source = resolve_age_band(store, event)
    bmi = weight_kg / (height_cm / 100.0) ** 2 if height_cm and weight_kg else None

    snapshot = {
        "device_model": event.device_model,
        "posture": event.posture,
        "measured_at": iso(event.measured_at),
        "received_at": iso(event.received_at),
        "height_cm": round(height_cm, 2) if height_cm is not None else None,
        "weight_kg": round(weight_kg, 2) if weight_kg is not None else None,
        "bmi": round(bmi, 1) if bmi is not None else None,
        "height_unit_raw": event.height_unit,
        "weight_unit_raw": event.weight_unit,
        "age_band": age_band,
        "age_band_source": age_source,
    }

    rule: RuleVersion | None = None
    band_used = None
    category = CATEGORY_INSUFFICIENT
    category_text = INSUFFICIENT_TEXT
    advice = INSUFFICIENT_ADVICE
    metric_value = None
    copy_blocked = False

    if credible and age_band is not None and bmi is not None:
        rule = select_rule(
            store,
            metric=METRIC_BMI_FOR_AGE,
            age_band=age_band,
            measured_at=event.measured_at,
            gray_allowed=store.gray_consent_granted(event.child_id),
        )
        if rule is not None:
            band_used = _find_band(rule, age_band, bmi)
            if band_used is not None:
                raw_text = rule.copy.get(band_used.category) or CATEGORY_TEXT_FALLBACK[band_used.category]
                raw_advice = WITHIN_ADVICE if band_used.category == CATEGORY_WITHIN else BAND_ADVICE
                try:
                    assert_family_safe(raw_text)
                    assert_family_safe(raw_advice)
                except ValueError:
                    # 规则文案触碰红线：拦截，降级为证据不足。
                    copy_blocked = True
                else:
                    category = band_used.category
                    category_text = raw_text
                    advice = raw_advice
                    metric_value = round(bmi, 1)

    return Conclusion(
        conclusion_id=new_id(),
        event_id=event.event_id,
        child_id=event.child_id,
        version_no=version_no,
        created_at=now,
        reason=reason,
        category=category,
        category_text=category_text,
        advice=advice,
        checks=checks,
        input_snapshot=snapshot,
        age_band=age_band,
        age_band_source=age_source,
        rule_id=rule.rule_id if rule else None,
        rule_version=rule.version if rule else None,
        gray_used=bool(rule and rule.gray),
        metric_value=metric_value,
        band=(
            {"category": band_used.category, "min": band_used.min_value, "max": band_used.max_value}
            if band_used
            else None
        ),
        copy_blocked=copy_blocked,
    )


def _signature(conclusion: Conclusion) -> tuple:
    """结论内容签名：未发生变化时不产生新版本。"""
    return (
        conclusion.category,
        conclusion.rule_id,
        conclusion.rule_version,
        conclusion.gray_used,
        conclusion.metric_value,
        conclusion.age_band,
        conclusion.category_text,
        conclusion.advice,
        conclusion.copy_blocked,
        tuple((check.name, check.passed) for check in conclusion.checks),
        conclusion.input_snapshot.get("height_cm"),
        conclusion.input_snapshot.get("weight_kg"),
    )


# ---------------------------------------------------------------- 对外操作


def ingest_event(store: Store, event: MeasurementEvent, *, now: datetime) -> Conclusion:
    """接收一次测量（含离线补传），生成首个结论版本。"""
    if event.event_id in store.events:
        raise ConflictError(f"测量事件已存在: {event.event_id}")
    store.ensure_child(event.child_id)
    store.events[event.event_id] = event
    conclusion = build_conclusion(store, event, now=now, reason=REASON_INITIAL, version_no=1)
    store.add_conclusion(conclusion)
    return conclusion


def reevaluate_event(store: Store, event: MeasurementEvent, *, now: datetime, reason: str) -> Conclusion | None:
    """重新评估一次测量；内容有变化才生成新版本，否则返回 None。"""
    current = store.current_conclusion(event.event_id)
    version_no = current.version_no + 1 if current else 1
    candidate = build_conclusion(store, event, now=now, reason=reason, version_no=version_no)
    if current is not None and _signature(current) == _signature(candidate):
        return None
    store.add_conclusion(candidate)
    return candidate


def _notify(store: Store, child_id: str, reason: str, event_ids: list[str], now: datetime) -> None:
    message = NOTIFY_MESSAGES[reason].format(n=len(event_ids))
    assert_family_safe(message)
    store.add_notification(
        Notification(
            notification_id=new_id(),
            child_id=child_id,
            kind=reason,
            message=message,
            event_ids=list(event_ids),
            created_at=now,
        )
    )


def apply_unit_correction(
    store: Store,
    event_id: str,
    *,
    height_unit: str | None = None,
    weight_unit: str | None = None,
    now: datetime,
) -> Conclusion | None:
    """修正上报单位并重新评估；有变化则生成新版本并通知家庭。"""
    event = store.events.get(event_id)
    if event is None:
        raise NotFoundError(event_id)
    if height_unit:
        event.height_unit = height_unit
    if weight_unit:
        event.weight_unit = weight_unit
    conclusion = reevaluate_event(store, event, now=now, reason=REASON_UNIT_CORRECTION)
    if conclusion is not None:
        _notify(store, event.child_id, REASON_UNIT_CORRECTION, [event.event_id], now)
    return conclusion


def apply_birthday_correction(store: Store, child_id: str, *, birth_date: date, now: datetime) -> list[str]:
    """修正生日：重估该儿童全部测量，返回发生变化的 event_id 列表。"""
    child = store.ensure_child(child_id)
    child.birth_date = birth_date
    changed = []
    for event in store.events_of_child(child_id):
        if reevaluate_event(store, event, now=now, reason=REASON_BIRTHDAY_CORRECTION) is not None:
            changed.append(event.event_id)
    if changed:
        _notify(store, child_id, REASON_BIRTHDAY_CORRECTION, changed, now)
    return changed


def withdraw_rule(store: Store, rule_id: str, version: int, *, now: datetime) -> dict[str, list[str]]:
    """撤回规则版本：受影响结论全部重估，旧结果停止展示，逐家庭通知。"""
    rule = store.rules.get((rule_id, version))
    if rule is None:
        raise NotFoundError(f"{rule_id} v{version}")
    rule.withdrawn = True
    changed_by_child: dict[str, list[str]] = {}
    for event in store.events.values():
        current = store.current_conclusion(event.event_id)
        if current is None or (current.rule_id, current.rule_version) != (rule_id, version):
            continue
        if reevaluate_event(store, event, now=now, reason=REASON_RULE_WITHDRAWAL) is not None:
            changed_by_child.setdefault(event.child_id, []).append(event.event_id)
    for child_id, event_ids in changed_by_child.items():
        _notify(store, child_id, REASON_RULE_WITHDRAWAL, event_ids, now)
    return changed_by_child


def set_gray_consent(
    store: Store,
    child_id: str,
    *,
    guardian_id: str,
    action: str,
    now: datetime,
) -> list[str]:
    """监护人授予/退出灰度同意；退出后立即按稳定版规则重估。"""
    if action not in ("grant", "revoke"):
        raise ValueError("action 必须是 grant 或 revoke")
    store.ensure_child(child_id).guardian_id = guardian_id
    store.consents[(child_id, SCOPE_GRAY_RULES)] = ConsentRecord(
        child_id=child_id,
        scope=SCOPE_GRAY_RULES,
        status="granted" if action == "grant" else "revoked",
        guardian_id=guardian_id,
        updated_at=now,
    )
    reason = REASON_GRAY_OPT_IN if action == "grant" else REASON_GRAY_OPT_OUT
    changed = []
    for event in store.events_of_child(child_id):
        current = store.current_conclusion(event.event_id)
        if current is None:
            continue
        if action == "revoke" and not current.gray_used:
            continue  # 未使用灰度规则的结论不受退出影响
        if reevaluate_event(store, event, now=now, reason=reason) is not None:
            changed.append(event.event_id)
    if changed:
        _notify(store, child_id, reason, changed, now)
    return changed
