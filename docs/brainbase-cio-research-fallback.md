# CIO research draft fallback

The Slack bridge acknowledges a newly claimed research mention in its original thread **before** the Brainbase task-create CLI runs. Duplicate event IDs do not send a second acknowledgment. This acknowledgment never authorizes paper or live execution.

When `brainbase task get --json` reports `success` for the registered top-level CIO task and the CLI has no typed terminal artifact, the monitor reads `brainbase task logs <id> --json --limit 501`. The installed CLI source emits `{ "items": [...] }` with raw event records (`type`, `ts`, `data`, optional lane fields). Assistant text is in `data.content`, a list of `{ "type": "text", "content": "..." }` blocks. This shape was checked from the installed CLI source without calling Brainbase. The task-get response supplies task ID, agent ID, status, and parent ID. The transcript does **not** authenticate a handoff, Risk approval, specialist report, or trading decision.

The fallback needs exactly one bounded top-level `assistant.message` after all tool events, matching the verified task and agent IDs, with no subagent or parent lane. It requires a final successful `idle` event and rejects extra utterances, partial chunks, wrong IDs, truncated transcripts, JSON/decision-like text, Slack destinations/mentions, credential-like text, instructions to redirect the reply, and executable/broker advice. A second task-get check must still match before the draft is reserved. Filter failures and non-success tasks keep the existing fixed unavailable/failure/timeout notice; raw transcript and evaluator text are never included in those notices. The posted reply is marked `CIO research draft (not risk-reviewed)` and warns that it is unverified and not a trading instruction.

The destination and CIO profile come from the trusted, persisted original thread correlation, not task text. SQLite reserves the response before the Slack send. A failed or uncertain send is **not** retried across restart; a delivery failure may mean Slack accepted the message. No broker API is invoked by this fallback.

## Limitations

* Brainbase CLI event records are untrusted; their task and agent ID fields can be copied or forged by an untrusted source. This is **not** authenticated research, attribution, specialist completion, or Risk approval. A typed artifact API is needed for those claims.
* This is a conservative text filter, not proof that arbitrary prose is safe or true. Unknown event shapes and content are rejected, and harmless drafts may be refused. The fallback may show a CIO status draft that says findings are pending.
* `--limit 501` detects more than 500 events; it does not give a transactional snapshot. A task can change after the final task-get check. Reservation gives at-most-once send attempts, not guaranteed delivery.
* Only the CLI client implements the draft path. Tests inject fake subprocess and Slack senders; no live Slack, Brainbase, or brokerage calls were made for this change.
