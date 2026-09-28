"""Paper-only virtual pool creation and membership lifecycle.

This module has no payment, brokerage, withdrawal, or transfer operation.  The
``starting_nav_cents`` value is simulated bookkeeping only.  Ownership is
expressed as exact integer virtual units: creation mints one unit per starting
NAV cent, all owned by the creator.  Invited members start with zero units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from fractions import Fraction
from pathlib import Path
import re
import sqlite3
from threading import Lock
from typing import Protocol, runtime_checkable


_MAX_CENTS = 9_223_372_036_854_775_807
_MAX_NAME_LENGTH = 120
_MAX_ID_LENGTH = 128
_REAL_MONEY_TERMS = frozenset({
    "bank", "cash", "deposit", "fiat", "money", "payment", "payout", "transfer",
    "wallet", "wire", "withdraw", "withdrawal",
})


class VirtualPoolError(ValueError):
    """Base error for invalid virtual-pool lifecycle requests."""


class NotFoundError(VirtualPoolError):
    """A requested virtual-pool record does not exist."""


class LifecycleError(VirtualPoolError):
    """A simulated virtual-pool record cannot make the requested transition."""


class PoolStatus(str, Enum):
    """Allowed lifecycle states for a simulated virtual pool."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MemberStatus(str, Enum):
    """Allowed membership states in a simulated virtual pool."""

    ACTIVE = "ACTIVE"


class InvitationStatus(str, Enum):
    """Allowed invitation states for simulated virtual membership."""

    PENDING = "PENDING"
    ACTIVATED = "ACTIVATED"
    REVOKED = "REVOKED"


