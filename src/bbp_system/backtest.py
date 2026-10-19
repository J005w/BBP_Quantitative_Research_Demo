"""Chronological BTCUSD bar research with the scanner's strategy and ledger.

This is hypothetical automatic acceptance of eligible research signals, not a
reconstruction of discretionary review or observed tick executions. Native Bid
OHLC is used with the preceding completed bar's spread carried forward. Missing
or zero spread uses an explicit scenario; future spread is never borrowed.
Within-bar path is unknown: stop wins an ambiguous stop/target bar. The shared
PaperLedger supplies sizing, costs, quote-side accounting, daily/drawdown pauses
and holding exits through an in-memory adapter; it never changes the live ledger.
"""

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import math

from .paper import PaperLedger, Quote
from .sizing import (RiskPolicy, BrokerBasis, CostProfile, QuantityGrid,
                     SizingRequest, decimal, request_from_dict, request_metrics)
from .strategy import (PERIODS, VERSION, FrameFeatures, PreparedFrames, RollingFrames,
                       evaluate_strategy, nearest_target)
from .research_parameters import effective_parameters
from .level_evidence import known_target_obstacles


DAY = 86400


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value):
    if value is None:
        return None
    if isinstance(value, str):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("research dates must specify a timezone")
        value = stamp.timestamp()
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("research dates require finite UTC epoch seconds")
    return float(value)


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name)


class _MemoryLedger(PaperLedger):
    """Single-run adapter: shared ledger rules without disk writes per candle.

    No persistent file is opened. Public reserve/enter methods and financial
    _apply_quote logic are inherited; final state receives the normal ledger
    consistency validation. The adapter is deliberately not a live ledger.
    """

    def __init__(self, initial_equity, policy, *, cost_model=None, slippage_multiplier=1):
        self.policy = policy
        self.cost_model = cost_model
        self.slippage_multiplier = slippage_multiplier
        equity = decimal(initial_equity)
        if equity <= 0:
            raise ValueError("initial equity must be positive")
        self._state = {
            "schema_version": 1, "mode": "SIMULATED_PAPER", "currency": "USD",
            "initial_equity": str(equity), "cash": str(equity), "peak_equity": str(equity),
            "day_start_equity": str(equity), "day_key": None, "as_of": None,
            "last_quote": None, "records": {}, "revision": 0, "policy": policy.as_dict(),
        }
        self.clock = lambda: self._state["as_of"] or 0

    @contextmanager
    def _locked(self):
        yield

    def _read(self):
        return self._state

    def _write(self, state):
        self._state = state

    def mark(self, quote):
        if self.cost_model is not None:
            # Validate quote sides before adding a financing price observation;
            # an invalid quote must not become a later rollover proxy.
            quote = quote.normalized()
            self.cost_model.observe_price(quote.time, quote.bid)
        _, events = self._apply_quote(self._state, quote, quote.time)
        if self.cost_model is not None:
            for record in self._state["records"].values():
                if record["status"] in ("OPEN", "CLOSED"):
                    record["accrued_costs"] = self._event_costs(record,
                        record["closed_at"] if record["status"] == "CLOSED" else quote.time)
        self._state["revision"] += 1
        return events

    def _event_costs(self, record, at):
        request = request_from_dict(record["request"])
        return self.cost_model.realised_costs(request.side, request.entry,
            record["entered_at"], at, decimal(record["volume"]),
            slippage_multiplier=self.slippage_multiplier)

    def _cash_cost(self, record, at):
        if self.cost_model is None:
            return super()._cash_cost(record, at)
        return decimal(self._event_costs(record, at)["cash_cost"])

    def active(self):
        return next((record for record in self._state["records"].values()
                     if record["status"] == "OPEN"), None)

    def flatten(self, quote, reason):
        """Research-boundary close; no open exposure spills into another fold."""
        self.mark(quote)
        record = self.active()
        if record is None:
            return
        pnl = self._pnl(record, quote)
        request = request_from_dict(record["request"])
        record.update(status="CLOSED", reason=reason,
                      exit_price=str(quote.bid if request.side == "LONG" else quote.ask),
                      closed_at=quote.time, fill_quote_time=quote.time, realized_pnl=str(pnl),
                      late_exit_seconds=max(0, quote.time-record["deadline_at"]), unrealized_pnl="0")
        self._state["cash"] = str(decimal(self._state["cash"]) + pnl)
        self._state["peak_equity"] = str(max(decimal(self._state["peak_equity"]), self._equity(self._state)))


