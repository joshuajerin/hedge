# Virtual member capital accounts

`hedge.member_accounts` keeps a local SQLite ledger for **virtual simulation
money**. It is a virtual-simulation accounting view. It supports virtual starting
balances, virtual contributions, deterministic ownership, and simulated NAV
allocation. It has no external operation and no order-submission path.

## Integration API

```python
from pathlib import Path

from hedge.member_accounts import MemberCapitalAccounts
from hedge.store import LocalStore

store = LocalStore(Path("/tmp/hedge-virtual-accounts.sqlite"))
accounts = MemberCapitalAccounts(store)

accounts.record_virtual_starting_balance(
    event_id="evt-alice-start",
    pool_id="club-alpha",
    member_id="alice",
    cents=10_000,
)
accounts.record_virtual_contribution(
    event_id="evt-bob-1",
    pool_id="club-alpha",
    member_id="bob",
    cents=5_000,
)

ownership = accounts.virtual_accounts(pool_id="club-alpha")
allocation = accounts.allocate_simulated_nav(
    pool_id="club-alpha",
    fund_nav_cents=18_001,
)
```

All amounts are Python `int` values in cents. Do not pass floats, decimals,
booleans, negative values, or zero values for a starting-balance or
contribution event. Simulated NAV may be zero, but cannot be negative.

`virtual_accounts()` returns `MemberCapitalAccount` values sorted by
`member_id`. Each has cumulative `contributed_cents` and an exact
`fractions.Fraction` `ownership`. `virtual_contribution_history()` returns
immutable `ContributionEvent` values in SQLite sequence order.

`allocate_simulated_nav()` returns `FundNavAllocation`. Each
`MemberNavAllocation.nav_cents` is integer cents, and all values sum exactly to
`fund_nav_cents`.

## Event rules

The only accepted event actions are:

- `VIRTUAL_STARTING_BALANCE`
- `VIRTUAL_CONTRIBUTION`

Every event has an immutable `event_id`, `pool_id`, `member_id`, positive
integer `cents`, action, and ISO-8601 `created_at`. IDs are non-empty ASCII
tokens of up to 128 characters using letters, digits, `.`, `_`, `:`, and `-`.

Repeating an event ID with exactly the same payload returns the original event
with `ContributionReceipt.idempotent == True`. Reusing an event ID with changed
pool, member, cents, action, or explicitly supplied timestamp raises
`ContributionEventConflict`. This preserves an append-only history through a
process restart.

Only the two virtual actions above are accepted. Unknown actions are rejected.

## Ownership and simulated NAV

Ownership is cumulative member virtual capital divided by total pool virtual
capital. It is independent of event insertion order. Members are ordered by
lexical `member_id`.

The caller supplies `fund_nav_cents`; this module does not obtain a value from
any external system. NAV allocation uses the largest-remainder method:

1. Compute each member's floor of `fund_nav_cents * member_cents / total_cents`.
2. Give remaining cents to the largest fractional remainders.
3. Break equal remainders with lexical `member_id` order.

Therefore the allocation is deterministic and cents-exact. A positive simulated
NAV with no virtual capital is rejected. A zero simulated NAV with no members
returns an empty allocation.

## SQLite data

`LocalStore` creates `virtual_contribution_events`. It is append-only for this
feature and records `sequence`, event identity, virtual action, cents, and
timestamp. `LocalStore.close()` closes the owned connection after use.
