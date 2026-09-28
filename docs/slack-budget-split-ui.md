# Slack budget-split UI

`hedge.slack_ui` is a dependency-free Block Kit builder for a **virtual planning
and contribution** split. It is not a payment collection flow. It does not send
money, charge cards, or execute trades.

## Public APIs

- `SlackThreadContext(workspace_id, channel_id, thread_ts, run_id)` validates
  the four opaque canonical destination identifiers.
- `build_budget_split_launch_card(context, signing_key)` returns in-thread
  launch `blocks`.
- `build_budget_split_modal(context, signing_key)` returns a Slack modal `view`.
- `parse_budget_split_form(values)` validates direct form values and returns an
  immutable `BudgetSplit`.
- `allocate_budget(split)` returns immutable `BudgetSplitPreview` and uses
  deterministic, cents-exact allocation.
- `build_allocation_preview_blocks(preview, context, signing_key)` returns the
  confirm/cancel review blocks.
- `build_confirm_result_blocks(preview)` and
  `build_cancel_result_blocks(group_name)` return safe final-result blocks.
- `sign_action_metadata(...)` and `validate_action_metadata(...)` create and
  verify action tokens. The token binds action, workspace, channel, root thread,
  and run ID.

`BudgetMember`, `BudgetSplit`, `Allocation`, and `BudgetSplitPreview` are frozen
dataclasses. `BudgetSplit.members` is normalized to a tuple. `to_dict()` methods
return serialization-safe plain dictionaries and lists.

## Allocation rules

All totals must be positive finite values with no more than two decimal places.
The preview allocates integer cents, so allocation cents always sum to total
cents exactly.

- With no weights, extra cents go to members in input order.
- With one or more weights, omitted weights mean `1`. The module floors exact
  proportional cents, then gives remaining cents to the largest fractional
  remainders. Equal remainders use input order.

There can be 1–20 unique members. Group/member text is trimmed, control-free,
and capped. Direct form strings are capped at 3,000 characters and numeric
strings at 64 characters. Currency must be three uppercase letters. UI-rendered
user text is escaped for Slack mrkdwn.

## Active bridge integration

`hedge.slack_bridge.build_app()` registers the card launch, open action, modal
submission, confirm action, and cancel action. A non-investment mention with an
explicit `budget`, `split`, `contribution`, or `pool` term takes this local path
before Brainbase. The launch card is posted with the incoming root `thread_ts`.
It is virtual planning only; it does not create a CIO task, payment, or trade.

`HEDGE_SLACK_ACTION_SECRET` is the only UI HMAC key. It must be at least 16
bytes. If it is absent or too short, the root thread gets an unavailable message
instead of an unsigned button. The value is not logged or persisted.

Do not trust an interactive payload merely because it contains a valid-looking
channel, user, `private_metadata`, or button value. Slack Bolt verifies the
Slack request signature. The bridge also retains the canonical
`ThreadCorrelation` for the inbound run in `SlackState`.

At launch, construct the UI context only from that trusted record:

```python
context = SlackThreadContext(
    workspace_id=correlation.workspace_id,
    channel_id=correlation.channel_id,
    thread_ts=correlation.root_thread_ts,
    run_id=correlation.run_id,
)
blocks = build_budget_split_launch_card(context, action_signing_key)
```

For `hedge.budget_split.open`, validate the button `value` before opening the
modal. Resolve `expected_context` from the durable run record, not from the
button or model output:

```python
validate_action_metadata(
    body["actions"][0]["value"],
    signing_key=action_signing_key,
    expected_context=context,
    expected_action="launch",
)
ack()
client.views_open(trigger_id=body["trigger_id"], view=build_budget_split_modal(context, action_signing_key))
```

For `hedge.budget_split.modal`, validate `view["private_metadata"]` with
`expected_action="modal"`. Flatten only the five known `block_id`/`action_id`
pairs into direct strings and pass those direct strings to `parse_budget_split_form`:

```python
values = {
    block_id: view["state"]["values"][block_id]["value"]["value"]
    for block_id in ("total", "currency", "group_name", "member_names", "weights")
}
split = parse_budget_split_form(values)
preview = allocate_budget(split)
```

The handler should return field errors on `ValueError`. It must keep a
server-side preview keyed to the already canonical run (or recompute only from
validated modal values). It must not accept a destination, run ID, or member
list from model output.

On `hedge.budget_split.confirm` or `.cancel`, resolve the canonical correlation
again from `SlackState.correlation_for_run(run_id)`, reconstruct `context`, and
call `validate_action_metadata` using the actual Slack team/channel/root thread
as the expected context. Send or update only that resolved thread. Confirmation
may record a virtual plan in a separate reviewed store; it must never invoke a
payment provider, broker, trade route, or model-selected Slack destination.

Keep `action_signing_key` in the bridge runtime configuration, not in logs,
messages, model prompts, or persistent UI state. Use at least 16 random bytes;
a separate rotation-aware bridge key manager is recommended for production.
