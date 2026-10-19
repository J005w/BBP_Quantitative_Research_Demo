"""CSV comparator checks; synthetic fixtures do not establish platform parity."""

import csv
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bbp_system.indicators import compute_indicators
from bbp_system.parity_reference import FIELDS, FRAMES, ReferenceError, compare_mt5_reference


NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def reference_rows(native="SMA"):
    rows = []
    for frame, seconds in FRAMES.items():
        boundary = int(NOW.timestamp()) // seconds * seconds
        bars = []
        tr = []
        for index in range(500):
            price = 10000 + index * .2 + 50 * math.sin(index / 7)
            width = 25 + index % 11
            bar = {"time": boundary - (500 - index) * seconds, "open": price,
                   "high": price + width, "low": price - width, "close": price + 5 * math.sin(index / 3)}
            bars.append(bar)
            tr.append(max(bar["high"] - bar["low"], abs(bar["high"] - bars[index - 1]["close"]),
                          abs(bar["low"] - bars[index - 1]["close"])) if index else bar["high"] - bar["low"])
        points = compute_indicators(bars)
        for index, (bar, point) in enumerate(zip(bars, points)):
            native_atr = sum(tr[index - 13:index + 1]) / 14 if index >= 13 else 0
            if native == "RMA":
                native_atr = point.atr or 0
            rows.append({"timeframe": frame, "raw_time": bar["time"] + 10800,
                         **{field: bar[field] for field in ("open", "high", "low", "close")},
                         "ema13": point.ema, "bull13": point.bull_power, "bear13": point.bear_power,
                         "adx_wilder14": point.adx or 0, "plus_di14": point.plus_di or 0,
                         "minus_di14": point.minus_di or 0, "native_atr14": native_atr,
                         "rma_atr14": point.atr if point.atr is not None else ""})
    return rows


class ReferenceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "fixture.csv"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rows, fields=FIELDS):
        with self.path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def compare(self, **kwargs):
        defaults = {"exported_at_utc": NOW, "timestamp_offset_seconds": 10800, "source_verified": True}
        defaults.update(kwargs)
        return compare_mt5_reference(self.path, **defaults)

    def test_same_candle_core_and_native_sma_are_distinguished(self):
        self.write(reference_rows())
        result = self.compare()
        self.assertEqual(result["status"], "PASS_CORE_NATIVE_ATR_DIFFERENT_SMOOTHING")
        self.assertTrue(result["core_reference_match"])
        self.assertEqual(result["compared_bars_total"], 800)
        for frame in result["timeframes"].values():
            self.assertEqual(frame["warmup_bars"], 300)
            self.assertEqual(frame["native_atr14"]["status"], "DISCREPANCY_DIFFERENT_SMOOTHING")
            self.assertEqual(frame["native_atr14"]["vs_simple_moving_average"]["status"], "MATCH")
            self.assertEqual(frame["metrics"]["bbp13"]["status"], "MATCH")
        self.assertEqual(result["tradingview_parity"], "NOT_ESTABLISHED")

    def test_rma_native_reference_matches_both(self):
        self.write(reference_rows(native="RMA"))
        result = self.compare()
        self.assertEqual(result["status"], "PASS_CORE_AND_NATIVE_ATR")
        self.assertTrue(all(frame["native_atr14"]["status"] == "MATCH_RMA" for frame in result["timeframes"].values()))

    def test_source_not_inferred_from_filename_or_numerical_agreement(self):
        self.write(reference_rows())
        result = self.compare(source_verified=False)
        self.assertEqual(result["status"], "UNVERIFIED_REFERENCE_SOURCE")
        self.assertFalse(result["source_execution_verified_by_caller"])

    def test_changed_directional_reference_reports_exact_metric(self):
        rows = reference_rows()
        rows[-1]["plus_di14"] += .1
        self.write(rows)
        result = self.compare()
        self.assertEqual(result["status"], "FAIL_REFERENCE_DISCREPANCY")
        metric = result["timeframes"]["M5"]["metrics"]["plus_di14"]
        self.assertEqual(metric["status"], "DISCREPANCY")
        self.assertAlmostEqual(metric["max_absolute_error"], .1)
        self.assertEqual(metric["compared_values"], 200)

    def test_unexplained_native_atr_difference_is_not_called_smoothing_difference(self):
        rows = reference_rows()
        rows[-1]["native_atr14"] += 100
        self.write(rows)
        result = self.compare()
        self.assertEqual(result["status"], "FAIL_REFERENCE_DISCREPANCY")
        self.assertEqual(result["timeframes"]["M5"]["native_atr14"]["status"], "DISCREPANCY_UNCLASSIFIED")

    def test_all_four_timeframes_and_full_warmup_required(self):
        rows = reference_rows()
        for selected in ([row for row in rows if row["timeframe"] != "H4"], rows[1:]):
            self.write(selected)
            with self.assertRaises(ReferenceError):
                self.compare()

    def test_forming_bars_and_wrong_offset_rejected(self):
        self.write(reference_rows())
        with self.assertRaises(ReferenceError):
            self.compare(timestamp_offset_seconds=0)
        with self.assertRaises(ReferenceError):
            self.compare(exported_at_utc=datetime(2026, 10, 3, 11, 59, tzinfo=timezone.utc))

    def test_field_order_alignment_and_extra_fields_rejected(self):
        rows = reference_rows()
        fields = ("raw_time", "timeframe", *FIELDS[2:])
        self.write(rows, fields=fields)
        with self.assertRaises(ReferenceError):
            self.compare()

    def test_duplicate_unordered_and_off_grid_timestamps_rejected(self):
        for delta in (0, -14400, 1):
            rows = reference_rows()
            rows[1]["raw_time"] = rows[0]["raw_time"] + delta
            self.write(rows)
            with self.assertRaises(ReferenceError):
                self.compare()

    def test_empty_buffer_sentinel_or_missing_post_warmup_value_rejected(self):
        for name, value in (("ema13", 1.7976931348623157e308), ("rma_atr14", ""), ("adx_wilder14", float("nan"))):
            rows = reference_rows()
            rows[-1][name] = value
            self.write(rows)
            with self.assertRaises(ReferenceError):
                self.compare()

    def test_bad_arguments_rejected(self):
        self.write(reference_rows())
        for changes in ({"warmup_bars": 27}, {"compare_bars": 0}, {"price_tolerance": -1},
                        {"source_verified": "yes"}, {"timestamp_offset_seconds": 3600},
                        {"exported_at_utc": datetime(2026, 10, 3)}):
            with self.subTest(changes=changes), self.assertRaises(ReferenceError):
                self.compare(**changes)


if __name__ == "__main__":
    unittest.main()