def _policy(config, scenario, parameters=None):
    parameters = effective_parameters(parameters)
    parameters.validate_holding_ceiling(config)
    return RiskPolicy(
        per_trade_fraction=config.risk.per_trade_fraction,
        total_fraction=config.risk.total_fraction,
        crypto_fraction=config.risk.group_fraction,
        max_hold_hours=config.holding.max_hours,
        research_exit_hours=None if parameters.max_hold_hours == 12 else int(parameters.max_hold_hours),
        daily_loss_fraction=scenario["daily_loss_fraction"],
        drawdown_fraction=scenario["drawdown_fraction"],
        gross_multiple=scenario["gross_notional_multiple"],
        margin_fraction=scenario["used_margin_fraction"],
    )


def _source(dataset, config, prepared=None, visible_window=499):
    if dataset.get("symbol") != "BTCUSD" or config.account.currency != "USD":
        raise ValueError("historical research supports BTCUSD and a USD account only")
    meta = dataset["metadata"]
    if meta.get("currency_profit") != "USD" or decimal(meta.get("trade_contract_size", 0)) != 1:
        raise ValueError("the qualified linear 1-BTC USD contract is required")
    if meta.get("chart_mode") != 0:
        raise ValueError("historical execution model requires MT5 Bid OHLC")
    for name in ("point", "trade_tick_size", "volume_min", "volume_max", "volume_step"):
        if decimal(meta[name]) <= 0:
            raise ValueError(f"positive {name} is required")
    return prepared or RollingFrames(dataset["frames"], window=visible_window, settings=config.strategy)


def _spread(previous_bar, meta, fallback, multiplier):
    points = previous_bar.get("spread_points", previous_bar.get("spread")) if previous_bar else None
    if points is not None and decimal(points) < 0:
        raise ValueError("historical spread must not be negative")
    if points is None or decimal(points) <= 0:
        return decimal(fallback) * decimal(multiplier), "SCENARIO_MISSING_OR_ZERO"
    return decimal(points) * decimal(meta["point"]) * decimal(multiplier), "PREVIOUS_COMPLETED_BAR_PROXY"


def _quote(bid, spread, stamp):
    return Quote("BTCUSD", float(stamp), decimal(bid), decimal(bid)+spread)


def bar_exit_event(record, bar, spread):
    """Return the conservative intrabar executable price, never a tick claim.

    Opening gap and 12-hour checks occur first in the ledger. For the surviving
    position, both price boundaries touched implies STOP, at the stop level.
    Long exits use Bid and short exits use synthetic Ask. No favorable gap
    interpolation or complete intrabar path is claimed.
    """
    request = request_from_dict(record["request"])
    low, high = decimal(bar["low"]), decimal(bar["high"])
    if request.side == "SHORT":
        low, high = low+spread, high+spread
    stop = low <= request.stop if request.side == "LONG" else high >= request.stop
    target = high >= request.target if request.side == "LONG" else low <= request.target
    if not stop and not target:
        return None
    price = request.stop if stop else request.target
    bid = price if request.side == "LONG" else price-spread
    return {"quote": _quote(bid, spread, bar["time"]+150),
            "reason": "STOP" if stop else "TARGET", "ambiguous": bool(stop and target),
            "timing": "ASSUMED_MIDBAR_TIME_UNKNOWN_TICK_PATH"}


