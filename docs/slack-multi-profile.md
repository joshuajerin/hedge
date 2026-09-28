# Hedge Slack multi-profile contract

Hedge uses seven **separate Slack apps and bot users**. The role keys and token
variable names below are immutable deployment identifiers. They contain no app
IDs and no credentials. Do not rename a key, share a token between roles, or
substitute an app from another environment.

| Role key | Slack app / bot profile | Manifest | Delivery | Bot token variable | App token variable |
| --- | --- | --- | --- | --- | --- |
| `hedge_cio` | Hedge | [`../slack/manifest.yaml`](../slack/manifest.yaml) | inbound Socket Mode and authorized thread replies | `SLACK_BOT_TOKEN` | `SLACK_APP_TOKEN` |
| `market_scout` | Market Scout | [`../slack/agent-manifests/market-scout.yaml`](../slack/agent-manifests/market-scout.yaml) | authorized outbound thread replies only | `HEDGE_MARKET_SCOUT_BOT_TOKEN` | none |
| `trend_analyst` | Trend Analyst | [`../slack/agent-manifests/trend-analyst.yaml`](../slack/agent-manifests/trend-analyst.yaml) | authorized outbound thread replies only | `HEDGE_TREND_ANALYST_BOT_TOKEN` | none |
| `news_analyst` | News Analyst | [`../slack/agent-manifests/news-analyst.yaml`](../slack/agent-manifests/news-analyst.yaml) | authorized outbound thread replies only | `HEDGE_NEWS_ANALYST_BOT_TOKEN` | none |
| `portfolio_manager` | Portfolio Manager | [`../slack/agent-manifests/portfolio-manager.yaml`](../slack/agent-manifests/portfolio-manager.yaml) | authorized outbound thread replies only | `HEDGE_PORTFOLIO_MANAGER_BOT_TOKEN` | none |
| `backtester` | Backtester | [`../slack/agent-manifests/backtester.yaml`](../slack/agent-manifests/backtester.yaml) | authorized outbound thread replies only | `HEDGE_BACKTESTER_BOT_TOKEN` | none |
| `risk_reviewer` | Risk Reviewer | [`../slack/agent-manifests/risk-reviewer.yaml`](../slack/agent-manifests/risk-reviewer.yaml) | authorized outbound thread replies only | `HEDGE_RISK_REVIEWER_BOT_TOKEN` | none |

[`../slack/agent-bots.yaml`](../slack/agent-bots.yaml) is the machine-readable
copy of this contract. `null` app-token entries are intentional: specialist
apps do not run Socket Mode and must not receive an app-level token.

## Authorization and correlation contract

Every orchestration hand-off carries this envelope unchanged:

```yaml
correlation_id: slack:<channel_id>:<thread_ts>:<message_ts>
slack_channel_id: <originating-channel-id>
slack_thread_ts: <originating-root-thread-timestamp>
slack_message_ts: <originating-message-timestamp>
authorized_slack_role: <one-role-key>
reply_in_thread: true
```

- The CIO derives `correlation_id` when the inbound bridge did not provide one.
  For a bridge-originated request it sets `authorized_slack_role: hedge_cio`.
- A specialist can receive its own role in `authorized_slack_role` only in an
  explicit delegated envelope. This is not implied by source-channel context.
- A bot posts only when all fields are present, `reply_in_thread` is true, and
  its immutable role key exactly matches `authorized_slack_role`. It posts only
  in the specified existing thread. It never starts a channel message or DM.
- A role without authorization returns its result to Brainbase orchestration,
  not Slack. The CIO may relay a specialist result under the Hedge profile.
- Risk Reviewer alone can make a final `hedge.decision.v1` decision or final
  `HOLD`. Other profiles may only attribute and relay its exact outcome.

## Required Slack administrator procedure

Perform these steps in the target workspace. Do them once per listed manifest;
do not update or reuse an old shared app.

1. In **Slack API → Your Apps**, create a **new app from a manifest** and import
   the role's checked-in manifest. Create seven different apps. Do not enter or
   commit an app ID, client secret, signing secret, bot token, refresh token, or
   app token in this repository.
2. Review **OAuth & Permissions** before installation. The six specialist apps
   must have exactly `chat:write`. They must have no Event Subscriptions, no
   Socket Mode, no Request URL, no app-level token, and no read, reaction, DM,
   or `chat:write.public` scope. The Hedge CIO app needs only
   `app_mentions:read`, `channels:history`, `chat:write`, and `reactions:write`.
   `channels:history` and `reactions:write` are required by the checked-in
   local bridge to read the originating public thread and add its two lifecycle
   reactions.
3. For **Hedge CIO only**, enable Socket Mode and create a new app-level token
   with only `connections:write`. Confirm that Event Subscriptions has only the
   `app_mention` bot event. Do not configure Socket Mode or an app-level token
   on a specialist app. Socket Mode needs no public Request URL and no signing
   secret in the local bridge.
4. Enable token rotation for each app. Install or reinstall that app to the
   target workspace after the final scopes are approved. Invite each distinct
   bot user to the intended **public** Hedge channel; `chat:write.public` is
   intentionally not granted. The CIO is the only profile that must receive
   mentions.
5. Rotate rather than migrate credentials: revoke every old token for a
   replaced/shared app, then collect the newly issued Bot User OAuth token for
   each of the seven fresh installs. For the CIO also create a newly issued
   `connections:write` app-level token after Socket Mode is enabled. Store only
   the current token values in the approved secret manager under the variable
   names in the table. No specialist has an app token. Never put a value in
   YAML, documentation examples, shell history, or command-line arguments.
6. Configure the local Socket Mode bridge with only the CIO's
   `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`. Configure each Brainbase agent's
   outbound Slack connection with its matching role's Bot User OAuth token.
   The CIO's bot token may be used by the bridge and the CIO connection because
   both are the same `hedge_cio` profile; it must not be used by another role.
7. Verify with a non-production test thread: mention only `@Hedge`, confirm the
   CIO bridge handles it, then send explicitly authorized role envelopes one at
   a time and confirm each specialist replies only in that same thread. Confirm
   that a specialist mention, DM, new channel post, or missing/mismatched role
   authorization produces no Slack post. Finally verify that only Risk Reviewer
   can emit a final decision or final `HOLD`.

If any manifest scope or delivery setting differs after import, fix it in Slack,
reinstall that one app, revoke its previous credentials, and repeat the test.
