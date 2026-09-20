"""面向产品团队的群体质量统计。

硬性约束：
- 只允许预定义的群体维度，禁止任何个体维度（child_id 等）；
- 每个群体单元必须达到最小群体门槛（默认 50）才输出数值，
  否则整格抑制，防止反推出单个孩子。
"""

from __future__ import annotations

from .models import CATEGORY_INSUFFICIENT
from .store import Store

MIN_COHORT = 50

ALLOWED_DIMS = ("device_model", "posture", "age_band", "category", "credibility", "rule_id")

# 个体维度即使被显式请求也直接拒绝。
FORBIDDEN_DIMS = ("child_id", "event_id", "guardian_id", "family_id")


def _dim_value(dim: str, event, conclusion) -> str:
    if dim == "device_model":
        return event.device_model
    if dim == "posture":
        return event.posture
    if dim == "age_band":
        return conclusion.age_band or "unknown"
    if dim == "category":
        return conclusion.category
    if dim == "credibility":
        return "ok" if all(check.passed for check in conclusion.checks) else "insufficient"
    if dim == "rule_id":
        return f"{conclusion.rule_id}v{conclusion.rule_version}" if conclusion.rule_id else "none"
    raise ValueError(dim)


def quality_stats(store: Store, dims: list[str], *, min_cohort: int = MIN_COHORT) -> dict:
    if not dims:
        raise ValueError("至少选择一个统计维度")
    for dim in dims:
        if dim in FORBIDDEN_DIMS:
            raise ValueError(f"不允许按个体维度统计: {dim}")
        if dim not in ALLOWED_DIMS:
            raise ValueError(f"不支持的统计维度: {dim}")

    cells: dict[tuple, list[int]] = {}
    for event in store.events.values():
        conclusion = store.current_conclusion(event.event_id)
        if conclusion is None:
            continue
        key = tuple(_dim_value(dim, event, conclusion) for dim in dims)
        cell = cells.setdefault(key, [0, 0])
        cell[0] += 1
        if conclusion.category == CATEGORY_INSUFFICIENT:
            cell[1] += 1

    out = []
    for key in sorted(cells):
        count, insufficient = cells[key]
        dims_dict = dict(zip(dims, key))
        if count < min_cohort:
            out.append({"dims": dims_dict, "suppressed": True, "reason": "below_minimum_cohort"})
        else:
            out.append(
                {
                    "dims": dims_dict,
                    "suppressed": False,
                    "count": count,
                    "insufficient_rate": round(insufficient / count, 3),
                }
            )
    return {"min_cohort": min_cohort, "cells": out}
