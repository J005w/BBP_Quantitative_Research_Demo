"""Causal trend_continuation_v1 research setups, shared by replay and scanning.

Prices are prospective reference levels, not orders or risk-qualified entries.
Net reward/risk, executable quotes and broker permission belong to sizing. Bar
availability defaults to nominal close only when no arrival metadata is given;
that assumption does not establish historical delivery or indicator parity.
"""

from __future__ import annotations

from collections.abc import Mapping
from bisect import bisect_right
import copy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import math
from numbers import Real

from .indicators import (IndicatorPoint, ConfirmedPivot, compute_indicators,
                         confirmed_pivots, _bars, _indicators_from_validated,
                         _pivots_from_validated)
from .indicators import CALCULATION_VERSION
from .research_parameters import effective_parameters


VERSION = "trend_continuation_v1"
PERIODS = {"H4": 14400, "H1": 3600, "M15": 900, "M5": 300}
TERMINAL_STATES = {"EXPIRED", "CANCELLED"}


@dataclass(frozen=True)
class FrameFeatures:
    """Same-candle features from the shared indicator core (or test fixtures)."""
    bars: tuple
    indicators: tuple
    pivots: tuple


class _PreparedView(dict):
    """A validated cache prefix; internal metadata never alters rule inputs."""
    def __init__(self, cache, frames, as_of):
        super().__init__(frames)
        self._cache, self._as_of = cache, as_of


class PreparedFrames:
    """Validated immutable-history snapshot for efficient causal research.

    Arithmetic is still the shared indicator core. Only completed prefixes are
    exposed to the evaluator; future indicator and pivot values stay hidden.
    Delayed/out-of-order predecessor arrivals are rejected during preparation.
    Build this once per dataset and settings, rather than recomputing indicators
    on each historical decision. It does not qualify assumed bar availability.
    """

    def __init__(self, frames, *, settings=None):
        self._settings = settings
        arrivals = [
            _availability(bar, seconds)
            for tf, seconds in PERIODS.items()
            for bar in (frames[tf].bars if isinstance(frames[tf], FrameFeatures)
                        else frames[tf].get("bars", []))
        ]
        validated = prepare_frames(frames, decision_time=max(arrivals, default=0), settings=settings)
        # Shared core dataclasses are frozen scalar records. Snapshot only
        # mutable supplied fixtures; bars were already copied by validation.
        self._frames = {
            tf: FrameFeatures(frame.bars,
                tuple(point if isinstance(point, IndicatorPoint) else copy.deepcopy(point)
                      for point in frame.indicators),
                tuple(pivot if isinstance(pivot, ConfirmedPivot) else copy.deepcopy(pivot)
                      for pivot in frame.pivots))
            for tf, frame in validated.items()
        }
        self._times = {
            tf: tuple(bar["available_at"] for bar in frame.bars)
            for tf, frame in self._frames.items()
        }
        self._pivot_times = {}
        self._point_index = {
            tf: {_get(point, "time"): point for point in frame.indicators}
            for tf, frame in self._frames.items()
        }
        for tf, frame in self._frames.items():
            times = tuple(_get(pivot, "available_at") for pivot in frame.pivots)
            # Native shared pivots are naturally ordered by confirmation. Keep
            # supplied fixture order intact if it is intentionally different.
            self._pivot_times[tf] = times if all(a <= b for a, b in zip(times, times[1:])) else None

    def at(self, decision_time):
        as_of = _finite(decision_time, "decision_time")
        output = {}
        for tf, frame in self._frames.items():
            count = bisect_right(self._times[tf], as_of)
            pivot_times = self._pivot_times[tf]
            pivots = (frame.pivots[:bisect_right(pivot_times, as_of)] if pivot_times is not None
                      else tuple(pivot for pivot in frame.pivots if _get(pivot, "available_at") <= as_of))
            output[tf] = FrameFeatures(
                frame.bars[:count], frame.indicators[:count], pivots)
        return _PreparedView(self, output, as_of)

    def full_history(self):
        """Read-only source history for iteration/coverage, not a rule view."""
        return dict(self._frames)


