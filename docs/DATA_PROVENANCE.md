# Data provenance

`src/bbp_demo/resources/synthetic_m5.csv` is freshly invented data, not a transformation of licensed or private observations. `bbp_demo.data.generated_rows()` constructs cents using integer trends and triangular waves; there is no random seed, external request, vendor file or hidden input.

Schema: `time,available_at,open,high,low,close,spread_points`. Times are epoch seconds on a UTC grid; prices are USD; spread points use a $0.01 point. There are 6,909 observed of 6,912 expected M5 rows over 2024-01-01 through 2024-01-25 (end exclusive). The invented interval 2024-01-21 08:20–08:35 UTC deliberately omits three bars. Complete aggregation yields H4 143, H1 575, M15 2,302 and M5 6,909 bars. One H4, one H1 and two M15 incomplete buckets are discarded. The report records the scored interval after warmup separately from full input coverage.

The CSV SHA-256 is `eb38c65c4fe6a2dea6fc38fbf868b1bf2bf4b947877f63b3b3a6a917bfb61070`. Regenerate it with `python run.py regenerate-data`; automated checks compare exact bytes. The raw pipeline validates schema, OHLC bounds, ordered unique grid timestamps and nominal availability before calculating anything.

`bbp_system.synthetic.engineered_rule_fixture()` is a separate authored feature case at an arbitrary 2026-01-01 origin. Indicators/pivots are stipulated to demonstrate candidate confirmation, acceptance, cost rejection and a losing cash bridge. **They are intentionally not computed from the illustration candles.** Do not combine these observations with the raw pipeline or market sample counts.

No account/trade records, raw broker data, machine reports, device notes, source screenshots or private Git objects are distributed. The prior private historical report is summarized only as recorded context in EVIDENCE.md. Exact configuration and source-code identities accompany every generated JSON report.
