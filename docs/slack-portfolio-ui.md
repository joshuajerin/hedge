# Slack paper-portfolio UI

`hedge.slack_ui` builds **read-only Block Kit** for Hedge's paper portfolio. It
never sends Slack requests. It does not accept funds, create a payment, place an
order, or trigger trading.

## Input contract

All input models are frozen dataclasses. They reject control characters,
untrimmed names, bad currency codes, non-integer cents, floats, booleans, and
out-of-range basis points.

- `FundDashboard(fund_name, nav_cents, return_bps, cash_cents, currency)`
- `PositionSnapshot(ticker, display_name, market_value_cents, pnl_cents,
  allocation_bps, risk, currency)`
- `MemberStake(member_name, virtual_contribution_cents, ownership_bps,
  current_value_cents, currency)`

Money is always an integer number of cents. `return_bps` and `pnl_cents` may be
negative. Allocation and ownership are `0` through `10_000` basis points.
`format_cents` and `format_basis_points` use these same rules. User-controlled
display fields are validated and placed only in `plain_text` Block Kit objects;
text placed in `mrkdwn` must use `escape_slack_text`.

## Builder API

Each builder returns `list[dict[str, Any]]` suitable for Slack `blocks`:

```python
from hedge.slack_ui import (
    FundDashboard, MemberStake, PositionSnapshot, SlackThreadContext,
    build_fund_dashboard_card, build_member_stake_card, build_position_card,
)

context = SlackThreadContext(
    workspace_id=correlation.workspace_id,
    channel_id=correlation.channel_id,
    thread_ts=correlation.root_thread_ts,
    run_id=correlation.run_id,
)

dashboard_blocks = build_fund_dashboard_card(
    FundDashboard("Hedge Paper Fund", 125_000, 245, 15_000, "USD"),
    context,
    action_signing_key,
)
position_blocks = build_position_card(
    PositionSnapshot("AAPL", "Apple Inc.", 30_000, 1_250, 2_400, "Moderate", "USD"),
    context,
    action_signing_key,
)
stake_blocks = build_member_stake_card(
    MemberStake("Avery", 20_000, 1_500, 22_500, "USD"),
    context,
    action_signing_key,
)
```

`PortfolioDashboard` is an alias for `FundDashboard`, and
`build_portfolio_dashboard_card` is an alias for `build_fund_dashboard_card`.
Each card has NAV/return/cash, position P&L/allocation/risk, or member virtual
contribution/ownership/current value as applicable. Each ends with the fixed
paper-only disclosure.

## Exact bridge integration API (later)

This change does **not** modify `hedge.slack_bridge`. A later bridge change may
post a selected builder result only after it reconstructs the immutable context
from its durable `ThreadCorrelation` record:

```python
client.chat_postMessage(
    channel=context.channel_id,
    thread_ts=context.thread_ts,
    text="Paper portfolio snapshot.",
    blocks=build_fund_dashboard_card(dashboard, context, action_signing_key),
)
```

Every card has exactly these navigation buttons and signed action values:

| Slack action ID | Required `expected_action` |
| --- | --- |
| `hedge.portfolio.portfolio` | `"portfolio"` |
| `hedge.portfolio.my_stake` | `"my_stake"` |
| `hedge.portfolio.positions` | `"positions"` |

For any handler, first derive `expected_context` only from the durable run and
the actual signed Slack envelope. Then verify the matching button value:

```python
validate_action_metadata(
    body["actions"][0]["value"],
    signing_key=action_signing_key,
    expected_context=context,  # reconstructed from ThreadCorrelation
    expected_action="positions",
)
```

`SlackThreadContext` is frozen and binds workspace, channel, root thread, and
run ID. The HMAC token covers all of those values and the navigation target.
Never use a channel, thread timestamp, run ID, or destination supplied by a
button, modal, model output, or client-side state. Slack request-signature
verification remains the bridge's responsibility. Keep the HMAC key in bridge
runtime configuration and require at least 16 bytes.

A navigation handler may only load a reviewed paper snapshot and post/update a
card in `context.channel_id` / `context.thread_ts`. It must not expose
contribution, transfer, buy, sell, payment, broker, or trade controls.