class RollingFrames(PreparedFrames):
    """Exact finite-window snapshots using the shared calculation conventions.

    Each timeframe's last ``window`` available candles is calculated afresh
    when its completed prefix changes. This reproduces the live capture's
    indicator seed and available pivot universe, rather than inheriting ancient
    levels or a longer EMA/ATR seed. Historical views retain the same window
    rule and hide future candles; availability remains the adapter's assumption.
    """

    def __init__(self, frames, *, window=499, settings=None):
        if type(window) is not int or window < 1:
            raise ValueError("rolling window must be a positive integer")
        super().__init__(frames, settings=settings)
        self.window = window
        self._rolling = {tf: {} for tf in PERIODS}
        self._validated_bars = {tf: tuple(_bars(frame.bars)) for tf, frame in self._frames.items()}

    def at(self, decision_time):
        as_of = _finite(decision_time, "decision_time")
        output = {}
        options = {} if self._settings is None else {
            "bbp_length": self._settings.bbp_ema_length, "di_length": self._settings.di_length,
            "adx_smoothing": self._settings.adx_smoothing, "atr_length": self._settings.atr_length,
        }
        left = 2 if self._settings is None else self._settings.pivot_left
        right = 2 if self._settings is None else self._settings.pivot_right
        for tf, full in self._frames.items():
            count = bisect_right(self._times[tf], as_of)
            # A chronological run reuses H4/H1/M15 snapshots across many M5
            # decisions. Retain recent views also used for candidate recovery.
            cached = self._rolling[tf]
            if count not in cached:
                bars = full.bars[max(0, count-self.window):count]
                validated = self._validated_bars[tf][max(0, count-self.window):count]
                cached[count] = FrameFeatures(bars, tuple(_indicators_from_validated(validated, **options)),
                                              tuple(_pivots_from_validated(validated, left=left, right=right)))
                if len(cached) > 64:
                    # Older views can be recomputed exactly; keep memory bounded.
                    del cached[next(iter(cached))]
            output[tf] = cached[count]
        return _PreparedView(self, output, as_of)

    def indicator_at(self, timeframe, opening, as_of):
        # The finite-window seed differs by snapshot. Never read the full-
        # history point index for a rolling view.
        frame = self.at(as_of)[timeframe]
        labels = tuple(bar["time"] for bar in frame.bars)
        index = bisect_right(labels, opening)-1
        return frame.indicators[index] if index >= 0 and labels[index] == opening else None


