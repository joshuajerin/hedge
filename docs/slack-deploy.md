# Slack deployment

Hedge has seven distinct Slack bot profiles. The **Hedge CIO** app is the only
inbound app: it runs Socket Mode, receives `app_mention`, and hands the source
thread to the CIO. The six specialist apps are outbound-only thread-reply
identities. They do not receive events or run Socket Mode.

Read [`slack-multi-profile.md`](slack-multi-profile.md) before provisioning. It
is the authoritative role-to-app/token-variable contract, correlation policy,
and administrator checklist. It deliberately contains variable names only, not
app IDs or credential values.

## What each app may do

| Profile | Inbound events / Socket Mode | OAuth bot scopes | App-level token |
| --- | --- | --- | --- |
| Hedge CIO | `app_mention` / enabled | `app_mentions:read`, `channels:history`, `chat:write`, `reactions:write` | yes, `connections:write` only |
| Each of six specialists | none / disabled | `chat:write` only | none |

The CIO bridge calls `conversations.replies` and adds lifecycle reactions, so
its `channels:history` and `reactions:write` scopes are necessary. Hedge is
installed in the intended public channel; no `chat:write.public`, private-group,
DM, or broad read scope is granted. Specialist apps need only `chat:write` to
reply after being invited to that channel.

## Install and credential rotation

Follow the seven-app, fresh-credential procedure in
[`slack-multi-profile.md`](slack-multi-profile.md#required-slack-administrator-procedure).
In summary: import each manifest as a new app, review the exact scopes, enable
Socket Mode and make a new `connections:write` app token for **Hedge CIO only**,
install/reinstall, invite each bot, revoke old credentials, and store newly
issued values only in the approved secret manager. Do not migrate an old shared
app's token into a role profile.

The local bridge reads only `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`; both map to
Hedge CIO in [`../slack/agent-bots.yaml`](../slack/agent-bots.yaml). Each
Brainbase agent receives only its own profile's Bot User OAuth token through its
secure connection setup. Do not supply a specialist app token because none
exists. Socket Mode needs no public request URL and the local bridge does not
need a Slack signing secret.

## Operating flow

1. A member mentions `@Hedge` in the invited public channel.
2. The CIO Socket Mode bridge reads the originating thread and creates the CIO
   task. No specialist app can receive this event.
3. The CIO preserves or derives the correlation envelope and delegates only
   needed work. A specialist can post only if its exact role is authorized for
   that existing thread; otherwise it returns its result to orchestration.
4. The CIO provides research context. Risk Reviewer is the sole role that may
   issue a final `hedge.decision.v1` decision or final `HOLD`; other roles only
   relay its attributed result.
5. The retained IBKR adapter remains disabled. No Slack or Brainbase profile
   can submit an order.

Use the final test in the multi-profile guide after every manifest, scope, or
credential rotation change.
