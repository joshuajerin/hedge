"""Focused contract tests for the isolated paper-only trading runtime."""

from __future__ import annotations

from decimal import Decimal
import unittest

from hedge.contracts import Decision
from hedge.risk_controls import RiskLimits
from hedge.trading_system import IdempotencyConflict, LiveExecutionProhibited, PaperTradingSystem, TradingState


def decision(*, decision_id: str, intents: list[dict[str, object]]) -> Decision:
    payload = {
        "schema_version": "hedge.decision.v1",
        "decision_id": decision_id,
        "pool_id": "pool_1",
        "mandate_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "intents": [],
    }
    for raw in intents:
        payload["intents"].append(
            {
                "schema_version": "hedge.trade-intent.v1",
                "decision_id": decision_id,
                "pool_id": "pool_1",
                "mandate_version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "order_type": "MKT",
                "limit_price": None,
                "rationale": "focused paper test",
                **raw,
            }
        )
    return Decision.from_dict(payload)


class PaperTradingSystemTests(unittest.TestCase):
    def test_happy_path_has_full_state_machine_and_paper_fill(self) -> None:
        runtime = PaperTradingSystem(initial_cash="1000")
        result = runtime.process(
            decision(decision_id="dec-buy", intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 2}]),
            {"AAPL": "100"},
        )

        self.assertEqual(result.state, TradingState.COMPLETED)
        self.assertEqual([event.state for event in result.events], [
            TradingState.PROPOSED,
            TradingState.VALIDATED,
            TradingState.RISK_APPROVED,
            TradingState.SIMULATED,
            TradingState.COMPLETED,
        ])
        self.assertEqual(result.fills[0].status, "FILLED")
        self.assertEqual(result.fills[0].reference, "paper:dec-buy:0")
        self.assertEqual(runtime.account.cash, Decimal("800"))
        self.assertEqual(runtime.account.positions["AAPL"], 2)
        self.assertTrue(all("broker" not in event.action.lower() for event in result.events))

    def test_duplicate_is_idempotent_and_payload_reuse_fails_closed(self) -> None:
        runtime = PaperTradingSystem(initial_cash="1000")
        original = decision(decision_id="dec-once", intents=[{"symbol": "MSFT", "side": "BUY", "quantity": 1}])
        first = runtime.process(original, {"MSFT": "50"})
        ledger_size = len(runtime.ledger())
        repeated = runtime.process(original, {"MSFT": "999"})

        self.assertTrue(repeated.idempotent)
        self.assertEqual(repeated.account, first.account)
        self.assertEqual(len(runtime.ledger()), ledger_size)
        changed = decision(decision_id="dec-once", intents=[{"symbol": "MSFT", "side": "BUY", "quantity": 2}])
        with self.assertRaises(IdempotencyConflict):
            runtime.process(changed, {"MSFT": "50"})
        self.assertEqual(runtime.account.cash, Decimal("950"))

    def test_insufficient_cash_and_uncovered_sell_reject_without_state_change(self) -> None:
        runtime = PaperTradingSystem(initial_cash="100")
        too_expensive = runtime.process(
            decision(decision_id="dec-cash", intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 2}]),
            {"AAPL": "60"},
        )
        uncovered = runtime.process(
            decision(decision_id="dec-sell", intents=[{"symbol": "AAPL", "side": "SELL", "quantity": 1}]),
            {"AAPL": "60"},
        )

        self.assertEqual(too_expensive.state, TradingState.REJECTED)
        self.assertIn("insufficient cash", too_expensive.reason or "")
        self.assertEqual(uncovered.state, TradingState.REJECTED)
        self.assertIn("uncovered sell", uncovered.reason or "")
        self.assertEqual(runtime.account.cash, Decimal("100"))
        self.assertEqual(dict(runtime.account.positions), {})

    def test_manually_constructed_malformed_decision_is_audited_and_idempotent(self) -> None:
        runtime = PaperTradingSystem(initial_cash="1000")
        malformed = Decision(
            schema_version="unsupported",
            decision_id="dec-malformed",
            pool_id="pool_1",
            mandate_version=1,
            intents=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

        first = runtime.process(malformed, {})
        ledger_size = len(runtime.ledger())
        repeated = runtime.process(malformed, {})

        self.assertEqual(first.state, TradingState.REJECTED)
        self.assertIn("invalid decision", first.reason or "")
        self.assertEqual([event.state for event in first.events], [TradingState.PROPOSED, TradingState.REJECTED])
        self.assertTrue(repeated.idempotent)
        self.assertEqual(len(runtime.ledger()), ledger_size)
        self.assertEqual(runtime.account.cash, Decimal("1000"))
        with self.assertRaises(IdempotencyConflict):
            runtime.process(
                decision(decision_id="dec-malformed", intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 1}]),
                {"AAPL": "10"},
            )

    def test_kill_switch_blocks_and_audit_records_operator_action(self) -> None:
        runtime = PaperTradingSystem(initial_cash="1000")
        runtime.activate_kill_switch("operator incident drill")
        result = runtime.process(
            decision(decision_id="dec-halted", intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 1}]),
            {"AAPL": "10"},
        )

        self.assertEqual(result.state, TradingState.HALTED)
        self.assertEqual(result.reason, "operator incident drill")
        self.assertEqual(runtime.account.cash, Decimal("1000"))
        self.assertIn("KILL_SWITCH_ACTIVATED", [event.action for event in runtime.ledger()])
        self.assertIn("KILL_SWITCH_BLOCKED", [event.action for event in result.events])

    def test_bounds_and_live_execution_are_prohibited(self) -> None:
        runtime = PaperTradingSystem(
            initial_cash="10000",
            limits=RiskLimits(max_orders_per_decision=1, max_open_orders=1, max_shares_per_order=5, max_notional_per_order="1000", max_position_shares=5, max_gross_exposure="1000"),
        )
        bounded = runtime.process(
            decision(decision_id="dec-bound", intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 6}]),
            {"AAPL": "10"},
        )
        self.assertEqual(bounded.state, TradingState.REJECTED)
        self.assertIn("max_shares_per_order", bounded.reason or "")
        with self.assertRaises(LiveExecutionProhibited):
            runtime.live_execute()
        with self.assertRaises(LiveExecutionProhibited):
            PaperTradingSystem(initial_cash="1", mode="live")

    def test_limit_order_that_cannot_fill_does_not_change_cash_or_position(self) -> None:
        runtime = PaperTradingSystem(initial_cash="100")
        limit = decision(
            decision_id="dec-limit",
            intents=[{"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LMT", "limit_price": 90}],
        )
        result = runtime.process(limit, {"AAPL": "100"})
        self.assertEqual(result.state, TradingState.COMPLETED)
        self.assertEqual(result.fills[0].status, "NOT_FILLED")
        self.assertEqual(runtime.account.cash, Decimal("100"))
        self.assertEqual(dict(runtime.account.positions), {})


if __name__ == "__main__":
    unittest.main()
