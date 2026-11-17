from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.pico_glitch_filter import PicoCountGlitchFilter, evaluate_direction_consistency


FIXTURE = Path(__file__).parent / "fixtures" / "pico_stationary_disturbance.json"


def apply(profile: str, values: list[int]) -> tuple[list[int], PicoCountGlitchFilter]:
    filt = PicoCountGlitchFilter(profile)
    output = [filt.process({"pan": value, "tilt": value, "zoom": value})["pan"] for value in values]
    return output, filt


class PicoCountGlitchFilterTests(unittest.TestCase):
    def test_off_preserves_every_original_cumulative_count(self):
        values = [0, 1, 0, -2, 5, 4]
        self.assertEqual(apply("off", values)[0], values)

    def test_supplied_stationary_disturbance_pattern_is_suppressed(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        filt = PicoCountGlitchFilter("conservative")
        accepted = []
        for pan, tilt, zoom in fixture["counts"]:
            accepted.append(filt.process({"pan": pan, "tilt": tilt, "zoom": zoom}))
        self.assertEqual({row["pan"] for row in accepted}, {1000})
        self.assertEqual({row["tilt"] for row in accepted}, {2000})
        self.assertEqual({row["zoom"] for row in accepted}, {3000})
        self.assertGreater(sum(filt.diagnostics(500)["suppressed_impulses"].values()), 0)

    def test_sustained_single_count_slow_motion_is_not_a_deadband(self):
        output, _ = apply("conservative", [0, 0, 1, 1, 1, 1])
        self.assertEqual(output[-1], 1)

    def test_fast_motion_reversal_stop_and_final_position_are_exact(self):
        values = list(range(0, 30)) + list(range(29, 9, -1)) + [10] * 4
        output, _ = apply("conservative", values)
        self.assertEqual(output[-1], 10)
        self.assertGreater(max(output), 20)

    def test_persistent_drift_remains_visible(self):
        values = [value for value in range(20) for _ in range(3)] + [19] * 4
        output, _ = apply("experimental", values)
        self.assertEqual(output[-1], 19)

    def test_50_hz_impulses_at_500_hz_are_rejected(self):
        values = [1 if index % 10 == 5 else 0 for index in range(100)]
        output, _ = apply("conservative", values)
        self.assertEqual(set(output), {0})

    def test_delay_is_bounded_and_rate_reported(self):
        filt = PicoCountGlitchFilter("conservative")
        self.assertEqual(filt.diagnostics(500)["nominal_delay_ms"], 4.0)
        self.assertEqual(filt.diagnostics(200)["nominal_delay_ms"], 10.0)
        experimental = PicoCountGlitchFilter("experimental")
        self.assertEqual(experimental.diagnostics(500)["nominal_delay_ms"], 6.0)

    def test_direction_consistency_candidate_is_evaluated_but_not_a_runtime_profile(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for axis_index, baseline in enumerate((1000, 2000, 3000)):
            raw = [row[axis_index] for row in fixture["counts"]]
            self.assertEqual(set(evaluate_direction_consistency(raw)), {baseline})
        movement = list(range(12)) + [11] * 2 + list(range(10, -1, -1)) + [0] * 2
        filtered = evaluate_direction_consistency(movement)
        self.assertEqual(filtered[-1], 0)
        self.assertGreater(max(filtered), 8)


if __name__ == "__main__":
    unittest.main()
