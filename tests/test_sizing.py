"""Independent paper quantity, quote-side P/L and binding-limit checks."""

from dataclasses import replace
from decimal import Decimal as D
import unittest

from bbp_system.sizing import (BrokerBasis, CostProfile, QuantityGrid, RiskPolicy,
                               SizingRequest, normalize_request, request_from_dict, size_position)


def request(side="LONG", entry=100, stop=None, target=None, costs=1):
    entry = D(str(entry))
    stop = D(str(stop if stop is not None else (90 if side == "LONG" else 110)))
    target = D(str(target if target is not None else (140 if side == "LONG" else 60)))
    return SizingRequest("BTCUSD", side, entry, stop, target,
                         BrokerBasis(D(1), -abs(entry - stop), abs(target - entry), entry / 2, entry),
                         CostProfile(D(str(costs)), D(0), D(0), "EXPLICIT_SYNTHETIC_ROUNDTRIP"),
                         QuantityGrid(D("0.01"), D(500), D("0.01")))


class SizingTests(unittest.TestCase):
    def test_200k_per_trade_is_500_with_floor_and_cost_recheck(self):
        sized = size_position(request(), equity=D(200000))
        self.assertTrue(sized.accepted)
        self.assertEqual(sized.volume, D("45.45"))
        self.assertEqual(sized.estimated_stop_loss, D("499.95"))
        self.assertEqual(sized.net_target_profit, D("1772.55"))
        self.assertLessEqual(sized.estimated_stop_loss, D(500))

    def test_all_explicit_costs_are_included_once(self):
        plan = replace(request(), costs=CostProfile(D(1), D(2), D(3), "explicit research estimates"))
        plan = replace(plan, target=D(160), basis=replace(plan.basis, target_pnl=D(60)))
        sized = size_position(plan, equity=200000)
        self.assertEqual(sized.volume, D("31.25"))
        self.assertEqual(sized.estimated_stop_loss, D(500))
        self.assertEqual(sized.net_reward_risk, D("3.375"))

    def test_minimum_lot_cannot_exceed_budget(self):
        plan = replace(request(), grid=QuantityGrid(D(100), D(500), D(1)),
                       basis=BrokerBasis(D(100), D(-1000), D(4000), D(5000), D(10000)))
        self.assertFalse(size_position(plan, equity=200000).accepted)

    def test_risk_caps_and_margin_notional_restrict_quantity(self):
        sized = size_position(request(), equity=200000, open_and_reserved_risk=1900, crypto_risk=900)
        self.assertEqual(sized.volume, D("9.09"))
        self.assertLessEqual(sized.estimated_stop_loss, D(100))
        self.assertFalse(size_position(request(), equity=200000, crypto_risk=1000).accepted)
        sized = size_position(request(), equity=200000, used_margin=49900)
        self.assertEqual(sized.volume, D("2.00"))
        sized = size_position(request(), equity=200000, current_gross_notional=199900)
        self.assertEqual(sized.volume, D("1.00"))
        sized = size_position(request(), equity=200000, free_margin=25)
        self.assertEqual(sized.volume, D("0.50"))

    def test_daily_drawdown_and_nonpositive_equity_pause(self):
        self.assertEqual(size_position(request(), equity=198000, day_start_equity=200000).reason, "DAILY_LOSS_PAUSE")
        self.assertEqual(size_position(request(), equity=190000, peak_equity=200000,
                                       policy=RiskPolicy(daily_loss_fraction=None)).reason, "DRAWDOWN_PAUSE")
        self.assertEqual(size_position(request(), equity=0).reason, "NONPOSITIVE_EQUITY")

    def test_net_reward_risk_after_costs_can_reject_a_gross_two_r_target(self):
        plan = request(target=120)
        self.assertEqual(size_position(plan, equity=200000).reason, "INSUFFICIENT_NET_REWARD_RISK")

    def test_short_and_nonunit_reference_volume_have_same_correct_risk(self):
        short = request("SHORT")
        sized = size_position(short, equity=200000)
        self.assertEqual(sized.volume, D("45.45"))
        long = request()
        half = replace(long, basis=BrokerBasis(D("0.5"), D(-5), D(20), D(25), D(50)))
        self.assertEqual(size_position(half, equity=200000).volume, sized.volume)

    def test_invalid_nonfinite_currency_and_inconsistent_broker_basis_rejected(self):
        for plan in (
            replace(request(), entry=D("NaN")),
            replace(request(), basis=replace(request().basis, currency="AUD")),
            replace(request(), basis=replace(request().basis, target_pnl=D(80))),
            replace(request(), basis=replace(request().basis, notional=D(1))),
            replace(request(), costs=CostProfile(D(-1), D(0), D(0), "invalid")),
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                normalize_request(plan)

    def test_policy_cannot_silently_raise_accepted_risk_limits(self):
        for kwargs in ({"per_trade_fraction":D(".01")}, {"total_fraction":D(".02")}, {"crypto_fraction":D(".006")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RiskPolicy(**kwargs)

    def test_serialized_request_roundtrips_exact_decimal_basis(self):
        plan = normalize_request(request())
        self.assertEqual(request_from_dict(plan.as_dict()), plan)


if __name__ == "__main__":
    unittest.main()
