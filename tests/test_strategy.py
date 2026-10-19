"""Rule fixtures and chronological causality checks, not indicator parity."""

import json
import unittest

from bbp_system.strategy import (
    FrameFeatures, PERIODS, evaluate_strategy, nearest_target, replay_strategy,
)


T = 144000


def bar(opening, seconds, *, close=108, low=100, high=120):
    return {"time": opening, "available_at": opening + seconds,
            "open": close, "high": high, "low": low, "close": close}


def point(record, *, ema=105, bbp=2, plus=30, minus=10, adx=30, atr=2):
    return {"time": record["time"], "available_at": record["available_at"],
            "ema": ema, "bbp": bbp, "plus_di": plus, "minus_di": minus,
            "adx": adx, "atr": atr}


def pivot(kind, price, origin, confirming):
    return {"kind": kind, "price": price, "origin_time": origin,
            "confirmed_bar_time": confirming["time"], "available_at": confirming["available_at"]}


def fixture(*, confirm_on=None, short=False):
    """Explicit known indicator/pivot fixtures isolate the frozen strategy rules."""
    h4 = [bar(T - (10 - index) * 14400, 14400) for index in range(10)]
    h1 = [bar(T - (10 - index) * 3600, 3600) for index in range(10)]
    m15 = [bar(T - (4 - index) * 900, 900) for index in range(4)]
    m15[0].update(close=104, open=104, low=100)
    m15[1].update(close=106, open=106, low=104)
    m15[2].update(close=106, open=106, low=104)
    m15[3].update(close=108, open=108, low=105)
    m5 = [bar(T - 3600 + index * 300, 300, high=114, low=101) for index in range(18)]
    m5[0]["low"] = 99
    if confirm_on is not None:
        m5[11 + confirm_on].update(close=112, open=112, high=114)
    result = {
        "H4": FrameFeatures(tuple(h4), tuple(point(item) for item in h4), (
            pivot("high", 110, h4[2]["time"], h4[4]),
            pivot("low", 80, h4[3]["time"], h4[5]),
            pivot("high", 150, h4[6]["time"], h4[8]),
            pivot("low", 90, h4[7]["time"], h4[9]),
        )),
        "H1": FrameFeatures(tuple(h1), tuple(point(item) for item in h1), (
            pivot("high", 140, h1[3]["time"], h1[5]),
            pivot("low", 95, h1[7]["time"], h1[9]),
        )),
        "M15": FrameFeatures(tuple(m15), tuple(
            point(item, ema=103 if index < 3 else 105, bbp=1 if index < 3 else 2)
            for index, item in enumerate(m15)
        ), ()),
        "M5": FrameFeatures(tuple(m5), tuple(point(item) for item in m5), (
            pivot("high", 110, m5[6]["time"], m5[8]),
            pivot("low", 100, m5[7]["time"], m5[9]),
        )),
    }
    if short:
        mirrored = {}
        for timeframe, frame in result.items():
            bars = tuple(dict(item, open=200-item["open"], close=200-item["close"],
                              high=200-item["low"], low=200-item["high"]) for item in frame.bars)
            points = tuple(dict(item, ema=200-item["ema"], bbp=-item["bbp"],
                                plus_di=item["minus_di"], minus_di=item["plus_di"]) for item in frame.indicators)
            pivots = tuple(dict(item, kind="low" if item["kind"] == "high" else "high",
                                price=200-item["price"]) for item in frame.pivots)
            mirrored[timeframe] = FrameFeatures(bars, points, pivots)
        return mirrored
    return result


def change_point(frames, timeframe, index, **values):
    frame = frames[timeframe]
    points = list(frame.indicators)
    points[index] = dict(points[index], **values)
    frames[timeframe] = FrameFeatures(frame.bars, tuple(points), frame.pivots)


