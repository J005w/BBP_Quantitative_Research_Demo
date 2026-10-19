"""Atomic simulated-paper lifecycle and durable financial invariants."""

from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from bbp_system.paper import LedgerBusyError, LedgerError, PaperLedger, Quote
from bbp_system.sizing import RiskPolicy
from test_sizing import request


NOW = 1791000000


def quote(time=NOW, bid=99, ask=100, tradable=True):
    return {"symbol":"BTCUSD", "time":time, "bid":bid, "ask":ask, "tradable":tradable}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper.json"
        self.ledger = PaperLedger(self.path, D(200000), clock=lambda: NOW)

    def reserve(self, identifier="one", plan=None, tick=None):
        return self.ledger.reserve(identifier, plan or request(), expires_at=NOW+300,
                                   quote=tick or quote(), confirmed=True, as_of=NOW)

    def opened(self, side="LONG"):
        plan, tick = request(side), quote(bid=100, ask=101) if side == "SHORT" else quote()
        self.assertTrue(self.reserve(plan=plan, tick=tick)["accepted"])
        self.assertTrue(self.ledger.enter("one", plan, tick, as_of=NOW)["accepted"])
        return self.ledger.snapshot()

    def test_reservation_transfer_idempotence_and_restart(self):
        initial = self.reserve()
        self.assertEqual(D(initial["snapshot"]["open_and_reserved_risk"]), D("499.95"))
        self.assertTrue(self.reserve()["accepted"])
        opened = self.ledger.enter("one", request(), quote(), as_of=NOW)
        self.assertTrue(opened["accepted"])
        self.assertEqual(D(opened["snapshot"]["open_and_reserved_risk"]), D("499.95"))
        again = self.ledger.enter("one", request(), quote(), as_of=NOW)
        self.assertEqual(len(again["snapshot"]["records"]), 1)
        reloaded = PaperLedger.load(self.path, clock=lambda: NOW)
        self.assertEqual(reloaded.snapshot()["records"], again["snapshot"]["records"])

    def test_current_equity_marks_bid_side_and_full_cost_once(self):
        opened = self.opened()
        self.assertEqual(D(opened["current_equity"]), D("199909.10"))
        moved = self.ledger.on_quote(quote(NOW+1, 101, 102), as_of=NOW+1)["snapshot"]
        self.assertEqual(D(moved["current_equity"]), D(200000))

    def test_gap_stop_exceeds_budget_and_uses_first_executable_bid(self):
        self.opened()
        closed = self.ledger.on_quote(quote(NOW+1, 70, 71), as_of=NOW+1)["snapshot"]
        record = closed["records"]["one"]
        self.assertEqual(record["reason"], "STOP")
        self.assertEqual(D(record["exit_price"]), D(70))
        self.assertEqual(D(record["realized_pnl"]), D("-1408.95"))
        self.assertEqual(D(closed["open_and_reserved_risk"]), 0)
        self.assertEqual(D(closed["cash"]), D("198591.05"))

    def test_short_stop_uses_ask_not_bid_and_target_uses_ask(self):
        self.opened("SHORT")
        closed = self.ledger.on_quote(quote(NOW+1, 109, 110.5), as_of=NOW+1)["snapshot"]
        self.assertEqual(closed["records"]["one"]["reason"], "STOP")
        self.assertEqual(D(closed["records"]["one"]["realized_pnl"]), D("-522.675"))

    def test_target_and_duplicate_exit_cannot_duplicate_realized_cash(self):
        self.opened()
        first = self.ledger.on_quote(quote(NOW+1, 140, 141), as_of=NOW+1)["snapshot"]
        second = self.ledger.on_quote(quote(NOW+1, 140, 141), as_of=NOW+1)["snapshot"]
        self.assertEqual(first["cash"], second["cash"])
        self.assertEqual(D(first["cash"]), D("201772.55"))
        self.assertEqual(first["records"]["one"]["reason"], "TARGET")

    def test_time_exit_waits_for_tradable_quote_and_records_late_fill(self):
        self.opened()
        deadline = NOW+43200
        blocked = self.ledger.on_quote(quote(deadline, 101, 102, False), as_of=deadline)["snapshot"]
        self.assertEqual(blocked["records"]["one"]["status"], "OPEN")
        final = self.ledger.on_quote(quote(deadline+3600, 101, 102), as_of=deadline+3600)["snapshot"]
        self.assertEqual(final["records"]["one"]["reason"], "TIME")
        self.assertEqual(final["records"]["one"]["late_exit_seconds"], 3600)

    def test_expiry_rejection_and_unconfirmed_candidate_release_risk(self):
        self.reserve()
        expired = self.ledger.on_quote(quote(NOW+300), as_of=NOW+300)["snapshot"]
        self.assertEqual(expired["records"]["one"]["status"], "EXPIRED")
        self.assertEqual(D(expired["open_and_reserved_risk"]), 0)
        bad = self.ledger.reserve("candidate", request(), expires_at=NOW+500,
                                  quote=quote(NOW+300), as_of=NOW+300)
        self.assertEqual(bad["reason"], "NOT_CONFIRMED")
        self.assertEqual(D(bad["snapshot"]["open_and_reserved_risk"]), 0)

    def test_one_symbol_and_manual_cancellation_are_enforced(self):
        self.reserve()
        other = self.reserve("two")
        self.assertEqual(other["reason"], "ONE_POSITION_OR_RESERVATION_PER_SYMBOL")
        cancelled = self.ledger.reject("one")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(D(cancelled["snapshot"]["open_and_reserved_risk"]), 0)

    def test_repriced_entry_resizes_and_does_not_count_own_reservation_twice(self):
        self.reserve()
        changed = request(entry=101)
        entered = self.ledger.enter("one", changed, quote(NOW+1, 100, 101), as_of=NOW+1)
        self.assertTrue(entered["accepted"])
        record = entered["snapshot"]["records"]["one"]
        self.assertEqual(D(record["volume"]), D("41.66"))
        self.assertEqual(D(record["estimated_stop_loss"]), D("499.92"))

    def test_stale_quote_cannot_open_or_close_a_paper_trade(self):
        self.reserve()
        rejected = self.ledger.enter("one", request(), quote(), as_of=NOW+61)
        self.assertEqual(rejected["reason"], "STALE_OR_UNTRADABLE_QUOTE")
        self.assertEqual(rejected["status"], "CANCELLED")

    def test_spread_cannot_open_a_position_with_an_already_crossed_stop(self):
        for side, tick in (("LONG", quote(bid=90, ask=100)), ("SHORT", quote(bid=100, ask=110))):
            plan = request(side)
            with self.subTest(side=side):
                ledger = PaperLedger(Path(self.directory.name) / (side + ".json"), D(200000), clock=lambda: NOW)
                preview = ledger.preview(plan, tick, as_of=NOW)
                self.assertEqual(preview.reason, "STOP_ALREADY_INVALIDATED")
                identifier = "invalid-" + side
                result = ledger.reserve(identifier, plan, expires_at=NOW+300, quote=tick, confirmed=True, as_of=NOW)
                self.assertEqual(result["reason"], "STOP_ALREADY_INVALIDATED")
                self.assertEqual(D(result["snapshot"]["open_and_reserved_risk"]), 0)

    def test_quote_side_invalidation_at_entry_releases_reservation(self):
        self.reserve()
        rejected = self.ledger.enter("one", request(), quote(NOW+1, 89, 100), as_of=NOW+1)
        self.assertEqual(rejected["reason"], "STOP_ALREADY_INVALIDATED")
        self.assertEqual(rejected["status"], "CANCELLED")
        self.assertEqual(D(rejected["snapshot"]["open_and_reserved_risk"]), 0)

    def test_daily_pause_after_gap_blocks_new_reservation(self):
        self.opened()
        self.ledger.on_quote(quote(NOW+1, 45, 46), as_of=NOW+1)
        paused = self.ledger.reserve("two", request(entry=46,stop=40,target=70), expires_at=NOW+200,
                                     quote=quote(NOW+1, 45, 46), confirmed=True, as_of=NOW+1)
        self.assertEqual(paused["reason"], "DAILY_LOSS_PAUSE")
        self.assertIn("DAILY_LOSS_PAUSE", paused["snapshot"]["new_entries_paused"])

    def test_preview_has_no_financial_mutation_and_reports_budgets(self):
        before = self.path.read_bytes()
        result = self.ledger.preview(request(), quote(), as_of=NOW)
        self.assertTrue(result.accepted)
        self.assertEqual(result.estimated_stop_loss, D("499.95"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(D(self.ledger.snapshot()["aggregate_budget"]), D(2000))

    def test_preview_only_excludes_its_own_confirmed_reservation(self):
        self.reserve()
        self.assertFalse(self.ledger.preview(request(), quote(), as_of=NOW).accepted)
        self.assertTrue(self.ledger.preview(request(), quote(), setup_id="one", as_of=NOW).accepted)
        self.ledger.enter("one", request(), quote(), as_of=NOW)
        self.assertEqual(self.ledger.preview(request(), quote(), setup_id="one", as_of=NOW).reason, "ALREADY_OPEN")

    def test_duplicate_id_with_conflicting_structure_is_rejected(self):
        self.reserve()
        conflict = self.reserve(plan=request(stop=91))
        self.assertEqual(conflict["reason"], "SETUP_ID_CONFLICT")
        self.assertEqual(D(conflict["snapshot"]["records"]["one"]["request"]["stop"]), D(90))

    def test_snapshot_tracks_current_ask_exposure_and_finalized_ids(self):
        self.opened()
        marked = self.ledger.on_quote(quote(NOW+1, 119, 120), as_of=NOW+1)["snapshot"]
        self.assertEqual(D(marked["gross_notional"]), D("5454.00"))
        self.assertEqual(marked["open_setup_ids"], ["one"])
        ended = self.ledger.on_quote(quote(NOW+2, 140, 141), as_of=NOW+2)["snapshot"]
        self.assertEqual(ended["finished_setup_ids"], ["one"])
        self.assertEqual(ended["open_setup_ids"], [])

    def test_out_of_order_quote_rolls_back_and_corrupt_cash_fails_closed(self):
        self.opened()
        before = self.path.read_bytes()
        with self.assertRaises(LedgerError):
            self.ledger.on_quote(quote(NOW-1), as_of=NOW+1)
        self.assertEqual(self.path.read_bytes(), before)
        data = json.loads(self.path.read_text())
        data["cash"] = "999999"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(LedgerError):
            PaperLedger.load(self.path)

    def test_restart_rechecks_persisted_position_sizing(self):
        self.reserve()
        data = json.loads(self.path.read_text())
        data["records"]["one"]["risk_context"]["equity"] = "400000"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(LedgerError):
            PaperLedger.load(self.path)

    def test_preview_cannot_approve_an_out_of_order_quote(self):
        self.ledger.on_quote(quote(), as_of=NOW)
        before = self.path.read_bytes()
        result = self.ledger.preview(request(), quote(NOW-1), as_of=NOW)
        self.assertEqual(result.reason, "OUT_OF_ORDER_QUOTE")
        self.assertFalse(result.accepted)
        self.assertEqual(self.path.read_bytes(), before)

    def test_policy_or_initial_balance_cannot_be_silently_reset(self):
        with self.assertRaises(LedgerError):
            PaperLedger(self.path, D(100000))
        with self.assertRaises(LedgerError):
            PaperLedger.load(self.path, policy=RiskPolicy(daily_loss_fraction=None))

    def test_os_lock_serializes_separate_ledger_instances(self):
        other = PaperLedger.load(self.path)
        with self.ledger._locked(), self.assertRaises(LedgerBusyError):
            other.snapshot()
        self.assertEqual(other.snapshot()["initial_equity"], "200000")


if __name__ == "__main__":
    unittest.main()
