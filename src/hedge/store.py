"""Small local SQLite state store for virtual-simulation Hedge data."""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
from pathlib import Path
from typing import Any


_MAX_CENTS = 9_223_372_036_854_775_807
_ID_MAX_LENGTH = 128


class ContributionEventConflict(ValueError):
    """An immutable virtual contribution event ID was reused with new data."""


def _identifier(value: object, field: str) -> str:
    """Return a conservative local identifier, rejecting ambiguous values."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string identifier")
    cleaned = value.strip()
    if not cleaned or cleaned != value or len(cleaned) > _ID_MAX_LENGTH:
        raise ValueError(f"{field} must be a non-empty identifier without surrounding whitespace")
    if not cleaned.isascii() or not all(character.isalnum() or character in "._:-" for character in cleaned):
        raise ValueError(f"{field} contains unsupported characters")
    return cleaned


def _positive_cents(value: object, field: str = "cents") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer number of cents")
    if value <= 0:
        raise ValueError(f"{field} must be positive; negative or zero virtual capital is not supported")
    if value > _MAX_CENTS:
        raise ValueError(f"{field} exceeds SQLite integer cents range")
    return value


def _event_time(value: object | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("created_at must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("created_at must include a UTC offset")
    return value


def _virtual_action(value: object) -> str:
    if value not in {"VIRTUAL_STARTING_BALANCE", "VIRTUAL_CONTRIBUTION"}:
        raise ValueError(
            "only VIRTUAL_STARTING_BALANCE or VIRTUAL_CONTRIBUTION events are allowed"
        )
    return str(value)


class LocalStore:
    """Persist local idempotency records and an append-only virtual ledger.

    The contribution ledger records simulated contributions only. Every row is
    an immutable ``VIRTUAL_CONTRIBUTION`` event in integer cents.
    """

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS handled_decisions "
            "(decision_id TEXT PRIMARY KEY, broker_refs TEXT NOT NULL)"
        )
        # Kept for compatibility with existing local databases. New account
        # behavior reads only the append-only events table below.
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS virtual_contributions "
            "(pool_id TEXT NOT NULL, member_id TEXT NOT NULL, cents INTEGER NOT NULL, "
            "PRIMARY KEY (pool_id, member_id))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS virtual_contribution_events ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_id TEXT NOT NULL UNIQUE, "
            "pool_id TEXT NOT NULL, "
            "member_id TEXT NOT NULL, "
            "cents INTEGER NOT NULL CHECK (cents > 0), "
            "action TEXT NOT NULL CHECK (action IN ('VIRTUAL_STARTING_BALANCE', 'VIRTUAL_CONTRIBUTION')), "
            "created_at TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS virtual_contribution_events_pool_member_sequence "
            "ON virtual_contribution_events(pool_id, member_id, sequence)"
        )
        self.connection.execute(
            "CREATE TRIGGER IF NOT EXISTS virtual_contribution_events_immutable_update "
            "BEFORE UPDATE ON virtual_contribution_events "
            "BEGIN SELECT RAISE(ABORT, 'virtual contribution events are immutable'); END"
        )
        self.connection.execute(
            "CREATE TRIGGER IF NOT EXISTS virtual_contribution_events_immutable_delete "
            "BEFORE DELETE ON virtual_contribution_events "
            "BEGIN SELECT RAISE(ABORT, 'virtual contribution events are immutable'); END"
        )
        self.connection.commit()

    def close(self) -> None:
        """Close the SQLite connection owned by this store."""
        self.connection.close()

    def handled(self, decision_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM handled_decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone() is not None

    def record(self, decision_id: str, broker_refs: str) -> None:
        self.connection.execute(
            "INSERT INTO handled_decisions(decision_id, broker_refs) VALUES (?, ?)",
            (decision_id, broker_refs),
        )
        self.connection.commit()

    def record_virtual_starting_balance(
        self,
        *,
        event_id: object,
        pool_id: object,
        member_id: object,
        cents: object,
        created_at: object | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append one immutable virtual starting-balance event."""
        return self.record_virtual_contribution(
            event_id=event_id,
            pool_id=pool_id,
            member_id=member_id,
            cents=cents,
            created_at=created_at,
            action="VIRTUAL_STARTING_BALANCE",
        )

    def record_virtual_contribution(
        self,
        *,
        event_id: object,
        pool_id: object,
        member_id: object,
        cents: object,
        created_at: object | None = None,
        action: object = "VIRTUAL_CONTRIBUTION",
    ) -> tuple[dict[str, Any], bool]:
        """Append a virtual starting-balance or contribution event.

        Repeating an event ID with the same pool, member, cents, and action is
        idempotent. Reusing it with any different immutable payload raises
        :class:`ContributionEventConflict`. Only virtual event actions are accepted.
        """
        event = {
            "event_id": _identifier(event_id, "event_id"),
            "pool_id": _identifier(pool_id, "pool_id"),
            "member_id": _identifier(member_id, "member_id"),
            "cents": _positive_cents(cents),
            "action": _virtual_action(action),
        }
        requested_time = _event_time(created_at)
        existing = self.connection.execute(
            "SELECT sequence, event_id, pool_id, member_id, cents, action, created_at "
            "FROM virtual_contribution_events WHERE event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if existing is not None:
            stored = self._event_dict(existing)
            if any(stored[key] != event[key] for key in ("event_id", "pool_id", "member_id", "cents", "action")):
                raise ContributionEventConflict("event_id already belongs to a different virtual contribution")
            if created_at is not None and stored["created_at"] != requested_time:
                raise ContributionEventConflict("event_id already belongs to a different virtual contribution")
            return stored, True

        try:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO virtual_contribution_events "
                    "(event_id, pool_id, member_id, cents, action, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        event["event_id"],
                        event["pool_id"],
                        event["member_id"],
                        event["cents"],
                        event["action"],
                        requested_time,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # A second local writer may have won after the initial lookup.
            existing = self.connection.execute(
                "SELECT sequence, event_id, pool_id, member_id, cents, action, created_at "
                "FROM virtual_contribution_events WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
            if existing is None:
                raise exc
            stored = self._event_dict(existing)
            if any(stored[key] != event[key] for key in ("event_id", "pool_id", "member_id", "cents", "action")):
                raise ContributionEventConflict("event_id already belongs to a different virtual contribution") from exc
            if created_at is not None and stored["created_at"] != requested_time:
                raise ContributionEventConflict("event_id already belongs to a different virtual contribution") from exc
            return stored, True

        return {"sequence": int(cursor.lastrowid), **event, "created_at": requested_time}, False

    def virtual_contribution_history(
        self, *, pool_id: object, member_id: object | None = None
    ) -> tuple[dict[str, Any], ...]:
        """Return immutable ledger rows in append order for one virtual pool."""
        pool = _identifier(pool_id, "pool_id")
        if member_id is None:
            rows = self.connection.execute(
                "SELECT sequence, event_id, pool_id, member_id, cents, action, created_at "
                "FROM virtual_contribution_events WHERE pool_id = ? ORDER BY sequence",
                (pool,),
            ).fetchall()
        else:
            member = _identifier(member_id, "member_id")
            rows = self.connection.execute(
                "SELECT sequence, event_id, pool_id, member_id, cents, action, created_at "
                "FROM virtual_contribution_events WHERE pool_id = ? AND member_id = ? ORDER BY sequence",
                (pool, member),
            ).fetchall()
        return tuple(self._event_dict(row) for row in rows)

    def virtual_contribution_balances(self, *, pool_id: object) -> dict[str, int]:
        """Return cumulative virtual contributed cents by member, sorted by ID."""
        pool = _identifier(pool_id, "pool_id")
        rows = self.connection.execute(
            "SELECT member_id, COALESCE(SUM(cents), 0) FROM virtual_contribution_events "
            "WHERE pool_id = ? GROUP BY member_id ORDER BY member_id",
            (pool,),
        ).fetchall()
        return {str(member_id): int(cents) for member_id, cents in rows}

    @staticmethod
    def _event_dict(row: tuple[object, ...]) -> dict[str, Any]:
        sequence, event_id, pool_id, member_id, cents, action, created_at = row
        return {
            "sequence": int(sequence),
            "event_id": str(event_id),
            "pool_id": str(pool_id),
            "member_id": str(member_id),
            "cents": int(cents),
            "action": str(action),
            "created_at": str(created_at),
        }
