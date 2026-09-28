"""Bounded research run admission and durable Slack-event replay protection."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import sqlite3
from threading import Barrier

import pytest

from hedge.slack_state import SlackState, ThreadCorrelation


def run(name: str) -> ThreadCorrelation:
    return ThreadCorrelation(name, "workspace", "channel", "1710000000.000100")


def deadline(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def status(path, run_id: str) -> str:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]


def test_active_and_unbounded_runs_block_new_root_thread_mentions(tmp_path) -> None:
    state = SlackState(tmp_path / "state.db")
    assert state.record_run(run("active"), deadline_at=deadline(600))
    state.set_run_status("active", "started")
    assert not state.record_run(run("new"), deadline_at=deadline(600))
    assert state.current_run_for_thread("workspace", "channel", run("active").root_thread_ts) == "active"
    state.close()

    unbounded = SlackState(tmp_path / "unbounded.db")
    assert unbounded.record_run(run("unbounded"))
    assert unbounded.expire_overdue_runs() == ()
    assert not unbounded.record_run(run("no"), deadline_at=deadline(600))
    unbounded.close()


def test_started_run_expires_and_new_mention_proceeds_without_replaying_old_run(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = SlackState(path)
    assert state.claim_inbound_event("Ev-first", "workspace")
    assert state.record_run(run("first"), event_id="Ev-first", deadline_at=deadline(-60))
    state.set_run_status("first", "started")
    assert state.record_run(run("second"), event_id="Ev-second", deadline_at=deadline(600))
    assert status(path, "first") == "expired"
    assert state.current_run_for_thread("workspace", "channel", run("first").root_thread_ts) == "second"
    assert not state.record_run(run("first"), deadline_at=deadline(600))
    with pytest.raises(ValueError, match="expired"):
        state.set_run_status("first", "completed")
    state.close()

    reopened = SlackState(path)
    assert not reopened.claim_inbound_event("Ev-first", "workspace")
    assert not reopened.record_run(run("first"), deadline_at=deadline(600))
    assert not reopened.record_run(run("third"), deadline_at=deadline(600))
    reopened.close()


def test_explicit_expiration_transition_is_idempotent(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = SlackState(path)
    assert state.record_run(run("old"), deadline_at=deadline(-30))
    state.set_run_status("old", "started")
    assert state.expire_overdue_runs() == ("old",)
    assert state.expire_overdue_runs() == ()
    assert status(path, "old") == "expired"
    assert state.record_run(run("fresh"), deadline_at=deadline(600))
    state.close()


def test_malformed_stored_deadline_fails_closed_and_input_must_be_aware(tmp_path) -> None:
    path = tmp_path / "state.db"
    state = SlackState(path)
    for bad in ("yesterday", "2026-01-01T00:00:00", "2026-01-01", 123):
        with pytest.raises(ValueError, match="deadline_at"):
            state.record_run(run("bad"), deadline_at=bad)
    assert state.record_run(run("legacy"), deadline_at=deadline(600))
    state._connection.execute("UPDATE runs SET deadline_at = ? WHERE run_id = 'legacy'", ("corrupted",))
    assert state.expire_overdue_runs() == ()
    assert not state.record_run(run("blocked"), deadline_at=deadline(600))
    state.set_run_status("legacy", "failed")
    assert state.record_run(run("reviewed"), deadline_at=deadline(600))
    state.close()


def test_competing_process_connections_only_admit_one_after_expiration(tmp_path) -> None:
    path = tmp_path / "state.db"
    seed = SlackState(path)
    assert seed.record_run(run("old"), deadline_at=deadline(-30))
    seed.set_run_status("old", "started")
    seed.close()
    barrier = Barrier(2)

    def contender(name: str) -> bool:
        connection = SlackState(path)
        try:
            barrier.wait(timeout=5)
            return connection.record_run(run(name), deadline_at=deadline(600))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(contender, ("new-a", "new-b")))
    assert sorted(results) == [False, True]
    assert status(path, "old") == "expired"
    reopened = SlackState(path)
    current = reopened.current_run_for_thread("workspace", "channel", run("old").root_thread_ts)
    assert current in {"new-a", "new-b"}
    assert not reopened.record_run(run("old"), deadline_at=deadline(600))
    reopened.close()


def test_run_id_never_reused_even_after_terminal_status(tmp_path) -> None:
    state = SlackState(tmp_path / "state.db")
    assert state.record_run(run("first"), deadline_at=deadline(600))
    assert not state.record_run(run("first"), deadline_at=deadline(600))
    state.set_run_status("first", "completed")
    assert not state.record_run(run("first"), deadline_at=deadline(600))
    assert state.record_run(run("second"), deadline_at=deadline(600))
    assert not state.record_run(run("first"), deadline_at=deadline(600))
    with pytest.raises(ValueError, match="different Slack thread"):
        state.record_run(ThreadCorrelation("first", "workspace", "other", "1710000000.000100"))
    state.close()
