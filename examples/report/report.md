# Reproducible correctness report

**Evidence: SYNTHETIC_CORRECTNESS_ONLY. No market-edge claim.**

Raw invented candles cover 2024-01-01T00:00:00Z to 2024-01-25T00:00:00Z (end exclusive).
6,909 of 6,912 expected M5 bars are present; three deliberately missing bars remain missing.
Incomplete higher-timeframe aggregates are discarded. See report.json for exact gaps, timeframe counts and hashes.

The raw OHLC replay computes indicators and confirmed pivots through the shared engine:
0 trades across 2,109 scored M5 openings.
All 8 scored calendar-day slices have zero trades. These slices are not held-out folds.
The compact demonstration warms up on 100 H4 candles. This is an explicit demo setting, not a retuned profitability result.

The separate engineered feature case admits 1 synthetic trade; its net result is $-143.699.
Its features are stipulated to exercise the rules and are not indicators calculated from the illustration bars.
The nearby-target variant is rejected after costs; doubling spread and slippage uses frozen parameters.
Neither the acceptance nor the rejection is empirical evidence.

Independent Decimal cash bridge: $100000 + ($-143.6993250000000000000000000) = $99856.30067500000000000000000.
Difference from engine: $0E-23; tolerance $0.000001.
The check independently rebuilds gross price P&L, commission, slippage and scenario financing for each trade.

Bar timestamps are UTC opens; features become available at completed nominal closes.
The confirmation bar opens at 2026-01-01T00:00:00Z and closes at 2026-01-01T00:05:00Z.
The hypothetical next-bar-open fill occurs at that boundary, using the previous completed bar's spread.
This convention assumes zero latency and does not establish broker-clock, network-arrival or tick-execution parity.

Recovery is proposed, synthetic-only and unselected. The real clock qualification gate remains unresolved.
The old private report's two hypothetical baseline trades are recorded context only; they are not included in these results.
No redistributable real-market study or profitability inference is delivered here.

Open dashboard.html for local playback, or run `python run.py dashboard --report report.json` for the guarded loopback server.
All trial choices, assumptions, risk caps, individual decisions and exact identities are in report.json.
