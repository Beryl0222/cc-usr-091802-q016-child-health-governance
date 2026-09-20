"""面向产品团队的质量统计：k-匿名群体门槛 + 维度白名单。

产品侧只能看到聚合后的质量数据：
- 任何分组人数低于 ``MIN_COHORT``（50）一律抑制（连同互补组合一起抑制，
  避免用“总数 - 可见组”反推出小群体）；
- 只允许按白名单维度分组，任何可定位到单个孩子的字段（person_id、
  record_id、生日、原始读数）都不进入统计；
- 灰度事件与正式事件可按 ``rule_canary`` 维度区分。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

MIN_COHORT = 50

# 仅这些维度可用于分组；新增维度必须经隐私角色评审。
ALLOWED_DIMENSIONS = frozenset({
    "model_id",
    "age_band",
    "sex",
    "verdict",
    "evidence_level",
    "rule_canary",
    "has_hard_problems",
})

# 抑制后对外展示的占位
SUPPRESSED = "<suppressed>"


@dataclass(frozen=True)
class QualityEvent:
    model_id: str
    age_band: str
    sex: str
    verdict: str
    evidence_level: str
    rule_canary: bool
    has_hard_problems: bool
    # 内部去重键，不参与任何对外输出，保证同记录只按当前版本计数一次。
    record_version: str = ""

    def dim(self, name: str):
        return getattr(self, name)


class StatsCollector:
    def __init__(self, min_cohort: int = MIN_COHORT):
        self.min_cohort = min_cohort
        self._events: dict[str, QualityEvent] = {}

    def record(self, event: QualityEvent) -> None:
        """登记某记录当前版本的质量事件；该记录产生新版本时覆盖旧事件。"""
        if not event.record_version:
            raise ValueError("质量事件必须绑定 record_version 去重键")
        self._events[event.record_version] = event

    def __len__(self) -> int:
        return len(self._events)

    def quality_report(self, group_by: Iterable[str]) -> dict:
        """返回满足最小群体门槛的分组质量统计。

        结构：{"min_cohort": 50, "groups": [...], "suppressed_groups": n,
                "total_events_in_report": n}
        """
        dims = tuple(group_by)
        bad = [d for d in dims if d not in ALLOWED_DIMENSIONS]
        if bad:
            raise ValueError(f"维度不在隐私白名单内: {bad}")
        if len(set(dims)) != len(dims):
            raise ValueError("group_by 存在重复维度")

        buckets: dict[tuple, list[QualityEvent]] = defaultdict(list)
        for e in self._events.values():
            buckets[tuple((d, e.dim(d)) for d in dims)].append(e)

        # 互补抑制：小桶不直接给数，也不让人从总数反推。
        raw_total = len(self._events)
        small_keys = {k for k, evs in buckets.items() if len(evs) < self.min_cohort}
        suppressed_events = sum(len(buckets[k]) for k in small_keys)
        reportable_total = raw_total - suppressed_events
        # 若被抑制的人数本身小于门槛，公布“参与统计总数”也可能泄露，
        # 因此总数按门槛取整为区间。
        total_floor = (reportable_total // self.min_cohort) * self.min_cohort

        groups = []
        def _sort_key(bucket_key):
            return tuple(str(v) for _, v in bucket_key)

        for bkey in sorted(buckets, key=_sort_key):
            if bkey in small_keys:
                continue
            evs = buckets[bkey]
            hard = sum(1 for e in evs if e.has_hard_problems)
            weak = sum(1 for e in evs if e.evidence_level == "weak")
            insufficient = sum(1 for e in evs if e.verdict == "insufficient_evidence")
            groups.append({
                **{d: _jsonable(v) for d, v in bkey},
                "event_count": len(evs),
                "insufficient_evidence_rate": round(insufficient / len(evs), 4),
                "hard_problem_rate": round(hard / len(evs), 4),
                "weak_evidence_rate": round(weak / len(evs), 4),
            })

        return {
            "min_cohort": self.min_cohort,
            "group_by": list(dims),
            "groups": groups,
            "suppressed_group_count": len(small_keys),
            "total_events_in_report": f"{total_floor}+" if small_keys else reportable_total,
            "note": "人数不足最小群体门槛的分组已被抑制，不提供任何可定位个人的明细。",
        }


def _jsonable(v):
    if isinstance(v, bool):
        return v
    return v
