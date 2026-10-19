"""Independent cash, direction, boundary and evidence checks for cost scenarios."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import unittest

from bbp_system.research_costs import (
    LinearContract, MarginEvidence, RolloverEvent, SwapMode, SwapProfile,
    conservative_margin, evaluate_trade_costs,
)


ENTRY = datetime(2026, 8, 20, 8, 10, tzinfo=timezone.utc)
EXIT = ENTRY + timedelta(hours=12)
CONTRACT = LinearContract("BTCUSD", D(1), "BTC", "USD", "USD")
PROFILE = SwapProfile("BTCUSD", SwapMode.INTEREST_CURRENT, D(-20), D(0),
                      "Dated fixture; not a qualified account profile", "RESEARCH_ASSUMPTION")


def event(offset_hours=1, multiplier=1, price=72000, event_id="fixture-event"):
    return RolloverEvent(event_id, "BTCUSD", ENTRY + timedelta(hours=offset_hours),
                         D(multiplier), "Explicit UTC fixture schedule", "RESEARCH_ASSUMPTION",
                         D(price) if price is not None else None)


def evaluate(**changes):
    arguments = dict(contract=CONTRACT, profile=PROFILE, side="LONG", volume=D("0.36"),
                     open_price=D(70000), opened_at_utc=ENTRY, closed_at_utc=EXIT,
                     events=[event()], commission_round_trip_per_lot=D(7),
                     slippage_round_trip_per_lot=D(12))
    arguments.update(changes)
    return evaluate_trade_costs(**arguments)


class ResearchCostTests(unittest.TestCase):
    def test_long_annual_percent_uses_current_price_contract_quantity_and_multiplier(self):
        costs = evaluate(events=[event(multiplier=3)])
        # 72,000 * 20% / 360 * 3 * 0.36 = 43.20, not entry-price financing.
        self.assertEqual(costs.financing[0].signed_cost, D("43.20"))
        self.assertEqual(costs.financing[0].price_basis, D(72000))
        self.assertEqual(costs.cash_cost, D("50.04"))
        self.assertEqual(costs.swap_profile, PROFILE)
        self.assertEqual((costs.symbol, costs.side, costs.volume), ("BTCUSD", "LONG", D("0.36")))

    def test_observed_zero_short_rate_does_not_inherit_long_debit(self):
        costs = evaluate(side="SHORT", events=[event(multiplier=3, price=None)])
        self.assertEqual(costs.financing[0].signed_cost, 0)
        self.assertEqual(costs.cash_cost, D("6.84"))

    def test_open_interest_uses_open_price_without_a_future_rollover_price(self):
        profile = replace(PROFILE, mode=SwapMode.INTEREST_OPEN)
        costs = evaluate(profile=profile, events=[event(multiplier=3, price=None)])
        self.assertEqual(costs.financing[0].signed_cost, D(42))
        self.assertEqual(costs.financing[0].price_basis, D(70000))

    def test_deposit_currency_mode_is_money_per_lot_not_annual_percent(self):
        profile = replace(PROFILE, mode=SwapMode.CURRENCY_DEPOSIT,
                          long_rate=D("-2.5"), short_rate=D(1))
        costs = evaluate(profile=profile, volume=D(2), events=[event(multiplier=3, price=None)])
        self.assertEqual(costs.financing[0].signed_cost, D(15))
        self.assertIsNone(costs.financing[0].price_basis)

    def test_disabled_and_zero_day_events_need_no_market_price(self):
        disabled = replace(PROFILE, mode=SwapMode.DISABLED, long_rate=D(0))
        self.assertEqual(evaluate(profile=disabled, events=[event(price=None)]).cash_cost, D("6.84"))
        self.assertEqual(evaluate(events=[event(multiplier=0, price=None)]).cash_cost, D("6.84"))

    def test_no_crossed_event_means_no_scenario_financing(self):
        costs = evaluate(events=[event(offset_hours=-1, price=None),
                                 event(offset_hours=13, event_id="after", price=None)])
        self.assertEqual(costs.financing, ())
        self.assertEqual(costs.cash_cost, D("6.84"))

    def test_identical_timestamp_convention_excludes_entry_includes_exit(self):
        costs = evaluate(events=[event(offset_hours=0, event_id="at-entry"),
                                 event(offset_hours=12, event_id="at-exit")])
        self.assertEqual([line.event_id for line in costs.financing], ["at-exit"])
        zero_duration = evaluate(closed_at_utc=ENTRY, events=[event(offset_hours=0)])
        self.assertEqual(zero_duration.financing, ())

    def test_aware_timezones_normalize_to_the_same_utc_instant(self):
        non_utc = replace(event(), at_utc=event().at_utc.astimezone(timezone(timedelta(hours=3))))
        self.assertEqual(evaluate(events=[non_utc]), evaluate())

    def test_multiple_events_charge_round_trip_fees_once_and_sort_lines(self):
        costs = evaluate(events=[event(2, 3, event_id="second"), event(1, 1, event_id="first")])
        self.assertEqual(costs.commission, D("2.52"))
        self.assertEqual(costs.slippage, D("4.32"))
        self.assertEqual(costs.cash_cost, D("64.44"))
        self.assertEqual([line.event_id for line in costs.financing], ["first", "second"])

    def test_credit_changes_realized_cash_but_cannot_reduce_stop_risk(self):
        profile = replace(PROFILE, mode=SwapMode.CURRENCY_DEPOSIT, short_rate=D(100))
        costs = evaluate(side="SHORT", profile=profile, events=[event(price=None)])
        self.assertEqual(costs.cash_cost, D("-29.16"))
        self.assertEqual(costs.net_pnl(D(-360)), D("-330.84"))
        self.assertEqual(costs.risk_cost, D("6.84"))
        self.assertEqual(costs.stop_loss_with_charges(D(360)), D("366.84"))

    def test_costs_are_subtracted_once_from_quote_side_pnl(self):
        costs = evaluate(events=[])
        # Quote-side gross already embeds 10 USD spread; it is not an extra fee.
        self.assertEqual(costs.net_pnl(D(90)), D("83.16"))
        self.assertEqual(costs.stop_loss_with_charges(D(10)), D("16.84"))

    def test_current_interest_requires_price_and_unknown_modes_fail_closed(self):
        with self.assertRaises(ValueError):
            evaluate(events=[event(price=None)])
        for mode in (1, 2, 3, 7, 8, 9, True, "5"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                replace(PROFILE, mode=mode)

    def test_duplicate_rollovers_are_rejected_even_under_different_ids(self):
        for events in ([event(), event(2)],
                       [event(), event(event_id="duplicate-instant")]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                evaluate(events=events)

    def test_symbol_units_and_currency_cannot_be_silently_reused(self):
        eth = LinearContract("ETHUSD", D(1), "ETH", "USD", "USD")
        with self.assertRaises(ValueError):
            evaluate(contract=eth)
        with self.assertRaises(ValueError):
            evaluate(events=[replace(event(), symbol="ETHUSD")])
        with self.assertRaises(ValueError):
            LinearContract("XAUUSD", D(100), "troy oz", "USD", "AUD")
        gold = LinearContract("XAUUSD", D(100), "troy oz", "USD", "USD")
        gold_profile = replace(PROFILE, symbol="XAUUSD")
        gold_event = replace(event(price=3600), symbol="XAUUSD")
        self.assertEqual(evaluate(contract=gold, profile=gold_profile,
                                  events=[gold_event]).financing[0].signed_cost, D(72))

    def test_invalid_data_and_naive_times_fail_before_accounting(self):
        for changes in (dict(volume=0), dict(side="BUY"), dict(open_price=D("NaN")),
                        dict(commission_round_trip_per_lot=-1), dict(slippage_round_trip_per_lot=True),
                        dict(opened_at_utc=ENTRY.replace(tzinfo=None)), dict(closed_at_utc=ENTRY-timedelta(seconds=1))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evaluate(**changes)
        for changes in (dict(multiplier=-1), dict(multiplier=D("Infinity")),
                        dict(at_utc=ENTRY.replace(tzinfo=None)), dict(evidence_kind="UNLABELLED")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(event(), **changes)

    def test_margin_keeps_conflicting_sources_and_uses_sourced_conservative_floor(self):
        demo = MarginEvidence("BTCUSD", "USD", D(423), ENTRY, "Dated demo calculation fixture",
                              "BROKER_OBSERVATION")
        floor = MarginEvidence("BTCUSD", "USD", D(42300), ENTRY,
                               "50% published floor at explicit 84,600 scenario price", "RESEARCH_ASSUMPTION")
        result = conservative_margin(contract=CONTRACT, volume=D("0.36"), at_utc=ENTRY,
                                     evidence=[demo, floor])
        self.assertEqual(result.amount, D(15228))
        self.assertEqual(result.binding, floor)
        self.assertEqual(result.evidence, (demo, floor))
        self.assertEqual(result.currency, "USD")

    def test_margin_cannot_use_future_or_mismatched_evidence(self):
        valid = MarginEvidence("BTCUSD", "USD", D(500), ENTRY, "Sourced fixture", "BROKER_OBSERVATION")
        for evidence in ([], [replace(valid, as_of_utc=ENTRY+timedelta(seconds=1))],
                         [replace(valid, symbol="ETHUSD")], [replace(valid, currency="AUD")],
                         [replace(valid, amount_per_lot=0)]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                conservative_margin(contract=CONTRACT, volume=1, at_utc=ENTRY, evidence=evidence)


if __name__ == "__main__":
    unittest.main()
