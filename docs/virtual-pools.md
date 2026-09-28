# Virtual pools

`hedge.virtual_pool` provides a local, paper-only pool lifecycle. It does not
collect payments, move funds, call a broker, or support withdrawals. A
`starting_nav_cents` value and mandate are simulated bookkeeping and policy, not an account balance or investment instruction.

## Lifecycle

1. Create a `DRAFT` pool with `VirtualPoolService.create_pool`.
2. The creator activates it with `activate_pool`.
3. The creator issues a pending invitation with `invite_member`.
4. Only that invitee can activate the invitation. The new active member has
   zero units.
5. The creator may revoke a pending invitation or archive an active pool.

Only the creator can activate, archive, issue invitations, or revoke them.
Archived pools do not accept new invitations. Each successful transition writes
one immutable audit event in sequence order. Repeating the same pool or invitation
creation request returns its stored record and emits no duplicate audit event; a reused
ID with different immutable fields is rejected.

## Exact simulated ownership

Pool creation mints exactly `starting_nav_cents` virtual units. The creator owns
all of those units. Invitation activation adds a member with zero units. No API
in this module changes unit supply or transfers units, so ownership is fully
deterministic and can be read with `unit_ownership(pool_id)`.

Use `PoolMember.ownership_fraction(pool.total_units)` when a ratio is needed;
it returns a stdlib `fractions.Fraction`, never a floating-point approximation.

## Local persistence

The service accepts only the generic `Store` protocol. The included
`SqliteVirtualPoolStore` is a local durable implementation:

```python
from hedge.virtual_pool import SqliteVirtualPoolStore, VirtualPoolService

store = SqliteVirtualPoolStore("./state/virtual-pools.sqlite")
pools = VirtualPoolService(store)
pool = pools.create_pool(
    pool_id="pool.demo",
    name="Demo simulation",
    mandate="Long-term diversified equities simulation",
    base_currency="USD",
    starting_nav_cents=100_000,
    mandate_risk="MEDIUM",
    creator_id="user.creator",
)
pools.activate_pool(pool_id=pool.pool_id, actor_id="user.creator")
```

Applications can implement `Store` with another local repository. Its mutating
methods must persist their supplied audit event atomically with the lifecycle
change. The module uses only the Python standard library.

## Validation boundary

Models are frozen and validate identifiers, timestamps, states, ISO-style
three-letter uppercase base currencies, a printable nonempty simulated mandate, finite SQLite-range integer cents, and
explicit risk levels (`LOW`, `MEDIUM`, `HIGH`). Starting NAV must be positive.
The API has no payment, transfer, deposit, withdrawal, bank, or brokerage
fields. Pool names and mandates also reject real-money language to keep this boundary clear.
