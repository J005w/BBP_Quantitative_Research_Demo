"""Compare a local MT5 reference export with the shared Python indicators.

No platform is opened and no reference data is written. The caller must record
how the read-only exporter was executed; a supplied CSV alone cannot prove its
source. Numeric agreement is scoped to the supplied closed candles and warmup.
"""

from __future__ import annotations

import csv
import math
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .indicators import compute_indicators


class ReferenceError(ValueError):
    """Reference structure, timing or coverage could not be verified."""


FRAMES = {"H4": 14400, "H1": 3600, "M15": 900, "M5": 300}
FIELDS = ("timeframe", "raw_time", "open", "high", "low", "close", "ema13", "bull13", "bear13",
          "adx_wilder14", "plus_di14", "minus_di14", "native_atr14", "rma_atr14")
PRICE_FIELDS = ("ema13", "bull13", "bear13", "bbp13", "rma_atr14")
CORE_ATTRIBUTES = {"ema13": "ema", "bull13": "bull_power", "bear13": "bear_power", "bbp13": "bbp",
                   "adx_wilder14": "adx", "plus_di14": "plus_di", "minus_di14": "minus_di", "rma_atr14": "atr"}


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ReferenceError(f"Reference {name} is missing or nonnumeric.") from None
    if not math.isfinite(number) or abs(number) >= 1e100:
        raise ReferenceError(f"Reference {name} is nonfinite or an empty-buffer sentinel.")
    return number


def _load(path: str | Path, exported_at: datetime, offset: int) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {frame: [] for frame in FRAMES}
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise ReferenceError("Reference CSV fields do not match BBP_Reference_Export.mq5.")
            for source in reader:
                if None in source or any(value is None for value in source.values()):
                    raise ReferenceError("Reference CSV has missing or extra row fields.")
                frame = source["timeframe"]
                if frame not in FRAMES:
                    raise ReferenceError("Reference CSV contains an unsupported timeframe.")
                raw = _finite(source["raw_time"], "raw_time")
                if not raw.is_integer() or raw <= offset:
                    raise ReferenceError("Reference bar timestamps must be positive integer epochs.")
                opening = int(raw) - offset
                closing = opening + FRAMES[frame]
                if closing > exported_at.timestamp():
                    raise ReferenceError("Reference includes a forming/future bar at the declared export time.")
                row: dict[str, Any] = {"time": opening, "raw_time": int(raw), "available_at": closing}
                for name in FIELDS[2:]:
                    row[name] = None if name == "rma_atr14" and source[name] == "" else _finite(source[name], name)
                if any(row[name] <= 0 for name in ("open", "high", "low", "close", "ema13")):
                    raise ReferenceError("Reference prices must be positive.")
                if not row["low"] <= row["open"] <= row["high"] or not row["low"] <= row["close"] <= row["high"]:
                    raise ReferenceError("Reference OHLC range is inconsistent.")
                if any(not 0 <= row[name] <= 100 + 1e-8 for name in ("adx_wilder14", "plus_di14", "minus_di14")):
                    raise ReferenceError("Reference directional indicators lie outside their valid range.")
                if row["native_atr14"] < 0 or row["rma_atr14"] is not None and row["rma_atr14"] < 0:
                    raise ReferenceError("Reference ATR must not be negative.")
                if groups[frame]:
                    delta = opening - groups[frame][-1]["time"]
                    if delta <= 0 or delta % FRAMES[frame]:
                        raise ReferenceError("Reference bar timestamps are duplicated, unordered or off timeframe spacing.")
                groups[frame].append(row)
    except (OSError, UnicodeError, csv.Error):
        raise ReferenceError("Reference CSV cannot be read; local paths and private details are omitted.") from None
    return groups


def _metric(pairs: list[tuple[float, float]], tolerance: float) -> dict[str, Any]:
    errors = [abs(reference - calculated) for reference, calculated in pairs]
    maximum = max(errors) if errors else None
    return {"compared_values": len(pairs), "max_absolute_error": maximum,
            "mean_absolute_error": math.fsum(errors) / len(errors) if errors else None,
            "absolute_tolerance": tolerance,
            "status": "MATCH" if maximum is not None and maximum <= tolerance else "DISCREPANCY"}


def _sma_atr(rows: list[dict[str, Any]]) -> list[float | None]:
    result: list[float | None] = []
    window: deque[float] = deque(maxlen=14)
    for index, row in enumerate(rows):
        tr = row["high"] - row["low"]
        if index:
            tr = max(tr, abs(row["high"] - rows[index - 1]["close"]), abs(row["low"] - rows[index - 1]["close"]))
        window.append(tr)
        result.append(math.fsum(window) / 14 if len(window) == 14 else None)
    return result


