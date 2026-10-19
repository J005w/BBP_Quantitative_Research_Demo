"""Causal execution/risk fixtures, not historical success or broker parity."""
import copy
import json
import math
from pathlib import Path
import unittest
from datetime import datetime

from bbp_system.backtest import run_backtest, run_walk_forward, bar_exit_event, _MemoryLedger
from bbp_system.config import load_config
from bbp_system.strategy import (FrameFeatures, PERIODS, PreparedFrames, RollingFrames, evaluate_strategy,
                                 prepare_frames, nearest_target)
from bbp_system.sizing import (RiskPolicy, BrokerBasis, CostProfile, QuantityGrid, SizingRequest,
                              request_metrics)
from bbp_system.paper import Quote
from decimal import Decimal
from test_strategy import fixture, T


ROOT = Path(__file__).resolve().parents[1]


def datetime_epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def research_fixture():
    frames = fixture(confirm_on=1)
    # Preserve rule fixture while giving the net-cost target enough distance.
    for tf in ("H1", "H4"):
        frame = frames[tf]
        pivots = tuple(dict(p, price=1000 if tf == "H1" else 1001)
                       if p["kind"] == "high" and p["price"] >= 140 else p for p in frame.pivots)
        frames[tf] = FrameFeatures(frame.bars, frame.indicators, pivots)
    m5 = frames["M5"]
    bars = []
    for index, source in enumerate(m5.bars):
        bar = dict(source, spread_points=10)
        if index >= 13:
            bar.update(open=113, close=113, high=116, low=105)
        bars.append(bar)
    frames["M5"] = FrameFeatures(tuple(bars), m5.indicators, m5.pivots)
    return {"symbol": "BTCUSD", "frames": frames, "metadata": {
        "point": .01, "trade_tick_size": .25, "trade_contract_size": 1,
        "currency_profit": "USD", "chart_mode": 0, "volume_min": .01,
        "volume_max": 10, "volume_step": .01, "volume_limit": 10,
    }}


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config" / "paper.json")
        self.scenario = json.loads((ROOT / "config" / "btc_research.json").read_text())

    def run_fixture(self, data=None, **options):
        data = data or research_fixture()
        # Explicit synthetic features isolate execution rules. Production uses
        # exact raw-candle RollingFrames; its parity is covered separately.
        cache = PreparedFrames(data["frames"], settings=self.config.strategy)
        return run_backtest(data, self.config, self.scenario,
                            warmup_h4=10, _prepared=cache, **options)

    def test_cached_and_uncached_prefix_decisions_are_identical(self):
        frames = fixture(confirm_on=1)
        cache = PreparedFrames(frames)
        old, cached = None, None
        for at in range(T-600, T+1801, 100):
            old = evaluate_strategy(frames, decision_time=at, tick_size=.25, previous=old)
            cached = evaluate_strategy(cache, decision_time=at, tick_size=.25, previous=cached)
            self.assertEqual(old, cached)
            self.assertEqual(prepare_frames(frames, decision_time=at), cache.at(at))

    def test_cache_handles_ordered_late_arrivals_without_future_pivots(self):
        frames = fixture(confirm_on=1)
        source = frames["M5"]
        bars = tuple(dict(b, available_at=b["available_at"]+90) for b in source.bars)
        points = tuple(dict(p, available_at=p["available_at"]+90) for p in source.indicators)
        pivots = tuple(dict(p, available_at=p["available_at"]+90) for p in source.pivots)
        frames["M5"] = FrameFeatures(bars, points, pivots)
        cache = PreparedFrames(frames)
        for at in (T, T+300, T+389, T+390, T+599):
            self.assertEqual(evaluate_strategy(frames, decision_time=at, tick_size=.25),
                             evaluate_strategy(cache, decision_time=at, tick_size=.25))
            self.assertTrue(all(p["available_at"] <= at for p in cache.at(at)["M5"].pivots))

    def test_cache_rejects_arrival_order_that_contaminates_features(self):
        frames = fixture()
        source = frames["M5"]
        bars = list(source.bars)
        bars[8] = dict(bars[8], available_at=T+9999)
        frames["M5"] = FrameFeatures(tuple(bars), source.indicators, source.pivots)
        with self.assertRaises(ValueError):
            PreparedFrames(frames)

    def test_rolling_views_match_naive_499_candle_calculations_and_old_prefixes(self):
        anchor = 1800000000
        raw = {}
        for tf, seconds in PERIODS.items():
            bars = []
            for index in range(700):
                opening = anchor-(700-index)*seconds
                price = 1000+index*.05+5*math.sin(index/3)
                bars.append({"time": opening, "available_at": opening+seconds,
                             "open": price, "close": price+.2,
                             "high": price+1, "low": price-1})
            raw[tf] = {"bars": bars}
        rolling = RollingFrames(raw, window=499, settings=self.config.strategy)
        for at in (anchor-3600, anchor-900, anchor-300, anchor, anchor-900, anchor):
            limited = {tf: {"bars": [b for b in frame["bars"] if b["available_at"] <= at][-499:]}
                       for tf, frame in raw.items()}
            naive = prepare_frames(limited, decision_time=at, settings=self.config.strategy)
            actual = rolling.at(at)
            self.assertEqual(actual, naive)
            self.assertTrue(all(len(frame.bars) == 499 for frame in actual.values()))
            self.assertEqual(evaluate_strategy(rolling, decision_time=at, tick_size=.01,
                                               settings=self.config.strategy),
                             evaluate_strategy(limited, decision_time=at, tick_size=.01,
                                               settings=self.config.strategy))
        from bbp_system.strategy import _at
        self.assertEqual(_at(rolling.at(anchor), anchor-900), rolling.at(anchor-900))

    def test_walk_forward_honors_requested_start_and_end(self):
        data = research_fixture()
        for tf, seconds in PERIODS.items():
            frame = data["frames"][tf]
            bars, points = list(frame.bars), list(frame.indicators)
            for opening in range(bars[-1]["time"]+seconds, T+8*86400, seconds):
                new = {"time": opening, "available_at": opening+seconds,
                       "open": 113, "high": 116, "low": 106, "close": 113, "spread_points": 10}
                bars.append(new)
                points.append(dict(points[-1], time=opening, available_at=opening+seconds, bbp=2))
            data["frames"][tf] = FrameFeatures(tuple(bars), tuple(points), frame.pivots)
        walk = run_walk_forward(data, self.config, self.scenario, warmup_h4=10,
                                context_days=1, test_days=1, start=T+3*86400, end=T+6*86400,
                                _prepared=PreparedFrames(data["frames"], settings=self.config.strategy))
        self.assertEqual(len(walk["folds"]), 2)
        self.assertEqual(datetime_epoch(walk["folds"][0]["start_utc"]), T+4*86400)
        self.assertEqual(datetime_epoch(walk["folds"][-1]["end_utc"]), T+6*86400)

    def test_next_bar_open_fill_uses_previous_spread_not_future_spread(self):
        data = research_fixture()
        source = data["frames"]["M5"]
        bars = list(source.bars)
        bars[13] = dict(bars[13], spread_points=999999)
        data["frames"]["M5"] = FrameFeatures(tuple(bars), source.indicators, source.pivots)
        result = self.run_fixture(data, end=T+600)
        self.assertEqual(result["summary"]["trade_count"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], T+300)
        self.assertAlmostEqual(trade["entry"], 113.1)
        self.assertNotEqual(trade["entry"], source.bars[12]["close"])
        self.assertGreater(trade["costs"], 0)
        self.assertLessEqual(trade["estimated_stop_loss"], 500)

    def test_future_price_mutation_cannot_change_past_entries(self):
        data = research_fixture()
        baseline = self.run_fixture(data, end=T+600)
        source = data["frames"]["M5"]
        bars = list(source.bars)
        bars[16] = dict(bars[16], open=999, high=1000, low=990, close=995)
        data["frames"]["M5"] = FrameFeatures(tuple(bars), source.indicators, source.pivots)
        mutated = self.run_fixture(data, end=T+600)
        self.assertEqual(baseline, mutated)

    def test_both_boundaries_same_bar_choose_stop(self):
        data = research_fixture()
        source = data["frames"]["M5"]
        bars = list(source.bars)
        bars[13] = dict(bars[13], low=50, high=1100)
        data["frames"]["M5"] = FrameFeatures(tuple(bars), source.indicators, source.pivots)
        result = self.run_fixture(data, end=T+600)
        self.assertEqual(result["ambiguous_stop_target_bars"], 1)
        self.assertEqual(result["trades"][0]["reason"], "STOP")
        self.assertLess(result["trades"][0]["pnl"], 0)

    def test_adverse_open_gap_can_exceed_planned_stop_loss(self):
        data = research_fixture()
        source = data["frames"]["M5"]
        bars = list(source.bars)
        bars[14] = dict(bars[14], open=20, high=30, low=10, close=25)
        data["frames"]["M5"] = FrameFeatures(tuple(bars), source.indicators, source.pivots)
        result = self.run_fixture(data, end=T+900)
        trade = result["trades"][0]
        self.assertEqual((trade["reason"], trade["exit"]), ("STOP", 20))
        self.assertGreater(-trade["pnl"], trade["estimated_stop_loss"])

    def test_spread_and_slippage_stress_reduces_net_result(self):
        normal = self.run_fixture(end=T+600)
        stressed = self.run_fixture(end=T+600, spread_multiplier=2, slippage_multiplier=2)
        self.assertEqual(stressed["summary"]["trade_count"], 1)
        self.assertLess(stressed["summary"]["net_profit"], normal["summary"]["net_profit"])

    def test_missing_spread_uses_labelled_scenario_and_may_reject_entry(self):
        data = research_fixture()
        source = data["frames"]["M5"]
        bars = tuple(dict(b, spread_points=None) for b in source.bars)
        data["frames"]["M5"] = FrameFeatures(bars, source.indicators, source.pivots)
        result = self.run_fixture(data)
        self.assertEqual(result["summary"]["trade_count"], 0)
        self.assertIn("SPREAD_ATR_LIMIT", result["rejections"])
        self.assertIn("SCENARIO_MISSING_OR_ZERO", result["spread_basis_counts"])
        self.assertEqual(result["assumptions"]["fallback_spread_usd"], 12)
        self.assertEqual(result["assessments"][0]["reason"], "SPREAD_ATR_LIMIT")
        self.assertNotIn("net_reward_risk", result["assessments"][0])

    def test_rejected_signal_assessment_explains_exact_shared_cost_math(self):
        data = research_fixture()
        for tf, target in (("H1", 155), ("H4", 180)):
            frame = data["frames"][tf]
            pivots = tuple(dict(p, price=target) if p["kind"] == "high" and p["price"] >= 1000
                           else p for p in frame.pivots)
            data["frames"][tf] = FrameFeatures(frame.bars, frame.indicators, pivots)
        result = self.run_fixture(data, end=T+600)
        self.assertEqual(result["summary"]["trade_count"], 0)
        assessment = result["assessments"][0]
        self.assertEqual(assessment["reason"], "INSUFFICIENT_NET_REWARD_RISK")
        self.assertFalse(assessment["accepted"])
        self.assertGreater(assessment["gross_reward_risk"], 2)
        self.assertLess(assessment["net_reward_risk"], 2)
        entry = Decimal(str(assessment["entry"]))
        stop = Decimal(str(assessment["stop"]))
        target = Decimal(str(assessment["target"]))
        costs = CostProfile(Decimal(7), Decimal(12), entry*Decimal(".2")/360*3, self.scenario["label"])
        request = SizingRequest("BTCUSD", "LONG", entry, stop, target,
            BrokerBasis(Decimal(1), -abs(entry-stop), abs(target-entry), entry/2, entry),
            costs, QuantityGrid(Decimal(".01"), Decimal(10), Decimal(".01")))
        metrics = request_metrics(request)
        self.assertAlmostEqual(assessment["total_cost_per_lot"], float(metrics["cost_per_lot"]))
        self.assertAlmostEqual(assessment["estimated_loss_per_lot"], float(metrics["loss_per_lot"]))
        self.assertAlmostEqual(assessment["net_target_profit_per_lot"], float(metrics["net_profit_per_lot"]))
        self.assertAlmostEqual(assessment["net_reward_risk"], float(metrics["net_profit_per_lot"]/metrics["loss_per_lot"]))

    def test_warmup_and_walk_forward_do_not_invent_results(self):
        data = research_fixture()
        result = run_backtest(data, self.config, self.scenario)
        self.assertEqual(result["status"], "INSUFFICIENT_HISTORY")
        walk = run_walk_forward(data, self.config, self.scenario, warmup_h4=10)
        self.assertEqual(walk["status"], "INSUFFICIENT_HELD_OUT_HISTORY")
        self.assertEqual(walk["folds"], [])

    def test_walk_forward_scores_independent_non_overlapping_folds_and_excludes_context_trade(self):
        data = research_fixture()
        for tf, seconds in PERIODS.items():
            frame = data["frames"][tf]
            bars, points = list(frame.bars), list(frame.indicators)
            for opening in range(bars[-1]["time"]+seconds, T+3*86400, seconds):
                new = {"time": opening, "available_at": opening+seconds,
                       "open": 113, "high": 116, "low": 106, "close": 113, "spread_points": 10}
                bars.append(new)
                points.append(dict(points[-1], time=opening, available_at=opening+seconds, bbp=2))
            data["frames"][tf] = FrameFeatures(tuple(bars), tuple(points), frame.pivots)
        entire = self.run_fixture(data)
        self.assertEqual(entire["summary"]["trade_count"], 1)
        walk = run_walk_forward(data, self.config, self.scenario, warmup_h4=10,
                                context_days=1, test_days=1,
                                _prepared=PreparedFrames(data["frames"], settings=self.config.strategy))
        self.assertEqual(len(walk["folds"]), 2)
        self.assertEqual(walk["summary"]["trade_count"], 0)
        self.assertEqual(walk["folds"][0]["end_utc"], walk["folds"][1]["start_utc"])
        for fold in walk["folds"]:
            self.assertEqual(fold["equity_curve"][0]["equity"], float(self.config.account.initial_balance))
            direct = self.run_fixture(data, start=fold["start_utc"], end=fold["end_utc"],
                                      entry_cutoff=datetime_epoch(fold["end_utc"])-43200)
            self.assertEqual(fold["trades"], direct["trades"])
            self.assertEqual(fold["net_profit"], direct["summary"]["net_profit"])

    def test_no_next_bar_does_not_fill_confirmation_close(self):
        data = research_fixture()
        source = data["frames"]["M5"]
        data["frames"]["M5"] = FrameFeatures(source.bars[:13], source.indicators[:13], source.pivots)
        result = self.run_fixture(data)
        self.assertEqual(result["summary"]["trade_count"], 0)
        self.assertEqual(result["coverage"]["end_utc"], "1970-01-02T16:05:00Z")

    def test_fold_purge_blocks_entry_at_exact_cutoff(self):
        result = self.run_fixture(end=T+600, entry_cutoff=T+300)
        self.assertEqual(result["summary"]["trade_count"], 0)
        self.assertEqual(result["assessments"][0]["reason"], "FOLD_END_12H_ENTRY_PURGE")
        self.assertEqual(result["rejections"]["FOLD_END_12H_ENTRY_PURGE"], 1)

    def test_short_exit_uses_ask_and_ambiguous_bar_stops_first(self):
        request = SizingRequest("BTCUSD", "SHORT", Decimal(100), Decimal(110), Decimal(50),
            BrokerBasis(Decimal(1), Decimal(-10), Decimal(50), Decimal(50), Decimal(100)),
            CostProfile(Decimal(0), Decimal(0), Decimal(0), "zero-cost rule fixture"),
            QuantityGrid(Decimal(".01"), Decimal(10), Decimal(".01")))
        ledger = _MemoryLedger(200000, RiskPolicy())
        q = Quote("BTCUSD", T, Decimal(100), Decimal(101))
        ledger.reserve("short", request, expires_at=T+300, quote=q, confirmed=True, as_of=T)
        ledger.enter("short", request, q, as_of=T)
        event = bar_exit_event(ledger.active(), {"time": T, "high": 109, "low": 40}, Decimal(1))
        self.assertTrue(event["ambiguous"])
        self.assertEqual(event["quote"].ask, 110)
        ledger.mark(event["quote"])
        self.assertEqual(ledger._state["records"]["short"]["reason"], "STOP")

    def test_shared_ledger_time_exit_and_daily_risk_control(self):
        request = SizingRequest("BTCUSD", "LONG", Decimal(100), Decimal(90), Decimal(150),
            BrokerBasis(Decimal(1), Decimal(-10), Decimal(50), Decimal(50), Decimal(100)),
            CostProfile(Decimal(0), Decimal(0), Decimal(0), "risk fixture"),
            QuantityGrid(Decimal(".01"), Decimal(100), Decimal(".01")))
        ledger = _MemoryLedger(200000, RiskPolicy())
        q = Quote("BTCUSD", T, Decimal(100), Decimal(100))
        ledger.reserve("time", request, expires_at=T+300, quote=q, confirmed=True, as_of=T)
        ledger.enter("time", request, q, as_of=T)
        ledger.mark(Quote("BTCUSD", T+43200, Decimal(101), Decimal(101)))
        self.assertEqual(ledger._state["records"]["time"]["reason"], "TIME")
        ledger._validate(ledger._state)

    def test_refuses_non_bid_or_unqualified_contract(self):
        for field, value in (("chart_mode", 1), ("trade_contract_size", 100), ("currency_profit", "AUD")):
            data = research_fixture()
            data["metadata"][field] = value
            with self.assertRaises(ValueError):
                self.run_fixture(data)


if __name__ == "__main__":
    unittest.main()
