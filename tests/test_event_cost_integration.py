"""Actual chronological replay/ledger hooks, not broker-cost qualification."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import unittest

from bbp_system.backtest import _MemoryLedger, run_backtest
from bbp_system.config import load_config
from bbp_system.execution_costs import EventCostProfile, ReplayCostModel
from bbp_system.paper import Quote
from bbp_system.research_costs import LinearContract, RolloverEvent, SwapMode, SwapProfile
from bbp_system.sizing import BrokerBasis, QuantityGrid, RiskPolicy, SizingRequest
from bbp_system.strategy import PreparedFrames
from test_backtest import ROOT, research_fixture
from test_strategy import T


def epoch(at):
    return datetime.fromtimestamp(at, timezone.utc)


def replay_model(*, rate=-1, multiplier=1, mode=SwapMode.CURRENCY_DEPOSIT,
                 event_at=T+600, supplied_price=None, price_known_at=None,
                 commission=2, slippage=3):
    contract = LinearContract("BTCUSD", D(1), "BTC", "USD", "USD")
    swap = SwapProfile("BTCUSD", mode, D(rate), D(0), "synthetic rates", "RESEARCH_ASSUMPTION")
    event = RolloverEvent("synthetic-rollover", "BTCUSD", epoch(event_at), D(multiplier),
                          "synthetic schedule", "RESEARCH_ASSUMPTION", supplied_price)
    profile = EventCostProfile("synthetic", "Unqualified integration scenario", contract, swap,
        epoch(T-86400), epoch(T-86400), epoch(T+2*86400), D(commission), D(slippage), (event,), True,
        event_price_known_at=((event.event_id, epoch(price_known_at)),) if price_known_at is not None else ())
    return ReplayCostModel(profile)


class EventBacktestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config" / "paper.json")
        self.scenario = json.loads((ROOT / "config" / "btc_research.json").read_text())

    def replay(self, model, *, end=T+900, data=None):
        data = data or research_fixture()
        return run_backtest(data, self.config, self.scenario, warmup_h4=10, end=end,
            _prepared=PreparedFrames(data["frames"], settings=self.config.strategy), cost_model=model)

    def test_actual_no_rollover_exit_charges_round_trip_once(self):
        result = self.replay(replay_model(), end=T+600)
        trade = result["trades"][0]
        self.assertEqual(result["summary"]["trade_count"], 1)
        self.assertEqual(trade["accrued_costs"]["financing"], [])
        self.assertAlmostEqual(trade["costs"], trade["volume"] * 5)
        self.assertAlmostEqual(trade["pnl"], trade["volume"] * (trade["exit"] - trade["entry"]) - trade["costs"])
        self.assertAlmostEqual(result["equity_curve"][-1]["equity"], float(self.config.account.initial_balance) + trade["pnl"])

    def test_actual_triple_debit_accrues_once_at_event_and_closed_cash_validates(self):
        result = self.replay(replay_model(multiplier=3))
        trade = result["trades"][0]
        self.assertEqual(len(trade["accrued_costs"]["financing"]), 1)
        self.assertEqual(D(trade["accrued_costs"]["financing"][0]["signed_cost"]), D(str(trade["volume"])) * 3)
        self.assertAlmostEqual(trade["costs"], trade["volume"] * 8)
        before, after = result["equity_curve"][1:3]
        self.assertAlmostEqual(before["equity"] - after["equity"], trade["volume"] * 3)
        self.assertAlmostEqual(result["equity_curve"][-1]["equity"], float(self.config.account.initial_balance) + trade["pnl"])

    def test_actual_credit_improves_cash_but_does_not_expand_entry_risk(self):
        credited = self.replay(replay_model(rate=1, multiplier=3))
        zero = self.replay(replay_model(rate=0, multiplier=3))
        a, b = credited["trades"][0], zero["trades"][0]
        self.assertEqual(a["volume"], b["volume"])
        self.assertEqual(a["estimated_stop_loss"], b["estimated_stop_loss"])
        self.assertEqual(a["net_reward_risk"], b["net_reward_risk"])
        self.assertAlmostEqual(a["pnl"] - b["pnl"], a["volume"] * 3)
        self.assertAlmostEqual(a["costs"], a["volume"] * 2)

    def test_unavailable_supplied_event_prices_cannot_change_prefix_or_entry(self):
        a = replay_model(mode=SwapMode.INTEREST_CURRENT, rate=-36,
                         supplied_price=D(100), price_known_at=T+800)
        b = replay_model(mode=SwapMode.INTEREST_CURRENT, rate=-36,
                         supplied_price=D(1000000), price_known_at=T+800)
        # Both profiles explicitly received the final rollover price later than
        # the event. Every prefix and later mark must retain the prior proxy.
        for end in (T+600, T+900, T+1200):
            left, right = self.replay(a, end=end), self.replay(b, end=end)
            self.assertEqual(left, right)
        trade = self.replay(a)["trades"][0]
        financing = trade["accrued_costs"]["financing"][0]
        self.assertIn("PROXY", financing["price_basis"])
        self.assertFalse(trade["accrued_costs"]["qualified_realised_costs"])

    def test_unchanged_conservative_hook_retains_original_result(self):
        data = research_fixture()
        options = dict(warmup_h4=10, end=T+600,
            _prepared=PreparedFrames(data["frames"], settings=self.config.strategy))
        default = run_backtest(data, self.config, self.scenario, **options)
        explicit = run_backtest(data, self.config, self.scenario, cost_model=None, **options)
        self.assertEqual(default, explicit)
        self.assertEqual(default["cost_methodology"]["model"], "ORIGINAL_CONSERVATIVE_SCENARIO")
        self.assertNotIn("accrued_costs", default["trades"][0])

    def test_event_currency_cannot_be_assumed_to_match_usd(self):
        m = replay_model()
        wrong = replace(m.profile.contract, price_currency="EUR", account_currency="EUR")
        with self.assertRaisesRegex(ValueError, "account currency"):
            self.replay(ReplayCostModel(replace(m.profile, contract=wrong)))


class EventProfileProvenanceTests(unittest.TestCase):
    def evidence(self, known_at=T-86400, kind="OFFICIAL_SPECIFICATION"):
        return {name: {"source": f"synthetic explicit {name} evidence, including waived amounts",
                       "evidence_kind": kind, "known_at_utc": epoch(known_at).isoformat()}
                for name in ("commission", "slippage", "schedule")}

    def qualified_profile(self):
        profile = replay_model().profile
        return replace(profile, swap=replace(profile.swap, evidence_kind="OFFICIAL_SPECIFICATION"),
                       events=tuple(replace(event, evidence_kind="BROKER_OBSERVATION") for event in profile.events))

    def test_full_qualification_requires_component_evidence_even_zero_costs(self):
        profile = self.qualified_profile()
        with self.assertRaisesRegex(ValueError, "commission"):
            replace(profile, qualified=True)
        with self.assertRaisesRegex(ValueError, "qualified commission"):
            replace(profile, qualified=True, component_evidence=self.evidence(kind="RESEARCH_ASSUMPTION"))
        qualified = replace(profile, qualified=True, component_evidence=self.evidence())
        self.assertTrue(qualified.qualified)
        with self.assertRaises(TypeError):
            qualified.component_evidence["commission"]["source"] = "changed"
        with self.assertRaisesRegex(ValueError, "predate"):
            replace(profile, component_evidence=self.evidence(known_at=T+1))

    def test_event_price_availability_and_prior_proxy_are_stable(self):
        event_time = T+600
        m = replay_model(mode=SwapMode.INTEREST_CURRENT, rate=-36,
                         supplied_price=D(10000), price_known_at=event_time+100)
        m.observe_price(T+300, 100)
        before = m.realised_costs("LONG", 100, T+300, event_time, 1)
        m.observe_price(event_time+200, 1000)
        after = m.realised_costs("LONG", 100, T+300, event_time+300, 1)
        self.assertEqual(before["cash_cost"], after["cash_cost"])
        self.assertEqual(D(before["cash_cost"]), D("5.1"))
        known = ReplayCostModel(replace(m.profile, event_price_known_at=((m.profile.events[0].event_id, epoch(event_time)),)))
        self.assertEqual(D(known.realised_costs("LONG", 100, T+300, event_time, 1)["cash_cost"]), D(15))
        with self.assertRaisesRegex(ValueError, "before its event"):
            replace(m.profile, event_price_known_at=((m.profile.events[0].event_id, epoch(event_time-1)),))

    def test_repeated_ledger_mark_and_exit_validation_never_double_financing(self):
        m = replay_model(commission=0, slippage=0, multiplier=3,
                         mode=SwapMode.INTEREST_CURRENT, rate=-36, supplied_price=D(100000), price_known_at=T+1000)
        ledger = _MemoryLedger(200000, RiskPolicy(), cost_model=m)
        opening = Quote("BTCUSD", T+300, D(100), D(100))
        ledger.mark(opening)
        request = SizingRequest("BTCUSD", "LONG", D(100), D(95), D(120),
            BrokerBasis(D(1), D(-5), D(20), D(50), D(100)), m.entry_costs("LONG", 100, T+300, 12),
            QuantityGrid(D(".01"), D(10), D(".01")))
        ledger.reserve("stable", request, expires_at=T+600, quote=opening, confirmed=True, as_of=T+300)
        ledger.enter("stable", request, opening, as_of=T+300)
        volume = D(ledger.active()["volume"])
        at_event = Quote("BTCUSD", T+600, D(100), D(100))
        ledger.mark(at_event)
        ledger.mark(at_event)
        self.assertEqual(D(ledger.active()["unrealized_pnl"]), -D(".3") * volume)
        ledger.flatten(Quote("BTCUSD", T+900, D(100), D(100)), "END")
        ledger.mark(Quote("BTCUSD", T+1200, D(1000), D(1000)))
        self.assertEqual(D(ledger._state["cash"]), D(200000) - D(".3") * volume)
        ledger._validate(ledger._state)

    def test_rejected_or_repeated_price_observations_cannot_poison_rollover_proxy(self):
        m = replay_model()
        ledger = _MemoryLedger(200000, RiskPolicy(), cost_model=m)
        quote = Quote("BTCUSD", T, D(100), D(100))
        ledger.mark(quote)
        ledger.mark(quote)
        self.assertEqual(len(m._prices), 1)
        with self.assertRaises(ValueError):
            ledger.mark(Quote("BTCUSD", T+300, D(10000), D(1)))
        self.assertEqual(m._prices, [(epoch(T), D(100))])
        with self.assertRaisesRegex(ValueError, "conflicting cost prices"):
            m.observe_price(T, 200)
        self.assertEqual(m._prices, [(epoch(T), D(100))])


if __name__ == "__main__":
    unittest.main()
