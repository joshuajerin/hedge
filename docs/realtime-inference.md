# Real-time paper inference

`hedge.realtime` and `hedge.inference` provide a local foundation for handling
timestamped market events. They are **paper-only**. They contain no broker
client, credential lookup, market-data request, or execution path.

## Flow

```text
approved feed adapter -> MarketEventStream -> InferenceService -> deployed Hedge CIO
                                                     |                   |
                                                     +-- HOLD on error <--+
```

1. An authorized feed adapter creates a strict `hedge.market-event.v1` event.
2. `MarketEventStream.submit()` validates it, rejects stale/future/out-of-order
   quotes, and makes a non-blocking enqueue into a bounded queue.
3. A worker passes `next_event()` values to `InferenceService.infer()`.
4. The service invokes an injected transport for the deployed Hedge CIO
   (`4ac44fd1-b386-49aa-8b39-a3ba7037fdc8`). It does not choose or configure a
   different model.
5. A response is accepted only when it is a strict
   `hedge.cio-inference-response.v1` `PAPER_PROPOSAL`, says the Risk Reviewer
   approved it, validates as `hedge.decision.v1`, passes `PaperPolicy`, and its
   intents match the event symbol. Every other terminal result is a HOLD.

An accepted result is still a **non-executable paper proposal**. Every
`InferenceOutcome` has `account_mode: "paper"` and
`broker_submission_disabled: true`. This foundation does not call
`broker.py`.

## Required CIO transport

Production wiring must provide one small async adapter that sends the canonical
`CioInferenceRequest.as_json()` payload to the already deployed Hedge CIO and
returns its parsed JSON response. The adapter must enforce the request deadline
at its transport boundary too. It must not use a broker, give the CIO broker
credentials, or turn a proposal into an order.

The CIO response has exactly these fields:

```json
{
  "schema_version": "hedge.cio-inference-response.v1",
  "outcome": "PAPER_PROPOSAL",
  "reason": "Risk Reviewer approved the proposal.",
  "risk_reviewer_approved": true,
  "decision": { "schema_version": "hedge.decision.v1", "...": "strict existing contract fields" }
}
```

For no action, the only valid response is:

```json
{
  "schema_version": "hedge.cio-inference-response.v1",
  "outcome": "HOLD",
  "reason": "Evidence is insufficient.",
  "risk_reviewer_approved": false,
  "decision": null
}
```

Unknown, missing, or coerced schema fields fail closed. Limit prices must be
finite numbers; intent and decision fields are not silently converted.

## Capacity and deadline controls

- `MarketEventStream(queue_capacity=...)` rejects a full queue immediately.
- `LatestQuoteBook(capacity=..., max_quote_age=..., max_future_skew=...)`
  bounds retained symbols and rejects stale, future, and non-monotonic quotes.
- `InferenceService(max_concurrency=..., timeout=..., max_quote_age=...)`
  applies one deadline across semaphore wait and CIO work. It rechecks quote
  freshness after acquiring capacity, and caps the CIO request deadline at the
  quote expiry. A queued quote that becomes stale therefore returns
  `HOLD_STALE_QUOTE` without routing. Contention becomes `HOLD_OVERLOADED`;
  transport expiry becomes `HOLD_TIMEOUT`.

Callers must record and alert on non-proposal statuses, especially
`HOLD_STALE_QUOTE`, `HOLD_OVERLOADED`, `HOLD_TIMEOUT`,
`HOLD_INVALID_RESPONSE`, and `HOLD_ROUTER_ERROR`. Retrying should start with a
new quote, rather than reusing a stale event.

## Minimal worker shape

```python
stream = MarketEventStream(queue_capacity=1024)
service = InferenceService(cio_transport, max_concurrency=4)

while True:
    event = await stream.next_event()
    try:
        outcome = await service.infer(event)
        # Persist or publish the paper-only outcome. Do not submit an order.
    finally:
        stream.task_done()
```

Workers may run concurrently, but their count should be bounded to the same
operational capacity as the CIO transport. Tests use injected local routers;
they make no external calls.
