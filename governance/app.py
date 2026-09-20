"""应用装配：种子目录、统计联动、审计日志与状态持久化。

生产部署时应从规则管理后台加载规则；这里的种子数据同样走完整的
三方会签校验，仅用于演示与测试，approver_id 带 ``demo-`` 前缀以示区分。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional

from .calibration import Reading
from .engine import Conclusion, MeasurementRecord, Notification
from .models import Approval, DeviceModel, Person
from .rules import Rule, RuleBook
from .service import (
    AuthorizationError,
    ConsentState,
    GovernanceError,
    GovernanceService,
)
from .stats import QualityEvent, StatsCollector

SEED_APPROVALS = {
    "medical": "demo-medical-officer",
    "privacy": "demo-privacy-officer",
    "copy": "demo-copy-officer",
}
APPROVED_AT = "2026-01-15T09:00:00Z"
EFFECTIVE_FROM = "2026-02-01T00:00:00Z"


def build_devices() -> dict[str, DeviceModel]:
    return {
        "watch-k1": DeviceModel(
            model_id="watch-k1",
            display_name="儿童电话手表 K1",
            measurement_modes=frozenset({"manual", "auto"}),
            calibration={
                # K1 自动感应体重（站立姿态）存在已知 +8% 偏差，出厂校准修正；
                # 手动录入读数不经过该传感器，不做修正。
                "weight": {
                    "standing": {"auto": (0.926, 0.0)},
                },
            },
        ),
        "watch-k2": DeviceModel(
            model_id="watch-k2",
            display_name="儿童电话手表 K2",
            measurement_modes=frozenset({"manual"}),
        ),
    }


def _signed_rule(
    *, rule_id: str, version: int, thresholds: dict, packs: dict,
    effective_from: str = EFFECTIVE_FROM, canary: bool = False,
    age_bands=None, sex_groups=frozenset({"any"}), applies_to_models=frozenset(),
) -> Rule:
    """构造规则并补齐绑定内容指纹的三方批准。"""
    rule = Rule(
        rule_id=rule_id,
        version=version,
        indicator="bmi",
        thresholds=thresholds,
        packs=packs,
        effective_from=effective_from,
        approvals=(),
        age_bands=frozenset(age_bands) if age_bands else frozenset(thresholds.keys()),
        sex_groups=sex_groups,
        applies_to_models=frozenset(applies_to_models),
        canary=canary,
    )
    fp = rule.fingerprint
    rule.approvals = tuple(
        Approval(role=role, approver_id=who, approved_at=APPROVED_AT,
                 content_fingerprint=fp)
        for role, who in SEED_APPROVALS.items()
    )
    return rule


def build_rulebook() -> RuleBook:
    """示例规则库。阈值是演示用参考区间，上线前须由真实医学负责人替换。"""
    from .models import SafetyPack

    book = RuleBook()

    within_pack = SafetyPack(
        headline="本次测量结果在同龄参考范围内",
        explanation="结合身高与体重计算的身体质量指数（BMI），处于该年龄段的参考区间内。"
                    "单次测量会受姿势、进食和运动影响，可结合长期趋势观察。",
        recommendation="保持均衡饮食、规律睡眠和日常活动即可，建议每 1–3 个月复测一次。",
    )
    attention_pack = SafetyPack(
        headline="本次测量结果建议结合生长趋势关注",
        explanation="本次 BMI 落在同龄参考区间之外。这只是生长参考信息，"
                    "单次读数也可能受测量姿势、衣物或设备误差影响。",
        recommendation="建议在清晨、空腹、着轻便衣物并保持正确姿势的条件下复测 2–3 次；"
                       "如持续偏离或您对生长发育有疑虑，可咨询儿科或儿童保健专业人员。",
    )
    packs = {"within_range": within_pack, "needs_attention": attention_pack}

    # 演示参考区间（BMI）。区间为 [下限, 上限)，按年龄区间与性别分组。
    thresholds_any = {
        "6-9": {"any": [13.5, 21.0]},
        "10-12": {"any": [14.0, 24.0]},
        "13-15": {"any": [15.0, 26.0]},
        "16-17": {"any": [16.0, 27.0]},
    }
    # 性别特异区间（演示值；正式值以医学角色批准的数据为准）
    thresholds_sexed = {
        "10-12": {"female": [14.0, 24.5], "male": [14.0, 23.5]},
        "13-15": {"female": [15.0, 26.5], "male": [15.5, 25.5]},
        "16-17": {"female": [15.5, 28.0], "male": [16.5, 27.0]},
    }

    book.add_rule(_signed_rule(
        rule_id="bmi-ref-sexneutral-v1", version=1,
        thresholds=thresholds_any, packs=packs,
    ))
    book.add_rule(_signed_rule(
        rule_id="bmi-ref-sexspecific-v2", version=2,
        thresholds=thresholds_sexed, packs=packs,
        sex_groups=frozenset({"female", "male"}),
    ))
    # 一条灰度新规则（例如修订后的青春期区间），仅在监护人同意后启用。
    canary_thresholds = {
        "13-15": {"female": [15.0, 27.5], "male": [15.5, 26.0]},
    }
    book.add_rule(_signed_rule(
        rule_id="bmi-ref-puberty-canary-v3", version=3,
        thresholds=canary_thresholds, packs=packs,
        sex_groups=frozenset({"female", "male"}),
        canary=True,
    ))
    return book


class Application:
    """服务门面：把治理服务、统计采集与审计串起来。"""

    def __init__(self, *, clock=None, data_path: Optional[str] = None):
        self.data_path = Path(data_path) if data_path else None
        self.devices = build_devices()
        self.rulebook = build_rulebook()
        self.service = GovernanceService(self.rulebook, self.devices, clock=clock)
        self.stats = StatsCollector()
        self.audit: list[dict] = []

    def log_audit(self, actor: str, action: str, detail: dict) -> None:
        from .models import isoformat, utcnow

        self.audit.append({
            "at": isoformat(self.service.now_dt()),
            "actor": actor,
            "action": action,
            "detail": {k: v for k, v in detail.items()
                       if k not in ("token",)},
        })

    def _collect(self, c: Conclusion) -> None:
        self.stats.record(QualityEvent(
            model_id=c.data_source.get("model_id", "unknown"),
            age_band=c.applicability.get("age_band", "unknown"),
            sex=c.applicability.get("sex", "unknown"),
            verdict=c.verdict,
            evidence_level=c.evidence_level,
            rule_canary=bool(c.canary),
            has_hard_problems=bool(c.reliability.get("hard_problems")),
            record_version=f"{c.record_id}#v{c.version}",
        ))

    @staticmethod
    def _require_role(principal: Mapping, *roles: str) -> None:
        if principal.get("role") not in roles:
            raise AuthorizationError(f"该操作需要角色: {', '.join(roles)}")

    @classmethod
    def _require_owner(cls, principal: Mapping, person_id: str) -> None:
        cls._require_role(principal, "guardian", "admin")
        if principal.get("role") == "guardian" and principal.get("person_id") != person_id:
            raise AuthorizationError("监护人只能访问本人绑定儿童的数据")

    # 业务方法：执行后记审计、收质量事件、持久化
    def submit_measurement(self, principal: Mapping, payload: Mapping) -> Conclusion:
        self._require_owner(principal, payload.get("person_id", ""))
        c = self.service.submit_measurement(payload)
        self._collect(c)
        self.log_audit(principal["role"], "measurement_submitted",
                       {"record_id": c.record_id, "person_id": c.person_id,
                        "verdict": c.verdict, "rule_id": c.rule_id})
        self.save()
        return c

    def set_canary_consent(self, principal: Mapping, person_id: str, enable: bool):
        self._require_owner(principal, person_id)
        state, affected = self.service.set_canary_consent(person_id, enable)
        for r in self.service.conclusions.values():
            self._collect(r[-1])
        self.log_audit(principal["role"], "canary_consent_changed",
                       {"person_id": person_id, "enable": enable,
                        "reprocessed": len(affected)})
        self.save()
        return {"state": state, "reprocessed": affected}

    def correct_units(self, principal: Mapping, record_id: str, readings: list) -> Conclusion:
        self._require_role(principal, "guardian", "admin")
        record = self.service.records.get(record_id)
        if record is None:
            raise GovernanceError("记录不存在")
        self._require_owner(principal, record.person_id)
        c = self.service.correct_units(record_id, readings)
        self._collect(c)
        self.log_audit(principal["role"], "units_corrected",
                       {"record_id": record_id, "new_version": c.version})
        self.save()
        return c

    def correct_birth_date(self, principal: Mapping, person_id: str, birth_date: str):
        self._require_owner(principal, person_id)
        affected = self.service.correct_birth_date(person_id, birth_date)
        for c in affected:
            self._collect(c)
        self.log_audit(principal["role"], "birth_date_corrected",
                       {"person_id": person_id, "affected_records": len(affected)})
        self.save()
        return affected

    def withdraw_rule(self, principal: Mapping, rule_id: str, reason: str):
        self._require_role(principal, "admin")
        affected = self.service.withdraw_rule(rule_id, reason)
        for c in affected:
            self._collect(c)
        self.log_audit(principal["role"], "rule_withdrawn",
                       {"rule_id": rule_id, "reason": reason,
                        "affected_records": len(affected)})
        self.save()
        return affected

    def quality_report(self, principal: Mapping, group_by: list[str]) -> dict:
        self._require_role(principal, "product", "admin")
        report = self.stats.quality_report(group_by)
        self.log_audit(principal["role"], "quality_report_accessed",
                       {"group_by": group_by})
        self.save()
        return report

    # ---------- 持久化 ----------
    def save(self) -> None:
        if self.data_path is None:
            return
        snap = self.service.snapshot()
        snap["withdrawn"] = [
            {"rule_id": rid, "at": at, "reason": reason}
            for rid, (at, reason) in self.rulebook._withdrawn.items()
        ]
        snap["audit"] = self.audit
        tmp = self.data_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.data_path)

    def load(self) -> bool:
        if self.data_path is None or not self.data_path.exists():
            return False
        snap = json.loads(self.data_path.read_text(encoding="utf-8"))
        svc = self.service
        svc.persons = {pid: Person(**d) for pid, d in snap["persons"].items()}
        svc.person_sex = dict(snap["person_sex"])
        from .service import ConsentState
        svc.consents = {pid: ConsentState(**d) for pid, d in snap["consents"].items()}
        svc.records = {}
        for rid, d in snap["records"].items():
            d = dict(d)
            d["readings"] = [Reading(**r) for r in d["readings"]]
            svc.records[rid] = MeasurementRecord(**d)
        svc.unit_corrections = snap.get("unit_corrections", {})
        svc.conclusions = {
            rid: [Conclusion(**c) for c in versions]
            for rid, versions in snap["conclusions"].items()
        }
        svc.notifications = {
            pid: [Notification(**n) for n in ns]
            for pid, ns in snap.get("notifications", {}).items()
        }
        svc._seq = snap.get("seq", 0)
        for w in snap.get("withdrawn", []):
            self.rulebook.withdraw(w["rule_id"], w["at"], w["reason"])
        self.audit = snap.get("audit", [])
        for versions in svc.conclusions.values():
            self._collect(versions[-1])
        return True
