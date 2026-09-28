"""Durable paper accounting contract tests; all cash and quotes are simulated."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from hedge.contracts import Decision
from hedge.paper_portfolio import PaperPortfolio, PriceQuote
from hedge.trading_system import IdempotencyConflict, TradingState

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def order(symbol="ABC", side="BUY", quantity=2):
    return Decision.draft(pool_id="pool", mandate_version=1, intents=[dict(
        symbol=symbol, side=side, quantity=quantity, order_type="MKT",
        rationale="paper only")])


def quote(cents=1000, at=NOW, source="fixture/feed"):
    return PriceQuote(cents, at, source)


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.db"
        self.now = NOW
        self.portfolio = PaperPortfolio(self.path, clock=lambda: self.now)
        self.portfolio.initialize("pool", 10000)

    def tearDown(self):
        self.portfolio.close()
        self.tmp.cleanup()

    def test_duplicate_restart_and_immutable_prices(self):
        decision = order()
        first = self.portfolio.process(decision, {"ABC": quote()})
        self.assertEqual(first.state, TradingState.COMPLETED)
        self.assertEqual(self.portfolio.process(decision, {"ABC": quote(5000)}).idempotent, True)
        self.assertEqual(self.portfolio.decision_prices(decision.decision_id)["ABC"].price_cents, 1000)
        self.assertEqual(len(self.portfolio.ledger("pool", decision.decision_id)), 5)
        self.portfolio.close()
        self.portfolio = PaperPortfolio(self.path, clock=lambda: self.now)
        again = self.portfolio.process(decision, {})
        self.assertTrue(again.idempotent)
        self.assertEqual(again.as_dict()["account"], first.as_dict()["account"])
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 8000)
        self.assertEqual(self.portfolio.snapshot("pool").positions["ABC"], 2)
        conflicting = Decision.from_dict({**decision.as_dict(), "intents": [
            {**decision.as_dict()["intents"][0], "quantity": 3}]})
        with self.assertRaises(IdempotencyConflict):
            self.portfolio.process(conflicting, {"ABC": quote()})

    def test_uncovered_sell_and_stale_quote_fail_closed(self):
        bad = self.portfolio.process(order(side="SELL"), {"ABC": quote()})
        self.assertEqual(bad.state, TradingState.REJECTED)
        self.assertIn("uncovered sell", bad.reason)
        stale = self.portfolio.process(order(), {"ABC": quote(at=NOW - timedelta(hours=1))})
        self.assertEqual(stale.state, TradingState.REJECTED)
        self.assertIn("stale", stale.reason)
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 10000)
        self.assertEqual(self.portfolio.snapshot("pool").positions, {})
        with self.assertRaisesRegex(ValueError, "quotes must cover"):
            self.portfolio.process(order(), {})

    def test_failed_write_rolls_back_every_change(self):
        # Abort after cash update, while appending a quote; SQLite must roll back all writes.
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER abort_paper_quote BEFORE INSERT ON paper_portfolio_quotes "
                       "BEGIN SELECT RAISE(ABORT, 'quote write failed'); END")
        decision = order()
        with self.assertRaises(sqlite3.DatabaseError):
            self.portfolio.process(decision, {"ABC": quote()})
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 10000)
        self.assertEqual(self.portfolio.ledger("pool", decision.decision_id), ())
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER abort_paper_quote")
        self.assertEqual(self.portfolio.process(decision, {"ABC": quote()}).state, TradingState.COMPLETED)

    def test_fifo_basis_realized_and_unrealized_pnl(self):
        self.portfolio.process(order(quantity=2), {"ABC": quote(1000)})
        self.portfolio.process(order(quantity=1), {"ABC": quote(1100)})
        self.portfolio.process(order(side="SELL", quantity=1), {"ABC": quote(1200)})
        snap = self.portfolio.snapshot("pool")
        self.assertEqual(snap.cash_cents, 8100)
        self.assertEqual(snap.positions["ABC"], 2)
        self.assertEqual(snap.total_basis_cents["ABC"], 2100)
        self.assertEqual(snap.unrealized_pnl_cents, 300)
        self.assertEqual(snap.nav_cents, 10500)
        self.assertEqual(snap.cost_basis_cents["ABC"], 1050)
        self.assertEqual(snap.quotes["ABC"].source, "fixture/feed")
        self.assertEqual(self.portfolio.dashboard_inputs("pool")["prices_cents"], {"ABC": 1200})

    def test_no_fabricated_quotes_and_refresh_requires_provenance(self):
        self.portfolio.process(order(), {"ABC": quote()})
        self.now += timedelta(minutes=6)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.portfolio.snapshot("pool")
        with self.assertRaisesRegex(ValueError, "quotes must cover"):
            self.portfolio.record_prices("pool", {})
        with self.assertRaisesRegex(ValueError, "stale"):
            self.portfolio.record_prices("pool", {"ABC": quote()})
        self.portfolio.record_prices("pool", {"ABC": quote(1200, at=self.now, source="new/feed")})
        self.assertEqual(self.portfolio.snapshot("pool").quotes["ABC"].source, "new/feed")
        self.assertEqual(self.portfolio.snapshot("pool").unrealized_pnl_cents, 400)
        with self.assertRaises(ValueError):
            self.portfolio.record_prices("pool", {"ABC": 1200})

    def test_quote_history_never_regresses_and_failed_refresh_is_atomic(self):
        self.portfolio.process(order(), {"ABC": quote()})
        self.now += timedelta(minutes=1)
        self.portfolio.record_prices("pool", {"ABC": quote(1300, self.now, "fresh/feed")})
        with self.assertRaisesRegex(ValueError, "regressed"):
            self.portfolio.record_prices("pool", {"ABC": quote(1200, NOW, "older/feed")})
        self.assertEqual(self.portfolio.snapshot("pool").prices_cents, {"ABC": 1300})
        self.assertEqual(sum(e.action == "PAPER_PRICES_RECORDED" for e in
                             self.portfolio.ledger("pool")), 1)

    def test_two_connections_serialize_against_current_cash(self):
        other = PaperPortfolio(self.path, clock=lambda: self.now)
        try:
            self.portfolio.process(order(quantity=5), {"ABC": quote(1000)})
            expensive = other.process(order(quantity=6), {"ABC": quote(1000)})
            self.assertEqual(expensive.state, TradingState.REJECTED)
            self.assertIn("insufficient cash", expensive.reason)
            self.assertEqual(other.snapshot("pool").cash_cents, 5000)
        finally:
            other.close()

    def test_partial_cent_average_basis_does_not_misstate_fund_pnl(self):
        self.portfolio.process(order(quantity=1), {"ABC": quote(1000)})
        self.portfolio.process(order(quantity=1), {"ABC": quote(1001)})
        self.assertEqual(self.portfolio.snapshot("pool").total_basis_cents["ABC"], 2001)
        with self.assertRaisesRegex(ValueError, "fractional average"):
            self.portfolio.dashboard_inputs("pool")

    def test_verified_contribution_boundary_idempotency_and_restart(self):
        # No implicit change follows an external join: caller must reconcile
        # fund-service contribution events before reporting a fund NAV.
        self.assertEqual(self.portfolio.contribution_events("pool"), {})
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 10000)
        with self.assertRaisesRegex(ValueError, "starting balance"):
            self.portfolio.record_contribution("pool", event_id="start:pool", cents=10000)
        self.assertTrue(self.portfolio.record_contribution("pool", event_id="join:alice", cents=2500))
        self.assertFalse(self.portfolio.record_contribution("pool", event_id="join:alice", cents=2500))
        with self.assertRaisesRegex(ValueError, "different request"):
            self.portfolio.record_contribution("pool", event_id="join:alice", cents=2501)
        self.portfolio.close()
        self.portfolio = PaperPortfolio(self.path, clock=lambda: self.now)
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 12500)
        self.assertEqual(self.portfolio.initialized_cash_cents("pool"), 10000)
        self.assertEqual(self.portfolio.contribution_events("pool"), {"join:alice": 2500})
        with self.assertRaisesRegex(ValueError, "different pool or cash"):
            self.portfolio.initialize("pool", 12500)
        self.assertEqual(sum(e.action == "VIRTUAL_CONTRIBUTION_APPLIED" for e in
                             self.portfolio.ledger("pool")), 1)

    def test_contribution_failed_audit_write_rolls_back_cash_and_event(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER abort_contribution_audit BEFORE INSERT ON paper_portfolio_audit "
                       "WHEN NEW.action='VIRTUAL_CONTRIBUTION_APPLIED' "
                       "BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.portfolio.record_contribution("pool", event_id="join:a", cents=500)
        self.assertEqual(self.portfolio.contribution_events("pool"), {})
        self.assertEqual(self.portfolio.snapshot("pool").cash_cents, 10000)

    def test_one_pool_and_paper_only(self):
        self.portfolio.initialize("pool", 10000)
        with self.assertRaises(ValueError):
            self.portfolio.initialize("another", 10000)
        with self.assertRaises(ValueError):
            PaperPortfolio(":memory:")
        with self.assertRaises(ValueError):
            PriceQuote(1, NOW, "")


if __name__ == "__main__":
    unittest.main()
