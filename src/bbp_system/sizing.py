"""Product-aware paper sizing with explicit broker P/L and cost assumptions.

This module submits no orders. BrokerBasis must come from a read-only broker
calculation at the specified executable entry/stop/target, in USD. Supported
simulation is a linear BTCUSD CFD; cross-currency/nonlinear contracts fail closed.
Quote-side prices already include spread. Costs are separate round-trip USD
amounts per lot; the slippage estimate is not also applied to the fill price.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR


def decimal(value, name="number"):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite numeric data")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{name} must be finite numeric data") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _positive(value, name):
    value = decimal(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def json_values(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_values(item) for item in value]
    return value


@dataclass(frozen=True)
class RiskPolicy:
    per_trade_fraction: Decimal = Decimal("0.0025")
    total_fraction: Decimal = Decimal("0.01")
    crypto_fraction: Decimal = Decimal("0.005")
    daily_loss_fraction: Decimal | None = Decimal("0.01")
    drawdown_fraction: Decimal | None = Decimal("0.05")
    gross_multiple: Decimal | None = Decimal("1")
    margin_fraction: Decimal | None = Decimal("0.25")
    max_hold_hours: int = 12
    research_exit_hours: int | None = None

    def __post_init__(self):
        for name in ("per_trade_fraction", "total_fraction", "crypto_fraction"):
            value = _positive(getattr(self, name), name)
            if value > 1:
                raise ValueError(f"{name} exceeds 1")
            object.__setattr__(self, name, value)
        if not self.per_trade_fraction <= self.crypto_fraction <= self.total_fraction:
            raise ValueError("risk caps must satisfy per trade <= crypto <= aggregate")
        if self.per_trade_fraction > Decimal("0.0025") or self.total_fraction > Decimal("0.01") or self.crypto_fraction > Decimal("0.005"):
            raise ValueError("policy must not exceed accepted per-trade/aggregate or research crypto ceilings")
        for name in ("daily_loss_fraction", "drawdown_fraction", "gross_multiple", "margin_fraction"):
            if getattr(self, name) is not None:
                value = _positive(getattr(self, name), name)
                if name != "gross_multiple" and value > 1:
                    raise ValueError(f"{name} exceeds 1")
                object.__setattr__(self, name, value)
        if type(self.max_hold_hours) is not int or self.max_hold_hours != 12:
            raise ValueError("the accepted paper holding limit is 12 hours")
        if self.research_exit_hours is not None and (
            type(self.research_exit_hours) is not int or not 1 <= self.research_exit_hours <= self.max_hold_hours
        ):
            raise ValueError("research exit horizon must be within the accepted holding limit")

    @property
    def effective_hold_hours(self):
        return self.max_hold_hours if self.research_exit_hours is None else self.research_exit_hours

    def as_dict(self):
        values = asdict(self)
        if self.research_exit_hours is None:
            del values["research_exit_hours"]
        return json_values(values)


@dataclass(frozen=True)
class BrokerBasis:
    reference_volume: Decimal
    stop_pnl: Decimal
    target_pnl: Decimal
    margin: Decimal
    notional: Decimal
    currency: str = "USD"


@dataclass(frozen=True)
class CostProfile:
    commission_per_lot: Decimal
    slippage_per_lot: Decimal
    financing_per_lot: Decimal
    label: str


@dataclass(frozen=True)
class QuantityGrid:
    minimum: Decimal
    maximum: Decimal
    step: Decimal


@dataclass(frozen=True)
class SizingRequest:
    symbol: str
    side: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    basis: BrokerBasis
    costs: CostProfile
    grid: QuantityGrid
    minimum_net_rr: Decimal = Decimal("2")

    def as_dict(self):
        return json_values(asdict(self))


def request_from_dict(data):
    return normalize_request(SizingRequest(
        symbol=data["symbol"], side=data["side"], entry=data["entry"], stop=data["stop"],
        target=data["target"], basis=BrokerBasis(**data["basis"]),
        costs=CostProfile(**data["costs"]), grid=QuantityGrid(**data["grid"]),
        minimum_net_rr=data["minimum_net_rr"],
    ))


def normalize_request(request):
    return _normalize_request(request, supported_symbol="BTCUSD")


def _normalize_request(request, *, supported_symbol):
    if request.symbol != supported_symbol or request.side not in ("LONG", "SHORT"):
        raise ValueError("only BTCUSD LONG/SHORT USD-CFD simulation is supported")
    entry, stop, target = (_positive(getattr(request, name), name) for name in ("entry", "stop", "target"))
    if not (stop < entry < target if request.side == "LONG" else target < entry < stop):
        raise ValueError("entry must lie strictly between structural stop and target")
    basis = BrokerBasis(**{
        name: decimal(getattr(request.basis, name), f"basis.{name}")
        for name in ("reference_volume", "stop_pnl", "target_pnl", "margin", "notional")
    })
    if request.basis.currency != "USD":
        raise ValueError("broker P/L and margin must be in the USD paper account currency")
    if basis.reference_volume <= 0 or basis.stop_pnl >= 0 or basis.target_pnl <= 0 or basis.margin <= 0 or basis.notional <= 0:
        raise ValueError("broker USD P/L, reference volume, margin and notional basis is invalid")
    value_at_stop = -basis.stop_pnl / (basis.reference_volume * abs(entry - stop))
    value_at_target = basis.target_pnl / (basis.reference_volume * abs(target - entry))
    if abs(value_at_stop - value_at_target) > max(value_at_stop, value_at_target) * Decimal("0.000001"):
        raise ValueError("broker P/L basis is not a consistent linear USD contract")
    expected_notional = entry * value_at_stop * basis.reference_volume
    if abs(expected_notional - basis.notional) > expected_notional * Decimal("0.000001"):
        raise ValueError("notional basis disagrees with the USD contract's price/P&L basis")
    grid = QuantityGrid(**{
        name: _positive(getattr(request.grid, name), f"grid.{name}")
        for name in ("minimum", "maximum", "step")
    })
    if grid.minimum > grid.maximum or (basis.reference_volume - grid.minimum) % grid.step != 0 or not grid.minimum <= basis.reference_volume <= grid.maximum:
        raise ValueError("reference volume or quantity range is not on the broker grid")
    if not isinstance(request.costs.label, str) or not request.costs.label.strip():
        raise ValueError("explicit cost assumption label is required")
    costs = CostProfile(**{
        name: decimal(getattr(request.costs, name), f"costs.{name}")
        for name in ("commission_per_lot", "slippage_per_lot", "financing_per_lot")
    }, label=request.costs.label)
    if any(getattr(costs, name) < 0 for name in ("commission_per_lot", "slippage_per_lot", "financing_per_lot")):
        raise ValueError("cost estimates must not be negative")
    return SizingRequest(request.symbol, request.side, entry, stop, target, basis, costs, grid,
                         _positive(request.minimum_net_rr, "minimum_net_rr"))


def request_metrics(request):
    return _request_metrics(normalize_request(request))


def _request_metrics(request):
    basis, costs = request.basis, request.costs
    fees = costs.commission_per_lot + costs.slippage_per_lot + costs.financing_per_lot
    return {
        "loss_per_lot": -basis.stop_pnl / basis.reference_volume + fees,
        "net_profit_per_lot": basis.target_pnl / basis.reference_volume - fees,
        "margin_per_lot": basis.margin / basis.reference_volume,
        "notional_per_lot": basis.notional / basis.reference_volume,
        "price_value_per_lot": -basis.stop_pnl / (basis.reference_volume * abs(request.entry - request.stop)),
        "cost_per_lot": fees,
    }


@dataclass(frozen=True)
class SizingResult:
    accepted: bool
    reason: str
    request: SizingRequest
    volume: Decimal
    estimated_stop_loss: Decimal
    net_target_profit: Decimal
    net_reward_risk: Decimal
    margin: Decimal
    notional: Decimal

    def as_dict(self):
        return json_values(asdict(self))


def size_position(request, *, equity, policy=None, open_and_reserved_risk=0,
                  crypto_risk=0, current_gross_notional=0, used_margin=0,
                  day_start_equity=None, peak_equity=None, free_margin=None):
    """Floor to the broker grid and recheck every entry limit in USD.

    Additional daily/drawdown/gross/margin controls are optional conservative
    research defaults. They pause new entries; existing positions keep exits.
    """
    request, policy = normalize_request(request), policy or RiskPolicy()
    return _size_normalized_position(request, equity=equity, policy=policy,
        open_and_reserved_risk=open_and_reserved_risk, crypto_risk=crypto_risk,
        current_gross_notional=current_gross_notional, used_margin=used_margin,
        day_start_equity=day_start_equity, peak_equity=peak_equity, free_margin=free_margin)


def _size_normalized_position(request, *, equity, policy, open_and_reserved_risk,
                  crypto_risk, current_gross_notional, used_margin,
                  day_start_equity, peak_equity, free_margin):
    equity = decimal(equity, "equity")
    metrics = _request_metrics(request)
    risk, group, gross, margin = (
        decimal(value, name) for value, name in (
            (open_and_reserved_risk, "risk"), (crypto_risk, "crypto risk"),
            (current_gross_notional, "gross exposure"), (used_margin, "used margin"),
        )
    )
    if min(risk, group, gross, margin) < 0:
        raise ValueError("existing risk/exposure/margin must not be negative")
    net_rr = metrics["net_profit_per_lot"] / metrics["loss_per_lot"]

    def rejected(reason):
        return SizingResult(False, reason, request, Decimal(0), Decimal(0), Decimal(0), net_rr, Decimal(0), Decimal(0))

    if equity <= 0:
        return rejected("NONPOSITIVE_EQUITY")
    if net_rr < request.minimum_net_rr:
        return rejected("INSUFFICIENT_NET_REWARD_RISK")
    allowances = [equity * policy.per_trade_fraction, equity * policy.total_fraction - risk,
                  equity * policy.crypto_fraction - group]
    if policy.daily_loss_fraction is not None and day_start_equity is not None:
        start = _positive(day_start_equity, "day_start_equity")
        allowances.append(start * policy.daily_loss_fraction - max(start - equity, Decimal(0)))
        if allowances[-1] <= 0:
            return rejected("DAILY_LOSS_PAUSE")
    if policy.drawdown_fraction is not None and peak_equity is not None:
        peak = _positive(peak_equity, "peak_equity")
        allowances.append(equity - peak * (1 - policy.drawdown_fraction))
        if allowances[-1] <= 0:
            return rejected("DRAWDOWN_PAUSE")
    if min(allowances) <= 0:
        return rejected("RISK_CAP_EXHAUSTED")
    maxima = [request.grid.maximum, min(allowances) / metrics["loss_per_lot"]]
    if policy.gross_multiple is not None:
        maxima.append((equity * policy.gross_multiple - gross) / metrics["notional_per_lot"])
    if policy.margin_fraction is not None:
        maxima.append((equity * policy.margin_fraction - margin) / metrics["margin_per_lot"])
    if free_margin is not None:
        maxima.append(decimal(free_margin, "free_margin") / metrics["margin_per_lot"])
    ceiling = min(maxima)
    if ceiling < request.grid.minimum:
        return rejected("BELOW_MINIMUM_QUANTITY_OR_EXPOSURE_CAP")
    steps = ((ceiling - request.grid.minimum) / request.grid.step).to_integral_value(rounding=ROUND_FLOOR)
    volume = request.grid.minimum + steps * request.grid.step
    # Defensive recheck avoids accepting a lot rounded above any constraint.
    if volume > ceiling or volume * metrics["loss_per_lot"] > min(allowances):
        return rejected("ROUNDING_RECHECK_FAILED")
    return SizingResult(True, "ACCEPTED_SIMULATED_PAPER", request, volume,
                        volume * metrics["loss_per_lot"], volume * metrics["net_profit_per_lot"],
                        net_rr, volume * metrics["margin_per_lot"], volume * metrics["notional_per_lot"])


def _qualified_research_request(request, profile, *, allow_fixture):
    from .instrument_qualification import qualify_profile
    qualification = qualify_profile(profile, allow_fixture=allow_fixture)
    if not qualification.research_ready or request.symbol != qualification.symbol:
        raise ValueError("instrument research profile is unqualified or differs from the request")
    request = _normalize_request(request, supported_symbol=qualification.symbol)
    facts = profile["terminal_observations"]
    observed = lambda name: decimal(facts[name]["value"], name)
    metrics = _request_metrics(request)
    if metrics["price_value_per_lot"] != observed("price_value_usd_per_price_unit_per_lot"):
        raise ValueError("sizing basis disagrees with qualified contract units")
    if (request.grid.minimum != observed("volume_minimum") or
        request.grid.step != observed("volume_step") or
        request.grid.maximum > observed("volume_maximum")):
        raise ValueError("sizing grid disagrees with qualified instrument grid")
    if metrics["margin_per_lot"] < metrics["notional_per_lot"] * observed("margin_fraction"):
        raise ValueError("sizing margin is below qualified product margin")
    if any(price % observed("tick_size") for price in (request.entry, request.stop, request.target)):
        raise ValueError("research prices are outside the instrument tick grid")
    return request


def research_request_metrics(request, profile, *, allow_fixture=False):
    """Qualified offline metrics; operational BTC sizing remains unchanged."""
    return _request_metrics(_qualified_research_request(request, profile, allow_fixture=allow_fixture))


def size_research_position(request, profile, *, equity, policy=None,
                           open_and_reserved_risk=0, group_risk=0,
                           current_gross_notional=0, used_margin=0,
                           day_start_equity=None, peak_equity=None, free_margin=None,
                           allow_fixture=False):
    """Use the shared grid/risk math for a separately qualified USD instrument.

    Group risk means crypto or metals for this proposed instrument. The offline
    wrapper cannot authorize a broker order or add symbols to PaperLedger.
    """
    request = _qualified_research_request(request, profile, allow_fixture=allow_fixture)
    return _size_normalized_position(request, equity=equity, policy=policy or RiskPolicy(),
        open_and_reserved_risk=open_and_reserved_risk, crypto_risk=group_risk,
        current_gross_notional=current_gross_notional, used_margin=used_margin,
        day_start_equity=day_start_equity, peak_equity=peak_equity, free_margin=free_margin)
