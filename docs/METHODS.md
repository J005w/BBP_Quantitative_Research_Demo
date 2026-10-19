# Methods and assumptions

## Time and information

Every raw timestamp is a UTC **bar open**. `available_at = open + duration` is an authored nominal-close assumption. Aggregate H4/H1/M15 bars are included only if all constituent M5 intervals exist. Missing bars are never interpolated. Higher-timeframe features use completed native bars; indicators are recomputed on the last available 499 bars. The compact raw run starts scoring after 100 completed H4 candles; the library's 499-H4 default is unchanged. The engineered case has ten stipulated H4 observations and is not a meaningful indicator warmup experiment.

Pivots become known after their configured right-side confirmation delay (two bars), not at the pivot origin. Strategy decisions cannot use a later availability timestamp. A completed M5 confirmation at a boundary permits a hypothetical fill at the next M5 open at that same boundary. This assumes zero calculation/transport latency. Late or missing review windows expire. Actual broker-clock/DST/session alignment, observed arrival timing and tick execution remain unqualified.

## Rules and costs

The fixed trend-continuation baseline uses H4 regime, H1 directional bias, M15 pullback and M5 confirmation, then known structural stops and nearest confirmed target. The minimum **net** reward/risk is 2. The report includes the accepted case and a cost-rejected target, with their exact setup identities and causal times. Level lifecycle diagnostics do not change nearest-price admission. Recovery, bounce, pilot and intrabar-observation workflows are excluded.

Prices denote an invented Bid series. A linear one-unit BTC/USD contract in a USD account is stipulated; this does not qualify a real venue's contract. Long entry uses Ask (Bid + carried preceding completed-bar spread), short entry Bid; closing sides are reversed. Point = $0.01, tradable tick = $0.25, volume step/minimum = 0.01, maximum = 10 invented lots. Source candle prices need not lie on the execution tick grid; executable levels are rounded by the engine.

The scenario charges round-trip commission $7 and slippage $12 per lot, plus conservative financing `entry × 20% / 360 × 3` per lot. The whole budget is charged even for a short holding period. Spread is already included in quote-side prices and is not deducted again. These are stress assumptions, not broker facts. The fixed cost-stress case doubles spread and slippage without reoptimizing parameters. Gap fills use the first available quote; losses may exceed planned stop risk. If both stop and target lie inside a candle, stop wins. Assumed midpoint event time is not observed tick time. Period-end exposure is flattened at the last assessed close; drawdown is sampled.

## Risk and accounting

An invented $100,000 initial balance supports per-trade risk 0.25%, aggregate 1%, crypto group 0.5%, one position per symbol and a 12-hour holding ceiling. Scenario controls add 1% daily-loss and 5% drawdown pauses, gross notional ceiling 1× equity, used margin cap 25% and at least 50% notional margin. Exit management continues while new risk is paused. Missing quotes can delay exits; no live enforcement is claimed.

The independent route recomputes signed `(exit-entry) × quantity`, individual cost legs and initial-to-final cash using Decimal without ledger/sizing/P&L helpers. Differences must be within $0.000001, because engine output crosses a float reporting boundary. This is an independent accounting check within a shared contract/scenario specification, not an independent research replication.

No parameter search, empirical holdout, confidence interval or return-selection claim is made. The four authored examples and their intent are recorded in the report's trial ledger. Native zero-trade calendar-day slices describe coverage; they are not independent walk-forward tests.
