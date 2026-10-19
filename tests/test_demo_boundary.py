"""Credential-free demo boundary and hand-checkable synthetic accounting."""

from decimal import Decimal
import json
from pathlib import Path
import unittest

from bbp_system.backtest import run_backtest
from bbp_system.config import ConfigError, load_config, parse_config
from bbp_system.strategy import PreparedFrames, evaluate_strategy
from bbp_system.synthetic import ENGINEERED_EPOCH, engineered_rule_fixture


ROOT = Path(__file__).resolve().parents[1]


class DemoBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT/'config/paper.json')
        self.scenario = json.loads((ROOT/'config/btc_research.json').read_text())

    def run_case(self, generous_target):
        data = engineered_rule_fixture(generous_target=generous_target)
        return run_backtest(data, self.config, self.scenario, warmup_h4=10,
            _prepared=PreparedFrames(data['frames'], settings=self.config.strategy))

    def test_demo_config_rejects_broker_and_order_enabling(self):
        for section, field, value in [('broker', 'server', 'example.invalid'),
                                       ('broker', 'platform', 'MT5'),
                                       (None, 'automatic_orders', True)]:
            data = json.loads((ROOT/'config/paper.json').read_text(), parse_float=Decimal)
            if section:
                data[section][field] = value
            else:
                data[field] = value
            with self.assertRaises(ConfigError):
                parse_config(data)

    def test_engineered_case_has_explicit_evidence_label_and_timing(self):
        data = engineered_rule_fixture()
        self.assertEqual(data['evidence_level'], 'ENGINEERED_FEATURE_CORRECTNESS_ONLY')
        candidate = evaluate_strategy(data['frames'], decision_time=ENGINEERED_EPOCH, tick_size=.25)
        self.assertEqual(candidate['state'], 'CANDIDATE')
        confirmed = evaluate_strategy(data['frames'], decision_time=ENGINEERED_EPOCH+300,
                                      tick_size=.25, previous=candidate)
        self.assertEqual(confirmed['state'], 'CONFIRMED')
        self.assertEqual(confirmed['confirmation_at'], ENGINEERED_EPOCH+300)
        self.assertFalse(confirmed['orders_enabled'])

    def test_accepted_case_reconciles_independently_from_price_and_costs(self):
        result = self.run_case(True)
        self.assertEqual(result['summary']['trade_count'], 1)
        trade = result['trades'][0]
        # Independent arithmetic route; no ledger helper or engine P&L function.
        volume = Decimal(str(trade['volume']))
        gross = (Decimal(str(trade['exit']))-Decimal(str(trade['entry'])))*volume
        costs = Decimal('19.1885')*volume
        expected_pnl = gross-costs
        self.assertAlmostEqual(float(expected_pnl), trade['pnl'], places=9)
        self.assertAlmostEqual(float(self.config.account.initial_balance+expected_pnl),
                               result['equity_curve'][-1]['equity'], places=8)
        self.assertEqual(result['assessments'][0]['entry'], 113.1)

    def test_nearby_obstacle_case_rejects_without_fictitious_trade(self):
        result = self.run_case(False)
        self.assertEqual(result['summary']['trade_count'], 0)
        self.assertEqual(result['rejections']['INSUFFICIENT_NET_REWARD_RISK'], 1)
        self.assertFalse(result['assessments'][0]['accepted'])
        self.assertEqual(result['equity_curve'][-1]['equity'], float(self.config.account.initial_balance))


if __name__ == '__main__':
    unittest.main()
