"""Virtual-simulation member capital accounts backed by :mod:`hedge.store`.

This module records virtual starting balances and contributions only. Ownership
and simulated-NAV views are deterministic calculations over an append-only
SQLite event ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from .store import ContributionEventConflict, LocalStore


_MAX_CENTS: Final = 9_223_372_036_854_775_807
VIRTUAL_STARTING_BALANCE: Final = "VIRTUAL_STARTING_BALANCE"
VIRTUAL_CONTRIBUTION: Final = "VIRTUAL_CONTRIBUTION"


__all__ = [
    "CapitalAccountError",
    "ContributionEvent",
    "ContributionEventConflict",
    "ContributionReceipt",
    "FundNavAllocation",
    "MemberCapitalAccount",
    "MemberCapitalAccounts",
    "MemberNavAllocation",
    "VIRTUAL_CONTRIBUTION",
    "VIRTUAL_STARTING_BALANCE",
]


class CapitalAccountError(ValueError):
    """A virtual-simulation member capital account input is invalid."""


def _nonnegative_cents(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapitalAccountError(f"{field} must be an integer number of cents")
    if value < 0:
        raise CapitalAccountError(f"{field} cannot be negative in a virtual capital account")
    if value > _MAX_CENTS:
        raise CapitalAccountError(f"{field} exceeds SQLite integer cents range")
    return value


@dataclass(frozen=True)
class ContributionEvent:
    """One immutable virtual starting-balance or contribution event in SQLite."""

    sequence: int
    event_id: str
    pool_id: str
    member_id: str
    cents: int
    created_at: str
    action: str = VIRTUAL_CONTRIBUTION

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "ContributionEvent":
        return cls(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            pool_id=str(row["pool_id"]),
            member_id=str(row["member_id"]),
            cents=int(row["cents"]),
            created_at=str(row["created_at"]),
            action=str(row["action"]),
        )


@dataclass(frozen=True)
class ContributionReceipt:
    """The stored event and whether this call was an idempotent repeat."""

    event: ContributionEvent
    idempotent: bool


@dataclass(frozen=True)
class MemberCapitalAccount:
    """One member's cumulative virtual capital and exact ownership fraction."""

    pool_id: str
    member_id: str
    contributed_cents: int
    ownership: Fraction


@dataclass(frozen=True)
class MemberNavAllocation:
    """A cents-exact allocation of caller-supplied simulated NAV."""

    pool_id: str
    member_id: str
    contributed_cents: int
    ownership: Fraction
    nav_cents: int


@dataclass(frozen=True)
class FundNavAllocation:
    """A deterministic, cents-exact view of virtual ownership at one simulated NAV."""

    pool_id: str
    fund_nav_cents: int
    total_contributed_cents: int
    allocations: tuple[MemberNavAllocation, ...]


