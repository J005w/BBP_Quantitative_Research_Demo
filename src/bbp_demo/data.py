"""Deterministic invented candles, strict loading and completed aggregation."""

import csv
from datetime import datetime, timezone
import hashlib
import math
from collections.abc import Iterator
from typing import Any
from pathlib import Path

from bbp_system.strategy import PERIODS

START = 1704067200  # 2024-01-01 00:00:00 UTC: an invented timeline.
COUNT = 24 * 288
OMITTED = {20 * 288 + 100, 20 * 288 + 101, 20 * 288 + 102}
FIELDS = ("time", "available_at", "open", "high", "low", "close", "spread_points")


def utc(value: int | float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _triangle(index: int, period: int) -> int:
    phase = index % period
    return phase if phase <= period // 2 else period - phase


def generated_rows() -> Iterator[tuple[int, int, str, str, str, str, int]]:
    """Integer cents avoid platform-dependent random or trigonometric output."""
    previous = 3000000
    for index in range(COUNT):
        close = (3000000 + 6 * index + 70 * _triangle(index, 96)
                 + 40 * _triangle(index, 720) + 20 * _triangle(index, 18))
        opening = previous
        previous = close
        if index in OMITTED:
            continue
        stamp = START + 300 * index
        cents = (opening, max(opening, close) + 300 + 20 * (index % 7),
                 min(opening, close) - 300 - 20 * (index % 5), close)
        yield (stamp, stamp + 300, *(f"{value // 100}.{value % 100:02d}" for value in cents), 100)


def write_fixture(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(FIELDS)
        writer.writerows(generated_rows())


def load_dataset(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = Path(path)
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError("Unexpected synthetic candle schema")
        for record in reader:
            if None in record or any(value is None for value in record.values()):
                raise ValueError("Missing or extra candle fields")
            row = {name: int(record[name]) if name in ("time", "available_at", "spread_points")
                   else float(record[name]) for name in FIELDS}
            if not all(math.isfinite(row[name]) for name in ("open", "high", "low", "close")):
                raise ValueError("Candle prices must be finite")
            if row["time"] < 0 or row["time"] % 300 or row["available_at"] != row["time"] + 300:
                raise ValueError("Candle availability must be nominal close on the UTC grid")
            if not 0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"] < 1e8:
                raise ValueError("Invalid candle bounds or nonfinite prices")
            if row["spread_points"] < 0 or rows and row["time"] <= rows[-1]["time"]:
                raise ValueError("Negative spread or nonascending/duplicate candles")
            rows.append(row)
    if not rows:
        raise ValueError("Empty candle data")
    frames = {"M5": {"bars": rows}}
    discarded = {}
    for timeframe, seconds in PERIODS.items():
        if timeframe == "M5":
            continue
        buckets = {}
        for row in rows:
            buckets.setdefault(row["time"] // seconds * seconds, []).append(row)
        complete = []
        for opening, group in sorted(buckets.items()):
            if len(group) != seconds // 300 or any(row["time"] != opening + index * 300 for index, row in enumerate(group)):
                continue
            complete.append({"time": opening, "available_at": opening + seconds,
                             "open": group[0]["open"], "close": group[-1]["close"],
                             "high": max(row["high"] for row in group),
                             "low": min(row["low"] for row in group)})
        frames[timeframe] = {"bars": complete}
        discarded[timeframe] = len(buckets) - len(complete)
    missing = [{"start_utc": utc(left["time"] + 300), "end_utc": utc(right["time"]),
                "bars": (right["time"] - left["time"]) // 300 - 1}
               for left, right in zip(rows, rows[1:]) if right["time"] != left["time"] + 300]
    expected = (rows[-1]["time"] - rows[0]["time"]) // 300 + 1
    coverage = {"start_utc": utc(rows[0]["time"]), "end_utc": utc(rows[-1]["available_at"]),
                "observed_m5_bars": len(rows), "expected_m5_bars": expected,
                "missing_m5_bars": expected - len(rows), "missing_intervals": missing,
                "timeframe_bar_counts": {tf: len(frame["bars"]) for tf, frame in frames.items()},
                "discarded_incomplete_aggregate_bars": discarded,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source": "Invented integer arithmetic; no vendor observations",
                "availability": "UTC nominal close, no network arrival or broker clock claim"}
    dataset = {"symbol": "BTCUSD", "frames": frames, "metadata": {
        "point": .01, "trade_tick_size": .25, "trade_contract_size": 1,
        "currency_profit": "USD", "chart_mode": 0, "volume_min": .01,
        "volume_max": 10, "volume_step": .01, "volume_limit": 10}}
    return dataset, coverage
