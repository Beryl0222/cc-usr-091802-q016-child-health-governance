"""服务编排：同意管理、记录与结论版本、修订触发、通知、家长视图。

任何展示给家庭的结论永远是某条记录的“当前版本”；修订时旧版本置为
``superseded`` 或 ``withheld`` 并立即停止展示，同时生成家庭通知。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .engine import (
    Conclusion,
    Evaluator,
    MeasurementRecord,
    Notification,
)
from .models import (
    Approval,
    Person,
    isoformat,
    parse_iso,
)
from .rules import Rule, RuleBook


class GovernanceError(ValueError):
    """请求层面的可预期错误（HTTP 层映射为 4xx）。"""


class AuthenticationError(GovernanceError):
    """未认证或令牌无效（HTTP 401）。"""


class AuthorizationError(GovernanceError):
    """已认证但无权执行该操作（HTTP 403）。"""


@dataclass
class ConsentState:
    canary_enabled: bool = False
    canary_granted_at: Optional[str] = None
    canary_revoked_at: Optional[str] = None
    history: list[dict] = field(default_factory=list)


class GovernanceService:
    def __init__(self, rulebook: RuleBook, devices: Mapping, *, clock=None):
        self.rulebook = rulebook
        self.devices = devices
        self.evaluator = Evaluator(rulebook, devices, clock=clock)
        self._clock = clock
        self.persons: dict[str, Person] = {}
        self.person_sex: dict[str, str] = {}
        self.consents: dict[str, ConsentState] = {}
        self.records: dict[str, MeasurementRecord] = {}
        # 原始单位修正留痕：record_id -> 历次修正说明
        self.unit_corrections: dict[str, list[dict]] = {}
        self.conclusions: dict[str, list[Conclusion]] = {}
        self.notifications: dict[str, list[Notification]] = {}
        self.tokens: dict[str, dict] = {}
        self._seq = 0

    # ---------- 时钟 ----------
    def now_dt(self):
        return self._clock() if self._clock else __import__(
            "governance.models", fromlist=["utcnow"]
        ).utcnow()

    # ---------- 家庭与令牌 ----------
    def register_person(self, person_id: str, birth_date: str, sex: str) -> Person:
        if person_id in self.persons:
            raise GovernanceError("儿童档案已存在")
        if sex not in ("female", "male"):
            raise GovernanceError("sex 仅支持 female / male")
        # 以当天校验生日合理性
        person = Person(person_id=person_id, birth_date=birth_date)
        person.age_years_on(isoformat(self.now_dt())[:10])
        self.persons[person_id] = person
        self.person_sex[person_id] = sex
        self.consents[person_id] = ConsentState()
        return person

    def issue_token(self, role: str, person_id: Optional[str] = None) -> str:
        if role not in ("guardian", "product", "admin"):
            raise GovernanceError("未知角色")
        if role == "guardian":
            if person_id not in self.persons:
                raise GovernanceError("监护人令牌必须绑定已登记儿童")
        token = secrets.token_hex(16)
        self.tokens[token] = {"role": role, "person_id": person_id}
        return token

    def authenticate(self, token: str) -> dict:
        principal = self.tokens.get(token)
        if principal is None:
            raise AuthenticationError("未认证或令牌已失效")
        return dict(principal)

    # ---------- 灰度同意 ----------
    def set_canary_consent(self, person_id: str, enable: bool) -> tuple[ConsentState, list[Conclusion]]:
        """必须由监护人显式开启；随时可退出，退出立即生效。

        返回 (同意状态, 因退出灰度而重评产生的新结论列表)。
        """
        state = self.consents.get(person_id)
        if state is None:
            raise GovernanceError("儿童档案不存在")
        at = isoformat(self.now_dt())
        affected: list[Conclusion] = []
        if enable and not state.canary_enabled:
            state.canary_enabled = True
            state.canary_granted_at = at
            state.history.append({"action": "grant", "at": at})
        elif not enable and state.canary_enabled:
            state.canary_enabled = False
            state.canary_revoked_at = at
            state.history.append({"action": "revoke", "at": at})
            # 退出灰度：所有“当前版本使用了灰度规则”的记录立即重评
            affected = self._reprocess_person(person_id, "canary_optout")
        return state, affected

    def canary_consented(self, person_id: str) -> bool:
        state = self.consents.get(person_id)
        return bool(state and state.canary_enabled)

    # ---------- 测量提交流 ----------
    def submit_measurement(self, payload: Mapping) -> Conclusion:
        person_id = payload.get("person_id")
        person = self.persons.get(person_id)
        if person is None:
            raise GovernanceError("儿童档案不存在，请先登记生日")
        model_id = payload.get("model_id")
        if model_id not in self.devices:
            raise GovernanceError(f"未知设备型号: {model_id}")

        now = self.now_dt()
        observed, time_source, clock_problems = self.evaluator.resolve_observed_at(payload, now)
        observed_iso = isoformat(observed)
        received_iso = isoformat(now)

        raw_readings = self._parse_readings(payload, observed_iso)
        self._seq += 1
        record_id = payload.get("record_id") or f"rec-{self._seq:08d}"
        if record_id in self.records:
            raise GovernanceError(f"记录号 {record_id} 已存在")

        record = MeasurementRecord(
            record_id=record_id,
            person_id=person_id,
            model_id=model_id,
            readings=raw_readings,
            collected_at=observed_iso,
            received_at=received_iso,
            time_source=time_source,
            sex=self.person_sex[person_id],
            backfill=bool(payload.get("backfill")),
        )
        self.records[record_id] = record

        conclusion = self.evaluator.evaluate(
            record, person,
            canary_consented=self.canary_consented(person_id),
            revision_reason="initial",
            version=1, supersedes_version=None,
            extra_hard_problems=clock_problems,
        )
        if clock_problems:
            conclusion.reliability["weak_signals"].append(
                "设备时钟异常已触发证据不足兜底，建议重新测量"
            )
        self._store_conclusion(conclusion, first=True)
        return conclusion

    @staticmethod
    def _parse_readings(payload: Mapping, observed_iso: str):
        from .calibration import Reading

        items = payload.get("readings")
        if not isinstance(items, list) or not items:
            raise GovernanceError("readings 必须是非空列表")
        out = []
        for item in items:
            try:
                value = float(item["value"])
            except (KeyError, TypeError, ValueError):
                raise GovernanceError("每个读数必须包含数值型 value")
            out.append(Reading(
                measurement=str(item["measurement"]),
                value=value,
                unit=str(item["unit"]),
                posture=str(item.get("posture", "standing")),
                capture_mode=str(item.get("capture_mode", "manual")),
                measured_at=str(item.get("measured_at", observed_iso)),
            ))
        return out

    # ---------- 修订触发 ----------
    def correct_units(self, record_id: str, corrected_readings: list[Mapping]) -> Conclusion:
        """单位录入错误的修正：保留原始提交，生成新版本。"""
        record = self.records.get(record_id)
        if record is None:
            raise GovernanceError("记录不存在")
        before = [{"measurement": r.measurement, "unit": r.unit, "value": r.value}
                  for r in record.readings]
        new_readings = self._parse_readings(
            {"readings": corrected_readings}, record.collected_at
        )
        record.readings = new_readings
        self.unit_corrections.setdefault(record_id, []).append({
            "at": isoformat(self.now_dt()),
            "reason": "unit_correction",
            "before": before,
            "after": [{"measurement": r.measurement, "unit": r.unit, "value": r.value}
                      for r in new_readings],
        })
        return self._reprocess_record(record, "unit_correction")

    def correct_birth_date(self, person_id: str, new_birth_date: str) -> list[Conclusion]:
        """生日修正：本人全部记录按新生日重评（年龄区间可能变化）。"""
        person = self.persons.get(person_id)
        if person is None:
            raise GovernanceError("儿童档案不存在")
        old = person.birth_date
        # 校验新日期合理
        Person(person_id=person_id, birth_date=new_birth_date).age_years_on(
            isoformat(self.now_dt())[:10]
        )
        self.persons[person_id] = Person(person_id=person_id, birth_date=new_birth_date)
        if old == new_birth_date:
            return []
        affected = self._reprocess_person(
            person_id, "birth_date_correction",
            extra={"old_birth_date": old, "new_birth_date": new_birth_date},
        )
        return affected

    def withdraw_rule(self, rule_id: str, reason: str) -> list[Conclusion]:
        """撤回规则：立即停用并重评所有当前版本引用它的记录。"""
        at = isoformat(self.now_dt())
        self.rulebook.withdraw(rule_id, at, reason)
        affected = []
        for record_id, versions in self.conclusions.items():
            current = versions[-1]
            if current.rule_id == rule_id and current.status == "active":
                record = self.records[record_id]
                conclusion = self.evaluator.evaluate(
                    record, self.persons[record.person_id],
                    canary_consented=self.canary_consented(record.person_id),
                    revision_reason="rule_withdrawn",
                    version=current.version + 1, supersedes_version=current.version,
                )
                current.status = "withheld"
                self._store_conclusion(conclusion, first=False,
                                       note=f"规则 {rule_id} 已撤回：{reason}")
                affected.append(conclusion)
        return affected

    def _reprocess_person(
        self, person_id: str, reason: str, *, extra: Optional[dict] = None
    ) -> list[Conclusion]:
        out = []
        for record_id, versions in self.conclusions.items():
            current = versions[-1]
            if current.person_id != person_id or current.status != "active":
                continue
            if reason == "canary_optout" and not current.canary:
                continue
            out.append(self._reprocess_record(self.records[record_id], reason, extra=extra))
        return out

    def _reprocess_record(
        self, record: MeasurementRecord, reason: str, *, extra: Optional[dict] = None
    ) -> Conclusion:
        current = self.conclusions[record.record_id][-1]
        conclusion = self.evaluator.evaluate(
            record, self.persons[record.person_id],
            canary_consented=self.canary_consented(record.person_id),
            revision_reason=reason,
            version=current.version + 1, supersedes_version=current.version,
        )
        current.status = "superseded"
        note = {
            "unit_correction": "测量单位或读数已由监护人更正",
            "birth_date_correction": "监护人更正了生日，适用年龄区间重新判定",
            "canary_optout": "监护人已退出算法灰度，改用正式规则重新解释",
            "backfill_reprocessing": "离线补传数据已按采集时规则重新解释",
        }.get(reason, reason)
        self._store_conclusion(conclusion, first=False, note=note, extra=extra)
        return conclusion

    # ---------- 结论落库与通知 ----------
    def _store_conclusion(self, conclusion: Conclusion, *, first: bool,
                          note: str = "", extra: Optional[dict] = None) -> None:
        bucket = self.conclusions.setdefault(conclusion.record_id, [])
        bucket.append(conclusion)
        kind = "first_result" if first else (
            "withheld" if conclusion.verdict == "insufficient_evidence"
            and conclusion.revision_reason == "rule_withdrawn" else "revised"
        )
        body = self._notification_body(conclusion, kind, note)
        nid = f"ntf-{conclusion.record_id}-v{conclusion.version}"
        notification = Notification(
            notification_id=nid,
            person_id=conclusion.person_id,
            record_id=conclusion.record_id,
            conclusion_version=conclusion.version,
            kind=kind,
            reason=conclusion.revision_reason,
            body=body,
            created_at=isoformat(self.now_dt()),
        )
        self.notifications.setdefault(conclusion.person_id, []).append(notification)
        conclusion.notification_ids.append(nid)

    @staticmethod
    def _notification_body(c: Conclusion, kind: str, note: str) -> str:
        if kind == "first_result":
            return f"您孩子 {c.record_id} 号记录的健康参考结论已生成。{c.headline}"
        if kind == "withheld":
            return (
                f"此前 {c.record_id} 号记录所依据的规则已被撤回，旧结论已停止展示。"
                f"系统已重新评估：{c.headline}"
            )
        return f"{c.record_id} 号记录的结论已修订（{note or c.revision_reason}），旧版本已停止展示。{c.headline}"

    # ---------- 查询 ----------
    def active_conclusion(self, record_id: str) -> Optional[Conclusion]:
        versions = self.conclusions.get(record_id)
        return versions[-1] if versions else None

    def parent_record_view(self, record_id: str) -> dict:
        """家长打开某次记录时看到的完整解释：来源、适用范围、理由、修订经过。"""
        record = self.records.get(record_id)
        if record is None:
            raise GovernanceError("记录不存在")
        versions = self.conclusions[record_id]
        current = versions[-1]
        revision_history = []
        for v in versions:
            revision_history.append({
                "version": v.version,
                "status": v.status,
                "verdict": v.verdict,
                "rule_id": v.rule_id,
                "rule_version": v.rule_version,
                "rule_fingerprint": v.rule_fingerprint,
                "computed_at": v.computed_at,
                "revision_reason": v.revision_reason,
                "supersedes_version": v.supersedes_version,
            })
        return {
            "record_id": record_id,
            "person_id": record.person_id,
            "current_version": current.version,
            "display": {
                "headline": current.headline,
                "explanation": current.explanation,
                "recommendation": current.recommendation,
                "verdict": current.verdict,
                "evidence_level": current.evidence_level,
            },
            "data_source": current.data_source,
            "applicability": current.applicability,
            "reliability": current.reliability,
            "revision_history": revision_history,
            "unit_corrections": self.unit_corrections.get(record_id, []),
            "not_a_diagnosis": "本结果仅为生长发育参考信息，不构成医学诊断；"
                               "如有疑虑请咨询儿科或儿童保健专业人员。",
        }

    def list_notifications(self, person_id: str) -> list[dict]:
        out = []
        for n in self.notifications.get(person_id, []):
            out.append({
                "notification_id": n.notification_id,
                "record_id": n.record_id,
                "conclusion_version": n.conclusion_version,
                "kind": n.kind,
                "reason": n.reason,
                "body": n.body,
                "created_at": n.created_at,
                "delivered": n.delivered,
            })
        return out

    # ---------- 序列化 ----------
    def snapshot(self) -> dict:
        """供持久化使用；令牌不落地（重启后重新签发）。"""
        def readings(rs):
            return [r.__dict__.copy() for r in rs]

        return {
            "persons": {pid: p.__dict__.copy() for pid, p in self.persons.items()},
            "person_sex": dict(self.person_sex),
            "consents": {pid: c.__dict__.copy() for pid, c in self.consents.items()},
            "records": {
                rid: {**r.__dict__, "readings": readings(r.readings)}
                for rid, r in self.records.items()
            },
            "unit_corrections": dict(self.unit_corrections),
            "conclusions": {
                rid: [c.as_dict() for c in versions]
                for rid, versions in self.conclusions.items()
            },
            "notifications": {
                pid: [n.__dict__.copy() for n in ns]
                for pid, ns in self.notifications.items()
            },
            "seq": self._seq,
        }
