from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from hedge.member_accounts import (
    CapitalAccountError,
    MemberCapitalAccounts,
    VIRTUAL_CONTRIBUTION,
    VIRTUAL_STARTING_BALANCE,
)
from hedge.store import ContributionEventConflict, LocalStore


class MemberCapitalAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "virtual-accounts.sqlite"
        self.store = LocalStore(self.path)
        self.accounts = MemberCapitalAccounts(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_virtual_starting_balances_and_contributions_set_exact_ownership(self) -> None:
        first = self.accounts.record_virtual_starting_balance(
            event_id="evt-alice-start", pool_id="pool-alpha", member_id="alice", cents=10_000,
            created_at="2025-01-01T00:00:00+00:00",
        )
        second = self.accounts.record_virtual_contribution(
            event_id="evt-alice-extra", pool_id="pool-alpha", member_id="alice", cents=5_000,
            created_at="2025-01-02T00:00:00+00:00",
        )
        third = self.accounts.record_virtual_starting_balance(
            event_id="evt-bob-start", pool_id="pool-alpha", member_id="bob", cents=5_000,
            created_at="2025-01-03T00:00:00+00:00",
        )

        self.assertFalse(first.idempotent)
        self.assertEqual(first.event.action, VIRTUAL_STARTING_BALANCE)
        self.assertEqual(second.event.action, VIRTUAL_CONTRIBUTION)
        self.assertEqual(third.event.sequence, 3)
        actual = self.accounts.virtual_accounts(pool_id="pool-alpha")
        self.assertEqual(
            actual,
            (
                actual[0].__class__("pool-alpha", "alice", 15_000, Fraction(3, 4)),
                actual[1].__class__("pool-alpha", "bob", 5_000, Fraction(1, 4)),
            ),
        )
        self.assertEqual([event.event_id for event in self.accounts.virtual_contribution_history(pool_id="pool-alpha")], [
            "evt-alice-start", "evt-alice-extra", "evt-bob-start",
        ])

    def test_simulated_nav_allocation_is_cents_exact_and_deterministic(self) -> None:
        # Deliberately append in reverse lexical member order.
        for member_id in ("casey", "blair", "alice"):
            self.accounts.record_virtual_starting_balance(
                event_id=f"evt-{member_id}", pool_id="pool-tie", member_id=member_id, cents=1,
                created_at="2025-01-01T00:00:00+00:00",
            )

        allocation = self.accounts.allocate_simulated_nav(pool_id="pool-tie", fund_nav_cents=2)
        self.assertEqual(allocation.total_contributed_cents, 3)
        self.assertEqual([item.member_id for item in allocation.allocations], ["alice", "blair", "casey"])
        self.assertEqual([item.nav_cents for item in allocation.allocations], [1, 1, 0])
        self.assertEqual(sum(item.nav_cents for item in allocation.allocations), 2)
        self.assertEqual([item.ownership for item in allocation.allocations], [Fraction(1, 3)] * 3)

    def test_duplicate_event_is_idempotent_and_changed_event_is_rejected(self) -> None:
        initial = self.accounts.record_virtual_contribution(
            event_id="evt-repeat", pool_id="pool-alpha", member_id="alice", cents=250,
            created_at="2025-01-01T00:00:00+00:00",
        )
        repeated = self.accounts.record_virtual_contribution(
            event_id="evt-repeat", pool_id="pool-alpha", member_id="alice", cents=250,
            created_at="2025-01-01T00:00:00+00:00",
        )
        self.assertFalse(initial.idempotent)
        self.assertTrue(repeated.idempotent)
        self.assertEqual(repeated.event, initial.event)
        self.assertEqual(len(self.accounts.virtual_contribution_history(pool_id="pool-alpha")), 1)
        with self.assertRaises(ContributionEventConflict):
            self.accounts.record_virtual_contribution(
                event_id="evt-repeat", pool_id="pool-alpha", member_id="alice", cents=251,
                created_at="2025-01-01T00:00:00+00:00",
            )

    def test_events_and_ownership_persist_across_store_restart(self) -> None:
        self.accounts.record_virtual_starting_balance(
            event_id="evt-alice", pool_id="pool-restart", member_id="alice", cents=400,
            created_at="2025-01-01T00:00:00+00:00",
        )
        self.accounts.record_virtual_contribution(
            event_id="evt-bob", pool_id="pool-restart", member_id="bob", cents=100,
            created_at="2025-01-02T00:00:00+00:00",
        )
        self.store.close()
        restarted_store = LocalStore(self.path)
        try:
            restarted = MemberCapitalAccounts(restarted_store)
            self.assertEqual(
                [(item.member_id, item.contributed_cents, item.ownership) for item in restarted.virtual_accounts(pool_id="pool-restart")],
                [("alice", 400, Fraction(4, 5)), ("bob", 100, Fraction(1, 5))],
            )
            allocation = restarted.allocate_simulated_nav(pool_id="pool-restart", fund_nav_cents=777)
            self.assertEqual([item.nav_cents for item in allocation.allocations], [622, 155])
            repeated = restarted.record_virtual_starting_balance(
                event_id="evt-alice", pool_id="pool-restart", member_id="alice", cents=400,
                created_at="2025-01-01T00:00:00+00:00",
            )
            self.assertTrue(repeated.idempotent)
        finally:
            restarted_store.close()
    def test_persisted_events_cannot_be_mutated_or_removed(self) -> None:
        self.accounts.record_virtual_starting_balance(
            event_id="evt-immutable", pool_id="pool-alpha", member_id="alice", cents=100,
            created_at="2025-01-01T00:00:00+00:00",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE virtual_contribution_events SET cents = 1 WHERE event_id = ?",
                ("evt-immutable",),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM virtual_contribution_events WHERE event_id = ?",
                ("evt-immutable",),
            )
        self.assertEqual(self.accounts.virtual_accounts(pool_id="pool-alpha")[0].contributed_cents, 100)

    def test_invalid_ids_amounts_actions_and_simulated_nav_are_rejected(self) -> None:
        invalid_calls = (
            {"event_id": " event", "pool_id": "pool", "member_id": "alice", "cents": 1},
            {"event_id": "evt", "pool_id": "pool name", "member_id": "alice", "cents": 1},
            {"event_id": "evt", "pool_id": "pool", "member_id": "alice", "cents": 0},
            {"event_id": "evt", "pool_id": "pool", "member_id": "alice", "cents": -1},
            {"event_id": "evt", "pool_id": "pool", "member_id": "alice", "cents": True},
            {"event_id": "evt", "pool_id": "pool", "member_id": "alice", "cents": 1.0},
        )
        for kwargs in invalid_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(CapitalAccountError):
                self.accounts.record_virtual_contribution(**kwargs)
        with self.assertRaises(CapitalAccountError):
            self.accounts.record_virtual_contribution(
                event_id="evt-action", pool_id="pool", member_id="alice", cents=1, action="OTHER"
            )
        with self.assertRaises(CapitalAccountError):
            self.accounts.allocate_simulated_nav(pool_id="pool", fund_nav_cents=-1)
        with self.assertRaises(CapitalAccountError):
            self.accounts.allocate_simulated_nav(pool_id="pool", fund_nav_cents=1)
        self.assertEqual(self.accounts.allocate_simulated_nav(pool_id="pool", fund_nav_cents=0).allocations, ())


if __name__ == "__main__":
    unittest.main()
