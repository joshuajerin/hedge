"""Durable, credential-free state for Hedge Slack correlation.

This module deliberately stores only opaque Slack identifiers and delivery state.
It never stores Slack tokens, message bodies, or Brainbase credentials.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_deadline(value: str) -> datetime:
    """Parse an ISO-8601 deadline; never treat a naive time as local/UTC."""

    if not isinstance(value, str) or not value:
        raise ValueError("deadline_at must be a timezone-aware ISO-8601 timestamp")
    try:
        deadline = datetime.fromisoformat(value)
        offset = deadline.utcoffset()
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("deadline_at must be a timezone-aware ISO-8601 timestamp") from error
    if offset is None:
        raise ValueError("deadline_at must be a timezone-aware ISO-8601 timestamp")
    return deadline.astimezone(UTC)


@dataclass(frozen=True)
class ThreadCorrelation:
    """The one canonical Slack destination for a Hedge run."""

    run_id: str
    workspace_id: str
    channel_id: str
    root_thread_ts: str

    def __post_init__(self) -> None:
        for name, value in (
            ("run_id", self.run_id),
            ("workspace_id", self.workspace_id),
            ("channel_id", self.channel_id),
            ("root_thread_ts", self.root_thread_ts),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required")


class SlackState:
    """SQLite-backed event idempotency and canonical thread-run state.

    A delivery is reserved before the injected sender is called.  A sender
    exception is kept as a terminal failure rather than retried automatically,
    because a timeout can mean Slack accepted the message.  This is intentionally
    fail-closed: callers must create a new, explicitly reviewed delivery event
    to retry.
    """

    def __init__(self, path: str | Path) -> None:
        raw_path = str(path)
        self.path = raw_path if raw_path == ":memory:" else str(Path(raw_path).expanduser())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    root_thread_ts TEXT NOT NULL,
                    event_id TEXT,
                    requester TEXT,
                    deadline_at TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thread_runs (
                    workspace_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    root_thread_ts TEXT NOT NULL,
                    current_run_id TEXT NOT NULL REFERENCES runs(run_id),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, channel_id, root_thread_ts)
                );
                CREATE TABLE IF NOT EXISTS delivery_events (
                    delivery_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing local state predates explicit operator metadata.  These
            # migrations are additive and retain old correlation records.
            for column in ("event_id TEXT", "requester TEXT", "deadline_at TEXT"):
                try:
                    self._connection.execute(f"ALTER TABLE runs ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def claim_inbound_event(self, event_id: str, workspace_id: str = "") -> bool:
        """Atomically claim a Slack event. False means it was already seen."""

        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id is required")
        event_key = f"{workspace_id}:{event_id}" if workspace_id else event_id
        with self._lock:
            try:
                self._connection.execute(
                    "INSERT INTO inbound_events(event_id, received_at) VALUES (?, ?)",
                    (event_key, _now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    # Short alias for adapters that use the old in-memory guard vocabulary.
    claim_event = claim_inbound_event

    def expire_overdue_runs(self) -> tuple[str, ...]:
        """Mark bounded unfinished runs expired. Missing/bad deadlines stay blocked.

        The admission path also expires its own thread inside its transaction,
        so the bridge does not need a poller to accept a fresh mention.
        """

        now = datetime.now(UTC)
        expired: list[str] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT run_id, deadline_at FROM runs WHERE status IN ('pending', 'started')"
                ).fetchall()
                for row in rows:
                    if self._expire_if_overdue(row, now):
                        expired.append(str(row["run_id"]))
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return tuple(expired)

    def _expire_if_overdue(self, row: sqlite3.Row, now: datetime) -> bool:
        # Malformed legacy rows must not release the thread. Operators must
        # review them and explicitly mark them failed/completed instead.
        try:
            deadline = _utc_deadline(row["deadline_at"])
        except ValueError:
            return False
        if deadline > now:
            return False
        self._connection.execute(
            "UPDATE runs SET status = 'expired', updated_at = ? "
            "WHERE run_id = ? AND status IN ('pending', 'started')",
            (now.isoformat(), row["run_id"]),
        )
        return True

    def record_run(
        self,
        correlation: ThreadCorrelation,
        *,
        event_id: str = "",
        requester: str = "",
        deadline_at: str = "",
    ) -> bool:
        """Admit a new run atomically after expiring a bounded previous run.

        An existing run ID is never admitted again, even after completion or
        expiration. A new Slack event must use a new ID; duplicate inbound
        event protection remains in claim_inbound_event().
        """

        if deadline_at:
            _utc_deadline(deadline_at)
        elif not isinstance(deadline_at, str):
            raise ValueError("deadline_at must be a timezone-aware ISO-8601 timestamp")
        now = datetime.now(UTC)
        values = (
            correlation.run_id,
            correlation.workspace_id,
            correlation.channel_id,
            correlation.root_thread_ts,
        )
        with self._lock:
            # Serialize admission across separate SlackState instances/processes.
            # The expiration and thread switch must commit together.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT workspace_id, channel_id, root_thread_ts FROM runs WHERE run_id = ?",
                    (correlation.run_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != values[1:]:
                        raise ValueError("run_id is already bound to a different Slack thread")
                    self._connection.execute("COMMIT")
                    return False
                active = self._connection.execute(
                    """SELECT runs.run_id, runs.status, runs.deadline_at FROM thread_runs
                       JOIN runs ON runs.run_id = thread_runs.current_run_id
                       WHERE thread_runs.workspace_id = ? AND thread_runs.channel_id = ?
                       AND thread_runs.root_thread_ts = ?""",
                    values[1:],
                ).fetchone()
                if active is not None and active["status"] in {"pending", "started"}:
                    if not self._expire_if_overdue(active, now):
                        self._connection.execute("COMMIT")
                        return False
                self._connection.execute(
                    """INSERT INTO runs(
                        run_id, workspace_id, channel_id, root_thread_ts, event_id, requester, deadline_at,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (*values, event_id or None, requester or None, deadline_at or None,
                     now.isoformat(), now.isoformat()),
                )
                self._connection.execute(
                    """INSERT INTO thread_runs(
                        workspace_id, channel_id, root_thread_ts, current_run_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id, channel_id, root_thread_ts) DO UPDATE SET
                        current_run_id=excluded.current_run_id, updated_at=excluded.updated_at""",
                    (*values[1:], correlation.run_id, now.isoformat()),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return True

    def set_run_status(self, run_id: str, status: str) -> None:
        if status not in {"pending", "started", "failed", "completed"}:
            raise ValueError("invalid run status")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? "
                "WHERE run_id = ? AND status != 'expired'",
                (status, _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown or expired run_id")

    def correlation_for_run(self, run_id: str) -> ThreadCorrelation | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id, workspace_id, channel_id, root_thread_ts FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return ThreadCorrelation(**dict(row)) if row is not None else None

    def current_run_for_thread(self, workspace_id: str, channel_id: str, root_thread_ts: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT current_run_id FROM thread_runs
                   WHERE workspace_id = ? AND channel_id = ? AND root_thread_ts = ?""",
                (workspace_id, channel_id, root_thread_ts),
            ).fetchone()
        return str(row["current_run_id"]) if row is not None else None

    def has_canonical_correlation(self, correlation: ThreadCorrelation) -> bool:
        return self.correlation_for_run(correlation.run_id) == correlation

    def reserve_delivery(self, delivery_id: str, correlation: ThreadCorrelation, role: str) -> bool:
        """Reserve one delivery only if its supplied destination is canonical."""

        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("delivery_id is required")
        if not isinstance(role, str) or not role:
            raise ValueError("role is required")
        with self._lock:
            row = self._connection.execute(
                "SELECT workspace_id, channel_id, root_thread_ts FROM runs WHERE run_id = ?",
                (correlation.run_id,),
            ).fetchone()
            if row is None or tuple(row) != (
                correlation.workspace_id,
                correlation.channel_id,
                correlation.root_thread_ts,
            ):
                return False
            try:
                self._connection.execute(
                    """INSERT INTO delivery_events(
                        delivery_id, run_id, role, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'reserved', ?, ?)""",
                    (delivery_id, correlation.run_id, role, _now(), _now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def finish_delivery(self, delivery_id: str, status: str) -> None:
        if status not in {"delivered", "failed"}:
            raise ValueError("invalid delivery status")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE delivery_events SET status = ?, updated_at = ? WHERE delivery_id = ?",
                (status, _now(), delivery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown delivery_id")

    def delivery_status(self, delivery_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM delivery_events WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return str(row["status"]) if row is not None else None
