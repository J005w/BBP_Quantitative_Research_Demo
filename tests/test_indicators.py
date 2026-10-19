"""Hand-calculated golden values, validation, warm-up and causal-prefix checks."""

import copy
import math
import unittest

from bbp_system.indicators import compute_indicators, confirmed_pivots


def bar(index, open, high, low, close, **metadata):
    return {"time": index * 60, "open": open, "high": high, "low": low, "close": close, **metadata}


GOLDEN = [
    bar(0, 10, 12, 9, 10),
    bar(1, 10, 13, 10, 12),
    bar(2, 12, 14, 8, 9),
    bar(3, 10, 15, 10, 14),
    bar(4, 14, 14, 9, 10),
    bar(5, 11, 16, 11, 15),
]


class IndicatorTests(unittest.TestCase):
    def short_points(self, bars=GOLDEN):
        return compute_indicators(bars, bbp_length=3, di_length=3, adx_smoothing=3, atr_length=3)

    def test_hand_calculated_ema_and_combined_bbp(self):
        # alpha = 1/2, first-close seed. Combined BBP = H + L - 2*EMA.
        points = self.short_points()
        self.assertEqual([point.ema for point in points], [10, 11, 10, 12, 11, 13])
        self.assertEqual([point.bull_power for point in points], [2, 2, 4, 3, 3, 3])
        self.assertEqual([point.bear_power for point in points], [-1, -1, -2, -2, -2, -2])
        self.assertEqual([point.bbp for point in points], [1, 1, 2, 1, 1, 1])

    def test_hand_calculated_wilder_atr(self):
        # TR = [3, 3, 6, 6, 5, 6]; initial SMA = 4; subsequent RMA.
        points = self.short_points()
        expected = [None, None, 4, 14 / 3, 43 / 9, 140 / 27]
        for point, value in zip(points, expected):
            if value is None:
                self.assertIsNone(point.atr)
            else:
                self.assertAlmostEqual(point.atr, value, places=12)

    def test_hand_calculated_dmi_and_adx(self):
        # DMI seeds bars 1..3: TR=5, +DM=2/3, -DM=2/3.
        # Next DM RMAs: (4/9,7/9), then (26/27,14/27).
        # DX at bars 3,4,5: 0,300/11,30; ADX seed = 210/11.
        points = self.short_points()
        for point in points[:3]:
            self.assertIsNone(point.plus_di)
            self.assertIsNone(point.minus_di)
        for index, plus, minus in (
            (3, 40 / 3, 40 / 3), (4, 80 / 9, 140 / 9), (5, 325 / 18, 175 / 18),
        ):
            self.assertAlmostEqual(points[index].plus_di, plus, places=12)
            self.assertAlmostEqual(points[index].minus_di, minus, places=12)
        self.assertTrue(all(point.adx is None for point in points[:5]))
        self.assertAlmostEqual(points[5].adx, 210 / 11, places=12)

    def test_normalized_bbp_uses_atr_and_preserves_warmup(self):
        points = self.short_points()
        self.assertIsNone(points[0].normalized_bbp)
        self.assertIsNone(points[1].normalized_bbp)
        for point, value in zip(points[2:], (1 / 2, 3 / 14, 9 / 43, 27 / 140)):
            self.assertAlmostEqual(point.normalized_bbp, value, places=12)

    def test_adx_continues_wilder_smoothing_after_its_seed(self):
        # Next smoothed DM values are 133/81 and 28/81, giving DX=10500/161.
        # ((210/11)*2 + 10500/161)/3 = 61040/1771.
        points = self.short_points(GOLDEN + [bar(6, 15, 19, 12, 18)])
        self.assertAlmostEqual(points[-1].adx, 61040 / 1771, places=12)

    def test_true_range_includes_upward_and_downward_gaps(self):
        bars = [
            bar(0, 100, 101, 99, 100),
            bar(1, 110, 112, 109, 111),
            bar(2, 90, 92, 89, 90),
        ]
        points = compute_indicators(bars, atr_length=2)
        # TRs 2,12,22: initial mean 7, subsequent RMA 14.5.
        self.assertIsNone(points[0].atr)
        self.assertEqual(points[1].atr, 7)
        self.assertEqual(points[2].atr, 14.5)

    def test_flat_prices_have_zero_atr_di_adx_and_undefined_normalization(self):
        points = self.short_points([bar(index, 100, 100, 100, 100) for index in range(9)])
        self.assertTrue(all(point.bbp == 0 for point in points))
        self.assertTrue(all(point.atr == 0 for point in points[2:]))
        self.assertTrue(all(point.plus_di == point.minus_di == 0 for point in points[3:]))
        self.assertTrue(all(point.adx == 0 for point in points[5:]))
        self.assertTrue(all(point.normalized_bbp is None for point in points))

    def test_directional_ties_contribute_neither_positive_nor_negative_dm(self):
        # Equal increases in high and decreases in low are directional ties.
        bars = [bar(index, 10, 11 + index, 9 - index, 10) for index in range(7)]
        points = self.short_points(bars)
        self.assertTrue(all(point.atr > 0 for point in points[2:]))
        self.assertTrue(all(point.plus_di == point.minus_di == 0 for point in points[3:]))
        self.assertEqual(points[5].adx, 0)

    def test_direction_is_separate_from_adx_strength(self):
        rising = [bar(index, 100 + index, 101 + index, 99 + index, 100 + index) for index in range(9)]
        falling = [bar(index, 100 - index, 101 - index, 99 - index, 100 - index) for index in range(9)]
        for points, direction in ((self.short_points(rising), "up"), (self.short_points(falling), "down")):
            with self.subTest(direction=direction):
                self.assertEqual(points[5].adx, 100)
                self.assertGreater(points[5].plus_di if direction == "up" else points[5].minus_di, 0)
                self.assertEqual(points[5].minus_di if direction == "up" else points[5].plus_di, 0)

    def test_default_warmup_indices(self):
        points = compute_indicators([bar(index, 100, 101, 99, 100) for index in range(30)])
        self.assertIsNone(points[12].atr)
        self.assertEqual(points[13].atr, 2)
        self.assertIsNone(points[13].plus_di)
        self.assertEqual(points[14].plus_di, 0)
        self.assertIsNone(points[26].adx)
        self.assertEqual(points[27].adx, 0)

    def test_every_indicator_prefix_is_unchanged_by_future_bars(self):
        extended = GOLDEN + [bar(6, 15, 19, 12, 18), bar(7, 18, 20, 7, 8)]
        complete = self.short_points(extended)
        for length in range(1, len(extended) + 1):
            with self.subTest(length=length):
                self.assertEqual(self.short_points(extended[:length]), complete[:length])

    def test_inputs_are_not_mutated(self):
        original = copy.deepcopy(GOLDEN)
        self.short_points()
        confirmed_pivots(GOLDEN)
        self.assertEqual(GOLDEN, original)

    def test_empty_and_single_bar_inputs(self):
        self.assertEqual(compute_indicators([]), [])
        self.assertEqual(confirmed_pivots([]), [])
        point = compute_indicators(GOLDEN[:1])[0]
        self.assertEqual(point.ema, 10)
        self.assertIsNone(point.atr)
        self.assertIsNone(point.plus_di)
        self.assertIsNone(point.adx)

    def test_invalid_periods_are_rejected(self):
        for name in ("bbp_length", "di_length", "adx_smoothing", "atr_length"):
            for value in (0, -1, 1.5, True):
                with self.subTest(parameter=name, value=value):
                    with self.assertRaises(ValueError):
                        compute_indicators(GOLDEN, **{name: value})

    def test_unordered_or_duplicate_times_are_rejected(self):
        for bars in ([GOLDEN[1], GOLDEN[0]], [GOLDEN[0], GOLDEN[0]]):
            with self.subTest(bars=bars):
                for function in (compute_indicators, confirmed_pivots):
                    with self.assertRaisesRegex(ValueError, "strictly increasing"):
                        function(bars)

    def test_nonfinite_nonpositive_or_invalid_ohlc_is_rejected(self):
        for field, value in (
            ("close", math.nan), ("high", math.inf), ("low", -1),
            ("open", 0), ("close", 13), ("high", 8), ("close", True),
        ):
            with self.subTest(field=field, value=value):
                bars = copy.deepcopy(GOLDEN[:1])
                bars[0][field] = value
                for function in (compute_indicators, confirmed_pivots):
                    with self.assertRaises(ValueError):
                        function(bars)

    def test_missing_fields_and_invalid_timestamps_are_rejected(self):
        bars = copy.deepcopy(GOLDEN[:1])
        del bars[0]["close"]
        with self.assertRaisesRegex(ValueError, "missing"):
            compute_indicators(bars)
        for value in (math.nan, math.inf, 0.5, True, "0"):
            with self.subTest(value=value):
                bars = copy.deepcopy(GOLDEN[:1])
                bars[0]["time"] = value
                with self.assertRaises(ValueError):
                    compute_indicators(bars)

    def test_optional_volume_is_not_used_as_a_price_indicator(self):
        bars = copy.deepcopy(GOLDEN)
        for index, item in enumerate(bars):
            item["volume"] = None if index == 0 else 0
        self.assertEqual(self.short_points(bars), self.short_points())
        bars[0]["volume"] = -1
        with self.assertRaises(ValueError):
            self.short_points(bars)

    def test_availability_metadata_must_be_finite_and_follow_the_label(self):
        for value in (-1, math.nan, math.inf, True):
            with self.subTest(value=value):
                bars = [bar(0, 100, 101, 99, 100, available_at=value)]
                with self.assertRaises(ValueError):
                    compute_indicators(bars)


class PivotTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            bar(index, (high + low) / 2, high, low, (high + low) / 2)
            for index, (high, low) in enumerate(zip(
                [2, 3, 6, 4, 3, 5, 6], [1, 2, 3, 2, 1.5, 2.5, 3],
            ))
        ]

    def test_pivot_requires_two_closed_right_hand_bars(self):
        self.assertEqual(confirmed_pivots(self.bars[:4]), [])
        pivot = confirmed_pivots(self.bars[:5])[0]
        self.assertEqual(pivot.kind, "high")
        self.assertEqual(pivot.price, 6)
        self.assertEqual(pivot.origin_index, 2)
        self.assertEqual(pivot.origin_time, 120)
        self.assertEqual(pivot.confirmed_index, 4)
        self.assertEqual(pivot.confirmed_bar_time, 240)
        self.assertIsNone(pivot.available_at)

    def test_pivots_include_high_and_low_without_unconfirmed_tail(self):
        pivots = confirmed_pivots(self.bars)
        self.assertEqual([(pivot.kind, pivot.origin_index, pivot.confirmed_index) for pivot in pivots], [
            ("high", 2, 4), ("low", 4, 6),
        ])

    def test_tied_extrema_are_not_pivots(self):
        tied_high = copy.deepcopy(self.bars)
        tied_high[3]["high"] = 6
        self.assertFalse(any(pivot.kind == "high" for pivot in confirmed_pivots(tied_high)))
        tied_low = copy.deepcopy(self.bars)
        tied_low[5]["low"] = 1.5
        self.assertFalse(any(pivot.kind == "low" for pivot in confirmed_pivots(tied_low)))

    def test_actual_availability_differs_from_confirming_bar_label(self):
        self.bars[4]["available_at"] = 300.25
        pivot = confirmed_pivots(self.bars[:5])[0]
        self.assertEqual(pivot.confirmed_bar_time, 240)
        self.assertEqual(pivot.available_at, 300.25)
        points = compute_indicators(self.bars[:5])
        self.assertEqual(points[-1].time, 240)
        self.assertEqual(points[-1].available_at, 300.25)

    def test_future_bars_do_not_change_previously_confirmed_pivots(self):
        complete = confirmed_pivots(self.bars)
        for length in range(len(self.bars) + 1):
            with self.subTest(length=length):
                available = [pivot for pivot in complete if pivot.confirmed_index < length]
                self.assertEqual(confirmed_pivots(self.bars[:length]), available)

    def test_pivots_reject_zero_or_invalid_confirmation_lengths(self):
        for parameter in ("left", "right"):
            for value in (0, -1, True, 1.5):
                with self.subTest(parameter=parameter, value=value):
                    with self.assertRaises(ValueError):
                        confirmed_pivots(self.bars, **{parameter: value})


if __name__ == "__main__":
    unittest.main()
