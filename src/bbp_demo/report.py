"""One frozen offline correctness demonstration, with independent cash checks."""

from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

from bbp_system.backtest import run_backtest
from bbp_system.config import load_config
from bbp_system.strategy import PreparedFrames, evaluate_strategy
from bbp_system.synthetic import engineered_rule_fixture, ENGINEERED_EPOCH

from .data import load_dataset, utc

RESOURCES = Path(__file__).parent / "resources"
EVIDENCE = "SYNTHETIC_CORRECTNESS_ONLY"


def canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=True) + "\n"


def independent_accounting(
    result: dict[str, Any],
    initial: Decimal | int | float | str,
    scenario: dict[str, Any],
    *,
    slippage_multiplier: int | float = 1,
) -> dict[str, Any]:
    """Recompute each cash leg without calling any ledger/sizing/P&L helper.

    This route is deliberately limited to the stated one-unit USD linear contract
    and conservative prepaid scenario costs. It is not a second market simulator.
    """
    total = Decimal(0)
    legs = []
    for trade in result["trades"]:
        if trade["side"] not in {"LONG", "SHORT"}:
            raise ValueError("Independent accounting requires an explicit LONG or SHORT side")
        entry, exit_price, quantity = (Decimal(str(trade[name])) for name in ("entry", "exit", "volume"))
        sign = Decimal(1 if trade["side"] == "LONG" else -1)
        gross = sign * (exit_price - entry) * quantity
        commission = Decimal(str(scenario["commission_round_trip_usd_per_lot"])) * quantity
        slippage = Decimal(str(scenario["slippage_round_trip_usd_per_lot"])) * Decimal(str(slippage_multiplier)) * quantity
        financing = (entry * Decimal(str(scenario["financing_annual_fraction"]))
                     / Decimal(str(scenario["financing_bank_days"]))
                     * Decimal(str(scenario["financing_rollover_multiplier"])) * quantity)
        net = gross - commission - slippage - financing
        difference = net - Decimal(str(trade["pnl"]))
        costs_difference = commission + slippage + financing - Decimal(str(trade["costs"]))
        if abs(difference) > Decimal("0.000001") or abs(costs_difference) > Decimal("0.000001"):
            raise ValueError("Independent trade accounting does not reconcile")
        total += net
        legs.append({"side": trade["side"], "quantity": str(quantity), "gross": str(gross),
                     "commission": str(commission), "slippage": str(slippage),
                     "financing": str(financing), "net": str(net), "difference": str(difference)})
    final = Decimal(str(initial)) + total
    engine_final = Decimal(str(result["equity_curve"][-1]["equity"])) if result["equity_curve"] else Decimal(str(initial))
    if abs(final - engine_final) > Decimal("0.000001"):
        raise ValueError("Independent cash bridge does not reconcile to engine equity")
    return {"status": "PASS", "initial_cash": str(initial), "gross_to_net_legs": legs,
            "independent_net": str(total), "final_cash": str(final), "engine_final_cash": str(engine_final),
            "difference": str(final - engine_final), "tolerance_usd": "0.000001",
            "scope": "USD linear unit contract; separate Decimal arithmetic and scenario cost formula"}


