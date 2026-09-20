"""家长视图：让一次记录的数据来源、适用范围、判断理由与修订经过可读。

旧版本只展示“何时因何原因被取代”的元信息，不再展示旧结论内容
（对应“停止展示旧结果”）。
"""

from __future__ import annotations

from datetime import timedelta

from .models import (
    CATEGORY_INSUFFICIENT,
    REASON_BIRTHDAY_CORRECTION,
    REASON_GRAY_OPT_IN,
    REASON_GRAY_OPT_OUT,
    REASON_INITIAL,
    REASON_RULE_WITHDRAWAL,
    REASON_UNIT_CORRECTION,
    Conclusion,
    iso,
)
from .store import Store

REASON_TEXT = {
    REASON_INITIAL: "首次评估",
    REASON_UNIT_CORRECTION: "单位修正",
    REASON_BIRTHDAY_CORRECTION: "生日信息修正",
    REASON_RULE_WITHDRAWAL: "规则撤回",
    REASON_GRAY_OPT_IN: "监护人同意参与灰度",
    REASON_GRAY_OPT_OUT: "监护人退出灰度",
}

STATUS_TEXT = {
    "current": "当前展示版本",
    "superseded": "已停止展示（被新版本取代）",
}

AGE_BAND_SOURCE_TEXT = {
    "birth_date": "根据生日计算",
    "reported": "设备上报",
    "unknown": "未知",
}

DISCLAIMER = "本结果仅供参考，不构成医学意见。"

OFFLINE_BACKFILL_THRESHOLD = timedelta(hours=24)


def _data_source(event) -> dict:
    return {
        "device_model": event.device_model,
        "posture": event.posture,
        "measured_at": iso(event.measured_at),
        "received_at": iso(event.received_at),
        "offline_backfill": (event.received_at - event.measured_at) >= OFFLINE_BACKFILL_THRESHOLD,
        "units_reported": {"height": event.height_unit, "weight": event.weight_unit},
    }


def _scope(store: Store, conclusion: Conclusion) -> tuple[dict | None, str | None]:
    if conclusion.rule_id is None:
        return None, "没有适用于本次测量的已批准规则，因此未给出结论。"
    rule = store.rules.get((conclusion.rule_id, conclusion.rule_version))
    if rule is None:
        return None, "结论所依据的规则信息缺失，已停止展示。"
    scope = {
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "reference": rule.reference,
        "gray": rule.gray,
        "valid_from": iso(rule.valid_from),
        "valid_to": iso(rule.valid_to) if rule.valid_to else None,
        "age_band": conclusion.age_band,
        "approvals": sorted(rule.approvals.keys()),
        "approval_note": "该规则已经医学、隐私与文案三方共同批准，且在采集时处于有效期内。",
        "withdrawn": rule.withdrawn,
    }
    note = "该规则版本已被撤回，结论已按现行规则重新评估。" if rule.withdrawn else None
    return scope, note


def _reasoning(conclusion: Conclusion) -> dict:
    return {
        "metric": "bmi_for_age",
        "metric_text": "身体质量指数（BMI，按年龄区间参考）",
        "value": conclusion.metric_value,
        "band": conclusion.band,
        "age_band": conclusion.age_band,
        "age_band_source": AGE_BAND_SOURCE_TEXT.get(conclusion.age_band_source, "未知"),
        "checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail}
            for check in conclusion.checks
        ],
        "normalized": {
            "height_cm": conclusion.input_snapshot.get("height_cm"),
            "weight_kg": conclusion.input_snapshot.get("weight_kg"),
            "bmi": conclusion.input_snapshot.get("bmi"),
        },
    }


def _current_view(store: Store, conclusion: Conclusion) -> dict:
    scope, scope_note = _scope(store, conclusion)
    view = {
        "version_no": conclusion.version_no,
        "created_at": iso(conclusion.created_at),
        "reason": conclusion_reason_text(conclusion),
        "category": conclusion.category,
        "category_text": conclusion.category_text,
        "advice": conclusion.advice,
        "scope": scope,
        "reasoning": _reasoning(conclusion),
    }
    if scope_note:
        view["scope_note"] = scope_note
    if conclusion.copy_blocked:
        view["copy_note"] = "原规则文案未通过安全审查，本次仅给出中性建议。"
    if conclusion.category == CATEGORY_INSUFFICIENT:
        view["insufficient_note"] = "证据不足时不形成结论，仅建议重新测量或咨询专业人员。"
    return view


def conclusion_history(store: Store, event_id: str) -> list[dict]:
    """修订经过：仅元信息，不含旧版本的结论内容。"""
    history = []
    for conclusion in store.conclusions.get(event_id, []):
        history.append(
            {
                "version_no": conclusion.version_no,
                "created_at": iso(conclusion.created_at),
                "reason": conclusion_reason_text(conclusion),
                "status": STATUS_TEXT.get(conclusion.status, conclusion.status),
            }
        )
    return history


def conclusion_reason_text(conclusion: Conclusion) -> str:
    base = REASON_TEXT.get(conclusion.reason, conclusion.reason)
    return f"{base}（{conclusion.reason}）" if conclusion.reason not in REASON_TEXT else base


def parent_view(store: Store, event_id: str) -> dict | None:
    event = store.events.get(event_id)
    if event is None:
        return None
    current = store.current_conclusion(event_id)
    view = {
        "event_id": event.event_id,
        "child_id": event.child_id,
        "data_source": _data_source(event),
        "current": _current_view(store, current) if current else None,
        "history": conclusion_history(store, event_id),
        "disclaimer": DISCLAIMER,
    }
    return view
