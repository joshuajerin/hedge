"""Durable, fail-closed paper fund coordination and caller-priced read models.

All amounts are simulated integer cents. The fixed virtual-pool units are
NOT capital-account ownership; member NAV follows the append-only ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from .flows import build_report
from .member_accounts import MemberCapitalAccounts, FundNavAllocation
from .slack_ui import FundDashboard, MemberStake, PositionSnapshot
from .store import LocalStore, _identifier, _positive_cents, _event_time
from .virtual_pool import (InvitationStatus, PoolStatus, SqliteVirtualPoolStore,
                           VirtualPool, VirtualPoolService)


class FundConsistencyError(RuntimeError):
    """A write is incomplete or stored membership and capital disagree; no view is safe."""


@dataclass(frozen=True)
class FundSnapshot:
    pool: VirtualPool
    nav: FundNavAllocation
    dashboard: FundDashboard
    positions: tuple[PositionSnapshot, ...]
    stakes: tuple[MemberStake, ...]
    report: dict[str, object]


class PaperFundService:
    """Own both SQLite stores at one file path; never accepts live execution services.

    A durable intent is saved before each multi-store change. Interrupted writes
    remain unreadable until the exact same request finishes on retry. Do not use
    the underlying stores directly while this coordinator is active.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ':memory:':
            raise ValueError('a durable fund requires a file-backed SQLite path')
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._pools_store = SqliteVirtualPoolStore(self.path)
        self._capital_store = LocalStore(self.path)
        self.pools = VirtualPoolService(self._pools_store)
        self.capital = MemberCapitalAccounts(self._capital_store)
        self._db = sqlite3.connect(self.path)
        with self._db:
            self._db.execute('CREATE TABLE IF NOT EXISTS paper_fund_intents '
                             '(intent_id TEXT PRIMARY KEY, pool_id TEXT NOT NULL, payload TEXT NOT NULL)')

    def close(self) -> None:
        self._db.close()
        self._capital_store.close()
        self._pools_store.close()

    def _intent(self, intent_id: str, pool_id: str, payload: dict[str, object]) -> dict[str, object]:
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        with self._db:
            self._db.execute('INSERT OR IGNORE INTO paper_fund_intents VALUES (?, ?, ?)',
                             (intent_id, pool_id, encoded))
        row = self._db.execute('SELECT pool_id, payload FROM paper_fund_intents WHERE intent_id=?',
                               (intent_id,)).fetchone()
        if row != (pool_id, encoded):
            raise ValueError('intent ID already belongs to a different paper fund request')
        return payload

    def create_virtual_pool(self, *, pool_id: str, actor_id: str, name: str,
                            starting_balance_cents: int, mandate: str,
                            mandate_risk: str = 'MEDIUM', base_currency: str = 'USD',
                            created_at: str | None = None) -> VirtualPool:
        """Create pool and creator capital. Retry identical input after interrupted writes."""
        pool_id = _identifier(pool_id, 'pool_id')
        actor_id = _identifier(actor_id, 'actor_id')
        starting_balance_cents = _positive_cents(starting_balance_cents, 'starting_balance_cents')
        if base_currency != 'USD':
            raise ValueError('paper report supports USD only')
        # Validate pool inputs before writing the durable intent.
        from .virtual_pool import VirtualPool as _Pool, MandateRisk
        stamp = _event_time(created_at)
        draft = _Pool(pool_id, name, mandate, base_currency, starting_balance_cents,
                      MandateRisk(mandate_risk), actor_id, PoolStatus.DRAFT, stamp)
        immutable = dict(pool_id=pool_id, actor_id=actor_id, name=name, mandate=mandate,
                         starting_balance_cents=starting_balance_cents,
                         mandate_risk=draft.mandate_risk.value, base_currency=base_currency)
        key = 'start:' + pool_id
        existing = self._db.execute('SELECT payload FROM paper_fund_intents WHERE intent_id=?', (key,)).fetchone()
        if existing is not None and created_at is None:
            stamp = json.loads(existing[0])['created_at']
        self._intent(key, pool_id, {**immutable, 'created_at': stamp})
        try:
            pool = self.pools.create_pool(pool_id=pool_id, creator_id=actor_id, name=name,
                mandate=mandate, mandate_risk=mandate_risk, base_currency=base_currency,
                starting_nav_cents=starting_balance_cents, created_at=stamp)
            self.capital.record_virtual_starting_balance(event_id=key, pool_id=pool_id,
                member_id=actor_id, cents=starting_balance_cents, created_at=stamp)
            self._check(pool_id)
        except Exception as exc:
            raise FundConsistencyError('paper pool creation incomplete; retry the identical request') from exc
        return pool

    def activate_pool(self, *, pool_id: str, actor_id: str) -> VirtualPool:
        self._check(pool_id)
        return self.pools.activate_pool(pool_id=pool_id, actor_id=actor_id)

    def invite_member(self, *, invitation_id: str, pool_id: str,
                      actor_id: str, member_id: str):
        self._check(pool_id)
        return self.pools.invite_member(invitation_id=invitation_id, pool_id=pool_id,
                                        actor_id=actor_id, invitee_id=member_id)

    def join_with_virtual_contribution(self, *, invitation_id: str, actor_id: str,
                                       contribution_cents: int, event_id: str,
                                       created_at: str | None = None):
        """Activate invite and append contribution; retries bind event, amount and invite."""
        invitation_id = _identifier(invitation_id, 'invitation_id')
        actor_id = _identifier(actor_id, 'actor_id')
        event_id = _identifier(event_id, 'event_id')
        cents = _positive_cents(contribution_cents, 'contribution_cents')
        invitation = self.pools.invitation(invitation_id)
        pool_id = invitation.pool_id
        if actor_id != invitation.invitee_id:
            raise ValueError('only the invited member can join')
        if self.pools.pool(pool_id).status is not PoolStatus.ACTIVE:
            raise ValueError('only an active pool can accept members')
        if invitation.status not in {InvitationStatus.PENDING, InvitationStatus.ACTIVATED}:
            raise ValueError('invitation cannot be activated')
        if event_id == 'start:' + pool_id:
            raise ValueError('event_id collides with the creator starting event')
        key = 'join:' + invitation_id
        existing = self._db.execute('SELECT payload FROM paper_fund_intents WHERE intent_id=?', (key,)).fetchone()
        stamp = json.loads(existing[0])['created_at'] if existing is not None and created_at is None else _event_time(created_at)
        self._intent(key, pool_id, dict(invitation_id=invitation_id, actor_id=actor_id,
                                       contribution_cents=cents, event_id=event_id, created_at=stamp))
        try:
            if invitation.status is InvitationStatus.PENDING:
                self.pools.activate_invitation(invitation_id=invitation_id, actor_id=actor_id,
                                               activated_at=stamp)
            member = self._pools_store.get_member(pool_id, actor_id)
            if member is None or invitation.status not in {InvitationStatus.PENDING, InvitationStatus.ACTIVATED}:
                raise FundConsistencyError('invitation did not activate')
            self.capital.record_virtual_contribution(event_id=event_id, pool_id=pool_id,
                member_id=actor_id, cents=cents, created_at=stamp)
            self._check(pool_id)
        except Exception as exc:
            raise FundConsistencyError('join incomplete; retry the identical request') from exc
        return member

    def _check(self, pool_id: str) -> VirtualPool:
        pool = self.pools.pool(pool_id)
        rows = self._db.execute('SELECT intent_id, payload FROM paper_fund_intents WHERE pool_id=?',
                                (pool_id,)).fetchall()
        intents = {key: json.loads(data) for key, data in rows}
        start = intents.get('start:' + pool_id)
        members = {m.member_id: m for m in self.pools.members(pool_id)}
        events = self.capital.virtual_contribution_history(pool_id=pool_id)
        by_id = {event.event_id: event for event in events}
        if (start is None or pool.creator_id not in members or
            start['actor_id'] != pool.creator_id or
            start['name'] != pool.name or start['mandate'] != pool.mandate or
            start['base_currency'] != pool.base_currency or
            start['mandate_risk'] != pool.mandate_risk.value or
            start['starting_balance_cents'] != pool.starting_nav_cents or
            start['created_at'] != pool.created_at):
            raise FundConsistencyError('pool has no matching coordinated creator intent')
        expected: dict[str, tuple[str, int, str, str]] = {}
        expected['start:' + pool_id] = (pool.creator_id, pool.starting_nav_cents,
                                         'VIRTUAL_STARTING_BALANCE', start['created_at'])
        invitation_rows = self._db.execute(
            'SELECT invitation_id, pool_id, invitee_id, status FROM virtual_pool_invitations '
            'WHERE pool_id=?', (pool_id,)).fetchall()
        invitations = {row[0]: row for row in invitation_rows}
        for key, data in intents.items():
            if key.startswith('join:'):
                expected[data['event_id']] = (data['actor_id'], data['contribution_cents'],
                                               'VIRTUAL_CONTRIBUTION', data['created_at'])
                invitation = invitations.get(data['invitation_id'])
                if (invitation is None or invitation[1] != pool_id or
                    invitation[2] != data['actor_id'] or invitation[3] != 'ACTIVATED'):
                    raise FundConsistencyError('join intent lacks activated membership')
        if (len(expected) != 1 + sum(key.startswith('join:') for key in intents) or
            set(by_id) != set(expected) or
            set(members) != {value[0] for value in expected.values()} or
            any((by_id[key].member_id, by_id[key].cents, by_id[key].action, by_id[key].created_at) != value
                for key, value in expected.items() if key in by_id)):
            raise FundConsistencyError('pool and virtual capital ledger disagree or write incomplete')
        self.pools.unit_ownership(pool_id)
        return pool

    def member_pools(self, *, member_id: str) -> tuple[VirtualPool, ...]:
        """Return only consistent pools with an active membership for this member."""
        actor = _identifier(member_id, "member_id")
        rows = self._db.execute(
            "SELECT pool_id FROM virtual_pool_members WHERE member_id=? ORDER BY pool_id",
            (actor,)).fetchall()
        return tuple(self._check(row[0]) for row in rows)

    def snapshot(self, *, pool_id: str, cash_cents: int, positions: Mapping[str, int],
                 prices_cents: Mapping[str, int], cost_basis_cents: Mapping[str, int] | None = None,
                 as_of: datetime | None = None) -> FundSnapshot:
        """Value supplied paper positions; no market fetch or stored trade execution."""
        pool = self._check(pool_id)
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError('as_of must include a UTC offset')
        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        if isinstance(cash_cents, bool) or not isinstance(cash_cents, int) or not 0 <= cash_cents <= 9_223_372_036_854_775_807:
            raise ValueError('cash_cents must be nonnegative integer cents')
        if set(prices_cents) != set(positions):
            raise ValueError('prices_cents must cover exactly the supplied positions')
        basis = cost_basis_cents or {}
        if set(basis) - set(positions):
            raise ValueError('cost_basis_cents contains an unknown position')
        values: dict[str, int] = {}
        for ticker, quantity in positions.items():
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError('position quantity must be a positive integer')
            price = _positive_cents(prices_cents[ticker], 'price_cents')
            values[ticker] = price * quantity
            if ticker in basis:
                _positive_cents(basis[ticker], 'cost_basis_cents')
        nav_cents = cash_cents + sum(values.values())
        if nav_cents > 9_223_372_036_854_775_807:
            raise ValueError('NAV exceeds supported integer cents range')
        allocation = self.capital.allocate_simulated_nav(pool_id=pool_id, fund_nav_cents=nav_cents)
        def bps(fraction: Fraction) -> int:
            return (fraction.numerator * 10_000 * 2 // fraction.denominator + 1) // 2
        contributed = allocation.total_contributed_cents
        dashboard = FundDashboard(pool.name, nav_cents,
            bps(Fraction(nav_cents - contributed, contributed)), cash_cents, pool.base_currency)
        cards = tuple(PositionSnapshot(ticker, ticker, values[ticker],
            (prices_cents[ticker] - basis[ticker]) * positions[ticker] if ticker in basis else 0,
            bps(Fraction(values[ticker], nav_cents)) if nav_cents else 0,
            'unassessed', pool.base_currency) for ticker in sorted(positions))
        stakes = tuple(MemberStake(a.member_id, a.contributed_cents, bps(a.ownership),
                                    a.nav_cents, pool.base_currency) for a in allocation.allocations)
        history = self.capital.virtual_contribution_history(pool_id=pool_id)
        report = build_report(
            contributions=[dict(pool_id=e.pool_id, member_id=e.member_id, cents=e.cents,
                                at=e.created_at) for e in history],
            equity=[dict(at=now.isoformat(), equity_cents=nav_cents)],
            fund=dict(nav_cents=nav_cents, cash_cents=cash_cents),
            positions=[dict(symbol=t, quantity=positions[t], current_price_cents=prices_cents[t],
                            entry_price_cents=basis.get(t, prices_cents[t]),
                            market_value_cents=values[t]) for t in sorted(positions)],
            capital_accounts=[dict(member_id=a.member_id, contributed_cents=a.contributed_cents,
                                   nav_cents=a.nav_cents, allocation_pct=float(a.ownership * 100))
                              for a in allocation.allocations], generated_at=now)
        return FundSnapshot(pool, allocation, dashboard, cards, stakes, report)
