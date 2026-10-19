"""Explicit dated event-cost profiles shared by chronological research.

No broker calendar is inferred. Future event prices are never entry inputs;
sizing uses entry-price scenarios and ignores credits. Accrued current-interest
uses a supplied observation or the last already observed quote, with that proxy
identified in the report. The conservative accepted profile remains separate.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .research_costs import EVIDENCE_KINDS, LinearContract, SwapProfile, SwapMode, RolloverEvent, evaluate_trade_costs
from .sizing import CostProfile, decimal


def instant(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("cost times require UTC epochs or timezone-aware labels")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("cost times must specify a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class EventCostProfile:
    profile_id: str
    label: str
    contract: LinearContract
    swap: SwapProfile
    known_at: datetime
    effective_from: datetime
    effective_until: datetime
    commission_per_lot: Decimal
    slippage_per_lot: Decimal
    events: tuple[RolloverEvent, ...]
    schedule_complete: bool
    qualified: bool = False
    event_price_known_at: tuple[tuple[str, datetime], ...] = ()
    component_evidence: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self):
        if not self.profile_id or not self.label or self.contract.symbol != self.swap.symbol:
            raise ValueError("named matching contract/cost profile required")
        for name in ("known_at", "effective_from", "effective_until"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        if self.effective_from >= self.effective_until:
            raise ValueError("cost profile needs ordered effective dates")
        for name in ("commission_per_lot", "slippage_per_lot"):
            value = decimal(getattr(self, name))
            if value < 0:
                raise ValueError("commission and slippage must be nonnegative")
            object.__setattr__(self, name, value)
        if type(self.schedule_complete) is not bool or type(self.qualified) is not bool:
            raise ValueError("cost qualification/completeness require explicit booleans")
        events = tuple(sorted(self.events, key=lambda event: event.at_utc))
        if (any(e.symbol != self.contract.symbol for e in events)
                or len({e.event_id for e in events}) != len(events)
                or len({e.at_utc for e in events}) != len(events)):
            raise ValueError("rollover events must match symbol and be unique")
        if self.qualified and (self.swap.evidence_kind == "RESEARCH_ASSUMPTION"
                or any(e.evidence_kind == "RESEARCH_ASSUMPTION" for e in events)):
            raise ValueError("research assumptions cannot become qualified cost evidence")
        prices_known = tuple((identifier, instant(at)) for identifier, at in self.event_price_known_at)
        event_map = {event.event_id: event for event in events}
        if (len({identifier for identifier, _ in prices_known}) != len(prices_known)
                or any(identifier not in event_map or event_map[identifier].current_price is None
                       for identifier, _ in prices_known)):
            raise ValueError("event-price availability must identify unique supplied event prices")
        if any(at < event_map[identifier].at_utc for identifier, at in prices_known):
            raise ValueError("a current rollover price cannot be known before its event")
        if self.qualified and self.swap.mode == SwapMode.INTEREST_CURRENT and any(
                event.current_price is not None and event.event_id not in dict(prices_known) for event in events):
            raise ValueError("qualified supplied rollover prices require observation availability")
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "event_price_known_at", tuple(sorted(prices_known)))
        components = {}
        for name, evidence in self.component_evidence.items():
            if name not in ("commission", "slippage", "schedule") or not isinstance(evidence, Mapping):
                raise ValueError("cost component evidence must identify commission, slippage or schedule")
            source, kind = evidence.get("source"), evidence.get("evidence_kind")
            if not isinstance(source, str) or not source.strip() or kind not in EVIDENCE_KINDS:
                raise ValueError("cost components require named observation/specification/assumption evidence")
            known = instant(evidence.get("known_at_utc"))
            if known > self.known_at:
                raise ValueError("cost profile cannot predate its component evidence availability")
            components[name] = MappingProxyType({"source": source, "evidence_kind": kind,
                                                "known_at_utc": known.isoformat()})
        if self.qualified and (set(components) != {"commission", "slippage", "schedule"}
                or any(evidence["evidence_kind"] == "RESEARCH_ASSUMPTION" for evidence in components.values())):
            raise ValueError("qualified costs require explicit qualified commission, slippage and schedule evidence, including waived costs")
        object.__setattr__(self, "component_evidence", MappingProxyType(components))

    def check_window(self, entered, ended, *, sizing=False):
        entered, ended = instant(entered), instant(ended)
        if sizing and self.known_at > entered:
            raise ValueError("cost schedule/rates were unavailable at entry")
        if not self.schedule_complete:
            raise ValueError("rollover schedule completeness is unqualified")
        if entered < self.effective_from or ended > self.effective_until or ended < entered:
            raise ValueError("cost profile does not cover the requested holding window")
        return entered, ended


class ReplayCostModel:
    def __init__(self, profile, *, source_sha256=None):
        if not isinstance(profile, EventCostProfile):
            raise TypeError("explicit EventCostProfile required")
        self.profile = profile
        self.source_sha256 = source_sha256
        self._prices = []

    def new_run(self):
        return type(self)(self.profile, source_sha256=self.source_sha256)

    def observe_price(self, at, price):
        at, price = instant(at), decimal(price)
        if price <= 0 or (self._prices and at < self._prices[-1][0]):
            raise ValueError("positive chronological cost price observations required")
        if self._prices and at == self._prices[-1][0]:
            if price != self._prices[-1][1]:
                raise ValueError("conflicting cost prices at one observation timestamp")
            return
        self._prices.append((at, price))

    def entry_costs(self, side, entry, at, horizon_hours, *, slippage_multiplier=1):
        entered = instant(at)
        ended = entered + timedelta(seconds=float(decimal(horizon_hours) * 3600))
        self.profile.check_window(entered, ended, sizing=True)
        # Replace every future interest-current price with an explicit entry
        # scenario, even if a retrospective input contains its final price.
        events = tuple(replace(event, current_price=decimal(entry)) for event in self.profile.events)
        costs = self._evaluate(side, entry, entered, ended, Decimal(1), events, slippage_multiplier)
        financing = sum((max(line.signed_cost, Decimal(0)) for line in costs.financing), Decimal(0))
        return CostProfile(costs.commission, costs.slippage, financing,
                           self.profile.label + "; entry-price financing scenario; credits excluded from risk")

    def realised_costs(self, side, entry, entered_at, exited_at, volume, *, slippage_multiplier=1):
        entered, exited = self.profile.check_window(entered_at, exited_at)
        events, price_basis = [], {}
        rate = self.profile.swap.long_rate if side == "LONG" else self.profile.swap.short_rate
        for event in self.profile.events:
            if not entered < event.at_utc <= exited:
                continue
            price = None
            if self.profile.swap.mode == SwapMode.INTEREST_CURRENT and rate and event.multiplier:
                known_at = dict(self.profile.event_price_known_at).get(event.event_id)
                # A retrospectively collected event price cannot revalue an
                # earlier mark or a previously closed cash flow. Stream charges
                # use the price explicitly available at the event, or the same
                # prior-quote proxy throughout that trade's accounting.
                if event.current_price is not None and known_at is not None and known_at <= event.at_utc:
                    price = event.current_price
                    price_basis[event.event_id] = "SUPPLIED_EVENT_PRICE_KNOWN_AT_EVENT"
                else:
                    available = [(at, value) for at, value in self._prices if at <= event.at_utc]
                    if not available:
                        raise ValueError("current-interest event has no available price observation")
                    price = available[-1][1]
                    price_basis[event.event_id] = "LAST_OBSERVED_QUOTE_PROXY; not a measured rollover price"
            events.append(replace(event, current_price=price))
        costs = self._evaluate(side, entry, entered, exited, decimal(volume), tuple(events), slippage_multiplier)
        result = {"cash_cost": str(costs.cash_cost), "risk_cost": str(costs.risk_cost),
                  "commission": str(costs.commission), "slippage": str(costs.slippage),
                  "financing": [{"event_id": line.event_id, "at_utc": line.at_utc.isoformat(),
                       "signed_cost": str(line.signed_cost), "price_basis": price_basis.get(line.event_id),
                       "supplied_price_known_at_utc": dict(self.profile.event_price_known_at).get(line.event_id).isoformat()
                           if line.event_id in dict(self.profile.event_price_known_at) else None,
                       "evidence_kind": line.evidence_kind} for line in costs.financing],
                  "qualified_cost_profile": self.profile.qualified,
                  "financing_prices_qualified": all("PROXY" not in basis for basis in price_basis.values()),
                  "qualified_realised_costs": self.profile.qualified and all("PROXY" not in basis for basis in price_basis.values())}
        return result

    def _evaluate(self, side, entry, entered, exited, volume, events, slippage_multiplier):
        return evaluate_trade_costs(contract=self.profile.contract, profile=self.profile.swap,
            side=side, volume=volume, open_price=entry, opened_at_utc=entered,
            closed_at_utc=exited, events=events,
            commission_round_trip_per_lot=self.profile.commission_per_lot,
            slippage_round_trip_per_lot=self.profile.slippage_per_lot * decimal(slippage_multiplier))

    def methodology(self):
        return {"model": "EVENT_BASED_PRODUCT_PROFILE", "profile_id": self.profile.profile_id,
                "label": self.profile.label, "source_sha256": self.source_sha256,
                "qualified": self.profile.qualified,
                "qualification_scope": "Rates, events and explicit commission/slippage/schedule evidence; proxy rollover prices separately unqualified",
                "component_evidence": {name: dict(evidence) for name, evidence in self.profile.component_evidence.items()},
                "unqualified_components": [name for name in ("commission", "slippage", "schedule")
                    if name not in self.profile.component_evidence or
                    self.profile.component_evidence[name]["evidence_kind"] == "RESEARCH_ASSUMPTION"],
                "interval_convention": "entry < rollover <= exit",
                "entry_financing": "Known schedule; entry-price scenario; debit-only risk allowance",
                "realised_financing": "Crossed events only; supplied prices explicitly known at event or fixed prior-quote proxy",
                "spread": "In executable prices; never a second cash charge"}


def load_cost_model(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 1 or data.get("model") != "EVENT_BASED_PRODUCT_PROFILE":
        raise ValueError("explicit versioned event-cost profile required")
    contract = LinearContract(**data["contract"])
    swap = SwapProfile(symbol=contract.symbol, **data["swap"])
    events = tuple(RolloverEvent(symbol=contract.symbol, at_utc=instant(item["at_utc"]),
        event_id=item["event_id"], multiplier=item["multiplier"], source=item["source"],
        evidence_kind=item["evidence_kind"], current_price=item.get("current_price")) for item in data["events"])
    profile = EventCostProfile(data["profile_id"], data["label"], contract, swap,
        instant(data["known_at_utc"]), instant(data["effective_from_utc"]), instant(data["effective_until_utc"]),
        data["commission_round_trip_per_lot"], data["slippage_round_trip_per_lot"], events,
        data["schedule_complete"], data.get("qualified", False),
        tuple((item["event_id"], instant(item["current_price_known_at_utc"]))
              for item in data["events"] if item.get("current_price_known_at_utc") is not None),
        data.get("component_evidence", {}))
    return ReplayCostModel(profile, source_sha256=hashlib.sha256(raw).hexdigest())