class MemberCapitalAccounts:
    """Read and append virtual-simulation capital account data using a :class:`LocalStore`.

    ``record_virtual_starting_balance`` and ``record_virtual_contribution``
    are the only write operations. A repeated event ID with the same immutable
    payload returns the original event and marks the receipt idempotent. A
    changed payload raises ``ContributionEventConflict``.
    """

    def __init__(self, store: LocalStore) -> None:
        if not isinstance(store, LocalStore):
            raise TypeError("store must be a LocalStore")
        self._store = store

    def record_virtual_starting_balance(
        self,
        *,
        event_id: object,
        pool_id: object,
        member_id: object,
        cents: object,
        created_at: object | None = None,
    ) -> ContributionReceipt:
        """Append a positive-cents virtual starting-balance event."""
        return self._record_virtual_event(
            event_id=event_id,
            pool_id=pool_id,
            member_id=member_id,
            cents=cents,
            created_at=created_at,
            action=VIRTUAL_STARTING_BALANCE,
        )

    def record_virtual_contribution(
        self,
        *,
        event_id: object,
        pool_id: object,
        member_id: object,
        cents: object,
        created_at: object | None = None,
        action: object = VIRTUAL_CONTRIBUTION,
    ) -> ContributionReceipt:
        """Append a positive-cents virtual contribution event.

        ``action`` is checked so that this API represents virtual simulation
        activity only.
        """
        return self._record_virtual_event(
            event_id=event_id,
            pool_id=pool_id,
            member_id=member_id,
            cents=cents,
            created_at=created_at,
            action=action,
        )

    def _record_virtual_event(
        self,
        *,
        event_id: object,
        pool_id: object,
        member_id: object,
        cents: object,
        created_at: object | None,
        action: object,
    ) -> ContributionReceipt:
        try:
            row, idempotent = self._store.record_virtual_contribution(
                event_id=event_id,
                pool_id=pool_id,
                member_id=member_id,
                cents=cents,
                created_at=created_at,
                action=action,
            )
        except ContributionEventConflict:
            raise
        except ValueError as exc:
            raise CapitalAccountError(str(exc)) from exc
        return ContributionReceipt(ContributionEvent.from_row(row), idempotent)

    def virtual_contribution_history(
        self, *, pool_id: object, member_id: object | None = None
    ) -> tuple[ContributionEvent, ...]:
        """Return the append-only history in stable SQLite sequence order."""
        try:
            rows = self._store.virtual_contribution_history(pool_id=pool_id, member_id=member_id)
        except ValueError as exc:
            raise CapitalAccountError(str(exc)) from exc
        return tuple(ContributionEvent.from_row(row) for row in rows)

    # A concise read alias; history remains append-only virtual ledger data.
    contribution_history = virtual_contribution_history

    def virtual_accounts(self, *, pool_id: object) -> tuple[MemberCapitalAccount, ...]:
        """Return accounts sorted by member ID with exact ``Fraction`` ownership."""
        try:
            balances = self._store.virtual_contribution_balances(pool_id=pool_id)
        except ValueError as exc:
            raise CapitalAccountError(str(exc)) from exc
        total = sum(balances.values())
        if total < 0:
            # Defensive: normal writes and the SQLite schema make this impossible.
            raise CapitalAccountError("negative virtual capital is not supported")
        return tuple(
            MemberCapitalAccount(str(pool_id), member_id, cents, Fraction(cents, total))
            for member_id, cents in balances.items()
        ) if total else ()

    # A concise read alias; returned accounts are still virtual-simulation data.
    accounts = virtual_accounts

    def allocate_simulated_nav(self, *, pool_id: object, fund_nav_cents: object) -> FundNavAllocation:
        """Allocate supplied simulated NAV using cumulative contributed capital.

        Each allocation is integer cents. The largest-remainder method assigns
        any leftover cents by descending remainder and then lexical member ID,
        so both ownership and rounding are independent of event insertion order.
        """
        nav = _nonnegative_cents(fund_nav_cents, "fund_nav_cents")
        accounts = self.virtual_accounts(pool_id=pool_id)
        normalized_pool_id = accounts[0].pool_id if accounts else self._pool_id(pool_id)
        total = sum(account.contributed_cents for account in accounts)
        if not accounts:
            if nav:
                raise CapitalAccountError("cannot allocate positive simulated NAV without virtual contributions")
            return FundNavAllocation(normalized_pool_id, nav, 0, ())

        quotients: dict[str, int] = {}
        remainders: list[tuple[int, str]] = []
        for account in accounts:
            quotient, remainder = divmod(nav * account.contributed_cents, total)
            quotients[account.member_id] = quotient
            remainders.append((remainder, account.member_id))
        remaining = nav - sum(quotients.values())
        for _remainder, member_id in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
            quotients[member_id] += 1

        return FundNavAllocation(
            normalized_pool_id,
            nav,
            total,
            tuple(
                MemberNavAllocation(
                    account.pool_id,
                    account.member_id,
                    account.contributed_cents,
                    account.ownership,
                    quotients[account.member_id],
                )
                for account in accounts
            ),
        )

    # Short alias for existing integrations; NAV remains caller-supplied and simulated.
    allocate_nav = allocate_simulated_nav

    @staticmethod
    def _pool_id(value: object) -> str:
        # Ask the store to apply the same identifier contract without modifying data.
        # A no-row history lookup still validates its pool ID.
        # This helper cannot access instance state, so it is replaced by the caller
        # in the no-account case below.
        if not isinstance(value, str) or not value or value != value.strip():
            raise CapitalAccountError("pool_id must be a non-empty identifier without surrounding whitespace")
        if len(value) > 128 or not value.isascii() or not all(c.isalnum() or c in "._:-" for c in value):
            raise CapitalAccountError("pool_id contains unsupported characters")
        return value