def _request(frames, setup, quote, meta, config, scenario, at, slippage_multiplier, *, parameters=None,
             cost_model=None):
    parameters = effective_parameters(parameters)
    entry = quote.ask if setup["side"] == "LONG" else quote.bid
    stop = decimal(setup["stop"])
    target = nearest_target(frames, decision_time=at, side=setup["side"], entry=float(entry),
                            tick_size=float(meta["trade_tick_size"]), settings=config.strategy)
    if target is None:
        raise ValueError("MISSING_EXECUTABLE_TARGET")
    target = decimal(target)
    maximum = decimal(meta["volume_max"])
    if decimal(meta.get("volume_limit", 0)) > 0:
        maximum = min(maximum, decimal(meta["volume_limit"]))
    financing = (entry * decimal(scenario["financing_annual_fraction"])
                 / decimal(scenario["financing_bank_days"])
                 * decimal(scenario["financing_rollover_multiplier"]))
    costs = CostProfile(decimal(scenario["commission_round_trip_usd_per_lot"]),
        decimal(scenario["slippage_round_trip_usd_per_lot"])*decimal(slippage_multiplier),
        financing, scenario["label"])
    if cost_model is not None:
        costs = cost_model.entry_costs(setup["side"], entry, at, parameters.max_hold_hours,
                                      slippage_multiplier=slippage_multiplier)
    return SizingRequest("BTCUSD", setup["side"], entry, stop, target,
        BrokerBasis(Decimal(1), -abs(entry-stop), abs(target-entry),
                    entry*decimal(scenario["margin_fraction_floor"]), entry),
        costs,
        QuantityGrid(decimal(meta["volume_min"]), maximum, decimal(meta["volume_step"])),
        minimum_net_rr=parameters.minimum_net_rr)


def _summary(trades, curve, initial):
    pnls = [decimal(trade["pnl"]) for trade in trades]
    wins, losses = sum((p for p in pnls if p > 0), Decimal(0)), -sum((p for p in pnls if p < 0), Decimal(0))
    peak, drawdown = decimal(initial), Decimal(0)
    for point in curve:
        equity = decimal(point["equity"])
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak if peak > 0 else Decimal(0))
    final = decimal(curve[-1]["equity"]) if curve else decimal(initial)
    costs = sum((decimal(t["costs"]) for t in trades), Decimal(0))
    return {"trade_count": len(trades), "net_profit": float(final-decimal(initial)),
            "return_percent": float((final/decimal(initial)-1)*100),
            "max_drawdown_percent": float(drawdown*100),
            "win_rate_percent": float(100*sum(p > 0 for p in pnls)/len(pnls)) if pnls else None,
            "profit_factor": float(wins/losses) if losses else None,
            "profit_factor_status": "DEFINED" if losses else "NO_LOSING_TRADES" if pnls else "NO_TRADES",
            "long_count": sum(t["side"] == "LONG" for t in trades),
            "short_count": sum(t["side"] == "SHORT" for t in trades),
            "estimated_costs": float(costs), "gross_profit": float(sum(pnls, Decimal(0))+costs)}


