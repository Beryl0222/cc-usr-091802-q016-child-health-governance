"""评估引擎：校准 → 可信度 → 选用已会签规则 → 形成可解释结论。

结论以“记录 + 版本”方式管理：
- 首次评估产生 v1；
- 单位修正、生日修正、规则撤回、监护人退出灰度都会触发重评，
  产生新版本并通知家庭；旧版本立即停止向家庭展示；
- 离线补传以采集时点选择规则（规则不追溯、也不提前适用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Optional

from .calibration import (
    Reading,
    build_bmi_input,
    calibrate_reading,
)
from .models import (
    ALLOWED_VERDICTS,
    ContentPolicyError,
    Person,
    SafetyPack,
    assert_safe_text,
    isoformat,
    parse_iso,
)

# 服务级兜底文案：证据不足 / 没有适用的已批准规则时使用。
# 同样必须过红线检查（诊断、羞辱、付费引导均禁止）。
NEUTRAL_PACK = SafetyPack(
    headline="本次数据暂不足以形成参考结论",
    explanation="设备提交的数据未通过可信度检查，或当前没有适用于该年龄与设备的有效规则。"
    "为避免误判，本次不给出体重相关分类。",
    recommendation="建议在安静、姿势正确的条件下重新测量身高和体重；"
    "如对孩子的生长发育有疑虑，可咨询儿科或儿童保健专业人员。",
    evidence_level="weak",
)
for _t in NEUTRAL_PACK.as_dict().values():
    if isinstance(_t, str):
        assert_safe_text(_t, where="服务兜底文案")

# 设备时钟补传允许的最大回看窗口，超出则数据来源不可信。
BACKFILE_MAX_HOURS = 24 * 30

REVISION_REASONS = (
    "initial",
    "unit_correction",
    "birth_date_correction",
    "rule_withdrawn",
    "canary_optout",
    "backfill_reprocessing",
)


@dataclass
class MeasurementRecord:
    """一次测量提交：原始读数与来源留痕。"""

    record_id: str
    person_id: str
    model_id: str
    readings: list[Reading]
    collected_at: str          # 采集时点（灰度/补传时按它选规则）
    received_at: str           # 服务端接收时点
    time_source: str           # server_clock | device_clock_backfill
    sex: str
    backfill: bool = False


@dataclass
class Conclusion:
    """一次评估的不可变结论版本。"""

    record_id: str
    person_id: str
    version: int
    verdict: str
    indicator: Optional[str]
    indicator_value: Optional[float]  # BMI，保留 1 位
    rule_id: Optional[str]
    rule_version: Optional[int]
    rule_fingerprint: Optional[str]
    canary: bool
    canary_consent_snapshot: Optional[bool]
    headline: str
    explanation: str
    recommendation: str
    evidence_level: str
    observed_at: str           # 选规则所用的采集时点
    computed_at: str
    revision_reason: str
    supersedes_version: Optional[int]
    # 可解释性明细（家长视图直接使用）
    data_source: dict
    applicability: dict
    reliability: dict
    status: str = "active"     # active | superseded | withheld
    notification_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Notification:
    notification_id: str
    person_id: str
    record_id: str
    conclusion_version: int
    kind: str       # first_result | revised | withheld
    reason: str
    body: str
    created_at: str
    delivered: bool = False


def _reading_as_dict(r: Reading) -> dict:
    return {
        "measurement": r.measurement,
        "value": r.value,
        "unit": r.unit,
        "posture": r.posture,
        "capture_mode": r.capture_mode,
        "measured_at": r.measured_at,
    }


class Evaluator:
    """无状态评估逻辑；版本与通知由上层 Service 维护。"""

    def __init__(self, rulebook, devices, *, clock=None):
        self.rulebook = rulebook
        self.devices = devices
        self.clock = clock

    def now(self) -> datetime:
        return self.clock() if self.clock else __import__("governance.models", fromlist=["utcnow"]).utcnow()

    def resolve_observed_at(self, payload: Mapping, now: datetime) -> tuple[datetime, str, list[str]]:
        """确定采集时点。在线测量用服务端时钟；离线补传可用设备时钟但要受约束。

        时钟不可信（缺失、无法解析、未来时间、超出补传窗口）一律记为硬性问题，
        因为无法确定“采集时”应适用哪一版规则。
        """
        problems: list[str] = []
        if not payload.get("backfill"):
            return now, "server_clock", problems
        raw = payload.get("collected_at")
        if not raw:
            problems.append("离线补传缺少采集时间，无法按采集时规则解释")
            return now, "device_clock_backfill", problems
        try:
            collected = parse_iso(raw)
        except (ValueError, TypeError):
            problems.append("采集时间格式无法解析，无法确认采集时适用的规则版本")
            return now, "device_clock_backfill", problems
        if collected > now + timedelta(minutes=5):
            problems.append("采集时间晚于当前时间，设备时钟不可信")
            return now, "device_clock_backfill", problems
        if now - collected > timedelta(hours=BACKFILE_MAX_HOURS):
            problems.append(
                f"采集时间距今超过 {BACKFILE_MAX_HOURS // 24} 天，超出补传窗口"
            )
        return collected, "device_clock_backfill", problems

    def evaluate(
        self,
        record: MeasurementRecord,
        person: Person,
        *,
        canary_consented: bool,
        revision_reason: str,
        version: int,
        supersedes_version: Optional[int],
        extra_hard_problems: Optional[list[str]] = None,
    ) -> Conclusion:
        if revision_reason not in REVISION_REASONS:
            raise ValueError(f"未知修订原因: {revision_reason}")
        now = self.now()
        observed = parse_iso(record.collected_at)
        computed_at = isoformat(now)

        device = self.devices.get(record.model_id)
        if device is None or device.retired:
            return self._fallback(
                record, version, supersedes_version, revision_reason, computed_at,
                [f"设备型号 {record.model_id} 不受支持或已停用"], canary_consented,
            )

        cal_problems: list[str] = list(extra_hard_problems or [])
        calibrated = {}
        measured_times = {}
        for r in record.readings:
            cm = calibrate_reading(r, device)
            calibrated[r.measurement] = cm
            measured_times[r.measurement] = r.measured_at
            cal_problems.extend(cm.problems)

        age = person.age_years_on(record.collected_at[:10])
        try:
            age_band = person.age_band_on(record.collected_at[:10])
        except ContentPolicyError:
            return self._fallback(
                record, version, supersedes_version, revision_reason, computed_at,
                [f"年龄 {age} 岁不在任何已批准适用区间内"], canary_consented,
            )

        bmi_input, bmi_problems = build_bmi_input(
            calibrated, age_years=age, measured_times=measured_times
        )

        weak_signals: list[str] = []
        if any(r.capture_mode == "auto" for r in record.readings):
            weak_signals.append("含自动感应读数，建议在相同姿势下人工复测一次以确认")
        if record.time_source == "device_clock_backfill":
            weak_signals.append("数据来自离线补传，采集时点依据设备时钟")

        data_source = {
            "model_id": device.model_id,
            "model_name": device.display_name,
            "time_source": record.time_source,
            "collected_at": record.collected_at,
            "received_at": record.received_at,
            "raw_readings": [_reading_as_dict(r) for r in record.readings],
            "calibrated": {
                k: {
                    "si_value": round(v.si_value, 4) if v.si_value == v.si_value else None,
                    "posture": v.posture,
                }
                for k, v in calibrated.items()
            },
        }
        applicability = {
            "age_years": age,
            "age_band": age_band,
            "age_basis": f"依据监护人维护的生日 {person.birth_date} 计算",
            "sex": record.sex,
            "indicator": "bmi",
        }
        reliability = {
            "hard_problems": cal_problems + bmi_problems,
            "weak_signals": weak_signals,
            "evidence_level": "normal",
        }

        if reliability["hard_problems"]:
            return self._fallback(
                record, version, supersedes_version, revision_reason, computed_at,
                reliability["hard_problems"], canary_consented,
                data_source=data_source, applicability=applicability,
                weak=weak_signals,
            )

        rule = self.rulebook.select(
            indicator="bmi",
            age_band=age_band,
            sex=record.sex,
            model_id=device.model_id,
            at=observed,
            canary_consent=canary_consented,
        )
        if rule is None:
            return self._fallback(
                record, version, supersedes_version, revision_reason, computed_at,
                ["采集时点没有处于有效期且完成三方会签的适用规则"], canary_consented,
                data_source=data_source, applicability=applicability,
                weak=weak_signals,
            )

        verdict = rule.classify(bmi_input["bmi"], age_band, record.sex)
        if verdict is None:
            return self._fallback(
                record, version, supersedes_version, revision_reason, computed_at,
                [f"规则 {rule.rule_id} 未覆盖 {age_band}/{record.sex} 分组"], canary_consented,
                data_source=data_source, applicability=applicability, weak=weak_signals,
            )

        pack = rule.packs[verdict]
        recommendation = pack.recommendation
        evidence_level = "normal"
        if weak_signals:
            evidence_level = "weak"
            recommendation = recommendation + " 本次数据可信度有限，建议在姿势正确的条件下重新测量确认。"

        threshold = rule.thresholds[age_band].get(record.sex) \
            or rule.thresholds[age_band].get("any")
        applicability.update({
            "rule_id": rule.rule_id,
            "rule_version": rule.version,
            "rule_fingerprint": rule.fingerprint,
            "rule_canary": rule.canary,
            "reference_range": {"from": threshold[0], "to": threshold[1], "indicator": "bmi"},
            "rule_effective_window": {
                "from": rule.effective_from,
                "to": rule.effective_to,
            },
            "applies_to_models": sorted(rule.applies_to_models),
        })
        reliability["evidence_level"] = evidence_level
        explanation = (
            f"{pack.explanation} 本次 BMI 约 {bmi_input['bmi']:.1f}，"
            f"适用的 {age} 岁年龄段参考区间为 {threshold[0]:g}–{threshold[1]:g}。"
        )

        return Conclusion(
            record_id=record.record_id,
            person_id=record.person_id,
            version=version,
            verdict=verdict,
            indicator="bmi",
            indicator_value=round(bmi_input["bmi"], 1),
            rule_id=rule.rule_id,
            rule_version=rule.version,
            rule_fingerprint=rule.fingerprint,
            canary=rule.canary,
            canary_consent_snapshot=canary_consented if rule.canary else None,
            headline=pack.headline,
            explanation=explanation,
            recommendation=recommendation,
            evidence_level=evidence_level,
            observed_at=record.collected_at,
            computed_at=computed_at,
            revision_reason=revision_reason,
            supersedes_version=supersedes_version,
            data_source=data_source,
            applicability=applicability,
            reliability=reliability,
        )

    def _fallback(
        self, record, version, supersedes_version, reason, computed_at, problems,
        canary_consented, *, data_source=None, applicability=None, weak=None,
    ) -> Conclusion:
        return Conclusion(
            record_id=record.record_id,
            person_id=record.person_id,
            version=version,
            verdict="insufficient_evidence",
            indicator=None,
            indicator_value=None,
            rule_id=None,
            rule_version=None,
            rule_fingerprint=None,
            canary=False,
            canary_consent_snapshot=None,
            headline=NEUTRAL_PACK.headline,
            explanation=NEUTRAL_PACK.explanation,
            recommendation=NEUTRAL_PACK.recommendation,
            evidence_level="weak",
            observed_at=record.collected_at,
            computed_at=computed_at,
            revision_reason=reason,
            supersedes_version=supersedes_version,
            data_source=data_source or {},
            applicability=applicability or {},
            reliability={
                "hard_problems": list(problems),
                "weak_signals": list(weak or []),
                "evidence_level": "weak",
            },
        )
