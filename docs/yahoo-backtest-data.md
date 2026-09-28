# Yahoo daily bars for reproducible paper backtests

`hedge.yahoo_history` is an **optional, read-only import boundary**. It converts
Yahoo Finance daily OHLCV into `BacktestBar`, stores a local JSON snapshot when
requested, and passes a reviewed `ScheduledDecision` collection to the isolated
`PaperBacktester`. It does **not** route orders, contact a broker, read credentials,
or call the live trading system.

Install Yahoo support only if you intend to fetch: `uv sync --extra yahoo`.
Tests inject a fetcher and never contact Yahoo.

```python
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from hedge.backtest import BacktestConfig, ScheduledDecision
from hedge.yahoo_history import load_history, run_yahoo_paper_backtest

history = load_history(
    ["AAPL", "MSFT"], start=date(2024, 1, 2), end=date(2024, 2, 1),
    as_of=datetime(2024, 2, 2, tzinfo=UTC),
    cache_dir=Path("./local-yahoo-cache"), cache_only=False,
)
# `reviewed` must be supplied by the caller, not generated from future bars.
# reviewed = [ScheduledDecision(signal_known_at, approved_decision), ...]
report = run_yahoo_paper_backtest(reviewed, history, config=BacktestConfig())
print(report.metrics, report.equity_points, history.digest)
# Subsequent offline run: same arguments, cache_only=True. No network is used.
```

`start` is inclusive and `end` is exclusive; both must be `date` values.
`as_of` and optional `now` must be aware datetimes. `as_of` must not precede
local midnight of the exclusive end or exceed `now`, so no incomplete daily
session is imported. It is a **range cutoff**, not evidence that current Yahoo
prices were available at that historical instant. For real point-in-time
research, use an archived, licensed point-in-time feed. If decisions use close
information, stamp each decision **after** that session's close: the paper
engine fills only on a later timestamp's bar. Daily bars are stamped at the
exchange-local session midnight converted to UTC; that timestamp is a session
label, **not** proof that the close was known at midnight. A decision stamped
at that midnight could improperly use its own closing price, so callers must
control decision provenance and timing.

A custom `fetcher(symbol: str, start: date, end: date)` may yield mappings
with keys `Date`, `Open`, `High`, `Low`, `Close`, `Volume`. An injected
fetcher must also supply an explicit `source="provider:dataset-version"` to
identify its provenance and avoid collisions with default Yahoo cache keys.
`Date` may be a date,
ISO date, or timezone-aware datetime. Naive datetimes are rejected. The default
fetcher uses `yfinance.Ticker(symbol).history(..., interval="1d",
auto_adjust=False, actions=False, repair=False)`. Prices are **unadjusted**;
corporate actions (dividends, splits, delistings, ticker changes), survivorship,
trading halts, and adjusted total returns are not modeled by the paper engine.
Specify `timezone` for the exchange; the default is `America/New_York` for US
equities. This importer handles daily OHLCV only, not intraday ticks.

The importer rejects missing columns/values, invalid or duplicate daily OHLCV,
out-of-range bars, empty symbols, and inconsistent dates across multiple symbols.
Pass `expected_sessions=[date(...), ...]` to detect omitted sessions even for
one symbol. It does not infer exchange holidays or market calendars; without
`expected_sessions`, a gap common to all symbols is not detectable. Inputs do
not silently fill or synthesize missing prices.

Cache filenames are SHA-256 hashes of symbols, date range, timezone, cutoff,
source, and schema. JSON records fetch time, full query, serialized bars and a
SHA-256 bar digest. Cache reads verify query, digest, content and freshness.
`cache_only=True` fails on cache miss or stale data, never falling back to
network. Default `max_age=7 days`; `max_age=None` explicitly allows old pinned
snapshots for offline reproduction. Keep the JSON and digest in a controlled
research archive if long-term results must be reproducible. The digest detects
accidental corruption, not malicious edits. Yahoo can revise prices between
fetches. Metadata source is a caller-supplied provenance label, not independent
verification. Never write private broker data to this cache.

`run_yahoo_paper_backtest(decisions, history, config=..., policy=...)` accepts
**already reviewed** `ScheduledDecision` values, enforces the paper policy and
decision contract, checks every intent symbol against loaded history, and
returns `YahooPaperRun`: `result` (`BacktestResult` with fills and ledger),
`metrics`, and chart-ready UTC ISO timestamp and decimal-string `equity_points`.
No strategy construction, Yahoo live quote API, or broker integration occurs.

Yahoo Finance/yfinance data is provided under the provider's current terms.
Confirm licensing and redistribution rights independently before storing or
sharing datasets. Yahoo quotes can be delayed, incomplete, revised, rate-limited
or unavailable. This adapter makes **no** real-time, accuracy, availability,
point-in-time, or trading-suitability guarantee. It is paper research only.