def build_report() -> dict[str, Any]:
    config = load_config(RESOURCES / "paper.json")
    scenario = json.loads((RESOURCES / "costs.json").read_text(encoding="utf-8"))
    dataset, coverage = load_dataset(RESOURCES / "synthetic_m5.csv")
    native = run_backtest(dataset, config, scenario, warmup_h4=100)
    if native["summary"]["trade_count"] != 0:
        raise ValueError("Frozen raw fixture changed; review results and evidence labels")
    cases = {}
    for name, generous, spread, slippage in (
        ("engineered_acceptance", True, 1, 1),
        ("engineered_cost_rejection", False, 1, 1),
        ("engineered_fixed_rule_cost_stress", True, 2, 2),
    ):
        data = engineered_rule_fixture(generous_target=generous)
        cases[name] = run_backtest(data, config, scenario, warmup_h4=10,
                                  _prepared=PreparedFrames(data["frames"], settings=config.strategy),
                                  spread_multiplier=spread, slippage_multiplier=slippage,
                                  end=ENGINEERED_EPOCH + 1800)
    accepted = cases["engineered_acceptance"]
    decisions = []
    candidate = evaluate_strategy(engineered_rule_fixture()["frames"],
                                  decision_time=ENGINEERED_EPOCH, tick_size=.25)
    decisions.append({"time": ENGINEERED_EPOCH, "action": "CANDIDATE", "case": "engineered_acceptance",
                      "reason": "Completed H4/H1 trend and M15 pullback; waiting for completed M5 confirmation",
                      "available_at": ENGINEERED_EPOCH, "decision_time": ENGINEERED_EPOCH,
                      "side": candidate["side"], "trigger": candidate["trigger"],
                      "trigger_available_at": candidate["trigger_available_at"]})
    for name, result in cases.items():
        for assessment in result["assessments"]:
            decision = {"time": assessment["time"], "action": "ACCEPT" if assessment["accepted"] else "REJECT",
                        "case": name, "reason": assessment["reason"], "side": assessment["side"],
                        "entry": assessment["entry"], "stop": assessment["stop"], "target": assessment["target"],
                        "available_at": assessment["time"], "decision_time": assessment["time"],
                        "confirmation_bar_open": assessment["time"] - 300,
                        "net_reward_risk": assessment.get("net_reward_risk")}
            if assessment["accepted"]:
                trade = next(t for t in result["trades"] if t["setup_id"] == assessment["setup_id"])
                decision.update(quantity=trade["volume"], fill_time=trade["entry_time"],
                                estimated_stop_loss=trade["estimated_stop_loss"])
            decisions.append(decision)
    accounting = independent_accounting(accepted, config.account.initial_balance, scenario)
    accounting["native_raw_ohlc"] = independent_accounting(native, config.account.initial_balance, scenario)
    accounting["cost_stress"] = independent_accounting(cases["engineered_fixed_rule_cost_stress"],
        config.account.initial_balance, scenario, slippage_multiplier=2)
    days = Counter(utc(point["time"] - 300)[:10] for point in native["equity_curve"])
    identities = {name: hashlib.sha256((RESOURCES / name).read_bytes()).hexdigest()
                  for name in ("paper.json", "costs.json", "synthetic_m5.csv")}
    source_digest = hashlib.sha256()
    for package in ("bbp_demo", "bbp_system"):
        folder = Path(__file__).parent.parent / package
        for path in sorted(folder.glob("*.py")):
            source_digest.update(f"{package}/{path.name}\n".encode())
            source_digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    identities["python_source_sha256"] = source_digest.hexdigest()
    return {"schema_version": 1, "evidence": EVIDENCE,
            "title": "Causal rules, explicit costs, reconciled cash",
            "claim": "Software correctness demonstration; no evidence of a trading edge",
            "coverage": coverage, "identities": identities,
            "assumptions": accepted["assumptions"], "risk_policy": accepted["risk_policy"],
            "decisions": sorted(decisions, key=lambda x: (x["time"], x["case"])),
            "trades": accepted["trades"], "accounting": accounting,
            "bars": list(engineered_rule_fixture()["frames"]["M5"].bars),
            "results": {"trade_count": len(accepted["trades"]), "net_profit": accepted["summary"]["net_profit"],
                        "final_equity": accounting["final_cash"],
                        "scope": "Engineered feature case, deliberately stipulated inputs",
                        "native_raw_ohlc": native["summary"], "native_scored_coverage": native["coverage"],
                        "scored_calendar_day_slices": dict(days), "zero_trade_day_slices": len(days),
                        "native_state_counts": native["state_counts"],
                        "cost_stress": cases["engineered_fixed_rule_cost_stress"]["summary"]},
            "engineered_cases": cases,
            "native_replay": native,
            "research_status": {"synthetic": "IMPLEMENTED_AND_REPRODUCIBLE",
                "recorded_private_data": "Prior report only; two hypothetical baseline trades; no raw observations exported",
                "real_reproducible_research": "NOT_INCLUDED; no permitted qualified market dataset or held-out return experiment",
                "recovery": "PROPOSED_SYNTHETIC_ONLY_UNSELECTED",
                "real_clock_qualification": "UNRESOLVED; original real package failed qualification"},
            "trial_ledger": [
                {"id": "raw-v1", "type": "raw synthetic candles", "selection": "Fixed baseline; no parameter search; zero entries retained"},
                {"id": "rule-accept-v1", "type": "stipulated features", "selection": "Authored to demonstrate acceptance and a net loss"},
                {"id": "rule-reject-v1", "type": "stipulated features", "selection": "Authored nearby target fails net reward/risk"},
                {"id": "cost-stress-v1", "type": "stipulated features", "selection": "Frozen rule parameters; double spread/slippage, no reoptimization"}],
            "limitations": ["All distributed candles, features and trades are invented.",
                "Engineered features are not derived from their illustrative OHLC bars.",
                "Raw replay uses 100-H4 warmup for compactness; source default 499 is unchanged in engine.",
                "No holdout, confidence interval or market edge estimate is claimed.",
                "One-bar nominal-close execution assumes zero decision latency; delivery and tick fills are unqualified.",
                "Full conservative round-trip cost budget is charged even for short holding periods.",
                "Calendar day slices are descriptive counts, not independent walk-forward folds.",
                "Real clock qualification and source/vendor redistribution rights remain unresolved for future empirical work."]}