def run_backtest(dataset, config, scenario, *, start=None, end=None, warmup_h4=499,
                 fallback_spread_usd=12, spread_multiplier=1, slippage_multiplier=1,
                 entry_cutoff=None, visible_window=499, _prepared=None, parameters=None, cost_model=None):
    """Run fixed rules on completed history; entry is the next M5 bar's open.

    ``start``/``end`` constrain assessed M5 opening times, not indicator history.
    Warm-up always comes from preceding H4 data. A missing next bar or a review
    gap of 300 seconds expires the signal. A 12-hour entry purge can be supplied
    through ``entry_cutoff``; any survivor is flattened at the last bar close.
    """
    if type(warmup_h4) is not int or warmup_h4 < 1:
        raise ValueError("warmup_h4 must be a positive integer")
    parameters = effective_parameters(parameters)
    parameters.validate_holding_ceiling(config)
    if any(decimal(value) <= 0 for value in (fallback_spread_usd, spread_multiplier, slippage_multiplier)):
        raise ValueError("spread and slippage assumptions must be positive")
    if not scenario.get("label") or decimal(scenario["margin_fraction_floor"]) < Decimal("0.5"):
        raise ValueError("labelled scenario and conservative 50% BTC margin floor are required")
    for name in ("commission_round_trip_usd_per_lot", "slippage_round_trip_usd_per_lot", "financing_annual_fraction"):
        if decimal(scenario[name]) < 0:
            raise ValueError("cost assumptions must be finite and nonnegative")
    for name in ("financing_bank_days", "financing_rollover_multiplier", "max_spread_atr_fraction"):
        if decimal(scenario[name]) <= 0:
            raise ValueError("financing calendar and spread assumptions must be positive")
    prepared = _source(dataset, config, _prepared, visible_window)
    full = prepared.full_history()
    h4 = full["H4"].bars
    requested_start, requested_end = _epoch(start), _epoch(end)
    if len(h4) < warmup_h4 or not full["M5"].bars:
        return {"status": "INSUFFICIENT_HISTORY", "strategy_version": VERSION,
                "parameters": parameters.as_dict(), "parameter_sha256": parameters.sha256,
                "summary": _summary([], [], config.account.initial_balance), "trades": [], "equity_curve": [],
                "assessments": [],
                "coverage": {"status": "INSUFFICIENT_HISTORY", "bar_count": 0, "days": 0,
                             "start_utc": None, "end_utc": None},
                "warnings": [f"At least {warmup_h4} completed H4 candles are required before scored decisions."]}
    warmup_at = h4[warmup_h4-1]["available_at"]
    start_at = max(warmup_at, requested_start if requested_start is not None else warmup_at)
    end_at = requested_end if requested_end is not None else full["M5"].bars[-1]["available_at"]
    cutoff = _epoch(entry_cutoff) if entry_cutoff is not None else end_at
    if cost_model is not None:
        if (cost_model.profile.contract.symbol != "BTCUSD" or cost_model.profile.contract.units_per_lot != 1
                or cost_model.profile.contract.price_currency != "USD"
                or cost_model.profile.contract.account_currency != config.account.currency):
            raise ValueError("BTC replay event costs require the same one-BTC USD contract and account currency")
        cost_model = cost_model.new_run()
    ledger = _MemoryLedger(config.account.initial_balance, _policy(config, scenario, parameters),
                           cost_model=cost_model, slippage_multiplier=slippage_multiplier)
    previous, seen, curve = None, set(), []
    assessments = []
    rejections, regimes, states, spreads = Counter(), Counter(), Counter(), Counter()
    ambiguities, assessed, last_quote = 0, 0, None
    meta = dataset["metadata"]
    m5 = full["M5"].bars
    first_open = None
    for index, bar in enumerate(m5):
        at = bar["time"]
        if at < start_at or bar["available_at"] > end_at:
            continue
        assessed += 1
        first_open = at if first_open is None else first_open
        spread, basis = _spread(m5[index-1] if index else None, meta, fallback_spread_usd, spread_multiplier)
        spreads[basis] += 1
        opening = _quote(bar["open"], spread, at)
        ledger.mark(opening)  # Adverse gap / target / max-hold before new entries.
        known = prepared.at(at)
        fresh = all(frame.bars and at-frame.bars[-1]["available_at"] <= PERIODS[tf]+60
                    for tf, frame in known.items())
        state = evaluate_strategy(prepared, decision_time=at, tick_size=meta["trade_tick_size"],
                                  previous=previous, settings=config.strategy, data_fresh=fresh, parameters=parameters)
        previous = state
        regimes[state["context"]["regime"]] += 1
        states[state["state"]] += 1
        if state["state"] == "CONFIRMED" and state["setup_id"] not in seen:
            sid = state["setup_id"]
            seen.add(sid)
            assessment = {"setup_id": sid, "signal_id": state["signal_id"], "time": at, "side": state["side"],
                          "entry": float(opening.ask if state["side"] == "LONG" else opening.bid),
                          "stop": state["stop"], "target": state["target"],
                          "signal_reference_entry": state["reference_entry"],
                          "accepted": False, "reason": None}
            assessment["level_evidence"] = known_target_obstacles(known, decision_time=at,
                side=state["side"], entry=assessment["entry"], tick_size=meta["trade_tick_size"],
                settings=config.strategy)
            assessments.append(assessment)
            if at >= cutoff:
                rejections["FOLD_END_12H_ENTRY_PURGE"] += 1
                assessment["reason"] = "FOLD_END_12H_ENTRY_PURGE"
            elif ledger.active():
                rejections["ONE_POSITION_PER_SYMBOL"] += 1
                assessment["reason"] = "ONE_POSITION_PER_SYMBOL"
            elif not fresh:
                rejections["STALE_HIGHER_TIMEFRAME"] += 1
                assessment["reason"] = "STALE_HIGHER_TIMEFRAME"
            elif state["confirmation_at"] != at:
                rejections["NEXT_BAR_OPEN_OR_REVIEW_WINDOW_MISSED"] += 1
                assessment["reason"] = "NEXT_BAR_OPEN_OR_REVIEW_WINDOW_MISSED"
            else:
                try:
                    atr = _field(known["M5"].indicators[-1], "atr")
                    if not atr or spread > decimal(atr)*decimal(scenario["max_spread_atr_fraction"]):
                        raise ValueError("SPREAD_ATR_LIMIT")
                    request = _request(prepared, state, opening, meta, config, scenario, at, slippage_multiplier,
                                       parameters=parameters, cost_model=cost_model)
                    sized = ledger.preview(request, opening, as_of=at)
                    metrics = request_metrics(request)
                    assessment.update({"entry": float(request.entry), "stop": float(request.stop),
                        "target": float(request.target), "stop_distance": float(abs(request.entry-request.stop)),
                        "target_distance": float(abs(request.target-request.entry)),
                        "gross_reward_risk": float(request.basis.target_pnl/-request.basis.stop_pnl),
                        "commission_per_lot": float(request.costs.commission_per_lot),
                        "slippage_per_lot": float(request.costs.slippage_per_lot),
                        "financing_per_lot": float(request.costs.financing_per_lot),
                        "total_cost_per_lot": float(metrics["cost_per_lot"]),
                        "estimated_loss_per_lot": float(metrics["loss_per_lot"]),
                        "net_target_profit_per_lot": float(metrics["net_profit_per_lot"]),
                        "net_reward_risk": float(sized.net_reward_risk),
                        "accepted": sized.accepted, "reason": sized.reason})
                    if not sized.accepted:
                        raise ValueError(sized.reason)
                    reserved = ledger.reserve(sid, request, expires_at=state["review_expires_at"],
                                              quote=opening, confirmed=True, as_of=at)
                    if not reserved["accepted"]:
                        raise ValueError(reserved["reason"])
                    entered = ledger.enter(sid, request, opening, as_of=at)
                    if not entered["accepted"]:
                        raise ValueError(entered["reason"])
                    assessment["reason"] = entered["reason"]
                except (ValueError, IndexError) as error:
                    rejections[str(error)] += 1
                    assessment.update(accepted=False, reason=str(error))
        active = ledger.active()
        if active:
            event = bar_exit_event(active, bar, spread)
            if event:
                ambiguities += int(event["ambiguous"])
                ledger.mark(event["quote"])
        # The timestamp denotes the last instant of the assessed M5 interval;
        # no entry decision sees this Close until nominal availability at +300.
        last_quote = _quote(bar["close"], spread, at+300-0.001)
        ledger.mark(last_quote)
        curve.append({"time": at+300, "equity": float(ledger._equity(ledger._state))})
    if last_quote is not None:
        ledger.flatten(last_quote, "RESEARCH_PERIOD_END")
        curve[-1]["equity"] = float(ledger._equity(ledger._state))
    ledger._validate(ledger._state)
    trades = []
    for sid, record in ledger._state["records"].items():
        if record["status"] != "CLOSED":
            continue
        request = request_from_dict(record["request"])
        signal_id = next((item["signal_id"] for item in assessments if item["setup_id"] == sid), sid)
        trades.append({"setup_id": sid, "signal_id": signal_id, "side": request.side, "entry_time": record["entered_at"],
                       "exit_time": record["closed_at"], "entry": float(request.entry),
                       "exit": float(decimal(record["exit_price"])), "stop": float(request.stop),
                       "target": float(request.target), "volume": float(decimal(record["volume"])),
                       "pnl": float(decimal(record["realized_pnl"])), "reason": record["reason"],
                       "costs": float(ledger._cash_cost(record, record["closed_at"])),
                       "estimated_stop_loss": float(decimal(record["estimated_stop_loss"])),
                       "net_reward_risk": float(decimal(record["sizing"]["net_reward_risk"])),
                       "holding_seconds": record["closed_at"]-record["entered_at"],
                       "late_exit_seconds": record.get("late_exit_seconds", 0),
                       **({"accrued_costs": ledger._event_costs(record, record["closed_at"])}
                          if cost_model is not None else {})})
    status = "COMPLETED_RESEARCH" if assessed else "INSUFFICIENT_SCORED_HISTORY"
    return {"status": status, "strategy_version": VERSION,
            "cost_methodology": cost_model.methodology() if cost_model is not None else {"model": "ORIGINAL_CONSERVATIVE_SCENARIO"},
            "summary": _summary(trades, curve, config.account.initial_balance),
            "parameters": parameters.as_dict(), "parameter_sha256": parameters.sha256,
            "coverage": {"status": status, "start_utc": _utc(first_open) if first_open is not None else None,
                         "end_utc": _utc(curve[-1]["time"]) if curve else None,
                         "bar_count": assessed, "days": (curve[-1]["time"]-first_open)/DAY if curve else 0,
                         "visible_closed_candles_per_timeframe": visible_window,
                         "warmup_h4_candles": warmup_h4, "warmup_completed_at_utc": _utc(warmup_at)},
            "equity_curve": curve, "trades": trades, "assessments": assessments, "rejections": dict(rejections),
            "regime_counts": dict(regimes), "state_counts": dict(states), "spread_basis_counts": dict(spreads),
            "ambiguous_stop_target_bars": ambiguities,
            "risk_policy": ledger.policy.as_dict(),
            "assumptions": {"execution": "Automatic hypothetical acceptance; scanner shared rules, no broker orders",
                "entry": "Next M5 Bid Open after completed confirmation; long Ask adds previous-bar spread proxy",
                "spread": "Previous completed M5 spread_points x point; zero/missing uses explicit scenario",
                "fallback_spread_usd": float(decimal(fallback_spread_usd)),
                "spread_multiplier": float(decimal(spread_multiplier)), "slippage_multiplier": float(decimal(slippage_multiplier)),
                "intrabar": "Stop first if both boundaries touched; assumed midpoint time, not observed ticks",
                "gap": "Exit at first available opening quote; stop gaps can exceed planned risk",
                "marking": "M5 closes plus entry/exit events; max drawdown is sampled, not tick-level",
                "costs": dict(scenario), "availability": "Historical nominal candle close, not observed delivery",
                "input_scope": f"Exactly last {visible_window} available closed candles per timeframe; shared indicators and pivots recomputed with that window seed",
                "boundary": "No scored data after requested end; survivor flattened at final assessed M5 close"},
            "warnings": ["Research simulation does not establish profitability or observed manual-review performance.",
                "Historical timezone/DST, session anchors and gaps remain unqualified.",
                "Bid OHLC and carried spread cannot reproduce actual Bid/Ask ticks or precise intrabar event times.",
                f"Costs and 50% margin floor are scenarios; a missed quote can delay the {parameters.max_hold_hours}-hour exit."]}


