"""单位换算、设备校准、姿态与可信度检查。"""

import unittest

from governance.calibration import (
    Reading,
    build_bmi_input,
    calibrate_reading,
    posture_age_ok,
)
from governance.models import DeviceModel


K1 = DeviceModel(
    model_id="watch-k1",
    display_name="K1",
    measurement_modes=frozenset({"manual", "auto"}),
    calibration={
        "weight": {"standing": {"auto": (0.926, 0.0)}},
    },
)


def reading(measurement, value, unit, posture="standing", mode="manual", when="2026-09-01T10:00:00Z"):
    return Reading(measurement, value, unit, posture, mode, when)


class UnitCalibrationTest(unittest.TestCase):
    def test_supports_multiple_units_and_normalizes_to_si(self):
        cm = calibrate_reading(reading("height", 167, "cm"), K1)
        m = calibrate_reading(reading("height", 1.67, "m"), K1)
        inch = calibrate_reading(reading("height", 65.75, "inch"), K1)
        self.assertAlmostEqual(cm.si_value, 1.67, places=6)
        self.assertAlmostEqual(m.si_value, 1.67, places=6)
        self.assertAlmostEqual(inch.si_value, 1.67, places=2)

        kg = calibrate_reading(reading("weight", 50.5, "kg"), K1)
        lb = calibrate_reading(reading("weight", 111.3, "lb"), K1)
        jin = calibrate_reading(reading("weight", 101, "jin"), K1)
        self.assertAlmostEqual(kg.si_value, 50.5, places=6)
        self.assertAlmostEqual(lb.si_value, 50.48, places=1)
        self.assertAlmostEqual(jin.si_value, 50.5, places=6)

    def test_unknown_unit_is_hard_failure(self):
        cm = calibrate_reading(reading("weight", 50, "stone"), K1)
        self.assertFalse(cm.trusted)
        self.assertTrue(any("不支持的单位" in p for p in cm.problems))

    def test_calibration_scoped_by_measurement_posture_and_mode(self):
        # 自动感应的体重应用 +8% 修正
        auto = calibrate_reading(reading("weight", 50.5, "kg", mode="auto"), K1)
        self.assertAlmostEqual(auto.si_value, 50.5 * 0.926, places=4)
        # 手动体重不修正
        manual = calibrate_reading(reading("weight", 50.5, "kg", mode="manual"), K1)
        self.assertAlmostEqual(manual.si_value, 50.5, places=6)
        # 同姿态的身高绝不应用体重的修正
        height = calibrate_reading(reading("height", 167, "cm", mode="auto"), K1)
        self.assertAlmostEqual(height.si_value, 1.67, places=6)

    def test_plausibility_rejects_impossible_values(self):
        bad = calibrate_reading(reading("weight", 5000, "g"), K1)  # 5kg 对身高场景另说
        self.assertTrue(bad.trusted)  # 5kg 在 1.5–200kg 内
        absurd = calibrate_reading(reading("height", 50, "m"), K1)
        self.assertFalse(absurd.trusted)

    def test_posture_age_policy(self):
        self.assertTrue(posture_age_ok("height", "standing", 13))
        self.assertFalse(posture_age_ok("height", "lying", 13))
        self.assertTrue(posture_age_ok("height", "lying", 1))
        self.assertFalse(posture_age_ok("weight", "held", 10))

    def test_retired_and_unsupported_mode(self):
        retired = DeviceModel(model_id="old", display_name="old", retired=True)
        cm = calibrate_reading(reading("weight", 30, "kg"), retired)
        self.assertTrue(any("已停用" in p for p in cm.problems))

        k2 = DeviceModel(model_id="k2", display_name="k2",
                         measurement_modes=frozenset({"manual"}))
        cm2 = calibrate_reading(reading("weight", 30, "kg", mode="auto"), k2)
        self.assertTrue(any("不支持采集方式" in p for p in cm2.problems))


class BmiAssemblyTest(unittest.TestCase):
    def _pair(self, h=1.67, w=50.5, when="2026-09-01T10:00:00Z"):
        return {
            "height": calibrate_reading(reading("height", h * 100, "cm", when=when), K1),
            "weight": calibrate_reading(reading("weight", w, "kg", when=when), K1),
        }

    def test_bmi_for_167_505(self):
        bmi, problems = build_bmi_input(self._pair(), age_years=13)
        self.assertEqual(problems, [])
        self.assertAlmostEqual(bmi["bmi"], 50.5 / 1.67 ** 2, places=2)
        self.assertLess(bmi["bmi"], 18.2)

    def test_missing_reading_blocks_bmi(self):
        only_h = {"height": self._pair()["height"]}
        bmi, problems = build_bmi_input(only_h, age_years=13)
        self.assertIsNone(bmi)
        self.assertTrue(any("缺少 weight" in p for p in problems))

    def test_posture_mismatch_blocks_bmi(self):
        pair = {
            "height": calibrate_reading(reading("height", 100, "cm", posture="lying"), K1),
            "weight": calibrate_reading(reading("weight", 30, "kg", posture="standing"), K1),
        }
        bmi, problems = build_bmi_input(pair, age_years=13)
        self.assertIsNone(bmi)
        self.assertTrue(any("姿态" in p for p in problems))

    def test_pair_time_gap_blocks_bmi(self):
        pair = self._pair()
        times = {
            "height": "2026-09-01T10:00:00Z",
            "weight": "2026-09-10T10:00:00Z",
        }
        bmi, problems = build_bmi_input(pair, age_years=13, measured_times=times)
        self.assertIsNone(bmi)
        self.assertTrue(any("时间相差超过" in p for p in problems))

    def test_impossible_bmi_blocked(self):
        # 2kg 配 1.67m：单项读数都在量程内，但 BMI 严重出界，疑似读数错误
        pair = {
            "height": calibrate_reading(reading("height", 1.67, "m"), K1),
            "weight": calibrate_reading(reading("weight", 2.0, "kg"), K1),
        }
        bmi, problems = build_bmi_input(pair, age_years=13)
        self.assertIsNone(bmi)
        self.assertTrue(any("BMI" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