class StrategyTests(unittest.TestCase):
    def evaluate(self, frames=None, at=T, **options):
        return evaluate_strategy(frames or fixture(), decision_time=at, tick_size=.25, **options)

    def test_candidate_freezes_first_four_bar_touch_and_known_structure(self):
        state = self.evaluate()
        self.assertEqual(state["state"], "CANDIDATE")
        self.assertEqual(state["side"], "LONG")
        self.assertEqual(state["candidate_at"], T)
        self.assertEqual(state["pullback_onset"], T - 3600)
        self.assertEqual((state["invalidation"], state["trigger"]), (95, 110))
        self.assertLessEqual(state["trigger_available_at"], state["candidate_at"])
        self.assertLessEqual(state["invalidation_available_at"], state["candidate_at"])
        self.assertEqual(state["context"], {"regime": "TREND", "direction": "LONG", "bias": "LONG"})
        self.assertIsNone(state["net_rr"])
        self.assertFalse(state["orders_enabled"])
        json.dumps(state, allow_nan=False)

    def test_long_and_short_confirmation_use_structural_buffer_and_inward_target(self):
        for short, side, stop, target, entry in (
            (False, "LONG", 98.75, 139.75, 112), (True, "SHORT", 101.25, 60.25, 88),
        ):
            with self.subTest(side=side):
                state = self.evaluate(fixture(confirm_on=1, short=short), at=T+300)
                self.assertEqual(state["state"], "CONFIRMED")
                self.assertEqual(state["side"], side)
                self.assertEqual((state["stop"], state["target"], state["reference_entry"]), (stop, target, entry))
                self.assertEqual(state["confirmation_at"], T+300)
                self.assertEqual(state["review_expires_at"], T+600)
                self.assertEqual(state["risk_status"], "NOT_ASSESSED")

    def test_atr_buffer_can_exceed_one_tradable_tick(self):
        frames = fixture(confirm_on=1)
        change_point(frames, "M5", 12, atr=10)
        self.assertEqual(self.evaluate(frames, at=T+300)["stop"], 98)

    def test_decimal_grid_does_not_add_a_tick_due_to_binary_rounding(self):
        frames = fixture(confirm_on=1)
        m5 = frames["M5"]
        bars = list(m5.bars)
        bars[0] = dict(bars[0], low=100.2)
        frames["M5"] = FrameFeatures(tuple(bars), m5.indicators, m5.pivots)
        change_point(frames, "M5", 12, atr=3)
        result = evaluate_strategy(frames, decision_time=T+300, tick_size=.1)
        self.assertEqual((result["stop"], result["target"]), (99.9, 139.9))

    def test_confirmation_boundaries_sixth_allowed_then_no_confirmation_expires(self):
        sixth = fixture(confirm_on=6)
        before = self.evaluate(sixth, at=T+1500)
        self.assertEqual(before["state"], "CANDIDATE")
        self.assertEqual(before["confirmation_bars_elapsed"], 5)
        at_sixth = self.evaluate(sixth, at=T+1800, previous=before)
        self.assertEqual(at_sixth["state"], "CONFIRMED")
        self.assertEqual(at_sixth["confirmation_bars_elapsed"], 6)
        expired = self.evaluate(fixture(), at=T+1800)
        self.assertEqual(expired["state"], "EXPIRED")
        self.assertEqual(expired["reasons"][0]["code"], "CONFIRMATION_EXPIRED")

    def test_review_expires_exactly_one_more_interval_and_cannot_recreate(self):
        frames = fixture(confirm_on=1)
        confirmed = self.evaluate(frames, at=T+300)
        alive = self.evaluate(frames, at=T+599, previous=confirmed)
        self.assertEqual(alive["state"], "CONFIRMED")
        expired = self.evaluate(frames, at=T+600, previous=alive)
        self.assertEqual(expired["state"], "EXPIRED")
        self.assertEqual(expired["reasons"][0]["code"], "REVIEW_EXPIRED")
        for at in (T+601, T+900, T+1200):
            expired = self.evaluate(frames, at=at, previous=expired)
            self.assertEqual(expired["state"], "EXPIRED")
            self.assertEqual(expired["setup_id"], confirmed["setup_id"])

    def test_late_capture_reconstructs_confirmation_and_reports_expired_review(self):
        state = self.evaluate(fixture(confirm_on=1), at=T+900)
        self.assertEqual(state["state"], "EXPIRED")
        self.assertEqual(state["confirmation_at"], T+300)
        self.assertEqual(state["reference_entry"], 112)

    def test_frozen_invalidation_is_not_replaced_by_new_h1_pivot(self):
        frames = fixture()
        h1 = frames["H1"]
        bars = list(h1.bars)
        bars[-1] = dict(bars[-1], available_at=T+300)
        points = list(h1.indicators)
        points[-1] = dict(points[-1], available_at=T+300)
        old = pivot("low", 95, bars[6]["time"], bars[8])
        new = pivot("low", 105, bars[7]["time"], bars[9])
        frames["H1"] = FrameFeatures(tuple(bars), tuple(points), (h1.pivots[0], old, new))
        m5 = frames["M5"]
        bars = list(m5.bars)
        bars[12] = dict(bars[12], close=100, open=100, low=99)
        frames["M5"] = FrameFeatures(tuple(bars), m5.indicators, m5.pivots)
        candidate = self.evaluate(frames)
        next_state = self.evaluate(frames, at=T+300, previous=candidate)
        self.assertEqual(next_state["state"], "CANDIDATE")
        self.assertEqual(next_state["invalidation"], 95)

    def test_invalidating_m5_close_precedes_signal_even_if_later_recovery_occurs(self):
        frames = fixture(confirm_on=2)
        m5 = frames["M5"]
        bars = list(m5.bars)
        bars[12] = dict(bars[12], close=94, open=94, low=93)
        frames["M5"] = FrameFeatures(tuple(bars), m5.indicators, m5.pivots)
        result = self.evaluate(frames, at=T+600)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["reasons"][0]["code"], "STRUCTURE_INVALIDATED")
        self.assertIsNone(result["confirmation_at"])

    def test_ema_recovery_and_momentum_must_improve_in_the_correct_direction(self):
        frames = fixture()
        change_point(frames, "M15", -1, bbp=1)
        result = self.evaluate(frames)
        self.assertEqual(result["state"], "WATCHING")
        self.assertEqual(result["reasons"][0]["code"], "NO_M15_RECOVERY")
        frames = fixture()
        change_point(frames, "M15", -1, ema=108)
        self.assertEqual(self.evaluate(frames)["reasons"][0]["code"], "NO_M15_RECOVERY")

    def test_adx_threshold_missing_structure_range_and_transition_are_explicit(self):
        frames = fixture()
        change_point(frames, "H4", -1, adx=24.99)
        result = self.evaluate(frames)
        self.assertEqual((result["state"], result["context"]["regime"]), ("WATCHING", "TRANSITION"))
        frames = fixture()
        h4 = frames["H4"]
        pivots = list(h4.pivots)
        pivots[-1] = dict(pivots[-1], price=80)  # tied lows do not make HH/HL
        frames["H4"] = FrameFeatures(h4.bars, h4.indicators, tuple(pivots))
        change_point(frames, "H4", -1, adx=19)
        self.assertEqual(self.evaluate(frames)["context"]["regime"], "RANGE")
        frames["H4"] = FrameFeatures(h4.bars, h4.indicators, ())
        self.assertEqual(self.evaluate(frames)["context"]["regime"], "UNCLASSIFIED")

    def test_higher_timeframe_conflict_cancels_when_delayed_observation_arrives(self):
        frames = fixture()
        h1 = frames["H1"]
        bars = list(h1.bars)
        bars[-1] = dict(bars[-1], available_at=T+300)
        points = list(h1.indicators)
        points[-1] = dict(points[-1], available_at=T+300, bbp=-2, plus_di=10, minus_di=30)
        known = pivot("low", 95, bars[6]["time"], bars[8])
        frames["H1"] = FrameFeatures(tuple(bars), tuple(points), (h1.pivots[0], known))
        candidate = self.evaluate(frames)
        self.assertEqual(candidate["state"], "CANDIDATE")
        cancelled = self.evaluate(frames, at=T+300, previous=candidate)
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.assertEqual(cancelled["reasons"][0]["code"], "TIMEFRAME_CONFLICT")

    def test_known_m5_trigger_does_not_change_when_later_pivot_is_confirmed(self):
        frames = fixture(confirm_on=4)
        m5 = frames["M5"]
        future = pivot("high", 120, m5.bars[11]["time"], m5.bars[13])
        frames["M5"] = FrameFeatures(m5.bars, m5.indicators, (*m5.pivots, future))
        candidate = self.evaluate(frames)
        confirmed = self.evaluate(frames, at=T+1200, previous=candidate)
        self.assertEqual(confirmed["state"], "CONFIRMED")
        self.assertEqual(confirmed["trigger"], 110)
        self.assertEqual(confirmed["setup_id"], candidate["setup_id"])

    def test_target_uses_nearest_known_level_and_does_not_skip_inward_invalid_level(self):
        frames = fixture()
        self.assertEqual(nearest_target(frames, decision_time=T, side="LONG", entry=112, tick_size=.25), 139.75)
        h1 = frames["H1"]
        near = dict(h1.pivots[0], price=112.1)
        frames["H1"] = FrameFeatures(h1.bars, h1.indicators, (near, *h1.pivots[1:]))
        self.assertIsNone(nearest_target(frames, decision_time=T, side="LONG", entry=112, tick_size=.25))
        h1_no_target = FrameFeatures(h1.bars, h1.indicators, (h1.pivots[1],))
        frames["H1"] = h1_no_target
        h4 = frames["H4"]
        frames["H4"] = FrameFeatures(h4.bars, h4.indicators, tuple(p for p in h4.pivots if p["kind"] == "low"))
        self.assertIsNone(nearest_target(frames, decision_time=T, side="LONG", entry=112, tick_size=.25))

    def test_missing_target_rejects_confirmation_and_net_rr_remains_unassessed(self):
        frames = fixture(confirm_on=1)
        h1 = frames["H1"]
        frames["H1"] = FrameFeatures(h1.bars, h1.indicators, tuple(
            dict(p, price=109) if p["kind"] == "high" else p for p in h1.pivots
        ))
        h4 = frames["H4"]
        frames["H4"] = FrameFeatures(h4.bars, h4.indicators, tuple(
            dict(p, price=107 if index == 0 else 110) if p["kind"] == "high" else p
            for index, p in enumerate(h4.pivots)
        ))
        result = self.evaluate(frames, at=T+300)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["reasons"][0]["code"], "MISSING_TARGET")
        self.assertIsNone(result["net_rr"])

    def test_stale_closed_session_short_permission_and_missing_data_reject(self):
        candidate = self.evaluate()
        for key, code in (("data_fresh", "STALE_DATA"), ("market_open", "MARKET_CLOSED")):
            result = self.evaluate(at=T+300, previous=candidate, **{key: False})
            self.assertEqual(result["state"], "CANCELLED")
            self.assertEqual(result["reasons"][0]["code"], code)
        result = self.evaluate(fixture(short=True), short_allowed=False)
        self.assertEqual(result["reasons"][0]["code"], "UNSUPPORTED_SHORT")
        frames = fixture()
        del frames["M15"]
        result = self.evaluate(frames, previous=candidate)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["reasons"][0]["code"], "MISSING_INVALID_DATA")

    def test_full_history_and_causal_prefix_have_identical_decisions_and_ids(self):
        frames = fixture(confirm_on=3)
        prefix = {}
        for tf, frame in frames.items():
            bars = tuple(item for item in frame.bars if item["available_at"] <= T+900)
            labels = {item["time"] for item in bars}
            prefix[tf] = FrameFeatures(
                bars, tuple(item for item in frame.indicators if item["time"] in labels),
                tuple(item for item in frame.pivots if item["available_at"] <= T+900),
            )
        full = replay_strategy(frames, tick_size=.25, until=T+900)
        incremental = replay_strategy(prefix, tick_size=.25)
        self.assertEqual(full, incremental)
        prior = None
        online = []
        for item in full:
            prior = self.evaluate(frames, at=item["decision_time"], previous=prior)
            online.append(prior)
        self.assertEqual(full, online)
        self.assertEqual(full[-1]["state"], "CONFIRMED")

    def test_future_forming_h4_values_and_future_pivots_cannot_affect_past(self):
        frames = fixture(confirm_on=1)
        expected = self.evaluate(frames, at=T+300)
        h4 = frames["H4"]
        future_bar = bar(T, 14400, close=60, low=50, high=70)
        future_point = point(future_bar, bbp=-50, plus=0, minus=99, adx=99)
        frames["H4"] = FrameFeatures(
            (*h4.bars, future_bar), (*h4.indicators, future_point),
            (*h4.pivots, pivot("high", 113, T-28800, future_bar)),
        )
        self.assertEqual(self.evaluate(frames, at=T+300), expected)

    def test_nominal_close_and_delayed_availability_are_both_enforced(self):
        frames = fixture(confirm_on=1)
        m15 = frames["M15"]
        bars = list(m15.bars)
        bars[-1] = dict(bars[-1], available_at=T+600)
        points = list(m15.indicators)
        points[-1] = dict(points[-1], available_at=T+600)
        frames["M15"] = FrameFeatures(tuple(bars), tuple(points), m15.pivots)
        result = self.evaluate(frames, at=T+300)
        self.assertNotEqual(result["state"], "CONFIRMED")
        result = self.evaluate(frames, at=T+600)
        self.assertEqual(result["candidate_at"], T+600)
        self.assertEqual(result["state"], "CANDIDATE")
        self.assertEqual(result["confirmation_bars_elapsed"], 0)

    def test_raw_bar_path_uses_shared_core_and_ignores_unclosed_values(self):
        frames = {tf: {"bars": [bar(T-period*(40-index), period) for index in range(40)]}
                  for tf, period in PERIODS.items()}
        result = self.evaluate(frames)
        self.assertEqual(result["state"], "WATCHING")
        self.assertEqual(result["context"]["regime"], "UNCLASSIFIED")
        frames["H4"]["bars"].append(bar(T, 14400, close=1, low=1, high=999))
        self.assertEqual(self.evaluate(frames), result)

    def test_previous_state_time_symbol_and_tick_guards(self):
        state = self.evaluate()
        with self.assertRaises(ValueError):
            self.evaluate(at=T-1, previous=state)
        with self.assertRaises(ValueError):
            self.evaluate(previous=dict(state, symbol="ETHUSD"))
        with self.assertRaises(ValueError):
            evaluate_strategy(fixture(), decision_time=T, tick_size=0)

    def test_missing_pullback_candle_prevents_an_invented_structural_stop(self):
        frames = fixture(confirm_on=1)
        m5 = frames["M5"]
        frames["M5"] = FrameFeatures(
            tuple(item for index, item in enumerate(m5.bars) if index != 2),
            tuple(item for index, item in enumerate(m5.indicators) if index != 2), m5.pivots,
        )
        result = self.evaluate(frames, at=T+300)
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["reasons"][0]["code"], "INSUFFICIENT_STOP_HISTORY")

    def test_out_of_order_arrival_is_rejected_before_using_dependent_indicators(self):
        frames = fixture()
        m15 = frames["M15"]
        bars = list(m15.bars)
        points = list(m15.indicators)
        bars[-2] = dict(bars[-2], available_at=T+100)
        points[-2] = dict(points[-2], available_at=T+100)
        frames["M15"] = FrameFeatures(tuple(bars), tuple(points), m15.pivots)
        for at in (T, T+300):
            result = self.evaluate(frames, at=at)
            self.assertEqual(result["state"], "WATCHING")
            self.assertEqual(result["reasons"][0]["code"], "MISSING_INVALID_DATA")

    def test_delayed_confirmation_can_be_invalidated_before_review_expiry(self):
        frames = fixture(confirm_on=1)
        m5 = frames["M5"]
        bars = list(m5.bars)
        points = list(m5.indicators)
        bars[12] = dict(bars[12], available_at=T+400)
        points[12] = dict(points[12], available_at=T+400)
        bars[13] = dict(bars[13], close=94, open=94, low=93)
        frames["M5"] = FrameFeatures(tuple(bars), tuple(points), m5.pivots)
        confirmed = self.evaluate(frames, at=T+400)
        self.assertEqual(confirmed["state"], "CONFIRMED")
        self.assertEqual(confirmed["review_expires_at"], T+700)
        cancelled = self.evaluate(frames, at=T+600, previous=confirmed)
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.assertEqual(cancelled["reasons"][0]["code"], "STRUCTURE_INVALIDATED")

    def test_directional_ties_and_unimproving_m5_momentum_do_not_confirm(self):
        frames = fixture(confirm_on=1)
        change_point(frames, "M5", 12, plus_di=10, minus_di=10)
        self.assertEqual(self.evaluate(frames, at=T+300)["state"], "CANDIDATE")
        frames = fixture()
        change_point(frames, "H4", -1, plus_di=10, minus_di=10)
        self.assertEqual(self.evaluate(frames)["context"]["regime"], "TRANSITION")


if __name__ == "__main__":
    unittest.main()
