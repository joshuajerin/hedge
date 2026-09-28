# Hedge decision contract

Brainbase agents may only hand the local runner a `hedge.decision.v1` document.
Each intent is constrained to US equity symbols, `BUY` or `SELL`, a positive share quantity, and a `MKT` or `LMT` order type. Limit prices must be finite positive numbers; `NaN`, `Infinity`, and `-Infinity` are rejected at the contract boundary. A decision is idempotent by `decision_id`.

The runner is deliberately independent of Brainbase: it rejects unknown schemas, caps order count and share size, runs in paper mode, requires a local `--confirm`, and records each handled decision in SQLite. It does not accept executable code, shell commands, credentials, or arbitrary broker methods from an agent.

The protocol is the change boundary. A future Slack adapter writes pool and mandate changes into a task payload. A future Brainbase task-result adapter converts the backtester/risk-reviewer output into this document. A future broker adapter can replace direct TWS sockets while preserving the same `Decision` and `PaperPolicy` interfaces.
