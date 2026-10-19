"""Causal availability, symmetric transitions and frozen level provenance."""

import copy
import unittest

from bbp_system.indicators import ConfirmedPivot
from bbp_system.research_levels import inspect_level, local_pivot_prominence


def pivot(kind="high", price=100, known_at=180):
    return ConfirmedPivot(kind, price, 1, 60, 2, 120, known_at)


def bar(time, opening, high, low, close, available_at=None):
    return {"time": time, "open": opening, "high": high, "low": low,
            "close": close, "available_at": time+60 if available_at is None else available_at}


class ResearchLevelTests(unittest.TestCase):
    def inspect(self, bars=(), **kwargs):
        options = dict(timeframe="H1", bars=bars, as_of=600, tick_size=1, bar_seconds=60)
        options.update(kwargs)
        return inspect_level(pivot(), **options)

    def test_touch_break_flip_reclaim_are_distinct_known_events(self):
        bars = [bar(180, 98, 100, 97, 98), bar(240, 98, 103, 98, 102),
                bar(300, 103, 104, 100, 103), bar(360, 102, 103, 98, 98)]
        expected = [(240, "tested"), (300, "broken"), (360, "flipped"), (420, "reclaimed")]
        for cutoff, state in expected:
            with self.subTest(cutoff=cutoff):
                observation = self.inspect(bars, as_of=cutoff)
                self.assertEqual(observation.state, state)
                self.assertTrue(all(event.known_at <= cutoff for event in observation.events))
        self.assertEqual((observation.touch_bars, observation.break_count,
                          observation.reclaim_count, observation.opposite_side_retest_count), (4, 1, 1, 1))

    def test_same_break_bar_cannot_confirm_opposite_side_retest(self):
        observation = self.inspect([bar(180, 103, 104, 100, 103)])
        self.assertEqual(observation.state, "broken")
        self.assertEqual(observation.opposite_side_retest_count, 0)

    def test_low_support_is_symmetric(self):
        bars = [bar(180, 102, 103, 100, 102), bar(240, 102, 102, 97, 98),
                bar(300, 97, 100, 96, 97), bar(360, 98, 102, 97, 102)]
        observation = inspect_level(pivot("low"), timeframe="H1", bars=bars,
                                    as_of=420, tick_size=1, bar_seconds=60)
        self.assertEqual(observation.original_role, "support")
        self.assertEqual(observation.state, "reclaimed")
        self.assertEqual([event.kind for event in observation.events if event.kind != "touch"],
                         ["break", "opposite_side_retest", "reclaim"])

    def test_close_at_zone_edge_is_touch_not_break(self):
        observation = self.inspect([bar(180, 98, 102, 98, 101)])
        self.assertEqual(observation.state, "tested")
        self.assertEqual(observation.break_count, 0)

    def test_gap_close_breaks_without_fabricating_a_touch(self):
        observation = self.inspect([bar(180, 103, 104, 102, 103)])
        self.assertEqual(observation.state, "broken")
        self.assertEqual(observation.touch_bars, 0)

    def test_future_pivot_is_hidden_and_unknown_confirmation_rejected(self):
        self.assertIsNone(self.inspect(as_of=179))
        with self.assertRaises(ValueError):
            inspect_level(pivot(known_at=None), timeframe="H1", bars=[], as_of=600, tick_size=1)

    def test_full_history_and_visible_prefix_identical_at_every_cutoff(self):
        bars = [bar(180, 98, 100, 97, 98), bar(240, 98, 103, 98, 102),
                bar(300, 103, 104, 100, 103), bar(360, 102, 103, 98, 98)]
        for cutoff in (180, 239, 240, 299, 300, 360, 420):
            prefix = [record for record in bars if record["available_at"] <= cutoff]
            self.assertEqual(self.inspect(bars, as_of=cutoff), self.inspect(prefix, as_of=cutoff))

    def test_delayed_bar_hidden_and_unavailable_prices_ignored(self):
        history = [bar(180, 98, 100, 97, 98),
                   bar(240, 98, 103, 98, 102, available_at=500)]
        changed = copy.deepcopy(history)
        changed[-1]["high"] = float("nan")
        self.assertEqual(self.inspect(history, as_of=300), self.inspect(changed, as_of=300))
        self.assertEqual(self.inspect(history, as_of=300).state, "tested")
        self.assertEqual(self.inspect(history, as_of=500).state, "broken")

    def test_confirmation_bar_and_partially_preknowledge_bar_not_retests(self):
        observation = self.inspect([bar(120, 98, 105, 97, 102),
                                    bar(180, 98, 105, 97, 102)], as_of=300)
        self.assertEqual(observation.break_count, 1)
        delayed_pivot = pivot(known_at=200)
        observation = inspect_level(delayed_pivot, timeframe="H1",
                                    bars=[bar(180, 98, 105, 97, 102)],
                                    as_of=300, tick_size=1, bar_seconds=60)
        self.assertEqual(observation.state, "fresh")
        self.assertIsNone(observation.first_observed_bar_time)

    def test_atr_zone_is_frozen_and_future_atr_rejected(self):
        observation = self.inspect(atr_at_confirmation=20, atr_known_at=180, zone_atr_fraction=0.1)
        self.assertEqual((observation.zone_low, observation.zone_high), (98, 102))
        with self.assertRaises(ValueError):
            self.inspect(atr_at_confirmation=20, atr_known_at=181, zone_atr_fraction=0.1)
        with self.assertRaises(ValueError):
            self.inspect(zone_atr_fraction=0.1)

    def test_price_overlap_preserves_timeframe_and_origin_provenance(self):
        first = self.inspect()
        other = self.inspect(timeframe="H4")
        another = inspect_level(ConfirmedPivot("high", 100, 0, 0, 2, 120, 180),
                                timeframe="H1", bars=[], as_of=600, tick_size=1)
        self.assertEqual(first.price, other.price)
        self.assertEqual(len({first.identity, other.identity, another.identity}), 3)

    def test_missing_intervals_and_truncated_history_are_reported(self):
        observation = self.inspect([bar(240, 95, 96, 94, 95), bar(420, 95, 96, 94, 95)])
        self.assertTrue(observation.history_starts_after_confirmation)
        self.assertEqual(observation.missing_nominal_intervals, 2)
        self.assertEqual(observation.last_known_bar_time, 420)
        self.assertEqual(observation.age_hours, (600-180)/3600)
        self.assertEqual(observation.state, "fresh")

    def test_nominal_availability_requires_explicit_duration_and_discloses_assumption(self):
        record = bar(180, 98, 100, 97, 98)
        del record["available_at"]
        self.assertIn("assumed", self.inspect([record]).availability_basis)
        with self.assertRaises(ValueError):
            self.inspect([record], bar_seconds=None)

    def test_local_prominence_requires_complete_available_neighborhood(self):
        bars = [bar(0, 95, 97, 94, 95), bar(60, 97, 100, 96, 98),
                bar(120, 96, 98, 95, 96), bar(180, 98, 110, 97, 100)]
        self.assertEqual(local_pivot_prominence(pivot(), bars=bars, left=1, right=1), 2)
        self.assertIsNone(local_pivot_prominence(pivot(), bars=bars[:2], left=1, right=1))
        self.assertEqual(local_pivot_prominence(pivot(), bars=bars[:3], left=1, right=1), 2)

    def test_explicit_epoch_can_still_be_declared_assumed_nominal(self):
        record = bar(180, 98, 100, 97, 98)
        record["availability_basis"] = "ASSUMED_NOMINAL_BAR_CLOSE_NOT_OBSERVED_ARRIVAL"
        self.assertIn("assumed", self.inspect([record]).availability_basis)

    def test_gap_in_local_prominence_neighborhood_returns_unknown(self):
        bars = [bar(0, 95, 97, 94, 95), bar(60, 97, 100, 96, 98),
                bar(180, 96, 98, 95, 96)]
        later_confirmation = ConfirmedPivot("high", 100, 1, 60, 2, 180, 240)
        self.assertIsNone(local_pivot_prominence(later_confirmation, bars=bars,
                                                left=1, right=1, bar_seconds=60))

    def test_invalid_price_and_availability_rejected(self):
        for value in (0, -1, float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.inspect(tick_size=value)
        with self.assertRaises(ValueError):
            self.inspect([bar(180, 98, 100, 97, 98, available_at=200)])


if __name__ == "__main__":
    unittest.main()
