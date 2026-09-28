# Brainbase result delivery boundary

`BrainbaseResultConsumer` is a bounded poller, not a live service yet. It persists
`task_id -> run_id, canonical Slack root, expected role, parent_task_id, deadline`
in SQLite. It stores no credentials, message body, or result text. Every `poll`
performs at most one task lookup and one structured-result lookup. The deadline
survives restart. Status, identity, affinity, handoff, and artifact shape must
validate before it calls `SlackResultDelivery` with its fixed profile mapping.
A reservation made before Slack send is terminal even after a crash or an
ambiguous timeout; never automatically retry it.

## Launch/service wiring needed

1. The inbound bridge currently calls `create_brainbase_task(...)`, discards its
   JSON return value, and marks the run started. Parse the **trusted** create
   response (`task_id`, `agent_id`, `status`), verify the configured CIO agent,
   and register the returned `task_id` with the bridge-generated `run_id` and
   `SlackState` canonical `ThreadCorrelation` before polling. A child launcher
   must register each child with its already-registered CIO `parent_task_id`,
   inherited root, and fixed expected specialist role. Do not register from a
   model-authored message, handoff, or transcript. If task creation succeeds but
   registration fails, leave the run in a failed/manual-reconciliation state;
   do not infer a task ID from logs.
2. Initialize `SlackState`, `SlackResultDelivery(state, EnvironmentSlackSender())`,
   and `BrainbaseResultConsumer(db_path, state, delivery, client)` from a managed
   worker. Use a persistent DB path (the same SQLite file can hold Slack state
   and task registry). Poll registered tasks periodically (e.g. 2–5 seconds).
   On `pending`, schedule another poll before the persisted deadline. On
   `delivered`, `failed`, `timed_out`, or `unsupported`, stop; update the run
   status through the owning service as appropriate. Do not send an invented
   status update as if it were a research result. Monitor stuck `reserved` rows
   for manual review, not replay.
3. An injected `BrainbaseClient` must return `get_task(task_id)` with an exact
   matching `task_id` and status; `get_result(task_id)` must retrieve an
   **authenticated, task-bound, structured terminal artifact**, not agent log
   text. Its result envelope is exactly `{task_id, role, artifact}`. CIO direct
   answers require `hedge.cio-result.v1` with canonical affinity, CIO role,
   `COMPLETED` status, and nonempty `text`. A multi-agent CIO answer requires
   `hedge.handoff-chain.v1` with the typed request, reports, draft, backtest,
   attestation and final `text`; the existing protocol validates parent hashes
   and root affinity. Specialist results require `hedge.specialist-result.v1`
   with a typed `ResearchRequest` and `SpecialistReport` whose specialty matches
   the registered fixed role and whose parent hash binds the request. The
   trusted adapter must independently attest that the result actually came
   from that terminal Brainbase task. JSON claims alone are not provenance.

## Specialist outbox API (disabled until provenance exists)

`ArtifactOutbox.deliver_specialist_report(delivery_id=..., report=...,
request=..., task_id=...)` explicitly raises `UnsupportedSpecialistDelivery`
after checking typed artifact status, root affinity, approved specialty, and
`report.parent_hashes` against `request.sha256()`. A bare report or a supplied
`task_id` is **not** proof of a registered, terminal task. This method never
reserves a delivery or calls Slack, even for valid-looking artifacts. The older
bare-report delivery behavior is unsupported. Do not treat the optional request
or task ID as a trusted registration or authenticated result envelope.

Before enabling this path, the runtime needs a trusted launch-time registry of
child task IDs, their CIO parent, fixed role, canonical root, and deadline, plus
an authenticated task-bound terminal result API. Verify each result against the
registry and the request hash before sending. Reserve atomically using a stable
key derived from the registered task ID (not a caller-supplied `delivery_id`),
so retries with new IDs cannot post the same report twice. A reservation must
remain terminal after an ambiguous Slack send. No such verified transport is
available in the current CLI, so `unsupported` and **no Slack send** is the
safe result; avoid copying task logs or accepting model-authored identifiers.

## Current CLI blocker

Local `brainbase task get --help` and `brainbase task logs --help` say `--json`
returns raw task and transcript records. They do not document a terminal result
or typed-artifact schema, provenance, or a task-bound artifact endpoint. The
included `BrainbaseCliClient` therefore supports `task get --json` for status
only; `get_result` raises `UnsupportedResult`. Successful CLI tasks end
`unsupported` with **no Slack result**. Do not scrape final transcript lines,
accept model-supplied channels/tokens, or manufacture a result from status.
A verified Brainbase artifact API/contract is required before production
result delivery can be turned on. This change does not deploy or modify the
existing launcher.