def markdown(report: dict[str, Any]) -> str:
    coverage, result, cash = report["coverage"], report["results"], report["accounting"]
    return f'''# Reproducible correctness report

**Evidence: {report["evidence"]}. No market-edge claim.**

Raw invented candles cover {coverage["start_utc"]} to {coverage["end_utc"]} (end exclusive).
{coverage["observed_m5_bars"]:,} of {coverage["expected_m5_bars"]:,} expected M5 bars are present; three deliberately missing bars remain missing.
Incomplete higher-timeframe aggregates are discarded. See report.json for exact gaps, timeframe counts and hashes.

The raw OHLC replay computes indicators and confirmed pivots through the shared engine:
{result["native_raw_ohlc"]["trade_count"]} trades across {result["native_scored_coverage"]["bar_count"]:,} scored M5 openings.
All {result["zero_trade_day_slices"]} scored calendar-day slices have zero trades. These slices are not held-out folds.
The compact demonstration warms up on 100 H4 candles. This is an explicit demo setting, not a retuned profitability result.

The separate engineered feature case admits {result["trade_count"]} synthetic trade; its net result is ${result["net_profit"]:,.3f}.
Its features are stipulated to exercise the rules and are not indicators calculated from the illustration bars.
The nearby-target variant is rejected after costs; doubling spread and slippage uses frozen parameters.
Neither the acceptance nor the rejection is empirical evidence.

Independent Decimal cash bridge: ${cash["initial_cash"]} + (${cash["independent_net"]}) = ${cash["final_cash"]}.
Difference from engine: ${cash["difference"]}; tolerance ${cash["tolerance_usd"]}.
The check independently rebuilds gross price P&L, commission, slippage and scenario financing for each trade.

Bar timestamps are UTC opens; features become available at completed nominal closes.
The confirmation bar opens at {utc(ENGINEERED_EPOCH)} and closes at {utc(ENGINEERED_EPOCH + 300)}.
The hypothetical next-bar-open fill occurs at that boundary, using the previous completed bar's spread.
This convention assumes zero latency and does not establish broker-clock, network-arrival or tick-execution parity.

Recovery is proposed, synthetic-only and unselected. The real clock qualification gate remains unresolved.
The old private report's two hypothetical baseline trades are recorded context only; they are not included in these results.
No redistributable real-market study or profitability inference is delivered here.

Open dashboard.html for local playback, or run `python run.py dashboard --report report.json` for the guarded loopback server.
All trial choices, assumptions, risk caps, individual decisions and exact identities are in report.json.
'''


def write_report(report: dict[str, Any], destination: str | Path) -> None:
    from .dashboard import render_dashboard
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "report.json").write_text(canonical(report), encoding="utf-8", newline="\n")
    (destination / "report.md").write_text(markdown(report), encoding="utf-8", newline="\n")
    (destination / "dashboard.html").write_text(render_dashboard(report, nonce="offline-static-report"), encoding="utf-8", newline="\n")
