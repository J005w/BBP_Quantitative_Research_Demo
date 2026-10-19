"""Portable pipeline, data provenance and independent accounting checks."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

from bbp_demo.data import FIELDS, generated_rows, load_dataset, write_fixture
from bbp_demo.report import RESOURCES, build_report, canonical, independent_accounting


class DemoReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report()

    def test_raw_fixture_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.csv"
            write_fixture(path)
            self.assertEqual(path.read_bytes(), (RESOURCES / "synthetic_m5.csv").read_bytes())

    def test_missing_intervals_are_not_filled(self):
        dataset, coverage = load_dataset(RESOURCES / "synthetic_m5.csv")
        self.assertEqual((coverage["observed_m5_bars"], coverage["missing_m5_bars"]), (6909, 3))
        self.assertEqual(coverage["discarded_incomplete_aggregate_bars"], {"H4": 1, "H1": 1, "M15": 2})
        m5 = {bar["time"]: bar for bar in dataset["frames"]["M5"]["bars"]}
        for tf, seconds in (("H4", 14400), ("H1", 3600), ("M15", 900)):
            for bar in dataset["frames"][tf]["bars"]:
                self.assertEqual(bar["available_at"], bar["time"] + seconds)
                parts = [m5[t] for t in range(bar["time"], bar["available_at"], 300)]
                self.assertEqual(bar["high"], max(part["high"] for part in parts))
                self.assertEqual(bar["close"], parts[-1]["close"])

    def test_duplicated_or_future_availability_csv_is_rejected(self):
        import csv
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.csv"
                first = list(next(generated_rows()))
                if not duplicate:
                    first[1] += 300
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(FIELDS)
                    writer.writerow(first)
                    if duplicate:
                        writer.writerow(first)
                with self.assertRaises(ValueError):
                    load_dataset(path)

    def test_engineered_and_raw_evidence_stay_separate(self):
        report = self.report
        self.assertEqual(report["evidence"], "SYNTHETIC_CORRECTNESS_ONLY")
        self.assertEqual(report["results"]["native_raw_ohlc"]["trade_count"], 0)
        self.assertEqual(report["results"]["trade_count"], 1)
        self.assertLess(report["results"]["net_profit"], 0)
        self.assertTrue(report["research_status"]["real_reproducible_research"].startswith("NOT_INCLUDED"))
        self.assertEqual(report["research_status"]["recovery"], "PROPOSED_SYNTHETIC_ONLY_UNSELECTED")

    def test_net_reward_rejection_is_visible(self):
        rejections = [d for d in self.report["decisions"] if d["action"] == "REJECT"]
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["reason"], "INSUFFICIENT_NET_REWARD_RISK")

    def test_next_bar_execution_has_no_earlier_feature_availability(self):
        for decision in self.report["decisions"]:
            self.assertLessEqual(decision["available_at"], decision["decision_time"])
            if decision["action"] == "ACCEPT":
                self.assertEqual(decision["confirmation_bar_open"] + 300, decision["fill_time"])
                self.assertEqual(decision["decision_time"], decision["fill_time"])

    def test_independent_route_detects_changed_cash_leg(self):
        case = copy.deepcopy(self.report["engineered_cases"]["engineered_acceptance"])
        scenario = self.report["assumptions"]["costs"]
        for field in ("pnl", "costs", "exit", "volume"):
            broken = copy.deepcopy(case)
            broken["trades"][0][field] += 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                independent_accounting(broken, 100000, scenario)

    def test_independent_route_detects_changed_equity(self):
        case = copy.deepcopy(self.report["engineered_cases"]["engineered_acceptance"])
        case["equity_curve"][-1]["equity"] += .01
        with self.assertRaises(ValueError):
            independent_accounting(case, 100000, self.report["assumptions"]["costs"])

    def test_fixed_parameter_cost_stress_reduces_result(self):
        normal = self.report["engineered_cases"]["engineered_acceptance"]
        stress = self.report["engineered_cases"]["engineered_fixed_rule_cost_stress"]
        self.assertEqual(normal["parameter_sha256"], stress["parameter_sha256"])
        self.assertLess(stress["summary"]["net_profit"], normal["summary"]["net_profit"])
        self.assertEqual(self.report["accounting"]["cost_stress"]["status"], "PASS")

    def test_resources_match_selected_offline_configuration(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((RESOURCES / "paper.json").read_bytes(), (root / "config/paper.json").read_bytes())
        self.assertEqual((RESOURCES / "costs.json").read_bytes(), (root / "config/btc_research.json").read_bytes())

    def test_complete_run_is_deterministic_and_has_no_adapter_import(self):
        self.assertEqual(canonical(self.report), canonical(build_report()))
        self.assertNotIn("MetaTrader5", sys.modules)
        self.assertNotIn("numpy", sys.modules)


if __name__ == "__main__":
    unittest.main()
