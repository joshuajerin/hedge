# Durable Slack result delivery

Hedge has one inbound Slack route: the **Hedge CIO** Socket Mode app receives
`app_mention`. The six specialist bots are outbound-only. Do not subscribe a
specialist app to `app_mention` or use a specialist token in the bridge.

## Durable correlation

The bridge generates an opaque `run_id` for every accepted CIO mention. It
stores this tuple in the SQLite database before it creates the Brainbase task:

```text
run_id + workspace_id + channel_id + root_thread_ts
```

The task payload includes the same tuple under `delivery`. A result integration
must read the stored tuple by `run_id`; it must not trust a channel or thread
returned by a model. The delivery service rejects any supplied tuple that is not
an exact match. The root thread timestamp is always used as Slack `thread_ts`.

Set `HEDGE_SLACK_STATE_DB` to a durable local path shared by the Socket Mode
bridge and the result adapter. If it is unset, the bridge uses
`~/.hedge/slack-state.sqlite3`. The process account needs read/write access to
that directory. Back up this SQLite file with the same care as operational
metadata, but it contains no Slack token or message body.

The database records inbound event IDs and outbound delivery IDs. Duplicate
events remain duplicates after restart. A delivery is reserved before Slack I/O.
If the sender errors or times out, Hedge records `failed` and does not retry the
same delivery ID automatically. This avoids a second post when Slack may have
accepted the first request. An operator or a reviewed workflow must create a
new delivery ID to retry.

## Root-thread research lifecycle

The inbound CIO bridge supplies a timezone-aware ISO-8601 `deadline_at` using
its existing call (inside the research `submit()` path):

```python
run_recorded = state.record_run(
    correlation,
    event_id=event_id,
    requester=request.user_id,
    deadline_at=deadline_at,
)
```

This call is the **admission and expiration integration point**:
it atomically changes the current `pending` or `started` run to `expired` if its
persisted deadline has passed, then admits the fresh, distinct run ID in the
same SQLite transaction. The bridge does **not** need a background poller or a
second call for this transition. A separate operator or worker may call
`state.expire_overdue_runs()` to materialize expired status before another
mention; it returns the newly expired run IDs and is safe to repeat.

An active run with a future deadline blocks another run in the same root
thread. A missing or malformed stored deadline also blocks automatically
replacing that run; an operator must explicitly review it and mark it `failed`
or `completed` before admitting a new run. New nonempty deadlines must carry a
timezone offset; naive or malformed timestamps are rejected. Missing deadlines
remain supported for legacy/local callers but never expire automatically.
Once expired, a run cannot be marked started/completed/failed again. A run ID is
never admitted again, even after its thread is reused; the next mention needs a
new event ID and run ID. The existing inbound event claim still prevents Slack
retries from starting or reposting work after restart. Expiration does **not**
retry Brainbase tasks or outbound Slack deliveries, or prove that a remote
research task has stopped; late results must be handled by the consuming
workflow's explicit policy.

## Immediate Tier-0 replies

Before it creates a Brainbase task, the inbound bridge checks each accepted
mention against a deliberately small, deterministic Tier-0 list. A complete
basic greeting or thank-you, a basic “what is Hedge/how does it work” question,
or a basic non-actionable investing-club/virtual-pool education question gets a
fixed concise reply immediately with Slack `say` in the original root thread.
No Tier-0 reply says that Hedge is coordinating, delegating, or will execute
anything.

The check is fail-closed. Any message containing a ticker marker or investment
work term—including research, analysis, portfolio, backtest, trade, proposal,
or risk—is Tier-1 and continues through the normal Brainbase CIO task path.
Other messages also use that path. The inbound event ID is claimed before both
the reaction and the Tier-0 reply, so a Socket Mode retry cannot post a second
reply. The existing `:eyes:` reaction remains on all accepted events; Tier-1
also keeps its existing `:brain:` reaction after task creation.

## Virtual budget-split launch

Before the regular Tier-0 and CIO paths, an explicit non-investment mention of
`budget`, `split`, `contribution`, or `pool` launches the local virtual planner.
For example, `@Hedge create a $500 budget split among people` posts the launch
card in the original root thread and creates **no** Brainbase task. Investment
work terms still win, so research, trading, backtest, and risk requests retain
the CIO route.

The launch card, modal preview, and confirm/cancel result use only the original
root thread. They never emit a CIO classification, HOLD status, coordinator
message, payment collection, or trade action.

`HEDGE_SLACK_ACTION_SECRET` is the only UI HMAC key. It must contain at least
16 UTF-8 bytes and is kept only in bridge process configuration. When it is
missing or too short, the bridge posts a concise planner-unavailable message in
the root thread and does not create a Brainbase task or an unsigned card.

## Result-adapter contract

Create `SlackResultDelivery` with the shared `SlackState` and an injected
`SlackSender`. The adapter is responsible for local Slack API I/O and can map
the supplied fixed `SlackProfile.token_env` to its own configured client. It
must never accept a raw token from a model or put a token in a result payload,
database, log, command line, or error response.

The only allowed role names are:

- `cio` — `SLACK_BOT_TOKEN`
- `market_scout` — `HEDGE_MARKET_SCOUT_BOT_TOKEN`
- `trend_analyst` — `HEDGE_TREND_ANALYST_BOT_TOKEN`
- `news_analyst` — `HEDGE_NEWS_ANALYST_BOT_TOKEN`
- `portfolio_manager` — `HEDGE_PORTFOLIO_MANAGER_BOT_TOKEN`
- `backtester` — `HEDGE_BACKTESTER_BOT_TOKEN`
- `risk_reviewer` — `HEDGE_RISK_REVIEWER_BOT_TOKEN`

These are environment-variable **names**, not values. Configure each matching
Slack app with `chat:write`, install it in the same workspace, and invite it to
the target channel. The CIO app also needs the Socket Mode and inbound scopes
listed in [`../slack/manifest.yaml`](../slack/manifest.yaml).

A sender receives only a fixed profile, canonical channel ID, canonical root
thread timestamp, text, and delivery ID. It should post with `chat.postMessage`
to that channel and `thread_ts`. It must not use a requested profile, channel,
or token selected by a Brainbase model. It should provide its own network
timeouts and should raise on any uncertain or non-successful response so the
delivery service can fail closed.

## Deployment order

1. Install the CIO and six specialist apps in the same Slack workspace, invite
   all seven to the delivery channels, and provide the environment variable
   names above through the local secret manager.
2. Set `HEDGE_SLACK_STATE_DB` to a persistent, process-owned path. Do not use
   an ephemeral container filesystem if the bridge can restart.
3. Deploy the CIO Socket Mode bridge. It is the only component allowed to call
   `build_app()` and receive inbound Slack events.
4. Deploy the separate result adapter with access to the same state database.
   It must construct `ThreadCorrelation` from persisted state, then call
   `SlackResultDelivery.deliver()` using a unique durable delivery ID.
5. Test one result for each role in a non-production channel. Verify the right
   bot profile posts in the original root thread, then restart the adapter and
   replay the same delivery ID to verify it does not post twice.

No bridge change creates a Brainbase callback by itself. The deployed Brainbase
workflow must call the result adapter with a role, `run_id`, result text, and
delivery ID. The adapter supplies all Slack destination and identity details
from its fixed configuration and durable state.