class MandateRisk(str, Enum):
    """Allowed simulated virtual-pool mandate risk settings."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AuditAction(str, Enum):
    """Auditable actions in the simulated virtual-pool lifecycle."""

    POOL_CREATED = "POOL_CREATED"
    POOL_ACTIVATED = "POOL_ACTIVATED"
    INVITATION_CREATED = "INVITATION_CREATED"
    INVITATION_ACTIVATED = "INVITATION_ACTIVATED"
    INVITATION_REVOKED = "INVITATION_REVOKED"
    POOL_ARCHIVED = "POOL_ARCHIVED"


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise VirtualPoolError(f"{field} must be a string identifier")
    if not value or value != value.strip() or len(value) > _MAX_ID_LENGTH:
        raise VirtualPoolError(f"{field} must be a non-empty identifier without surrounding whitespace")
    if not value.isascii() or not all(char.isalnum() or char in "._:-" for char in value):
        raise VirtualPoolError(f"{field} contains unsupported characters")
    return value


def _has_real_money_language(value: str) -> bool:
    """Reject prohibited real-money terms even when punctuation surrounds them."""
    words = set(re.findall(r"[a-z]+", value.lower()))
    return bool(words & _REAL_MONEY_TERMS or "real money" in value.lower())


def _safe_name(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > _MAX_NAME_LENGTH:
        raise VirtualPoolError("name must be a non-empty name without surrounding whitespace")
    if _has_real_money_language(value):
        raise VirtualPoolError("real-money language is not permitted in a virtual pool name")
    return value


def _safe_mandate(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 500:
        raise VirtualPoolError("mandate must be non-empty text without surrounding whitespace")
    if not value.isascii() or not all(character.isprintable() and character not in "\r\n\t" for character in value):
        raise VirtualPoolError("mandate must contain printable ASCII text only")
    if _has_real_money_language(value):
        raise VirtualPoolError("real-money language is not permitted in a virtual pool mandate")
    return value


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isascii() or not value.isupper() or not value.isalpha():
        raise VirtualPoolError("base_currency must be a three-letter uppercase currency code")
    return value


def _positive_cents(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VirtualPoolError("starting_nav_cents must be an integer")
    if value <= 0:
        raise VirtualPoolError("starting_nav_cents must be positive for a virtual pool")
    if value > _MAX_CENTS:
        raise VirtualPoolError("starting_nav_cents exceeds SQLite integer range")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise VirtualPoolError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VirtualPoolError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise VirtualPoolError(f"{field} must include a UTC offset")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _enum(value: object, enum_type: type[Enum], field: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        permitted = ", ".join(item.value for item in enum_type)  # type: ignore[attr-defined]
        raise VirtualPoolError(f"{field} must be one of: {permitted}") from exc


@dataclass(frozen=True)
class VirtualPool:
    """Validated state of one simulated virtual pool."""

    pool_id: str
    name: str
    mandate: str
    base_currency: str
    starting_nav_cents: int
    mandate_risk: MandateRisk
    creator_id: str
    status: PoolStatus
    created_at: str
    activated_at: str | None = None
    archived_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", _identifier(self.pool_id, "pool_id"))
        object.__setattr__(self, "name", _safe_name(self.name))
        object.__setattr__(self, "mandate", _safe_mandate(self.mandate))
        object.__setattr__(self, "base_currency", _currency(self.base_currency))
        object.__setattr__(self, "starting_nav_cents", _positive_cents(self.starting_nav_cents))
        object.__setattr__(self, "mandate_risk", _enum(self.mandate_risk, MandateRisk, "mandate_risk"))
        object.__setattr__(self, "creator_id", _identifier(self.creator_id, "creator_id"))
        object.__setattr__(self, "status", _enum(self.status, PoolStatus, "status"))
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        if self.activated_at is not None:
            object.__setattr__(self, "activated_at", _timestamp(self.activated_at, "activated_at"))
        if self.archived_at is not None:
            object.__setattr__(self, "archived_at", _timestamp(self.archived_at, "archived_at"))
        if self.status is PoolStatus.DRAFT and (self.activated_at is not None or self.archived_at is not None):
            raise VirtualPoolError("draft pools cannot have activation or archive timestamps")
        if self.status is PoolStatus.ACTIVE and (self.activated_at is None or self.archived_at is not None):
            raise VirtualPoolError("active pools require activated_at and cannot have archived_at")
        if self.status is PoolStatus.ARCHIVED and self.archived_at is None:
            raise VirtualPoolError("archived pools require archived_at")

    @property
    def total_units(self) -> int:
        """Fixed simulated unit supply. It never represents a payment balance."""
        return self.starting_nav_cents


@dataclass(frozen=True)
class PoolMember:
    """A member and virtual-unit allocation in a simulated pool."""

    pool_id: str
    member_id: str
    status: MemberStatus
    units: int
    joined_at: str
    role: str = "MEMBER"

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", _identifier(self.pool_id, "pool_id"))
        object.__setattr__(self, "member_id", _identifier(self.member_id, "member_id"))
        object.__setattr__(self, "status", _enum(self.status, MemberStatus, "status"))
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 0:
            raise VirtualPoolError("units must be a non-negative integer")
        object.__setattr__(self, "joined_at", _timestamp(self.joined_at, "joined_at"))
        if self.role not in {"CREATOR", "MEMBER"}:
            raise VirtualPoolError("role must be CREATOR or MEMBER")

    def ownership_fraction(self, total_units: int) -> Fraction:
        if isinstance(total_units, bool) or not isinstance(total_units, int) or total_units <= 0:
            raise VirtualPoolError("total_units must be a positive integer")
        return Fraction(self.units, total_units)


@dataclass(frozen=True)
class Invitation:
    """A simulated virtual-pool invitation; it never grants money rights."""

    invitation_id: str
    pool_id: str
    invitee_id: str
    invited_by: str
    status: InvitationStatus
    created_at: str
    activated_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "invitation_id", _identifier(self.invitation_id, "invitation_id"))
        object.__setattr__(self, "pool_id", _identifier(self.pool_id, "pool_id"))
        object.__setattr__(self, "invitee_id", _identifier(self.invitee_id, "invitee_id"))
        object.__setattr__(self, "invited_by", _identifier(self.invited_by, "invited_by"))
        object.__setattr__(self, "status", _enum(self.status, InvitationStatus, "status"))
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        if self.activated_at is not None:
            object.__setattr__(self, "activated_at", _timestamp(self.activated_at, "activated_at"))
        if self.status is InvitationStatus.ACTIVATED and self.activated_at is None:
            raise VirtualPoolError("activated invitations require activated_at")
        if self.status is not InvitationStatus.ACTIVATED and self.activated_at is not None:
            raise VirtualPoolError("only activated invitations may have activated_at")


@dataclass(frozen=True)
class AuditEvent:
    """An immutable event in the simulated virtual-pool audit trail."""

    sequence: int
    pool_id: str
    action: AuditAction
    actor_id: str
    subject_id: str
    created_at: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise VirtualPoolError("sequence must be a positive integer")
        object.__setattr__(self, "pool_id", _identifier(self.pool_id, "pool_id"))
        object.__setattr__(self, "action", _enum(self.action, AuditAction, "action"))
        object.__setattr__(self, "actor_id", _identifier(self.actor_id, "actor_id"))
        object.__setattr__(self, "subject_id", _identifier(self.subject_id, "subject_id"))
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))


@runtime_checkable
class Store(Protocol):
    """Persistence boundary used by :class:`VirtualPoolService`.

    Implementations must make each mutating operation durable before returning
    and append the supplied audit action in the same transaction.
    """

    def create_pool(self, pool: VirtualPool, creator: PoolMember, event: AuditEvent) -> None: ...
    def get_pool(self, pool_id: str) -> VirtualPool | None: ...
    def save_pool(self, pool: VirtualPool, event: AuditEvent) -> None: ...
    def list_members(self, pool_id: str) -> tuple[PoolMember, ...]: ...
    def get_member(self, pool_id: str, member_id: str) -> PoolMember | None: ...
    def create_invitation(self, invitation: Invitation, event: AuditEvent) -> None: ...
    def get_invitation(self, invitation_id: str) -> Invitation | None: ...
    def activate_invitation(self, invitation: Invitation, member: PoolMember, event: AuditEvent) -> None: ...
    def revoke_invitation(self, invitation: Invitation, event: AuditEvent) -> None: ...
    def audit_events(self, pool_id: str) -> tuple[AuditEvent, ...]: ...


class SqliteVirtualPoolStore:
    """Small durable SQLite store for the simulated virtual-pool protocol."""

    def __init__(self, path: str | Path) -> None:
        raw_path = str(path)
        self.path = raw_path if raw_path == ":memory:" else str(Path(raw_path).expanduser())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock, self._connection:
            self._connection.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS virtual_pools (
                    pool_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mandate TEXT NOT NULL,
                    base_currency TEXT NOT NULL,
                    starting_nav_cents INTEGER NOT NULL CHECK(starting_nav_cents > 0),
                    mandate_risk TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS virtual_pool_members (
                    pool_id TEXT NOT NULL REFERENCES virtual_pools(pool_id),
                    member_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    units INTEGER NOT NULL CHECK(units >= 0),
                    joined_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(pool_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS virtual_pool_invitations (
                    invitation_id TEXT PRIMARY KEY,
                    pool_id TEXT NOT NULL REFERENCES virtual_pools(pool_id),
                    invitee_id TEXT NOT NULL,
                    invited_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS virtual_pool_pending_invitee
                    ON virtual_pool_invitations(pool_id, invitee_id) WHERE status = 'PENDING';
                CREATE TABLE IF NOT EXISTS virtual_pool_audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_id TEXT NOT NULL REFERENCES virtual_pools(pool_id),
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            # Keep local repositories created by an early module version readable.
            try:
                self._connection.execute(
                    "ALTER TABLE virtual_pools ADD COLUMN mandate TEXT NOT NULL "
                    "DEFAULT 'Legacy virtual simulation mandate'"
                )
            except sqlite3.OperationalError:
                pass

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_pool(self, pool: VirtualPool, creator: PoolMember, event: AuditEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO virtual_pools(
                    pool_id, name, mandate, base_currency, starting_nav_cents, mandate_risk,
                    creator_id, status, created_at, activated_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pool.pool_id, pool.name, pool.mandate, pool.base_currency, pool.starting_nav_cents,
                 pool.mandate_risk.value, pool.creator_id, pool.status.value, pool.created_at,
                 pool.activated_at, pool.archived_at),
            )
            self._connection.execute(
                """INSERT INTO virtual_pool_members VALUES (?, ?, ?, ?, ?, ?)""",
                (creator.pool_id, creator.member_id, creator.status.value, creator.units, creator.joined_at, creator.role),
            )
            self._append_event(event)

    def get_pool(self, pool_id: str) -> VirtualPool | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM virtual_pools WHERE pool_id = ?", (pool_id,)).fetchone()
        return self._pool(row) if row is not None else None

    def save_pool(self, pool: VirtualPool, event: AuditEvent) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE virtual_pools SET status=?, activated_at=?, archived_at=? WHERE pool_id=?""",
                (pool.status.value, pool.activated_at, pool.archived_at, pool.pool_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("unknown pool_id")
            self._append_event(event)

    def list_members(self, pool_id: str) -> tuple[PoolMember, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_pool_members WHERE pool_id = ? ORDER BY member_id", (pool_id,)
            ).fetchall()
        return tuple(self._member(row) for row in rows)

    def get_member(self, pool_id: str, member_id: str) -> PoolMember | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM virtual_pool_members WHERE pool_id = ? AND member_id = ?", (pool_id, member_id)
            ).fetchone()
        return self._member(row) if row is not None else None

    def create_invitation(self, invitation: Invitation, event: AuditEvent) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO virtual_pool_invitations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invitation.invitation_id, invitation.pool_id, invitation.invitee_id, invitation.invited_by,
                 invitation.status.value, invitation.created_at, invitation.activated_at),
            )
            self._append_event(event)

    def get_invitation(self, invitation_id: str) -> Invitation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM virtual_pool_invitations WHERE invitation_id = ?", (invitation_id,)
            ).fetchone()
        return self._invitation(row) if row is not None else None

    def activate_invitation(self, invitation: Invitation, member: PoolMember, event: AuditEvent) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE virtual_pool_invitations SET status=?, activated_at=? WHERE invitation_id=? AND status='PENDING'",
                (invitation.status.value, invitation.activated_at, invitation.invitation_id),
            )
            if cursor.rowcount != 1:
                raise LifecycleError("invitation is no longer pending")
            self._connection.execute(
                "INSERT INTO virtual_pool_members VALUES (?, ?, ?, ?, ?, ?)",
                (member.pool_id, member.member_id, member.status.value, member.units, member.joined_at, member.role),
            )
            self._append_event(event)

    def revoke_invitation(self, invitation: Invitation, event: AuditEvent) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE virtual_pool_invitations SET status=? WHERE invitation_id=? AND status='PENDING'",
                (invitation.status.value, invitation.invitation_id),
            )
            if cursor.rowcount != 1:
                raise LifecycleError("invitation is no longer pending")
            self._append_event(event)

    def audit_events(self, pool_id: str) -> tuple[AuditEvent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM virtual_pool_audit_events WHERE pool_id=? ORDER BY sequence", (pool_id,)
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def _append_event(self, event: AuditEvent) -> None:
        self._connection.execute(
            "INSERT INTO virtual_pool_audit_events(pool_id, action, actor_id, subject_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (event.pool_id, event.action.value, event.actor_id, event.subject_id, event.created_at),
        )

    @staticmethod
    def _pool(row: sqlite3.Row) -> VirtualPool:
        return VirtualPool(**dict(row))

    @staticmethod
    def _member(row: sqlite3.Row) -> PoolMember:
        return PoolMember(**dict(row))

    @staticmethod
    def _invitation(row: sqlite3.Row) -> Invitation:
        return Invitation(**dict(row))

    @staticmethod
    def _event(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(**dict(row))


class VirtualPoolService:
    """Lifecycle coordinator that depends only on the :class:`Store` protocol."""

    def __init__(self, store: Store) -> None:
        if not isinstance(store, Store):
            raise TypeError("store must implement the virtual-pool Store protocol")
        self._store = store

    def create_pool(
        self,
        *,
        pool_id: object,
        name: object,
        mandate: object,
        base_currency: object,
        starting_nav_cents: object,
        mandate_risk: object,
        creator_id: object,
        created_at: object | None = None,
    ) -> VirtualPool:
        identifier = _identifier(pool_id, "pool_id")
        normalized_name = _safe_name(name)
        normalized_mandate = _safe_mandate(mandate)
        currency = _currency(base_currency)
        nav_cents = _positive_cents(starting_nav_cents)
        risk = _enum(mandate_risk, MandateRisk, "mandate_risk")
        creator_id = _identifier(creator_id, "creator_id")
        requested_created_at = None if created_at is None else _timestamp(created_at, "created_at")

        existing = self._store.get_pool(identifier)
        if existing is not None:
            immutable_fields_match = (
                existing.name == normalized_name
                and existing.mandate == normalized_mandate
                and existing.base_currency == currency
                and existing.starting_nav_cents == nav_cents
                and existing.mandate_risk is risk
                and existing.creator_id == creator_id
            )
            if not immutable_fields_match or (
                requested_created_at is not None and existing.created_at != requested_created_at
            ):
                raise VirtualPoolError("pool_id is already bound to a different virtual pool creation")
            return existing

        timestamp = requested_created_at or _now()
        pool = VirtualPool(
            pool_id=identifier,
            name=normalized_name,
            mandate=normalized_mandate,
            base_currency=currency,
            starting_nav_cents=nav_cents,
            mandate_risk=risk,
            creator_id=creator_id,
            status=PoolStatus.DRAFT,
            created_at=timestamp,
        )
        creator = PoolMember(pool.pool_id, pool.creator_id, MemberStatus.ACTIVE, pool.total_units, timestamp, "CREATOR")
        self._store.create_pool(
            pool,
            creator,
            self._event(pool.pool_id, AuditAction.POOL_CREATED, pool.creator_id, pool.pool_id, timestamp),
        )
        return pool

    def activate_pool(self, *, pool_id: object, actor_id: object, activated_at: object | None = None) -> VirtualPool:
        pool = self._required_pool(pool_id)
        actor = _identifier(actor_id, "actor_id")
        if actor != pool.creator_id:
            raise LifecycleError("only the creator can activate a virtual pool")
        if pool.status is not PoolStatus.DRAFT:
            raise LifecycleError("only draft pools can be activated")
        timestamp = _now() if activated_at is None else _timestamp(activated_at, "activated_at")
        updated = VirtualPool(**{**pool.__dict__, "status": PoolStatus.ACTIVE, "activated_at": timestamp})
        self._store.save_pool(updated, self._event(pool.pool_id, AuditAction.POOL_ACTIVATED, actor, pool.pool_id, timestamp))
        return updated

    def archive_pool(self, *, pool_id: object, actor_id: object, archived_at: object | None = None) -> VirtualPool:
        pool = self._required_pool(pool_id)
        actor = _identifier(actor_id, "actor_id")
        if actor != pool.creator_id:
            raise LifecycleError("only the creator can archive a virtual pool")
        if pool.status is not PoolStatus.ACTIVE:
            raise LifecycleError("only active pools can be archived")
        timestamp = _now() if archived_at is None else _timestamp(archived_at, "archived_at")
        updated = VirtualPool(**{**pool.__dict__, "status": PoolStatus.ARCHIVED, "archived_at": timestamp})
        self._store.save_pool(updated, self._event(pool.pool_id, AuditAction.POOL_ARCHIVED, actor, pool.pool_id, timestamp))
        return updated

    def invite_member(
        self,
        *,
        invitation_id: object,
        pool_id: object,
        invitee_id: object,
        actor_id: object,
        created_at: object | None = None,
    ) -> Invitation:
        identifier = _identifier(invitation_id, "invitation_id")
        pool = self._required_pool(pool_id)
        actor = _identifier(actor_id, "actor_id")
        invitee = _identifier(invitee_id, "invitee_id")
        requested_created_at = None if created_at is None else _timestamp(created_at, "created_at")

        existing = self._store.get_invitation(identifier)
        if existing is not None:
            immutable_fields_match = (
                existing.pool_id == pool.pool_id
                and existing.invitee_id == invitee
                and existing.invited_by == actor
            )
            if not immutable_fields_match or (
                requested_created_at is not None and existing.created_at != requested_created_at
            ):
                raise VirtualPoolError("invitation_id is already bound to a different invitation")
            return existing

        if pool.status is PoolStatus.ARCHIVED:
            raise LifecycleError("archived pools cannot issue invitations")
        if actor != pool.creator_id:
            raise LifecycleError("only the creator can issue invitations")
        if invitee == pool.creator_id or self._store.get_member(pool.pool_id, invitee) is not None:
            raise LifecycleError("an active member cannot be invited")
        timestamp = requested_created_at or _now()
        invitation = Invitation(identifier, pool.pool_id, invitee, actor, InvitationStatus.PENDING, timestamp)
        self._store.create_invitation(
            invitation,
            self._event(pool.pool_id, AuditAction.INVITATION_CREATED, actor, invitee, timestamp),
        )
        return invitation

    def activate_invitation(self, *, invitation_id: object, actor_id: object, activated_at: object | None = None) -> PoolMember:
        invitation = self._required_invitation(invitation_id)
        actor = _identifier(actor_id, "actor_id")
        pool = self._required_pool(invitation.pool_id)
        if pool.status is not PoolStatus.ACTIVE:
            raise LifecycleError("invitations can be activated only for active pools")
        if actor != invitation.invitee_id:
            raise LifecycleError("only the invitee can activate an invitation")
        if invitation.status is not InvitationStatus.PENDING:
            raise LifecycleError("only pending invitations can be activated")
        if self._store.get_member(pool.pool_id, actor) is not None:
            raise LifecycleError("an active member cannot activate an invitation")
        timestamp = _now() if activated_at is None else _timestamp(activated_at, "activated_at")
        activated = Invitation(**{**invitation.__dict__, "status": InvitationStatus.ACTIVATED, "activated_at": timestamp})
        member = PoolMember(pool.pool_id, actor, MemberStatus.ACTIVE, 0, timestamp)
        self._store.activate_invitation(activated, member, self._event(pool.pool_id, AuditAction.INVITATION_ACTIVATED, actor, actor, timestamp))
        return member

    def revoke_invitation(self, *, invitation_id: object, actor_id: object, revoked_at: object | None = None) -> Invitation:
        invitation = self._required_invitation(invitation_id)
        actor = _identifier(actor_id, "actor_id")
        pool = self._required_pool(invitation.pool_id)
        if actor != pool.creator_id:
            raise LifecycleError("only the creator can revoke an invitation")
        if invitation.status is not InvitationStatus.PENDING:
            raise LifecycleError("only pending invitations can be revoked")
        timestamp = _now() if revoked_at is None else _timestamp(revoked_at, "revoked_at")
        revoked = Invitation(**{**invitation.__dict__, "status": InvitationStatus.REVOKED})
        self._store.revoke_invitation(revoked, self._event(pool.pool_id, AuditAction.INVITATION_REVOKED, actor, invitation.invitee_id, timestamp))
        return revoked

    def pool(self, pool_id: object) -> VirtualPool:
        return self._required_pool(pool_id)

    def invitation(self, invitation_id: object) -> Invitation:
        return self._required_invitation(invitation_id)

    def members(self, pool_id: object) -> tuple[PoolMember, ...]:
        pool = self._required_pool(pool_id)
        return self._store.list_members(pool.pool_id)

    def unit_ownership(self, pool_id: object) -> dict[str, int]:
        """Return exact, deterministic virtual-unit ownership by member ID."""
        pool = self._required_pool(pool_id)
        members = self._store.list_members(pool.pool_id)
        ownership = {member.member_id: member.units for member in members}
        if sum(ownership.values()) != pool.total_units:
            raise LifecycleError("stored member units do not match fixed virtual unit supply")
        return ownership

    def audit_events(self, pool_id: object) -> tuple[AuditEvent, ...]:
        pool = self._required_pool(pool_id)
        return self._store.audit_events(pool.pool_id)

    def _required_pool(self, pool_id: object) -> VirtualPool:
        identifier = _identifier(pool_id, "pool_id")
        pool = self._store.get_pool(identifier)
        if pool is None:
            raise NotFoundError("unknown pool_id")
        return pool

    def _required_invitation(self, invitation_id: object) -> Invitation:
        identifier = _identifier(invitation_id, "invitation_id")
        invitation = self._store.get_invitation(identifier)
        if invitation is None:
            raise NotFoundError("unknown invitation_id")
        return invitation

    @staticmethod
    def _event(pool_id: str, action: AuditAction, actor_id: str, subject_id: str, created_at: str) -> AuditEvent:
        # The store assigns the durable sequence. A valid placeholder lets the
        # generic protocol transport an event before that assignment.
        return AuditEvent(1, pool_id, action, actor_id, subject_id, created_at)


# The short name is intentionally convenient for local callers while the
# service remains coupled only to Store.
VirtualPoolStore = SqliteVirtualPoolStore
