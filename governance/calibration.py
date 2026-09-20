"""单位换算、设备校准与可信度检查。

请求方可以给任何受支持的单位，但换算系数与校准参数由服务侧持有，
设备端无法通过“自带校准”影响结论。任何一步不满足，结论即为
``insufficient_evidence``，而不是勉强分类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .models import DeviceModel

# 到 SI 的换算：multiply raw by factor -> SI（身高 m，体重 kg）
UNIT_TO_SI: Mapping[str, Mapping[str, float]] = {
    "height": {
        "cm": 0.01,
        "m": 1.0,
        "inch": 0.0254,
        "in": 0.0254,
    },
    "weight": {
        "kg": 1.0,
        "g": 0.001,
        "lb": 0.45359237,
        "lbs": 0.45359237,
        "jin": 0.5,  # 市斤
    },
}

# 哪些姿态对哪类测量可信。低龄儿童卧位测身长是可信的，
# 大龄儿童卧位“身高”通常是错误姿势，直接判低可信。
POSTURE_POLICY: Mapping[str, Mapping[str, tuple[int, int]]] = {
    # measurement -> posture -> 允许的整岁区间
    "height": {
        "standing": (2, 17),
        "lying": (0, 3),
    },
    "weight": {
        "standing": (2, 17),
        "held": (0, 3),      # 家长抱着称量后扣减
        "lying": (0, 3),
    },
}

# 校准后 SI 值的生理合理区间，超出视为读数异常而非儿童异常。
SI_PLAUSIBLE = {
    "height": (0.40, 2.30),
    "weight": (1.5, 200.0),
    "bmi": (9.0, 45.0),
}

REQUIRED_FOR_BMI = ("height", "weight")


@dataclass(frozen=True)
class Reading:
    """单条原始读数（设备实际上报的内容，留痕以便还原）。"""

    measurement: str
    value: float
    unit: str
    posture: str
    capture_mode: str  # manual | auto
    measured_at: str   # 设备声称的测量时刻（仅参考）


@dataclass
class CalibratedMeasurement:
    measurement: str
    si_value: float
    posture: str
    problems: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return not self.problems


def calibrate_reading(reading: Reading, device: DeviceModel) -> CalibratedMeasurement:
    """把一条原始读数换算为 SI 并执行设备校准、姿态与合理性检查。"""
    problems: list[str] = []

    units = UNIT_TO_SI.get(reading.measurement)
    if units is None:
        problems.append(f"不支持的测量类型: {reading.measurement}")
        return CalibratedMeasurement(reading.measurement, float("nan"), reading.posture, problems)

    factor = units.get(reading.unit)
    if factor is None:
        problems.append(f"不支持的单位: {reading.unit}（支持: {', '.join(sorted(units))}）")

    if reading.capture_mode not in device.measurement_modes:
        problems.append(f"设备型号 {device.model_id} 不支持采集方式 {reading.capture_mode}")
    if device.retired:
        problems.append(f"设备型号 {device.model_id} 已停用")
    if not isinstance(reading.value, (int, float)) or reading.value != reading.value:
        problems.append("读数缺失或非数值")
        factor = None

    si = 0.0
    if factor is not None:
        by_measurement = device.calibration.get(reading.measurement, {})
        by_posture = by_measurement.get(reading.posture, {})
        mul, add = by_posture.get(reading.capture_mode, by_posture.get("all", (1.0, 0.0)))
        si = float(reading.value) * mul * factor + add * factor
        lo, hi = SI_PLAUSIBLE[reading.measurement]
        if not (lo <= si <= hi):
            problems.append(
                f"{reading.measurement} 校准后 {si:.3f} 超出合理区间 [{lo}, {hi}]"
            )

    policy = POSTURE_POLICY.get(reading.measurement, {})
    if reading.posture not in policy:
        problems.append(f"{reading.measurement} 不支持采集姿态 {reading.posture}")

    return CalibratedMeasurement(reading.measurement, si, reading.posture, problems)


def posture_age_ok(measurement: str, posture: str, age_years: int) -> bool:
    rng = POSTURE_POLICY.get(measurement, {}).get(posture)
    return rng is not None and rng[0] <= age_years <= rng[1]


def build_bmi_input(
    readings: Mapping[str, CalibratedMeasurement],
    *,
    age_years: int,
    max_pair_gap_hours: float = 72.0,
    measured_times: Mapping[str, str] | None = None,
) -> tuple[dict[str, float] | None, list[str]]:
    """校验身高+体重配对是否足以支持 BMI 结论。

    返回 ({"bmi": ..., "height_m": ..., "weight_kg": ...}, problems)。
    """
    problems: list[str] = []
    for name in REQUIRED_FOR_BMI:
        r = readings.get(name)
        if r is None:
            problems.append(f"缺少 {name} 读数，无法计算 BMI")
        elif not r.trusted:
            problems.extend(r.problems)
        elif not posture_age_ok(name, r.posture, age_years):
            problems.append(f"{name} 的采集姿态 {r.posture} 与年龄 {age_years} 不匹配")

    if measured_times:
        # 身高体重要在同一时间窗内，跨数月的拼配不构成一次结论。
        from .models import parse_iso

        ts = []
        for name in REQUIRED_FOR_BMI:
            if name in measured_times:
                ts.append(parse_iso(measured_times[name]))
        if len(ts) == 2 and abs((ts[0] - ts[1]).total_seconds()) > max_pair_gap_hours * 3600:
            problems.append(f"身高与体重测量时间相差超过 {max_pair_gap_hours:g} 小时")

    if problems:
        return None, problems

    h = readings["height"].si_value
    w = readings["weight"].si_value
    bmi = w / (h * h)
    lo, hi = SI_PLAUSIBLE["bmi"]
    if not (lo <= bmi <= hi):
        problems.append(f"计算所得 BMI {bmi:.1f} 超出合理区间，疑似读数错误")
        return None, problems
    return {"bmi": bmi, "height_m": h, "weight_kg": w}, []
