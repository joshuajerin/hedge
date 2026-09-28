# Hedge paper flows and charts

Hedge can turn reviewed `hedge.decision.v1` documents and read-only paper
account exports into a local report. This is an observability layer only. It
has no broker import, no environment-variable reads, no network request, and
no order-submission API.

The source modules use only the Python standard library:

- `hedge.flows` builds the serializable `hedge.dashboard.v1` report.
- `hedge.dashboard` renders that report as one self-contained HTML file.

## Inputs

Call `build_report` with normalized values. Inputs may be lists of mappings;
`decisions` also accepts validated `hedge.contracts.Decision` objects.

| Input | Required fields | Optional fields |
| --- | --- | --- |
| `decisions` | `decision_id`, `pool_id`, `mandate_version`, `created_at`, `intents` | `status` / `review_status`, `reviewed_at` / `updated_at` |
| decision intent | `symbol`, `side` (`BUY` or `SELL`), `quantity`, `order_type` (`MKT` or `LMT`) | `limit_price` |
| contribution event | `cents` (or `amount_cents`) | `created_at`/`at`, `pool_id`, `member_id` |
| contribution snapshot | `{pool_id: {member_id: cents}}` | — |
| equity snapshot | `equity` (USD) or `equity_cents` | `as_of` / `at`, `benchmark` (display-only index value) |
| fund summary | `nav` (USD) or `nav_cents` | `cash`/`cash_cents`, `return_periods`, `benchmark` |
| benchmark | `name`, `return_periods` | may also be nested under `fund.benchmark` |
| paper position | `symbol`, `quantity`, `entry_price`/`avg_cost`, `current_price`/`mark_price` | cents alternatives, `market_value`, `unrealized_pnl`, `realized_pnl`, `allocation_pct`, `thesis_status`, `risk_status`, `as_of` |
| capital account | `member_id`, `contributed_cents`, `nav_cents` | `display_name`, `withdrawn_cents`, `realized_pnl_cents`, `unrealized_pnl_cents`, `total_pnl_cents`, `allocation_pct`, `as_of` |
| research stage | `agent` or `stage`, `status` | `order`, `started_at`, `completed_at`/`updated_at` |

Timestamps must be timezone-aware ISO-8601 strings, for example
`2025-01-02T12:00:00Z`. Invalid or missing timestamps remain visible as
unknown rather than being silently assigned the machine's local time.

Only known display fields are copied into the report. Extra input fields, such
as raw agent output or credentials, are intentionally excluded.

## Build a report

```python
from datetime import UTC, datetime
from hedge.dashboard import write_dashboard
from hedge.flows import build_report

report = build_report(
    generated_at=datetime.now(UTC),
    decisions=[reviewed_decision],
    contributions=virtual_contribution_events,
    equity=paper_equity_snapshots,
    positions=paper_positions,
    fund=normalized_fund_summary,
    benchmark=normalized_benchmark,  # optional if fund.benchmark is supplied
    capital_accounts=normalized_member_accounts,
    research_stages=agent_stage_events,
    stale_after_seconds=15 * 60,
    max_drawdown_pct=10,
)
write_dashboard("dashboard/hedge-report.html", report)
```

Open `dashboard/hedge-report.html` locally. It embeds the report JSON and
vanilla JS/CSS in a single file. It does not load a CDN, analytics, a broker,
or any remote data source.

## What the report shows

- **Latency and staleness:** age and fresh/stale/unknown state for equity,
  decision flow, and research flow. `stale_after_seconds` is explicit.
- **Fund summary:** NAV, cash, period returns, benchmark returns, and the
  period-by-period benchmark delta. Inputs are display-only and are not prices
  fetched by Hedge.
- **Equity curve, P&L, and drawdown:** equity snapshots produce timestamped
  paper equity, cumulative paper P&L, and high-water-mark drawdown series.
  `charts.equity_curve` is a public alias of `charts.equity` for chart clients.
  P&L starts at the first supplied snapshot.
- **Decision/order flow:** whitelisted decision events and cumulative counts of
  decisions and proposed intents. An intent is a proposal, not an order sent.
- **Virtual pool:** cumulative contribution chart and member/pool cents
  breakdowns. These are virtual balances, not transfers or account balances.
- **Risk:** `blocked` when any research stage is blocked, failed, or rejected;
  otherwise `warning` for a drawdown limit breach or stale feed; otherwise
  `ok`. Reasons are included in `summary.risk.reasons`.
- **Capital accounts:** caller-supplied, normalized, virtual per-member
  contributed/withdrawn capital, allocated NAV, realized/unrealized P&L, and
  allocation. This report does not allocate NAV or write a member ledger.
- **Research and position cards:** ordered agent-stage timeline and cards with
  entry/current price, market value, unrealized/realized P&L, allocation, and
  thesis/risk status.

## Integration requirements

1. Validate agent output with `Decision.from_dict` and the local `PaperPolicy`
   before treating it as a decision input. The dashboard does not authorize or
   validate a trade.
2. Export virtual contributions from the local store into one of the supported
   input shapes. Current `LocalStore` intentionally remains the source of
   truth; this layer does not write to it.
3. Supply timestamped paper-equity and position exports from a read-only
   recorder. Do not connect this report layer to `broker.submit`.
4. Feed Brainbase lifecycle events into `research_stages`, including the
   terminal Risk Reviewer status. A blocked or rejected terminal stage will be
   visible as a blocked risk state.
5. Regenerate the HTML after data changes. Serve it only as a local static
   artifact unless access controls are added outside this package.

`build_report` returns plain dict/list/string/number/bool values, so callers may
also serialize it using `json.dumps(report, allow_nan=False)` or deliver it to a
different static renderer.

## Fund and capital-account contract

`fund.return_periods` and `benchmark.return_periods` are mappings from a caller
chosen label (for example `"1D"`, `"1M"`, or `"YTD"`) to a percentage. A value
of `1.25` means **1.25%**, not `0.0125`. The report emits rows with
`return_pct`, `benchmark_return_pct`, and `benchmark_delta_pct`; a delta is
present only when both inputs exist.

Use integer cents for all `*_cents` fields. `Decimal` is accepted at the input
boundary and is normalized to JSON-safe `int` cents or finite JSON numbers in
the output. The dashboard does not infer member ownership, move funds, or call
a broker/payment service. Supply precomputed capital-account rows from the
read-only accounting export.

## Public APIs

- `hedge.flows.build_report(...)` returns the complete
  `hedge.dashboard.v1` plain-data report. New dashboard inputs are `fund`,
  `benchmark`, and `capital_accounts`.
- `hedge.flows.fund_summary(...)`, `equity_flow(...)`,
  `position_snapshot(...)`, and `capital_account_summary(...)` are public
  normalized-data helpers for callers that render their own views.
- `hedge.dashboard.render_dashboard(report, title=...)` returns safe,
  self-contained HTML. `write_dashboard(path, report, title=...)` writes the
  same HTML and returns the `Path`.

The renderer JSON-encodes script data with `<`, `>`, and `&` escaped. Dynamic
values inserted by the dashboard JavaScript are HTML-escaped before insertion.
