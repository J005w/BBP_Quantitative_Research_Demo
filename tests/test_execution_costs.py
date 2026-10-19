from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import unittest

from bbp_system.execution_costs import EventCostProfile, ReplayCostModel
from bbp_system.research_costs import LinearContract, SwapProfile, SwapMode, RolloverEvent
from bbp_system.backtest import _MemoryLedger
from bbp_system.paper import Quote
from bbp_system.sizing import RiskPolicy, SizingRequest, BrokerBasis, QuantityGrid


START = datetime(2026, 9, 1, tzinfo=timezone.utc)


def model(mode=SwapMode.CURRENCY_DEPOSIT, rate=-1, short=0, multiplier=1, price=None):
    contract = LinearContract("BTCUSD", D(1), "BTC", "USD", "USD")
    swap = SwapProfile("BTCUSD", mode, D(rate), D(short), "explicit test scenario", "RESEARCH_ASSUMPTION")
    event = RolloverEvent("rollover1", "BTCUSD", START + timedelta(hours=1), D(multiplier),
                         "explicit test scenario", "RESEARCH_ASSUMPTION", price)
    profile = EventCostProfile("scenario", "Not broker qualified", contract, swap,
        START-timedelta(days=1), START-timedelta(days=1), START+timedelta(days=2),
        D(0), D(0), (event,), True)
    return ReplayCostModel(profile)


class EventExecutionTests(unittest.TestCase):
    def test_future_rollover_price_never_changes_entry_risk(self):
        a = model(SwapMode.INTEREST_CURRENT, rate=-20, price=D(100))
        b = model(SwapMode.INTEREST_CURRENT, rate=-20, price=D(1000000))
        self.assertEqual(a.entry_costs("LONG", D(100), START, 12),
                         b.entry_costs("LONG", D(100), START, 12))

    def test_zero_events_zero_short_and_boundaries(self):
        m = model()
        self.assertEqual(m.realised_costs("LONG", 100, START, START+timedelta(minutes=59), 1)["cash_cost"], "0")
        self.assertEqual(m.realised_costs("LONG", 100, START, START+timedelta(hours=1), 1)["cash_cost"], "1")
        self.assertEqual(m.realised_costs("LONG", 100, START+timedelta(hours=1), START+timedelta(hours=2), 1)["cash_cost"], "0")
        self.assertEqual(m.realised_costs("SHORT", 100, START, START+timedelta(hours=2), 1)["cash_cost"], "0")

    def test_credit_realised_but_never_increases_entry_capacity(self):
        m = model(rate=1, multiplier=3)
        self.assertEqual(m.entry_costs("LONG", 100, START, 12).financing_per_lot, D(0))
        self.assertEqual(m.realised_costs("LONG", 100, START, START+timedelta(hours=2), 2)["cash_cost"], "-6")

    def test_unknown_calendar_future_rates_and_missing_prices_fail(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "completeness"):
            ReplayCostModel(replace(m.profile, schedule_complete=False)).entry_costs("LONG", 100, START, 12)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            ReplayCostModel(replace(m.profile, known_at=START+timedelta(seconds=1))).entry_costs("LONG", 100, START, 12)
        with self.assertRaisesRegex(ValueError, "no available"):
            model(SwapMode.INTEREST_CURRENT, rate=-20).realised_costs("LONG", 100, START, START+timedelta(hours=2), 1)

    def test_price_proxy_cannot_borrow_a_later_quote(self):
        m = model(SwapMode.INTEREST_CURRENT, rate=-20)
        m.observe_price(START, 100)
        m.observe_price(START+timedelta(hours=2), 10000)
        result = m.realised_costs("LONG", 100, START, START+timedelta(hours=2), 1)
        self.assertEqual(D(result["cash_cost"]), D(20)/100/360*100)
        self.assertIn("PROXY", result["financing"][0]["price_basis"])
        self.assertEqual(m.new_run()._prices, [])

    def test_duplicate_events_and_false_qualification_rejected(self):
        m = model()
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(m.profile, events=m.profile.events*2)
        with self.assertRaisesRegex(ValueError, "assumptions"):
            replace(m.profile, qualified=True)

    def test_chronological_ledger_accrues_once_and_validates_cash(self):
        m = model()
        ledger = _MemoryLedger(200000, RiskPolicy(), cost_model=m)
        stamp = START.timestamp()
        q = Quote("BTCUSD", stamp, D(100), D(100))
        ledger.mark(q)
        request = SizingRequest("BTCUSD", "LONG", D(100), D(95), D(120),
            BrokerBasis(D(1), D(-5), D(20), D(50), D(100)),
            m.entry_costs("LONG", 100, START, 12), QuantityGrid(D(".01"), D(10), D(".01")))
        self.assertTrue(ledger.reserve("id", request, expires_at=stamp+300, quote=q, confirmed=True, as_of=stamp)["accepted"])
        self.assertTrue(ledger.enter("id", request, q, as_of=stamp)["accepted"])
        volume = D(ledger.active()["volume"])
        ledger.mark(Quote("BTCUSD", stamp+1800, D(100), D(100)))
        self.assertEqual(D(ledger.active()["unrealized_pnl"]), D(0))
        ledger.mark(Quote("BTCUSD", stamp+3600, D(100), D(100)))
        self.assertEqual(D(ledger.active()["unrealized_pnl"]), -volume)
        ledger.mark(Quote("BTCUSD", stamp+3700, D(100), D(100)))
        self.assertEqual(D(ledger.active()["unrealized_pnl"]), -volume)
        ledger.flatten(Quote("BTCUSD", stamp+4000, D(100), D(100)), "TEST_END")
        self.assertEqual(D(ledger._state["cash"]), D(200000)-volume)
        ledger._validate(ledger._state)


if __name__ == "__main__":
    unittest.main()