def _get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    if isinstance(value, (IndicatorPoint, ConfirmedPivot)):
        return getattr(value, key, default)
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _availability(bar, seconds):
    label = _finite(bar["time"], "bar time")
    if not label.is_integer():
        raise ValueError("bar labels must be integer UTC seconds")
    value = bar.get("available_at")
    if value is None and bar.get("available_at_utc"):
        stamp = datetime.fromisoformat(bar["available_at_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("availability requires a timezone")
        value = stamp.timestamp()
    available = label + seconds if value is None else _finite(value, "availability")
    if available < label + seconds:
        raise ValueError("availability precedes nominal candle close")
    return available


def prepare_frames(frames, *, decision_time, settings=None):
    """Validate/filter closed prefixes before computing shared causal features."""
    as_of = _finite(decision_time, "decision_time")
    if isinstance(frames, PreparedFrames):
        if settings is not None and settings != frames._settings:
            raise ValueError("Prepared history belongs to different indicator settings")
        return frames.at(as_of)
    output = {}
    for timeframe, seconds in PERIODS.items():
        source = frames.get(timeframe)
        if source is None:
            raise ValueError(f"Missing {timeframe} data")
        raw_bars = source.bars if isinstance(source, FrameFeatures) else source.get("bars", [])
        if not isinstance(source, FrameFeatures) and source.get("errors"):
            raise ValueError(f"Invalid {timeframe} data")
        bars = []
        pending_closed_labels = []
        for original in raw_bars:
            availability = _availability(original, seconds)
            if availability <= as_of:
                bars.append(dict(original, available_at=availability))
            elif original["time"] + seconds <= as_of:
                pending_closed_labels.append(original["time"])
        if bars and any(label < bars[-1]["time"] for label in pending_closed_labels):
            raise ValueError("An unavailable predecessor would contaminate dependent features")
        if any(
            right["time"] <= left["time"] or right["available_at"] < left["available_at"]
            for left, right in zip(bars, bars[1:])
        ):
            # A later point's calculation cannot inherit an unseen earlier bar.
            # Out-of-order arrivals require reconciliation before rule evaluation.
            raise ValueError("Candle labels/availability require ordered reconciliation")
        if isinstance(source, FrameFeatures):
            labels = {bar["time"]: bar["available_at"] for bar in bars}
            points = [point for point in source.indicators if _get(point, "time") in labels]
            if len(points) != len(bars) or any(
                _get(point, "time") != bar["time"]
                or _get(point, "available_at") != bar["available_at"]
                for point, bar in zip(points, bars)
            ):
                raise ValueError("Precomputed features must match candle availability")
            pivots = [
                pivot for pivot in source.pivots
                if _get(pivot, "available_at") is not None and _get(pivot, "available_at") <= as_of
                and _get(pivot, "confirmed_bar_time") in labels
                and _get(pivot, "available_at") == labels[_get(pivot, "confirmed_bar_time")]
            ]
        else:
            options = {} if settings is None else {
                "bbp_length": settings.bbp_ema_length, "di_length": settings.di_length,
                "adx_smoothing": settings.adx_smoothing, "atr_length": settings.atr_length,
            }
            points = compute_indicators(bars, **options)
            pivots = confirmed_pivots(
                bars, left=2 if settings is None else settings.pivot_left,
                right=2 if settings is None else settings.pivot_right,
            )
        output[timeframe] = FrameFeatures(tuple(bars), tuple(points), tuple(pivots))
    return output


def _at(frames, as_of):
    if isinstance(frames, _PreparedView):
        return frames._cache.at(min(as_of, frames._as_of))
    return {
        key: FrameFeatures(
            tuple(bar for bar in frame.bars if bar["available_at"] <= as_of),
            tuple(point for point in frame.indicators if _get(point, "available_at") <= as_of),
            tuple(pivot for pivot in frame.pivots if _get(pivot, "available_at") <= as_of),
        ) for key, frame in frames.items()
    }


def _indicator_at(frames, timeframe, opening):
    if isinstance(frames, _PreparedView):
        if isinstance(frames._cache, RollingFrames):
            return frames._cache.indicator_at(timeframe, opening, frames._as_of)
        point = frames._cache._point_index[timeframe].get(opening)
        return point if point is not None and _get(point, "available_at") <= frames._as_of else None
    return next((point for point in frames[timeframe].indicators
                 if _get(point, "time") == opening), None)


def _pivots(frame, kind):
    return sorted(
        (pivot for pivot in frame.pivots if _get(pivot, "kind") == kind),
        key=lambda pivot: (_get(pivot, "origin_time"), _get(pivot, "available_at")),
    )


def _point(frame):
    return frame.indicators[-1] if frame.indicators else None


def _direction(point, side):
    if point is None or any(_get(point, name) is None for name in ("bbp", "plus_di", "minus_di")):
        return False
    bbp, plus, minus = (_get(point, name) for name in ("bbp", "plus_di", "minus_di"))
    return bbp > 0 and plus > minus if side == "LONG" else bbp < 0 and minus > plus


def _context(frames, parameters):
    h4, h1 = frames["H4"], frames["H1"]
    point = _point(h4)
    highs, lows = _pivots(h4, "high"), _pivots(h4, "low")
    context = {"regime": "UNCLASSIFIED", "direction": None, "bias": "UNCLASSIFIED"}
    if point is None or any(_get(point, name) is None for name in ("bbp", "plus_di", "minus_di", "adx")) or len(highs) < 2 or len(lows) < 2:
        return context
    rising = _get(highs[-1], "price") > _get(highs[-2], "price") and _get(lows[-1], "price") > _get(lows[-2], "price")
    falling = _get(highs[-1], "price") < _get(highs[-2], "price") and _get(lows[-1], "price") < _get(lows[-2], "price")
    side = "LONG" if rising and _direction(point, "LONG") else "SHORT" if falling and _direction(point, "SHORT") else None
    if side is not None and _get(point, "adx") >= parameters.h4_adx_minimum:
        context.update(regime="TREND", direction=side)
    else:
        context["regime"] = "RANGE" if _get(point, "adx") < 20 and not rising and not falling else "TRANSITION"
        return context
    known = _pivots(h1, "low" if side == "LONG" else "high")
    h1_point = _point(h1)
    if h1_point is None or not known or any(_get(h1_point, name) is None for name in ("bbp", "plus_di", "minus_di")):
        return context
    level = _get(known[-1], "price")
    relevant = [bar for bar in h1.bars if bar["time"] > _get(known[-1], "origin_time")]
    intact = all(bar["close"] >= level if side == "LONG" else bar["close"] <= level for bar in relevant)
    context["bias"] = side if _direction(h1_point, side) and intact else "CONFLICT"
    return context


def _round(price, tick, upward):
    value = Decimal(str(price)) / Decimal(str(tick))
    return float(value.to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR) * Decimal(str(tick)))


def _target(prepared, side, entry, tick):
    kind = "high" if side == "LONG" else "low"
    levels = [_get(pivot, "price") for tf in ("H1", "H4") for pivot in _pivots(prepared[tf], kind)]
    eligible = [level for level in levels if level > entry] if side == "LONG" else [level for level in levels if level < entry]
    if not eligible:
        return None
    # Do not skip a nearby level to manufacture a larger reward/risk ratio.
    level = min(eligible) if side == "LONG" else max(eligible)
    raw_target = Decimal(str(level)) + (-Decimal(str(tick)) if side == "LONG" else Decimal(str(tick)))
    target = _round(raw_target, tick, side == "SHORT")
    return target if (target > entry if side == "LONG" else target < entry) else None


def nearest_target(frames, *, decision_time, side, entry, tick_size, settings=None):
    """Select only H1/H4 levels known at executable-entry assessment time."""
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    tick = _finite(tick_size, "tick_size")
    price = _finite(entry, "entry")
    if tick <= 0 or price <= 0:
        raise ValueError("tick_size and entry must be positive")
    return _target(prepare_frames(frames, decision_time=decision_time, settings=settings), side, price, tick)


def _reason(code, message):
    return {"code": code, "message": message}


def _finish(state, status, code, message, at):
    finished = list(state.get("finished_setup_ids", []))
    if status in TERMINAL_STATES and state.get("setup_id") and state["setup_id"] not in finished:
        finished.append(state["setup_id"])
    return dict(state, state=status, decision_time=at, reasons=[_reason(code, message)], finished_setup_ids=finished)


def _blank(symbol, decision_time, context, parameters):
    return {
        "strategy_version": VERSION, "state": "WATCHING", "symbol": symbol,
        "calculation_version": CALCULATION_VERSION,
        "setup_id": None, "signal_id": None, "side": None, "decision_time": decision_time,
        "parameters": parameters.as_dict(), "parameter_sha256": parameters.sha256,
        "candidate_at": None, "confirmation_at": None, "review_expires_at": None,
        "pullback_onset": None, "recovery_bar_time": None, "invalidation": None,
        "trigger": None, "stop": None, "target": None, "reference_entry": None,
        "confirmation_bars_elapsed": 0, "context": context, "reasons": [],
        "risk_status": "NOT_ASSESSED", "net_rr": None, "orders_enabled": False,
        "finished_setup_ids": [],
        "availability_basis": "Provided availability, otherwise assumed nominal candle close",
    }


def _create_candidate(frames, decision_time, tick, symbol, previous, parameters):
    m15 = frames["M15"]
    lookback = parameters.m15_pullback_lookback
    if len(m15.bars) < lookback:
        return None, _reason("INSUFFICIENT_WARMUP", f"{lookback} completed M15 indicator observations are required.")
    recovery = m15.bars[-1]
    candidate_at = recovery["available_at"]
    creation_frames = _at(frames, candidate_at)
    context = _context(creation_frames, parameters)
    if context["regime"] != "TREND" or context["bias"] != context["direction"]:
        return None, _reason("TIMEFRAME_CONFLICT", "H4 trend and H1 bias do not agree at recovery.")
    side = context["direction"]
    kind = "low" if side == "LONG" else "high"
    h1_pivot = _pivots(creation_frames["H1"], kind)[-1]
    invalidation = _get(h1_pivot, "price")
    points = m15.indicators
    latest, preceding = points[-1], points[-2]
    recovery_ok = (
        recovery["close"] > _get(latest, "ema") and _get(latest, "bbp") > 0 and _get(latest, "bbp") > _get(preceding, "bbp")
        if side == "LONG" else
        recovery["close"] < _get(latest, "ema") and _get(latest, "bbp") < 0 and _get(latest, "bbp") < _get(preceding, "bbp")
    )
    if not recovery_ok:
        return None, _reason("NO_M15_RECOVERY", "M15 has no directional EMA recovery with improving BBP.")
    window = list(zip(m15.bars[-lookback:], points[-lookback:]))
    def touched(bar, point):
        # Exact-touch baseline needs no ATR. A proximity variant requires the
        # same M15 observation's Wilder ATR; M5 ATR cannot define this distance.
        allowance = 0
        if parameters.m15_ema_proximity_atr:
            atr = _get(point, "atr")
            if atr is None or atr <= 0:
                return False
            allowance = float(parameters.m15_ema_proximity_atr) * atr
        ema = _get(point, "ema")
        return bar["low"] <= ema + allowance if side == "LONG" else bar["high"] >= ema - allowance
    touches = [
        index for index, (bar, point) in enumerate(window)
        if touched(bar, point)
    ]
    if not touches:
        return None, _reason("NO_M15_PULLBACK", f"No EMA touch within {parameters.m15_ema_proximity_atr} M15 ATR occurred in the {lookback} completed M15 bars.")
    onset = window[touches[0]][0]["time"]
    if any(
        bar["close"] < invalidation if side == "LONG" else bar["close"] > invalidation
        for bar, _ in window[touches[0]:]
    ):
        return None, _reason("STRUCTURE_INVALIDATED", "M15 pullback breached the frozen H1 invalidation.")
    triggers = _pivots(creation_frames["M5"], "high" if side == "LONG" else "low")
    if not triggers:
        return None, _reason("UNCONFIRMED_M5_STRUCTURE", "No local M5 trigger was already confirmed at recovery.")
    trigger = triggers[-1]
    identity = "|".join(str(value) for value in (
        VERSION, symbol, side, recovery["time"], onset,
        _get(h1_pivot, "origin_time"), invalidation,
        _get(trigger, "origin_time"), _get(trigger, "price"),
    ))
    signal_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    setup_id = signal_id if parameters.is_baseline else hashlib.sha256(
        f"{identity}:{parameters.sha256}".encode("utf-8")).hexdigest()[:24]
    if previous and (
        setup_id in previous.get("finished_setup_ids", [])
        or previous.get("setup_id") == setup_id and previous.get("state") in TERMINAL_STATES
    ):
        return None, _reason("SETUP_ALREADY_FINISHED", "The same recovery cannot create another setup.")
    state = _blank(symbol, decision_time, context, parameters)
    if previous:
        state["finished_setup_ids"] = list(previous.get("finished_setup_ids", []))
    state.update(
        state="CANDIDATE", setup_id=setup_id, signal_id=signal_id, side=side, candidate_at=candidate_at,
        recovery_bar_time=recovery["time"], pullback_onset=onset,
        invalidation=invalidation, trigger=_get(trigger, "price"),
        trigger_origin_time=_get(trigger, "origin_time"),
        trigger_available_at=_get(trigger, "available_at"),
        invalidation_origin_time=_get(h1_pivot, "origin_time"),
        invalidation_available_at=_get(h1_pivot, "available_at"),
        reasons=[_reason("AWAITING_M5_CONFIRMATION", f"Waiting for a qualifying close in the next {parameters.m5_confirmation_window} completed M5 bars.")],
    )
    return state, None


def _advance(state, frames, decision_time, tick, parameters):
    side = state["side"]
    if state["state"] == "CONFIRMED":
        if decision_time >= state["review_expires_at"]:
            return _finish(state, "EXPIRED", "REVIEW_EXPIRED", "The one-interval manual-review window expired.", decision_time)
        if any(
            (bar["close"] < state["invalidation"] if side == "LONG" else bar["close"] > state["invalidation"])
            for bar in frames["M5"].bars if bar["available_at"] > state["confirmation_at"]
        ):
            return _finish(state, "CANCELLED", "STRUCTURE_INVALIDATED", "A completed M5 close breached the frozen H1 invalidation before review.", decision_time)
        context = _context(frames, parameters)
        if context["regime"] != "TREND" or context["direction"] != side or context["bias"] != side:
            return _finish(state, "CANCELLED", "TIMEFRAME_CONFLICT", "Higher timeframes no longer support this direction.", decision_time)
        return dict(state, decision_time=decision_time, context=context)
    following = [bar for bar in frames["M5"].bars if bar["available_at"] > state["candidate_at"]]
    for count, bar in enumerate(following, 1):
        at = bar["available_at"]
        known = _at(frames, at)
        context = _context(known, parameters)
        state = dict(state, confirmation_bars_elapsed=count, context=context)
        if bar["close"] < state["invalidation"] if side == "LONG" else bar["close"] > state["invalidation"]:
            return _finish(state, "CANCELLED", "STRUCTURE_INVALIDATED", "A completed M5 close breached the frozen H1 invalidation.", decision_time)
        if context["regime"] != "TREND" or context["direction"] != side or context["bias"] != side:
            return _finish(state, "CANCELLED", "TIMEFRAME_CONFLICT", "Higher timeframes no longer support this direction.", decision_time)
        point = _indicator_at(known, "M5", bar["time"])
        crossed = bar["close"] > state["trigger"] if side == "LONG" else bar["close"] < state["trigger"]
        if crossed and _direction(point, side):
            atr = _get(point, "atr")
            observed = [item for item in known["M5"].bars if item["time"] >= state["pullback_onset"]]
            if (
                atr is None or not observed or observed[0]["time"] > state["pullback_onset"]
                or any(right["time"] - left["time"] != 300 for left, right in zip(observed, observed[1:]))
            ):
                return _finish(state, "CANCELLED", "INSUFFICIENT_STOP_HISTORY", "ATR and the complete M5 pullback history are required for a stop.", decision_time)
            buffer = max(Decimal(str(tick)), Decimal("0.1") * Decimal(str(atr)))
            raw_stop = (
                Decimal(str(min(item["low"] for item in observed))) - buffer if side == "LONG"
                else Decimal(str(max(item["high"] for item in observed))) + buffer
            )
            stop = _round(
                raw_stop, tick, side == "SHORT",
            )
            entry = bar["close"]
            if stop <= 0 or not (stop < entry if side == "LONG" else stop > entry):
                return _finish(state, "CANCELLED", "INVALID_STOP_DISTANCE", "Buffered structural stop does not invalidate the reference entry.", decision_time)
            target = _target(known, side, entry, tick)
            if target is None:
                return _finish(state, "CANCELLED", "MISSING_TARGET", "No eligible confirmed H1/H4 target remains beyond reference entry.", decision_time)
            state.update(
                state="CONFIRMED", confirmation_at=at, review_expires_at=at + 300,
                stop=stop, target=target, reference_entry=entry,
                reasons=[_reason("ENTRY_RISK_REVIEW_REQUIRED", f"Signal confirmed; executable entry, costs, net RR >= {parameters.minimum_net_rr} and risk limits still require assessment.")],
            )
            return _advance(state, frames, decision_time, tick, parameters)
        if count == parameters.m5_confirmation_window:
            return _finish(state, "EXPIRED", "CONFIRMATION_EXPIRED", f"{parameters.m5_confirmation_window} completed M5 bars passed without confirmation.", decision_time)
    return dict(state, decision_time=decision_time)


def evaluate_strategy(
    frames, *, decision_time, tick_size, symbol="BTCUSD", previous=None,
    short_allowed=True, data_fresh=True, market_open=True, settings=None,
    parameters=None,
):
    """Evaluate causal setup state at a UTC epoch, retaining frozen prior state.

    A later snapshot reconstructs missed confirmation bars chronologically. A
    completed sixth bar may confirm; otherwise it expires. Confirmed review
    expires at exactly confirmation+300 seconds, before equal-time invalidation.
    """
    as_of = _finite(decision_time, "decision_time")
    parameters = effective_parameters(parameters)
    tick = _finite(tick_size, "tick_size")
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    if previous and (previous.get("symbol") != symbol or previous.get("strategy_version") != VERSION):
        raise ValueError("previous state belongs to another symbol or strategy")
    if previous and previous.get("calculation_version") != CALCULATION_VERSION:
        raise ValueError("previous state belongs to a different or unrecorded calculation version; reconcile before restart")
    if previous and previous.get("parameter_sha256", effective_parameters().sha256) != parameters.sha256:
        raise ValueError("previous state belongs to different research parameters")
    if previous and previous.get("parameters") is not None and previous["parameters"] != parameters.as_dict():
        raise ValueError("previous state parameters do not match their recorded hash")
    if previous and as_of < previous["decision_time"]:
        raise ValueError("decision_time cannot move backwards")
    try:
        prepared = prepare_frames(frames, decision_time=as_of, settings=settings)
        context = _context(prepared, parameters)
    except (ValueError, KeyError, TypeError, OverflowError):
        state = previous or _blank(symbol, as_of, {"regime": "UNCLASSIFIED", "direction": None, "bias": "UNCLASSIFIED"}, parameters)
        return _finish(state, "CANCELLED" if state["state"] in {"CANDIDATE", "CONFIRMED"} else "WATCHING", "MISSING_INVALID_DATA", "Closed data or causal availability is missing/invalid.", as_of)
    state = _blank(symbol, as_of, context, parameters)
    if previous:
        state["finished_setup_ids"] = list(previous.get("finished_setup_ids", []))
    for eligible, code, message in (
        (data_fresh, "STALE_DATA", "Fresh eligible data is required; a closed session and a delayed feed must be distinguished."),
        (market_open, "MARKET_CLOSED", "Broker session eligibility does not permit a new setup."),
    ):
        if not eligible:
            active = previous if previous and previous["state"] in {"CANDIDATE", "CONFIRMED"} else state
            return _finish(active, "CANCELLED" if active["state"] in {"CANDIDATE", "CONFIRMED"} else "WATCHING", code, message, as_of)
    if previous and previous["state"] in {"CANDIDATE", "CONFIRMED"}:
        state = dict(previous, parameters=parameters.as_dict(), parameter_sha256=parameters.sha256,
                     signal_id=previous.get("signal_id", previous.get("setup_id")))
    else:
        if context["regime"] != "TREND":
            return _finish(state, "WATCHING", "UNCONFIRMED_STRUCTURE" if context["regime"] == "UNCLASSIFIED" else "INELIGIBLE_REGIME", "H4 is not an eligible confirmed directional trend.", as_of)
        if context["bias"] != context["direction"]:
            return _finish(state, "WATCHING", "TIMEFRAME_CONFLICT", "H1 bias does not agree with the H4 trend.", as_of)
        candidate, reason = _create_candidate(prepared, as_of, tick, symbol, previous, parameters)
        if candidate is None:
            if reason["code"] == "SETUP_ALREADY_FINISHED" and previous.get("state") in TERMINAL_STATES:
                return dict(previous, decision_time=as_of, context=context)
            state["reasons"] = [reason]
            return state
        state = candidate
    if state["side"] == "SHORT" and not short_allowed:
        return _finish(state, "CANCELLED", "UNSUPPORTED_SHORT", "The instrument profile does not permit short exposure.", as_of)
    return _advance(state, prepared, as_of, tick, parameters)


def replay_strategy(frames, *, tick_size, symbol="BTCUSD", until=None, settings=None, short_allowed=True, parameters=None):
    """Replay the same evaluator in chronological completed-M5 availability order."""
    if until is None:
        source = frames.get("M5")
        bars = source.bars if isinstance(source, FrameFeatures) else source.get("bars", [])
        until = max((_availability(bar, PERIODS["M5"]) for bar in bars), default=0)
    prepared = prepare_frames(frames, decision_time=until, settings=settings)
    decisions = sorted({bar["available_at"] for bar in prepared["M5"].bars})
    results = []
    previous = None
    for at in decisions:
        previous = evaluate_strategy(
            prepared, decision_time=at, tick_size=tick_size, symbol=symbol,
            previous=previous, settings=settings, short_allowed=short_allowed, parameters=parameters,
        )
        results.append(previous)
    return results
