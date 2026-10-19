"""Standalone causal level observations; never used by baseline trade admission.

A pivot is visible only after its explicit confirmation availability. A touch
means range/zone intersection, a break means a close through the far zone edge,
and reclaim means a later close back beyond the original edge. ``flipped`` is
an OHLC observation of a later opposite-side retest, not proof of support.
Only completed, available bars opening at/after the level's knowledge time
are evidence. No missing candle or intrabar sequence is invented.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from numbers import Real
from typing import Literal

from .indicators import ConfirmedPivot


LevelState = Literal["fresh", "tested", "broken", "reclaimed", "flipped"]


@dataclass(frozen=True)
class LevelEvent:
    kind: Literal["touch", "break", "reclaim", "opposite_side_retest"]
    bar_time: int
    known_at: float
    close: float


@dataclass(frozen=True)
class LevelObservation:
    identity: str
    timeframe: str
    kind: str
    original_role: str
    price: float
    origin_time: int
    confirmed_bar_time: int
    known_at: float
    as_of: float
    age_hours: float
    zone_low: float
    zone_high: float
    state: LevelState
    touch_bars: int
    break_count: int
    reclaim_count: int
    opposite_side_retest_count: int
    events: tuple[LevelEvent, ...]
    first_observed_bar_time: int | None
    last_known_bar_time: int | None
    last_known_bar_available_at: float | None
    history_starts_after_confirmation: bool
    missing_nominal_intervals: int | None
    availability_basis: str


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _seconds(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError("bar_seconds must be a positive integer")
    return value


def _known_bars(bars, as_of, bar_seconds):
    """Validate visible prices only; unavailable future prices cannot affect us."""
    visible = []
    assumed = False
    for record in bars:
        if not isinstance(record, Mapping):
            raise ValueError("bars must be OHLC mappings")
        opening = _finite(record["time"], "bar time")
        if not opening.is_integer():
            raise ValueError("bar time must be integer epoch seconds")
        arrival = record.get("available_at")
        if arrival is None:
            if bar_seconds is None:
                raise ValueError("explicit available_at or bar_seconds required")
            arrival = opening + bar_seconds
            basis_assumed = True
        else:
            arrival = _finite(arrival, "bar available_at")
            basis_assumed = False
        if arrival <= opening or (bar_seconds is not None and arrival < opening + bar_seconds):
            raise ValueError("availability must follow the bar opening/nominal close")
        if arrival > as_of:
            continue
        declared_basis = str(record.get("availability_basis", "")).lower()
        assumed = assumed or basis_assumed or "assumed" in declared_basis or "nominal" in declared_basis
        if visible and (opening <= visible[-1]["time"] or arrival < visible[-1]["available_at"]):
            raise ValueError("visible bars must have increasing labels and ordered arrivals")
        prices = {key: _finite(record[key], f"bar {key}") for key in ("open", "high", "low", "close")}
        if min(prices.values()) <= 0 or not (
            prices["low"] <= prices["open"] <= prices["high"]
            and prices["low"] <= prices["close"] <= prices["high"]
        ):
            raise ValueError("bar violates positive OHLC bounds")
        visible.append({"time": int(opening), "available_at": arrival, **prices})
    return visible, assumed


def inspect_level(
    pivot: ConfirmedPivot,
    *,
    timeframe: str,
    bars: Sequence[Mapping[str, object]],
    as_of: float,
    tick_size: float,
    bar_seconds: int | None = None,
    zone_ticks: float = 1.0,
    atr_at_confirmation: float | None = None,
    atr_known_at: float | None = None,
    zone_atr_fraction: float = 0.0,
) -> LevelObservation | None:
    """Inspect one pivot without changing the shared strategy's target policy.

    Zone half-width is frozen at max(tick_size*zone_ticks, confirmation ATR*
    zone_atr_fraction). ATR availability must be supplied and no later than
    pivot knowledge. Missing arrival metadata requires an explicit nominal
    duration, which is recorded as an assumption, not measured delivery.

    A flip requires a *later* bar opening and closing on the broken side while
    its range intersects the zone. A break/reclaim candle cannot also establish
    a flip. Status summarizes the last transition; event history stays intact.
    ``fresh`` means no test in the supplied post-confirmation history. Coverage
    flags must accompany it, particularly with a truncated rolling window.
    """
    if not isinstance(pivot, ConfirmedPivot) or pivot.kind not in ("high", "low"):
        raise ValueError("pivot must be a ConfirmedPivot with high/low kind")
    if not isinstance(timeframe, str) or not timeframe:
        raise ValueError("timeframe must be a nonempty provenance label")
    bar_seconds = _seconds(bar_seconds)
    cutoff = _finite(as_of, "as_of")
    if pivot.available_at is None:
        raise ValueError("pivot confirmation availability must be explicit")
    known_at = _finite(pivot.available_at, "pivot available_at")
    if known_at <= pivot.confirmed_bar_time or pivot.origin_time > pivot.confirmed_bar_time:
        raise ValueError("invalid pivot confirmation chronology")
    if known_at > cutoff:
        return None
    price = _finite(pivot.price, "pivot price")
    tick = _finite(tick_size, "tick_size")
    ticks = _finite(zone_ticks, "zone_ticks")
    fraction = _finite(zone_atr_fraction, "zone_atr_fraction")
    if price <= 0 or tick <= 0 or ticks < 0 or fraction < 0:
        raise ValueError("price/tick must be positive and zone widths nonnegative")
    atr = 0.0
    if atr_at_confirmation is not None:
        atr = _finite(atr_at_confirmation, "atr_at_confirmation")
        if atr < 0 or atr_known_at is None or _finite(atr_known_at, "atr_known_at") > known_at:
            raise ValueError("ATR must be nonnegative and known at pivot confirmation")
    elif fraction > 0 or atr_known_at is not None:
        raise ValueError("ATR zone requires an ATR value and its availability")
    width = max(tick * ticks, atr * fraction)
    if width >= price:
        raise ValueError("zone must retain positive prices")
    lower, upper = price - width, price + width
    visible, assumed = _known_bars(bars, cutoff, bar_seconds)
    evidence = [bar for bar in visible if bar["time"] >= known_at]
    state: LevelState = "fresh"
    events: list[LevelEvent] = []
    touches = breaks = reclaims = flips = 0
    for bar in evidence:
        touch = bar["low"] <= upper and bar["high"] >= lower
        broken_side = bar["close"] > upper if pivot.kind == "high" else bar["close"] < lower
        original_side = bar["close"] < lower if pivot.kind == "high" else bar["close"] > upper
        opens_broken_side = bar["open"] > upper if pivot.kind == "high" else bar["open"] < lower
        if touch:
            touches += 1
            events.append(LevelEvent("touch", bar["time"], bar["available_at"], bar["close"]))
        if state in ("fresh", "tested", "reclaimed") and broken_side:
            state = "broken"
            breaks += 1
            events.append(LevelEvent("break", bar["time"], bar["available_at"], bar["close"]))
        elif state in ("broken", "flipped") and original_side:
            state = "reclaimed"
            reclaims += 1
            events.append(LevelEvent("reclaim", bar["time"], bar["available_at"], bar["close"]))
        elif state in ("broken", "flipped") and touch and broken_side and opens_broken_side:
            state = "flipped"
            flips += 1
            events.append(LevelEvent("opposite_side_retest", bar["time"], bar["available_at"], bar["close"]))
        elif state == "fresh" and touch:
            state = "tested"
    missing = None
    if bar_seconds is not None:
        missing = sum(max(0, (right["time"] - left["time"]) // bar_seconds - 1)
                      for left, right in zip(evidence, evidence[1:]))
    return LevelObservation(
        identity=f"{timeframe}:{pivot.kind}:{pivot.origin_time}:{pivot.confirmed_bar_time}:{known_at!r}:{price!r}",
        timeframe=timeframe, kind=pivot.kind,
        original_role="resistance" if pivot.kind == "high" else "support",
        price=price, origin_time=pivot.origin_time,
        confirmed_bar_time=pivot.confirmed_bar_time, known_at=known_at,
        as_of=cutoff, age_hours=(cutoff-known_at)/3600,
        zone_low=lower, zone_high=upper, state=state,
        touch_bars=touches, break_count=breaks, reclaim_count=reclaims,
        opposite_side_retest_count=flips, events=tuple(events),
        first_observed_bar_time=evidence[0]["time"] if evidence else None,
        last_known_bar_time=visible[-1]["time"] if visible else None,
        last_known_bar_available_at=visible[-1]["available_at"] if visible else None,
        history_starts_after_confirmation=not visible or visible[0]["time"] > known_at,
        missing_nominal_intervals=missing,
        availability_basis="assumed nominal close for some bars" if assumed else "explicit bar availability",
    )


def local_pivot_prominence(
    pivot: ConfirmedPivot, *, bars: Sequence[Mapping[str, object]],
    left: int = 2, right: int = 2, bar_seconds: int | None = None,
) -> float | None:
    """Minimum clearance versus neighbors available at confirmation, in price.

    This local metric is not full-market topographic prominence. Return None
    if the complete declared neighborhood is absent, instead of treating a
    truncated history as strong evidence. Future neighbors are excluded.
    """
    if not isinstance(pivot, ConfirmedPivot) or pivot.available_at is None:
        raise ValueError("explicit confirmed pivot required")
    if any(type(value) is not int or value <= 0 for value in (left, right)):
        raise ValueError("left/right must be positive integers")
    visible, _ = _known_bars(bars, _finite(pivot.available_at, "pivot available_at"), _seconds(bar_seconds))
    origin = next((index for index, bar in enumerate(visible) if bar["time"] == pivot.origin_time), None)
    if origin is None or origin < left or origin + right >= len(visible):
        return None
    key = "high" if pivot.kind == "high" else "low"
    if pivot.kind not in ("high", "low") or visible[origin][key] != pivot.price:
        raise ValueError("pivot does not match its origin bar")
    neighbors = visible[origin-left:origin] + visible[origin+1:origin+right+1]
    neighborhood = visible[origin-left:origin+right+1]
    if bar_seconds is not None and any(
        after["time"] - before["time"] != bar_seconds
        for before, after in zip(neighborhood, neighborhood[1:])
    ):
        return None
    return (pivot.price - max(bar["high"] for bar in neighbors) if pivot.kind == "high"
            else min(bar["low"] for bar in neighbors) - pivot.price)
