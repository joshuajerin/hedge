# Paper-only trading system

`hedge.trading_system.PaperTradingSystem` is a local, synchronous simulator.
It is not an IBKR adapter and it has no network or broker dependency. Its only
execution outcome is a `paper:<decision_id>:<order_index>` reference.

## Runtime contract

Create the runtime with a USD cash balance and optional long-only positions.
For every call to `process(decision, prices)`, supply one finite, positive price
for each symbol in the decision and each existing non-zero position. The caller
owns quote collection, review, and persistence.

```python
from hedge.contracts import Decision
from hedge.trading_system import PaperTradingSystem

runtime = PaperTradingSystem(initial_cash="1000", initial_positions={"AAPL": 2})
result = runtime.process(reviewed_decision, {"AAPL": "185.20"})
assert result.terminal
print(result.as_dict())
```

The runtime canonicalizes and revalidates `hedge.decision.v1` locally, then
uses the supplied snapshot only. It does not fetch a price, read credentials,
contact IBKR, or import `hedge.broker`.

## State machine and audit ledger

A new decision follows this exact path:

```text
PROPOSED -> VALIDATED -> RISK_APPROVED -> SIMULATED -> COMPLETED
                       \-> REJECTED
PROPOSED/VALIDATED/RISK_APPROVED \-> HALTED (kill switch)
```

Each transition appends an immutable `LedgerEvent` with sequence number, UTC
timestamp, decision ID, previous and new state, action, reason, and details.
Read it with `runtime.ledger()` or `runtime.ledger(decision_id)`. Store the
returned event dictionaries in the operator's durable audit system if runtime
restart recovery is required. This core is intentionally in-memory; it does
not silently claim durable execution state.

A repeated decision with the same canonical payload returns the first terminal
`ProcessingResult` with `idempotent=True`. It does not alter the account or add
ledger records. Reuse of a decision ID with different contents raises
`IdempotencyConflict`; it is never simulated.

## Controls and fail-closed behavior

`PaperPolicy` is run before `RiskGate`. The runtime independently requires
`account_mode == "paper"`, even when a custom policy object is passed.
`RiskLimits` has hard, configurable bounds for:

- orders per decision and simulated open orders;
- shares and notional per order;
- shares per position; and
- gross marked exposure.

The risk gate evaluates all orders against a shadow account before simulation.
It requires cash for buys and requires every sell to be covered by the current
or earlier filling position. It rejects missing prices, non-finite prices,
uncovered sells, insufficient cash, and any bound breach. Limit buys reserve
the less favourable limit price for the gate. A limit order only fills when its
snapshot price reaches the limit; otherwise it is recorded as `NOT_FILLED` and
makes no paper-account change.

`activate_kill_switch(reason)` blocks future decisions and records both the
operator action and each blocked decision. `deactivate_kill_switch(reason)` is
also auditable. Neither operation replays work or changes existing fills.

Validation, risk, or internal errors produce a terminal `REJECTED` result with
no account update. There is no partial simulated decision. A manually
constructed malformed `Decision` is also recorded as `PROPOSED -> REJECTED`
before any policy or risk work. Its rejection is an auditable, idempotent
terminal outcome; later reuse of its decision ID with different contents still
raises `IdempotencyConflict`.

## Live execution is prohibited

Live execution is explicitly unsupported. Constructing with `mode="live"` or
calling `live_execute()` raises `LiveExecutionProhibited`. Do not connect this
runtime to `IbkrPaperBroker`, TWS, IB Gateway, a broker client, or any external
execution service. A future integration must remain paper-only and must provide
its own durable ledger/idempotency store, reviewed deterministic price snapshot,
and an explicit operator-owned kill-switch lifecycle before it can call this
core.
