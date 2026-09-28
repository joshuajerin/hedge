# Paper fund service

`hedge.fund_service.PaperFundService(path)` owns a local file-backed SQLite database.
No network, brokerage, payment, or live account is used. All money-like
numbers are **simulated integer USD cents**, not funds that can be deposited or
withdrawn. Other currencies are rejected because `flows.build_report` currently
labels contribution charts USD. Close with `service.close()` when done.

```python
from hedge.fund_service import PaperFundService

fund = PaperFundService("state/paper-fund.sqlite")
fund.create_virtual_pool(
    pool_id="research-1", actor_id="U-CREATOR", name="Research One",
    starting_balance_cents=100_000, mandate="Simulated long only research",
    mandate_risk="MEDIUM", base_currency="USD",
)
fund.activate_pool(pool_id="research-1", actor_id="U-CREATOR")
fund.invite_member(invitation_id="invite-1", pool_id="research-1",
                   actor_id="U-CREATOR", member_id="U-SECOND")
fund.join_with_virtual_contribution(
    invitation_id="invite-1", actor_id="U-SECOND",
    contribution_cents=300_000, event_id="join-event-1",
)
visible = fund.member_pools(member_id="U-SECOND")
view = fund.snapshot(
    pool_id="research-1", cash_cents=200_000,
    positions={"AAPL": 10}, prices_cents={"AAPL": 20_000},
    cost_basis_cents={"AAPL": 18_000},
)
fund.close()
```

`view.nav` holds exact `Fraction` contribution ownership and cents-exact
member NAV allocations. `view.dashboard` is `slack_ui.FundDashboard`;
`view.positions` are `slack_ui.PositionSnapshot` cards; `view.stakes` are
`slack_ui.MemberStake` cards. `view.report` is the `flows.build_report`
document for `dashboard.render_dashboard`. All marks, position quantities,
optional cost bases, and paper cash come from the caller. No market prices or
trading state are loaded or saved by the service. The `PaperTradingSystem`
can produce positions externally, but its in-memory decisions/fills are not
persisted or replayed here. A missing cost basis produces zero *displayed*
position P&L rather than an estimated cost. All positions must be long,
positive whole shares with positive integer-cent prices; positions and prices
must match exactly. `as_of` must be timezone-aware when supplied.

The virtual-pool store mints a fixed unit supply entirely to the creator;
new members get zero pool units. **Units are not the capital ledger.** The
fund's economic NAV allocation follows the append-only virtual contribution
ledger and can differ from unit ownership. `FundSnapshot.nav.allocations`
is authoritative for member value. A later market price changes only the
caller-calculated snapshot, not persisted capital events. The dashboard
return compares supplied NAV with all accumulated virtual contributions;
it is not a time-weighted performance measure.

Pool creation and member joining touch two existing store APIs. They do not
share a transaction, even though both stores use the same SQLite file. The
coordinator first records an immutable durable intent, then writes pool or
membership and capital records. Duplicate identical requests preserve their
original timestamp and ledger event. A conflicting retry is rejected. If a
write is interrupted, `FundConsistencyError` blocks snapshots and
`member_pools` for the affected pool until the **identical request** is
retried and completes. The service does not silently undo partial records,
or claim atomicity. Do not mutate the stores directly or write concurrently
through multiple service instances: their lifecycle read-before-write checks
and separate connections have no cross-instance serialization guarantee.
There is no automatic valuation persistence or recovery daemon.

Run focused tests with `PYTHONPATH=src uv run pytest -q tests/test_fund_service.py`.
