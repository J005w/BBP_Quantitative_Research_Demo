"""Synthetic decimal ties, adjacent moves and independent arithmetic checks."""
from decimal import Decimal, localcontext
import math
import unittest

from bbp_system.indicators import compute_indicators, _bars
from bbp_system.strategy import RollingFrames
from bbp_system.strategy import PERIODS as FRAMES


def record(time, high, low, close=None):
    close = close or (Decimal(high)+Decimal(low))/2
    values = {"open": str(close), "high": str(high), "low": str(low), "close": str(close)}
    return {"time": time, "available_at": time+300, **{key: float(value) for key, value in values.items()}, "price_text": values}


class DecimalIndicatorTests(unittest.TestCase):
    def test_synthetic_exact_decimal_ties_do_not_gain_direction(self):
        # Constructed numbers reproduce binary subtraction asymmetry. They are
        # arithmetic test cases, not retained market observations.
        for high1, low1, high2, low2 in (("10.11", "9", "10.21", "8.9"),
                                        ("101.01", "100.5", "101.02", "100.49")):
            bars = [record(0, high1, low1), record(300, high2, low2)]
            up = bars[1]["high"]-bars[0]["high"]
            down = bars[0]["low"]-bars[1]["low"]
            self.assertNotEqual(up, down)  # Regression reproduces binary artifact.
            point = compute_indicators(bars, di_length=1, adx_smoothing=1)[-1]
            self.assertEqual((point.plus_di, point.minus_di, point.adx), (0, 0, 0))
            no_text = [{k: v for k, v in b.items() if k != "price_text"} for b in bars]
            self.assertEqual(compute_indicators(no_text, di_length=1)[-1].plus_di, 0)

    def test_adjacent_valid_tick_and_multiple_scales_preserve_direction(self):
        for scale in (Decimal("0.00000001"), Decimal(1), Decimal("1000000"), Decimal("1000000000000")):
            for high2, low2, expected in (("10.21", "9.4", "plus_di"), ("10.2", "9.39", "minus_di")):
                bars = [record(0, Decimal("10.1")*scale, Decimal("9.5")*scale),
                        record(300, Decimal(high2)*scale, Decimal(low2)*scale)]
                point = compute_indicators(bars, di_length=1, adx_smoothing=1)[-1]
                self.assertGreater(getattr(point, expected), 0)
                self.assertEqual(point.adx, 100)

    def test_decimal_text_cannot_override_validated_price(self):
        bars = [record(0, "101", "99")]
        bars[0]["price_text"]["high"] = "102"
        with self.assertRaisesRegex(ValueError, "disagrees"):
            compute_indicators(bars)

    def test_initialization_ema_and_rma_atr_independent_decimal_reference(self):
        bars = [record(i*300, str(101+i*.1), str(99+i*.1), str(100+i*.1)) for i in range(550)]
        actual = compute_indicators(bars)
        self.assertIsNone(actual[12].atr)
        self.assertIsNotNone(actual[13].atr)
        self.assertIsNone(actual[13].plus_di)
        self.assertIsNotNone(actual[14].plus_di)
        self.assertIsNone(actual[26].adx)
        self.assertIsNotNone(actual[27].adx)
        with localcontext() as ctx:
            ctx.prec = 60
            ema, atr, ranges = Decimal(bars[0]["price_text"]["close"]), None, []
            for i, bar in enumerate(bars):
                text = bar["price_text"]
                high, low, close = map(Decimal, (text["high"], text["low"], text["close"]))
                if i:
                    ema += Decimal(1)/7*(close-ema)
                    prior = Decimal(bars[i-1]["price_text"]["close"])
                    ranges.append(max(high-low, abs(high-prior), abs(low-prior)))
                else:
                    ranges.append(high-low)
                if i == 13:
                    atr = sum(ranges)/14
                elif i > 13:
                    atr = (13*atr+ranges[-1])/14
                self.assertAlmostEqual(actual[i].ema, float(ema), delta=1e-11)
                if atr is not None:
                    self.assertAlmostEqual(actual[i].atr, float(atr), delta=1e-12)

    def test_exact_499_window_and_future_prefix_causality(self):
        frames = {}
        for frame, seconds in FRAMES.items():
            bars = [record(1000000-(501-i)*seconds, str(round(102+math.sin(i)*.3, 4)),
                           str(round(98+math.cos(i)*.3, 4)), "100") for i in range(501)]
            for bar in bars:
                bar["available_at"] = bar["time"]+seconds
            frames[frame] = {"bars": bars}
        rolling = RollingFrames(frames)
        view = rolling.at(1000000)
        for frame in FRAMES:
            self.assertEqual(len(view[frame].bars), 499)
            self.assertEqual(view[frame].indicators, tuple(compute_indicators(frames[frame]["bars"][-499:])))
        cutoff = 1000000-14400
        before = rolling.at(cutoff)
        changed = {frame: {"bars": [record(b["time"], "202", "198", "200") | {"available_at": b["available_at"]}
                    if b["available_at"] > cutoff else b for b in data["bars"]]} for frame, data in frames.items()}
        after = RollingFrames(changed).at(cutoff)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
