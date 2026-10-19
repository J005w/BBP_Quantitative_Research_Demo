"""Explicitly engineered feature case for deterministic rule/ledger inspection.

These indicator and pivot features are stipulated inputs. They are intentionally
not calculated from these OHLC bars: the case isolates candidate confirmation,
next-bar entry, rejection and accounting logic. Use the separate raw-candle
dataset for end-to-end indicator/replay checks. Neither establishes market edge.
"""

from .strategy import FrameFeatures


ENGINEERED_EPOCH = 1767225600  # 2026-01-01T00:00:00Z; arbitrary synthetic origin.


def _bar(opening, seconds, *, close=108, low=100, high=120):
    return {"time": opening, "available_at": opening+seconds,
            "open": close, "high": high, "low": low, "close": close}


def _point(record, *, ema=105, bbp=2, plus=30, minus=10, adx=30, atr=2):
    return {"time": record["time"], "available_at": record["available_at"],
            "ema": ema, "bbp": bbp, "plus_di": plus, "minus_di": minus,
            "adx": adx, "atr": atr}


def _pivot(kind, price, origin, confirming):
    return {"kind": kind, "price": price, "origin_time": origin,
            "confirmed_bar_time": confirming["time"], "available_at": confirming["available_at"]}


def engineered_rule_fixture(*, generous_target=True, confirm_on=1):
    """Return synthetic precomputed features and an explicit linear contract.

    ``generous_target=False`` creates a nearby obstacle that fails net reward to
    risk after scenario costs. Both are authored correctness examples, not
    selected outcomes or computed market indicators.
    """
    t = ENGINEERED_EPOCH
    h4 = [_bar(t-(10-i)*14400, 14400) for i in range(10)]
    h1 = [_bar(t-(10-i)*3600, 3600) for i in range(10)]
    m15 = [_bar(t-(4-i)*900, 900) for i in range(4)]
    m15[0].update(close=104, open=104, low=100)
    m15[1].update(close=106, open=106, low=104)
    m15[2].update(close=106, open=106, low=104)
    m15[3].update(close=108, open=108, low=105)
    m5 = [_bar(t-3600+i*300, 300, high=114, low=101) for i in range(18)]
    m5[0]["low"] = 99
    if confirm_on is not None:
        m5[11+confirm_on].update(close=112, open=112, high=114)
    for i, item in enumerate(m5):
        item["spread_points"] = 10
        if i >= 13:
            item.update(open=113, close=113, high=116, low=105)
    frames = {
        "H4": FrameFeatures(tuple(h4), tuple(_point(b) for b in h4), (
            _pivot("high", 110, h4[2]["time"], h4[4]),
            _pivot("low", 80, h4[3]["time"], h4[5]),
            _pivot("high", 1001 if generous_target else 150, h4[6]["time"], h4[8]),
            _pivot("low", 90, h4[7]["time"], h4[9]),
        )),
        "H1": FrameFeatures(tuple(h1), tuple(_point(b) for b in h1), (
            _pivot("high", 1000 if generous_target else 140, h1[3]["time"], h1[5]),
            _pivot("low", 95, h1[7]["time"], h1[9]),
        )),
        "M15": FrameFeatures(tuple(m15), tuple(_point(b, ema=103 if i < 3 else 105,
            bbp=1 if i < 3 else 2) for i, b in enumerate(m15)), ()),
        "M5": FrameFeatures(tuple(m5), tuple(_point(b) for b in m5), (
            _pivot("high", 110, m5[6]["time"], m5[8]),
            _pivot("low", 100, m5[7]["time"], m5[9]),
        )),
    }
    return {"symbol": "BTCUSD", "evidence_level": "ENGINEERED_FEATURE_CORRECTNESS_ONLY",
            "frames": frames, "metadata": {
                "point": .01, "trade_tick_size": .25, "trade_contract_size": 1,
                "currency_profit": "USD", "chart_mode": 0, "volume_min": .01,
                "volume_max": 10, "volume_step": .01, "volume_limit": 10,
                "provenance": "Authored synthetic unit contract; no broker source",
            }}
