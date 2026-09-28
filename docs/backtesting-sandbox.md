# Paper-only backtesting sandbox

`hedge.backtest` simulates reviewed Hedge decisions against **caller-supplied,
local OHLCV bars**. It is a research tool. It is not a broker adapter and never
fetches prices, reads broker credentials, writes to the Hedge store, or submits
an order.

## Safety boundary

- The engine imports only `hedge.contracts` and `hedge.policy`, plus the Python
  standard library. It does not import `hedge.broker`, `market_data`, or
  `store`.
- `PaperBacktester` refuses a `PaperPolicy` whose `account_mode` is not
  `"paper"`.
- The standalone runner reads only its three explicit local files. It does not
  read environment variables or accept broker configuration.
- Inputs are immutable value objects. Results are in-memory immutable tuples or
  read-only mappings. Nothing is sent anywhere.

This separation is intentional: the sandbox has no integration point with the
current disabled IBKR adapter. Any future integration must remain a separate,
reviewed export/import step; do not connect this runner to broker credentials or
an order-submission API.

## Inputs and execution model

Create `BacktestBar` (also exported as `OHLCVBar`) values with a ticker,
ISO-8601 date/time, open, high, low, close, and optional volume. Values are
normalized to UTC and `Decimal`; duplicate `(symbol, timestamp)` bars and
invalid OHLC ranges are rejected. Bar/input order does not affect output.

Schedule validated `TradeIntent` values with `ScheduledIntent`, or validated
whole `Decision` values with `ScheduledDecision`. `run_decisions` checks the
complete Decision identity and decision-wide `PaperPolicy` limits. `run` checks
low-level intents and their per-order limits. `run_strategy` accepts a local
pure callback that returns intents or decisions; each emitted `Decision` is
validated as a complete decision, and duplicate IDs among emitted decisions
reject the whole run before any paper fill. It never runs supplied code from a file or agent
response.

A signal at time `T` may first fill on a bar for its symbol **strictly after**
`T`. `fill_delay_bars=1` picks that next bar. Each intent is a DAY order:

- `MKT` fills at the eligible bar's open, with adverse `slippage_bps`.
- `LMT` fills only when that eligible bar crosses its limit. It fills exactly at
  the limit: the model deliberately does not claim favorable gap improvement.
  Slippage is capped so it cannot violate the limit.
- `commission_per_order` is applied to every simulated fill.
- Buys that would take cash below zero are rejected. `SELL` intents that exceed
  the held position are rejected by default. Set `PaperPolicy(allow_short_sales=True)`
  only for an explicit short simulation.
- No later eligible bar and a missed limit are recorded as rejected orders.

The `BacktestResult` contains immutable fills/rejections (`ledger`), the
close-marked `equity_curve`, positions, and metrics: `total_return`, negative
fractional `max_drawdown`, `win_rate`, completed/winning sell counts, and cash
figures. A completed trade is a covered SELL fill. Its win comparison includes
allocated buy cost and the sell commission. Win rate is `0` until a covered sell
occurs.

## Python use

```python
from hedge.backtest import BacktestBar, BacktestConfig, BacktestEngine, ScheduledDecision
from hedge.contracts import Decision

bars = [
    BacktestBar("AAPL", "2025-01-02T00:00:00Z", 100, 102, 99, 101, 1_000_000),
    BacktestBar("AAPL", "2025-01-03T00:00:00Z", 101, 104, 100, 103, 1_100_000),
]
decision = Decision.from_dict(...)  # a reviewed hedge.decision.v1 document
result = BacktestEngine(bars, BacktestConfig(initial_cash="10000", slippage_bps="5")).run_decisions(
    [ScheduledDecision("2025-01-02T00:00:00Z", decision)]
)
print(result.metrics)
print(result.ledger)
```

## File-only runner

The `sandbox/` directory is intentionally not packaged as a Hedge command. Run
it directly from this checkout:

```bash
.venv/bin/python sandbox/run_backtest.py \
  --config sandbox/backtest_config.example.json \
  --bars sandbox/bars.example.csv \
  --decisions sandbox/decisions.example.json \
  --output /tmp/hedge-paper-result.json
```

The config must contain `"mode": "paper"` and only
`initial_cash`, `commission_per_order`, `slippage_bps`, and `fill_delay_bars`.
The bars CSV must have `timestamp,symbol,open,high,low,close` (and may have
`volume`). The decisions file has a `scheduled_decisions` list of
`{"timestamp": ..., "decision": <hedge.decision.v1>}` entries. Output is local
JSON containing paper metrics, positions, ledger, and equity curve.
