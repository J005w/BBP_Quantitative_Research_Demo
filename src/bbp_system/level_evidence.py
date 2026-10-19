"""Causal target-obstacle evidence using the existing level observer.

Observations accompany the baseline nearest-level choice; lifecycle states do
not alter admission. One-tick zones are explicit frozen diagnostic defaults.
"""
from dataclasses import asdict

from .indicators import ConfirmedPivot
from .research_levels import inspect_level
from .strategy import PERIODS, prepare_frames, nearest_target, _get, _finite


def known_target_obstacles(frames, *, decision_time, side, entry, tick_size, settings=None):
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    price, tick = _finite(entry, "entry"), _finite(tick_size, "tick_size")
    if price <= 0 or tick <= 0:
        raise ValueError("entry and tick_size must be positive")
    prepared = prepare_frames(frames, decision_time=decision_time, settings=settings)
    output = []
    kind = "high" if side == "LONG" else "low"
    for timeframe in ("H1", "H4"):
        frame = prepared[timeframe]
        labels = {bar["time"]: index for index, bar in enumerate(frame.bars)}
        bars = [dict(bar, availability_basis=bar.get("availability_basis", "provided-or-assumed nominal close"))
                for bar in frame.bars]
        for pivot in frame.pivots:
            level = _get(pivot, "price")
            if _get(pivot, "kind") != kind or not (level > price if side == "LONG" else level < price):
                continue
            if not isinstance(pivot, ConfirmedPivot):
                pivot = ConfirmedPivot(kind, level, labels.get(_get(pivot, "origin_time"), -1),
                    _get(pivot, "origin_time"), labels.get(_get(pivot, "confirmed_bar_time"), -1),
                    _get(pivot, "confirmed_bar_time"), _get(pivot, "available_at"))
            observation = inspect_level(pivot, timeframe=timeframe, bars=bars,
                as_of=decision_time, tick_size=tick, bar_seconds=PERIODS[timeframe], zone_ticks=1)
            if observation is not None:
                output.append({**asdict(observation),
                    "zone_basis": "Research diagnostic default: frozen one-trade-tick half-width",
                    "admission_policy": "Nearest confirmed price; lifecycle states do not change admission"})
    output.sort(key=lambda item: (item["price"] if side == "LONG" else -item["price"],
                                 item["timeframe"], item["known_at"], item["identity"]))
    nearest = output[0]["price"] if output else None
    for item in output:
        item["nearest_price"] = item["price"] == nearest
    return {"side": side, "entry": price, "as_of": decision_time,
            "nearest_target": nearest_target(frames, decision_time=decision_time,
                side=side, entry=price, tick_size=tick, settings=settings),
            "levels": output, "skipped_nearer_levels": [],
            "coverage_note": "Fresh means no test observed in supplied post-confirmation history; inspect coverage flags."}