def run_walk_forward(dataset, config, scenario, *, context_days=14, test_days=7,
                     warmup_h4=499, visible_window=499, start=None, end=None, _prepared=None,
                     parameters=None,
                     **backtest_options):
    """Fixed-rule chronological held-out windows; no parameter fitting.

    First 14 scored days are training/context only, then non-overlapping 7-day
    tests. Every fold starts flat with USD 200k independent equity and resets
    daily/drawdown state. Indicator warm-up is inherited from strictly prior
    bars. The last 12 hours reject new entries; surviving exposure is flattened
    before the next fold. Training trades/PnL never enter held-out metrics.
    """
    if type(context_days) is not int or type(test_days) is not int or min(context_days, test_days) < 1:
        raise ValueError("walk-forward windows require positive integer days")
    parameters = effective_parameters(parameters)
    parameters.validate_holding_ceiling(config)
    prepared = _source(dataset, config, _prepared, visible_window=visible_window)
    full = prepared.full_history()
    h4, m5 = full["H4"].bars, full["M5"].bars
    policy = {"name": "FIXED_RULE_WALK_FORWARD_NO_OPTIMIZATION", "context_days": context_days,
              "test_days": test_days, "warmup_h4_candles": warmup_h4,
              "visible_closed_candles_per_timeframe": visible_window,
              "initial_equity_per_fold": float(config.account.initial_balance),
              "boundary": f"Flat independent folds; last {parameters.max_hold_hours}h entry purge, end flatten; no training performance scored",
              "parameters": parameters.as_dict(), "parameter_sha256": parameters.sha256,
              "selection": "Fixed explicit research parameters; no selection using future folds"}
    if len(h4) < warmup_h4 or not m5:
        return {"status": "INSUFFICIENT_HISTORY", "folds": [], "policy": policy,
                "warnings": ["History is shorter than the declared warm-up."]}
    origin = max(h4[warmup_h4-1]["available_at"], _epoch(start) if start is not None else 0)
    latest = min(m5[-1]["available_at"], _epoch(end) if end is not None else m5[-1]["available_at"])
    test_start = origin+context_days*DAY
    folds = []
    while test_start+test_days*DAY <= latest:
        test_end = test_start+test_days*DAY
        result = run_backtest(dataset, config, scenario, start=test_start, end=test_end,
                              entry_cutoff=test_end-float(parameters.max_hold_hours)*3600,
                              warmup_h4=warmup_h4, visible_window=visible_window,
                              _prepared=prepared, parameters=parameters, **backtest_options)
        summary = result["summary"]
        folds.append({"label": f"Held-out {len(folds)+1}", "start_utc": _utc(test_start),
                      "end_utc": _utc(test_end), "context_start_utc": _utc(test_start-context_days*DAY),
                      **summary, "coverage": result["coverage"], "trades": result["trades"],
                      "equity_curve": result["equity_curve"], "assessments": result["assessments"],
                      "rejections": result["rejections"]})
        test_start = test_end
    return {"status": "COMPLETED_FIXED_RULE_RESEARCH" if folds else "INSUFFICIENT_HELD_OUT_HISTORY",
            "folds": folds, "policy": policy,
            "summary": {"fold_count": len(folds), "trade_count": sum(f["trade_count"] for f in folds),
                        "net_profit": sum(f["net_profit"] for f in folds),
                        "positive_folds": sum(f["net_profit"] > 0 for f in folds),
                        "negative_folds": sum(f["net_profit"] < 0 for f in folds),
                        "aggregate_return_percent": None,
                        "aggregation": "Independent fold dollar PnL summed; no compounded return across reset accounts"},
            "warnings": ["No optimization was performed; held-out windows test the fixed research hypothesis.",
                         "Fold resets, bar-level execution and unqualified historical timestamps limit interpretation."]}
