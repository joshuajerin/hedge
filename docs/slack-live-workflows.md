# Hedge CIO local Slack workflows (paper only)

The Socket Mode bridge keeps its existing greeting, virtual budget planner and Brainbase research routes. It handles only exact `portfolio`, `my stake`, and `positions` queries locally. A longer investment question still routes to CIO research. Explicit `create virtual pool` is a separate workflow from the budget-split planner: confirming a budget preview does **not** create a pool.

## Required configuration

- `HEDGE_APPROVED_WORKSPACE_IDS`: comma-separated workspace IDs. Required and must include the identity returned by Slack `auth_test`.
- `HEDGE_APPROVED_CHANNEL_IDS`: comma-separated channel IDs, or `*` for all channels in approved workspaces where the bot receives events. Required. Empty or missing lists reject all inbound events and actions. `HEDGE_APPROVED_WORKSPACE_IDS` still restricts the workspace.
- `HEDGE_SLACK_ACTION_SECRET`: at least 16 bytes, used for thread-bound signed button metadata. Without it the interactive local workflows stay unavailable.
- `HEDGE_SLACK_STATE_DB`: durable thread/event correlation database (default `~/.hedge/slack-state.sqlite3`).
- `HEDGE_SLACK_FUND_DRAFT_DB`: durable, private pending confirmation database (default `~/.hedge/slack-fund-drafts.sqlite3`).
- `HEDGE_PAPER_FUND_DB`: durable paper-fund pool and capital ledger database (default `~/.hedge/paper-fund.sqlite3`). This must be a distinct SQLite file from the draft database.
- `HEDGE_PAPER_PORTFOLIO_DIR`: directory of separate per-pool paper-portfolio SQLite files (default `~/.hedge/paper-portfolios`). Pool IDs are validated before a file is selected.

Keep the same file paths and action signing key across bridge restarts. Back up the three state/fund databases and each per-pool paper-portfolio database as one operational set. Restrict file permissions to the bridge operator; these files contain Slack user IDs and virtual ledger records. Do not put credentials into any SQLite path or Slack message. This code does not deploy anything or configure Slack for you.

## Pool creation

Mention Hedge with an exact command:

`@Hedge create virtual pool Friends | USD 500.00 | mandate: Learn index funds | risk: low | members: <@U123>, <@U456>`

The last `members:` field is optional. Amounts require two decimal places, currency is a three-letter uppercase code, risk is `low`, `medium`, or `high`, and member IDs must be explicit Slack mentions. A review card lists the exact virtual starting balance, mandate, risk, and invited IDs. No pool exists until its creator clicks **Confirm virtual pool** in the same approved thread. The signed action is bound to the persisted Slack run, team, and channel; only the creator may confirm. A repeated click uses the same deterministic pool ID. The service records the creator's starting balance in its append-only virtual ledger and initializes that pool's separate paper-portfolio SQLite ledger with the same simulated starting cash. The confirmed pool is activated for virtual invitations. Invited IDs have zero capital and no membership until they explicitly join; a mere invitation does not grant portfolio visibility.

## Invited-member join

An explicitly invited Slack user can mention Hedge with:

`@Hedge join virtual pool pool-<id> | USD 25.00`

Use the exact created pool ID and its currency. The bridge verifies an active pool and a pending invitation bound to the requester's Slack user ID. It then posts a review card in the approved thread; no membership or virtual balance changes until **Confirm virtual join** is clicked by that same invitee. A deterministic join event ID makes button retries idempotent across restarts. After `PaperFundService` durably accepts the virtual contribution, the bridge verifies that exact contribution in the authoritative capital ledger before mirroring it into the per-pool paper cash ledger. It checks the full set of mirrored contribution event IDs and amounts before showing NAV. If a cross-file write is interrupted, NAV is unavailable until the same signed join confirmation is retried. No payment, deposit, or brokerage action exists.

If a partial multi-store write occurs, `PaperFundService` fails closed until the *identical* confirmation is retried. Its durable intent and event IDs prevent duplicate virtual credit. A message delivery interruption may still leave a created pool without a Slack confirmation post; inspect the local fund record rather than sending money or creating a second pool.

## Portfolio cards

`@Hedge portfolio`, `@Hedge my stake`, and `@Hedge positions` render member-scoped local Block Kit cards. Navigation buttons carry signed thread metadata. Membership is checked from persisted fund records on every request, including each navigation click. `portfolio` and `my stake` show authoritative virtual contribution history and exact capital ownership. A newly confirmed, cash-only paper account can show *simulated starting NAV* and paper value from its persisted starting cash; no market valuation is inferred.

For an existing portfolio, priced NAV, member value, and positions appear **only** when the per-pool paper ledger exists, all held symbols have persisted nonfuture quotes no older than five minutes, per-share basis is exact, and the paper-cash ledger contains exactly the same virtual contribution event IDs and amounts as the authoritative fund capital ledger. Each priced position shows the stored quote source and timestamp. If a join or an externally recorded virtual contribution changes capital without audited reconciliation to paper cash, cards show contributions but mark NAV/positions/current value unavailable. Missing, stale, or inconsistent paper holdings fail closed instead of using Slack text, dummy prices, or the original starting balance as a current quote. Members with no active pool membership see a clear empty state.


The bridge neither collects payments nor calls a bank or brokerage. Paper-fund snapshot calculations require caller-supplied, verified paper positions and prices; the Slack bridge does not accept Slack text as prices and calls `snapshot` only using member-authorized, persisted, fresh portfolio data. Research remains a separate Brainbase route. All commands and tests can be exercised with local fake Slack clients and SQLite temp files; no live Slack or Brainbase call is needed for those tests.
