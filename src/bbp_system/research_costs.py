"""Offline, explicit-event cost accounting for linear research contracts.

This module is deliberately separate from the accepted paper sizing/ledger.
It neither discovers broker schedules nor connects to MT5. The caller supplies
versioned rates, actual/scenario UTC events and their evidence. Negative broker
swap rates are debits; positive rates are credits. Returned cash costs use the
opposite sign: positive is a charge and negative a credit.

Interest-current prices observed after entry are outcome data. They may be used
to reconcile a completed trade, but not to size that trade at its entry. Entry
risk scenarios must use prices and schedules explicitly assumed/known then.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import IntEnum

from .sizing import decimal


class SwapMode(IntEnum):
    DISABLED = 0
    CURRENCY_DEPOSIT = 4
    INTEREST_CURRENT = 5
    INTEREST_OPEN = 6


EVIDENCE_KINDS = frozenset({
    "BROKER_OBSERVATION", "OFFICIAL_SPECIFICATION", "RESEARCH_ASSUMPTION",
})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _positive(value, name):
    value = decimal(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative(value, name):
    value = decimal(value, name)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _utc(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _evidence(source, kind):
    _text(source, "evidence source")
    if kind not in EVIDENCE_KINDS:
        raise ValueError("evidence kind must distinguish observation/specification/assumption")


@dataclass(frozen=True)
class LinearContract:
    symbol: str
    units_per_lot: Decimal
    unit_name: str
    price_currency: str
    account_currency: str

    def __post_init__(self):
        for name in ("symbol", "unit_name", "price_currency", "account_currency"):
            _text(getattr(self, name), name)
        if self.price_currency != self.account_currency:
            raise ValueError("cross-currency contracts require a separate explicit FX conversion model")
        object.__setattr__(self, "units_per_lot", _positive(self.units_per_lot, "units per lot"))


@dataclass(frozen=True)
class SwapProfile:
    symbol: str
    mode: SwapMode
    long_rate: Decimal
    short_rate: Decimal
    source: str
    evidence_kind: str
    bank_days: int = 360

    def __post_init__(self):
        _text(self.symbol, "symbol")
        if isinstance(self.mode, bool):
            raise ValueError("swap mode must be an explicit supported mode")
        try:
            object.__setattr__(self, "mode", SwapMode(self.mode))
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported swap mode; points/currency conversion/reopening are not inferred") from error
        for name in ("long_rate", "short_rate"):
            object.__setattr__(self, name, decimal(getattr(self, name), name))
        _evidence(self.source, self.evidence_kind)
        if type(self.bank_days) is not int or self.bank_days != 360:
            raise ValueError("supported MetaQuotes annual-interest modes use a 360-day bank year")
        if self.mode == SwapMode.DISABLED and (self.long_rate != 0 or self.short_rate != 0):
            raise ValueError("disabled swap profile must have zero rates")


@dataclass(frozen=True)
class RolloverEvent:
    event_id: str
    symbol: str
    at_utc: datetime
    multiplier: Decimal
    source: str
    evidence_kind: str
    current_price: Decimal | None = None

    def __post_init__(self):
        _text(self.event_id, "event id")
        _text(self.symbol, "symbol")
        object.__setattr__(self, "at_utc", _utc(self.at_utc, "rollover time"))
        object.__setattr__(self, "multiplier", _nonnegative(self.multiplier, "daily swap multiplier"))
        if self.current_price is not None:
            object.__setattr__(self, "current_price", _positive(self.current_price, "rollover price"))
        _evidence(self.source, self.evidence_kind)


@dataclass(frozen=True)
class FinancingLine:
    event_id: str
    at_utc: datetime
    signed_cost: Decimal
    multiplier: Decimal
    price_basis: Decimal | None
    evidence_kind: str
    source: str


@dataclass(frozen=True)
class TradeCosts:
    currency: str
    symbol: str
    side: str
    volume: Decimal
    swap_profile: SwapProfile
    commission: Decimal
    slippage: Decimal
    financing: tuple[FinancingLine, ...]

    @property
    def cash_cost(self):
        """Signed total to subtract once from quote-side gross P/L."""
        return self.commission + self.slippage + sum(
            (line.signed_cost for line in self.financing), Decimal(0),
        )

    @property
    def risk_cost(self):
        """Charges only: anticipated financing credits never enlarge a risk budget."""
        return self.commission + self.slippage + sum(
            (max(line.signed_cost, Decimal(0)) for line in self.financing), Decimal(0),
        )

    def net_pnl(self, gross_quote_side_pnl):
        return decimal(gross_quote_side_pnl, "gross P/L") - self.cash_cost

    def stop_loss_with_charges(self, gross_stop_loss):
        return _nonnegative(gross_stop_loss, "gross stop loss") + self.risk_cost


def evaluate_trade_costs(*, contract, profile, side, volume, open_price,
                         opened_at_utc, closed_at_utc, events,
                         commission_round_trip_per_lot, slippage_round_trip_per_lot):
    """Evaluate supplied rollovers while a position is open: entry < event <= exit.

    The endpoint convention models entry after, and exit after, the event at an
    identical timestamp. This is an explicit research convention, not broker
    sequencing evidence. Duplicate event IDs or instants fail closed. Source
    completeness is the caller's responsibility; no events is not proof of zero
    financing. Commission and financial slippage are round-trip amounts charged
    once, independently of event count. Spread belongs in quote-side prices.
    """
    if contract.symbol != profile.symbol:
        raise ValueError("contract and swap profile symbols differ")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    volume = _positive(volume, "volume")
    open_price = _positive(open_price, "open price")
    opened = _utc(opened_at_utc, "entry time")
    closed = _utc(closed_at_utc, "exit time")
    if closed < opened:
        raise ValueError("exit must not precede entry")
    commission = _nonnegative(commission_round_trip_per_lot, "commission per lot") * volume
    slippage = _nonnegative(slippage_round_trip_per_lot, "slippage per lot") * volume
    rate = profile.long_rate if side == "LONG" else profile.short_rate
    lines, seen_ids, seen_instants = [], set(), set()
    for event in sorted(events, key=lambda item: item.at_utc):
        if event.symbol != contract.symbol:
            raise ValueError("rollover event symbol differs from contract")
        if event.event_id in seen_ids or event.at_utc in seen_instants:
            raise ValueError("duplicate rollover event id or instant")
        seen_ids.add(event.event_id)
        seen_instants.add(event.at_utc)
        if not opened < event.at_utc <= closed:
            continue
        basis = None
        if profile.mode == SwapMode.DISABLED or rate == 0 or event.multiplier == 0:
            cost = Decimal(0)
        elif profile.mode == SwapMode.CURRENCY_DEPOSIT:
            cost = -rate * volume * event.multiplier
        else:
            basis = open_price if profile.mode == SwapMode.INTEREST_OPEN else event.current_price
            if basis is None:
                raise ValueError("current-interest rollover requires an explicit event price")
            cost = (-rate / Decimal(100) / profile.bank_days * basis
                    * contract.units_per_lot * volume * event.multiplier)
        lines.append(FinancingLine(event.event_id, event.at_utc, cost, event.multiplier,
                                   basis, event.evidence_kind, event.source))
    return TradeCosts(contract.account_currency, contract.symbol, side, volume,
                      profile, commission, slippage, tuple(lines))


@dataclass(frozen=True)
class MarginEvidence:
    symbol: str
    currency: str
    amount_per_lot: Decimal
    as_of_utc: datetime
    source: str
    evidence_kind: str

    def __post_init__(self):
        _text(self.symbol, "symbol")
        _text(self.currency, "margin currency")
        object.__setattr__(self, "amount_per_lot", _nonnegative(self.amount_per_lot, "margin per lot"))
        object.__setattr__(self, "as_of_utc", _utc(self.as_of_utc, "margin evidence time"))
        _evidence(self.source, self.evidence_kind)


@dataclass(frozen=True)
class MarginAssessment:
    currency: str
    amount: Decimal
    binding: MarginEvidence
    evidence: tuple[MarginEvidence, ...]


def conservative_margin(*, contract, volume, at_utc, evidence):
    """Retain every explicitly supplied estimate and use the greatest requirement.

    A specification floor converted to cash at a scenario price is an explicit
    research estimate; it must carry that provenance. Account-wide leverage is
    not accepted as a substitute for product evidence. Historical effective
    dates/expiry remain a separate qualification task for the caller.
    """
    volume, at = _positive(volume, "volume"), _utc(at_utc, "margin evaluation time")
    estimates = tuple(evidence)
    if not estimates:
        raise ValueError("at least one sourced margin estimate is required")
    for item in estimates:
        if item.symbol != contract.symbol or item.currency != contract.account_currency:
            raise ValueError("margin evidence must match the symbol and account currency")
        if item.as_of_utc > at:
            raise ValueError("margin evidence must be available by the evaluation time")
    binding = max(estimates, key=lambda item: item.amount_per_lot)
    if binding.amount_per_lot <= 0:
        raise ValueError("positive sourced margin is required; zero metadata is not a qualified exemption")
    return MarginAssessment(contract.account_currency, binding.amount_per_lot * volume,
                            binding, estimates)
