"""Causal calculations on supplied, already closed OHLC bars.

Startup conventions are explicit and still require captured-reference parity:
* EMA starts at the first close, with alpha = 2 / (length + 1).
* ATR uses TR[0] = high[0] - low[0], then Wilder RMA with an SMA seed.
* DMI's first DM/TR sample is unavailable; its aligned SMA seed uses bars
  1 through di_length. Directional ties contribute zero to both DM series.
* ADX seeds an RMA from the first adx_smoothing available DX observations.
  Zero smoothed TR gives zero DI; zero DI sum gives zero DX.
* Missing intervals are not filled. TR uses the preceding supplied close.

Bar ``time`` is an integer UTC epoch label, usually the opening time. This
module cannot establish that a bar is closed. The adapter must establish that
before calling it and may attach an actual ``available_at`` UTC epoch value.
An absent availability value stays unknown; a bar label is not an arrival time.

These functions implement indicator arithmetic and confirmed observations,
not market regimes, trade recommendations, profitability or live feed parity.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import math
from numbers import Real
from typing import Literal

CALCULATION_VERSION = "decimal_directional_moves_v2"


@dataclass(frozen=True)
class IndicatorPoint:
    time: int
    ema: float
    bull_power: float
    bear_power: float
    bbp: float
    atr: float | None
    plus_di: float | None
    minus_di: float | None
    adx: float | None
    normalized_bbp: float | None
    available_at: int | float | None = None


@dataclass(frozen=True)
class ConfirmedPivot:
    kind: Literal["high", "low"]
    price: float
    origin_index: int
    origin_time: int
    confirmed_index: int
    confirmed_bar_time: int
    available_at: int | float | None


@dataclass(frozen=True)
class _Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    available_at: int | float | None
    decimal_high: Decimal
    decimal_low: Decimal
    positive_dm: float = 0.0
    negative_dm: float = 0.0


def _decimal_price(record, name, value):
    """Use original decimal text when supplied; otherwise the number's decimal representation.

    No epsilon or assumed price grid: adjacent representable decimal moves remain
    distinct. Text must describe the validated numeric price, not override it.
    """
    raw = record.get("price_text", {}).get(name, str(value))
    try:
        exact = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid original decimal price text") from None
    if not exact.is_finite() or exact <= 0 or float(exact) != value or len(exact.as_tuple().digits) > 700:
        raise ValueError("Original decimal price text disagrees with numeric price")
    return exact


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_length(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bars(records: Sequence[Mapping[str, object]]) -> list[_Bar]:
    bars: list[_Bar] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"bar {index} must be an OHLC mapping")
        missing = {"time", "open", "high", "low", "close"} - record.keys()
        if missing:
            raise ValueError(f"bar {index} is missing: {', '.join(sorted(missing))}")
        timestamp = _finite_number(record["time"], f"bar {index} time")
        if not timestamp.is_integer():
            raise ValueError(f"bar {index} time must be integer UTC epoch seconds")
        time = int(record["time"])
        if bars and time <= bars[-1].time:
            raise ValueError("bar times must be strictly increasing without duplicates")
        prices = {
            name: _finite_number(record[name], f"bar {index} {name}")
            for name in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in prices.values()):
            raise ValueError(f"bar {index} prices must be positive")
        low, high = prices["low"], prices["high"]
        if not low <= prices["open"] <= high or not low <= prices["close"] <= high:
            raise ValueError(f"bar {index} violates OHLC bounds")
        if "volume" in record and record["volume"] is not None:
            volume = _finite_number(record["volume"], f"bar {index} volume")
            if volume < 0:
                raise ValueError(f"bar {index} volume must not be negative")
        availability = record.get("available_at")
        if availability is not None:
            value = _finite_number(availability, f"bar {index} available_at")
            if value < time:
                raise ValueError(f"bar {index} available_at precedes its bar label")
            availability = int(availability) if value.is_integer() else value
        high_decimal = _decimal_price(record, "high", prices["high"])
        low_decimal = _decimal_price(record, "low", prices["low"])
        positive, negative = 0.0, 0.0
        if bars:
            # Enough precision for the complete finite-float exponent range.
            # Cache exact DM once; rolling windows retain their own RMA seeds.
            with localcontext() as context:
                context.prec = 1400
                up = high_decimal - bars[-1].decimal_high
                down = bars[-1].decimal_low - low_decimal
                positive = float(up) if up > down and up > 0 else 0.0
                negative = float(down) if down > up and down > 0 else 0.0
        bars.append(_Bar(time, **prices, available_at=availability,
                         decimal_high=high_decimal, decimal_low=low_decimal,
                         positive_dm=positive, negative_dm=negative))
    return bars


def _rma(values: Sequence[float | None], length: int) -> list[float | None]:
    """Seed using the first length available observations; preserve warm-up."""
    results: list[float | None] = []
    seed: list[float] = []
    previous: float | None = None
    for value in values:
        if value is None:
            results.append(None)
            continue
        if previous is None:
            seed.append(value)
            if len(seed) < length:
                results.append(None)
                continue
            previous = math.fsum(item / length for item in seed)
        else:
            previous += (value - previous) / length
        results.append(previous)
    return results


def compute_indicators(
    bars: Sequence[Mapping[str, object]], *, bbp_length: int = 13,
    di_length: int = 14, adx_smoothing: int = 14, atr_length: int = 14,
) -> list[IndicatorPoint]:
    """Return one causal point per supplied closed bar, with None warm-up.

    For N bars indexed from zero, ATR first appears at atr_length - 1,
    DI at di_length, and ADX at di_length + adx_smoothing - 1. EMA and BBP
    start immediately. BBP/ATR remains None during warm-up or when ATR is zero.
    Default startup values are provisional until same-candle reference parity.
    """
    for name, value in (
        ("bbp_length", bbp_length), ("di_length", di_length),
        ("adx_smoothing", adx_smoothing), ("atr_length", atr_length),
    ):
        _positive_length(value, name)
    values = _bars(bars)
    return _indicators_from_validated(values, bbp_length=bbp_length, di_length=di_length,
                                      adx_smoothing=adx_smoothing, atr_length=atr_length)


def _indicators_from_validated(values, *, bbp_length=13, di_length=14,
                               adx_smoothing=14, atr_length=14):
    """Shared arithmetic for immutable _Bar records already strictly checked.

    Internal research adapter only. Public compute_indicators always validates
    raw mappings and lengths first; the adapter validates full history once.
    No alternative seed, smoothing, formula or availability rule is introduced.
    """
    if not values:
        return []

    true_ranges: list[float] = []
    positive_dm: list[float | None] = [None]
    negative_dm: list[float | None] = [None]
    for index, bar in enumerate(values):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        previous = values[index - 1]
        true_ranges.append(max(
            bar.high - bar.low, abs(bar.high - previous.close),
            abs(bar.low - previous.close),
        ))
        positive_dm.append(bar.positive_dm)
        negative_dm.append(bar.negative_dm)

    atr = _rma(true_ranges, atr_length)
    dmi_tr = _rma([None, *true_ranges[1:]], di_length)
    positive_rma, negative_rma = _rma(positive_dm, di_length), _rma(negative_dm, di_length)
    plus_di: list[float | None] = []
    minus_di: list[float | None] = []
    dx: list[float | None] = []
    for tr, positive, negative in zip(dmi_tr, positive_rma, negative_rma):
        if tr is None or positive is None or negative is None:
            plus_di.append(None)
            minus_di.append(None)
            dx.append(None)
            continue
        plus = 0.0 if tr == 0 else 100 * (positive / tr)
        minus = 0.0 if tr == 0 else 100 * (negative / tr)
        plus_di.append(plus)
        minus_di.append(minus)
        total = plus + minus
        dx.append(0.0 if total == 0 else 100 * abs(plus - minus) / total)
    adx = _rma(dx, adx_smoothing)

    output: list[IndicatorPoint] = []
    ema = values[0].close
    alpha = 2.0 / (bbp_length + 1)
    for index, bar in enumerate(values):
        if index:
            ema += alpha * (bar.close - ema)
        bull, bear = bar.high - ema, bar.low - ema
        # The combined value is the sum, never an average or order-flow measure.
        bbp = bull + bear
        normalized = None if atr[index] is None or atr[index] == 0 else bbp / atr[index]
        calculated = (ema, bull, bear, bbp, atr[index], plus_di[index], minus_di[index], adx[index], normalized)
        if any(value is not None and not math.isfinite(value) for value in calculated):
            raise ValueError("indicator calculation exceeds finite numeric range")
        output.append(IndicatorPoint(
            time=bar.time, ema=ema, bull_power=bull, bear_power=bear, bbp=bbp,
            atr=atr[index], plus_di=plus_di[index], minus_di=minus_di[index],
            adx=adx[index], normalized_bbp=normalized, available_at=bar.available_at,
        ))
    return output


def confirmed_pivots(
    bars: Sequence[Mapping[str, object]], *, left: int = 2, right: int = 2,
) -> list[ConfirmedPivot]:
    """Return only strict pivots whose right-hand bars are in this closed prefix.

    A tie to any neighboring high/low disqualifies that pivot. ``available_at``
    belongs to the confirming bar; without metadata it is None. Decision code
    must require known availability and cannot use ``confirmed_bar_time`` as
    an availability timestamp. The full-history list must be replayed using its
    confirmation/availability fields, never by treating origin time as discovery.
    """
    _positive_length(left, "left")
    _positive_length(right, "right")
    values = _bars(bars)
    return _pivots_from_validated(values, left=left, right=right)


def _pivots_from_validated(values, *, left=2, right=2):
    """Shared strict-pivot arithmetic on previously validated immutable bars."""
    output: list[ConfirmedPivot] = []
    for origin in range(left, len(values) - right):
        bar = values[origin]
        neighbors = values[origin - left:origin] + values[origin + 1:origin + right + 1]
        confirmation = origin + right
        confirming_bar = values[confirmation]
        for kind, price, qualifies in (
            ("high", bar.high, all(bar.high > neighbor.high for neighbor in neighbors)),
            ("low", bar.low, all(bar.low < neighbor.low for neighbor in neighbors)),
        ):
            if qualifies:
                output.append(ConfirmedPivot(
                    kind=kind, price=price, origin_index=origin, origin_time=bar.time,
                    confirmed_index=confirmation, confirmed_bar_time=confirming_bar.time,
                    available_at=confirming_bar.available_at,
                ))
    return output