def compare_mt5_reference(csv_path: str | Path, *, exported_at_utc: datetime, timestamp_offset_seconds: int,
                          source_verified: bool = False, warmup_bars: int = 300, compare_bars: int = 200,
                          price_tolerance: float = .001, directional_tolerance: float = .01) -> dict[str, Any]:
    """Compare final closed bars after an explicit warmup using identical OHLC.

    ``source_verified`` means the caller independently observed execution of
    the project's DEMO/server-guarded local MQL5 exporter. It is not inferred
    from a filename. All four timeframes and at least 500 candles are required
    by the defaults. This establishes no TradingView parity or trading result.
    """
    if not isinstance(exported_at_utc, datetime) or exported_at_utc.tzinfo is None or exported_at_utc.utcoffset() is None:
        raise ReferenceError("Reference export time must be timezone-aware.")
    if type(timestamp_offset_seconds) is not int or timestamp_offset_seconds not in (0, 7200, 10800):
        raise ReferenceError("Reference timestamp offset must be explicitly 0, 7200 or 10800 seconds.")
    if type(source_verified) is not bool:
        raise ReferenceError("Reference source verification must be an explicit boolean.")
    if type(warmup_bars) is not int or warmup_bars < 28 or type(compare_bars) is not int or compare_bars <= 0:
        raise ReferenceError("Reference warmup must be at least 28 bars and comparison count must be positive.")
    for value in (price_tolerance, directional_tolerance):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ReferenceError("Reference tolerances must be finite and positive.")
    exported_at_utc = exported_at_utc.astimezone(timezone.utc)
    groups = _load(csv_path, exported_at_utc, timestamp_offset_seconds)
    frames, core_matched, native_matched, native_sma_matched = {}, True, True, True
    total = 0
    for frame, rows in groups.items():
        if len(rows) < warmup_bars + compare_bars:
            raise ReferenceError(f"Reference {frame} needs at least {warmup_bars + compare_bars} closed bars.")
        points = compute_indicators(rows)
        selected = list(zip(rows[-compare_bars:], points[-compare_bars:]))
        if len(rows) - compare_bars < warmup_bars:
            raise ReferenceError("Reference comparison overlaps the required warmup.")
        metrics = {}
        for name, attribute in CORE_ATTRIBUTES.items():
            pairs = []
            for row, point in selected:
                reference = row["bull13"] + row["bear13"] if name == "bbp13" else row[name]
                calculated = getattr(point, attribute)
                if reference is None or calculated is None:
                    raise ReferenceError("Reference comparison contains unavailable indicator values after warmup.")
                pairs.append((reference, calculated))
            tolerance = price_tolerance if name in PRICE_FIELDS else directional_tolerance
            metrics[name] = _metric(pairs, tolerance)
        core_match = all(metric["status"] == "MATCH" for metric in metrics.values())
        rma_pairs = [(row["native_atr14"], point.atr) for row, point in selected]
        sma = _sma_atr(rows)
        sma_pairs = [(row["native_atr14"], value) for row, value in zip(rows[-compare_bars:], sma[-compare_bars:])]
        atr_to_rma, atr_to_sma = _metric(rma_pairs, price_tolerance), _metric(sma_pairs, price_tolerance)
        native_status = "MATCH_RMA" if atr_to_rma["status"] == "MATCH" else (
            "DISCREPANCY_DIFFERENT_SMOOTHING" if atr_to_sma["status"] == "MATCH" else "DISCREPANCY_UNCLASSIFIED")
        gaps = sum(1 for previous, current in zip(rows, rows[1:]) if current["time"] - previous["time"] > FRAMES[frame])
        frames[frame] = {
            "closed_bars": len(rows), "warmup_bars": len(rows) - compare_bars,
            "compared_bars": compare_bars, "first_open_utc": datetime.fromtimestamp(rows[0]["time"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_close_utc": datetime.fromtimestamp(rows[-1]["available_at"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "unclassified_gap_events": gaps, "core_status": "MATCH" if core_match else "DISCREPANCY",
            "metrics": metrics,
            "native_atr14": {"status": native_status, "vs_wilder_rma": atr_to_rma, "vs_simple_moving_average": atr_to_sma},
        }
        total += compare_bars
        core_matched = core_matched and core_match
        native_matched = native_matched and atr_to_rma["status"] == "MATCH"
        native_sma_matched = native_sma_matched and (atr_to_rma["status"] == "MATCH" or atr_to_sma["status"] == "MATCH")
    numeric_status = "PASS_CORE_AND_NATIVE_ATR" if core_matched and native_matched else (
        "PASS_CORE_NATIVE_ATR_DIFFERENT_SMOOTHING" if core_matched and native_sma_matched else "FAIL_REFERENCE_DISCREPANCY")
    status = numeric_status if source_verified or numeric_status == "FAIL_REFERENCE_DISCREPANCY" else "UNVERIFIED_REFERENCE_SOURCE"
    return {
        "schema_version": 1, "status": status, "numeric_status": numeric_status,
        "reference_source": "LOCAL_MT5_BUILTINS_AND_EXPORTED_RMA",
        "source_execution_verified_by_caller": source_verified,
        "symbol": "BTCUSD", "exporter_expected_server": "ICMarketsAU-Demo",
        "exporter_declared_demo_guard": True,
        "native_reference_fields": ["ema13", "bull13", "bear13", "adx_wilder14", "plus_di14", "minus_di14", "native_atr14"],
        "exporter_custom_formula_fields": ["rma_atr14"],
        "exported_at_utc": exported_at_utc.isoformat().replace("+00:00", "Z"),
        "timestamp_offset_seconds": timestamp_offset_seconds,
        "required_warmup_bars": warmup_bars, "compared_bars_total": total, "timeframes": frames,
        "core_reference_match": core_matched,
        "tradingview_parity": "NOT_ESTABLISHED",
        "historical_dst_and_sessions": "PENDING",
        "evidence_scope": "Same supplied closed OHLC, final comparison window after warmup. CSV fields cannot independently prove exporter identity. Native ATR smoothing is assessed separately; no strategy performance claim.",
    }
