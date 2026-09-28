"""Focused regression tests for the isolated Hedge paper backtest sandbox."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import subprocess
import sys
from pathlib import Path
import unittest

from hedge.backtest import (
    BacktestBar,
    BacktestConfig,
    BacktestEngine,
    PaperBacktester,
    ScheduledDecision,
    ScheduledIntent,
)
from hedge.contracts import ContractError, Decision, TradeIntent
from hedge.policy import PaperPolicy


ROOT = Path(__file__).resolve().parents[1]


def intent(
    *,
    decision_id: str = "decision-1",
    symbol: str = "AAPL",
    side: str = "BUY",
    quantity: int = 1,
    order_type: str = "MKT",
    limit_price: str | None = None,
) -> TradeIntent:
    return TradeIntent.from_dict(
        {
            "schema_version": "hedge.trade-intent.v1",
            "decision_id": decision_id,
            "pool_id": "pool-1",
            "mandate_version": 1,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "rationale": "test signal",
            "created_at": "2025-01-01T00:00:00+00:00",
        }
    )


def bars(*values: tuple[str, str, str, str, str, str]) -> list[BacktestBar]:
    return [
        BacktestBar(symbol, timestamp, opening, high, low, close, volume)
        for timestamp, symbol, opening, high, low, close, volume in values
    ]


class PaperBacktestTests(unittest.TestCase):
    def test_market_fill_is_next_bar_open_with_adverse_slippage_and_fee(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "110", "116", "109", "115", "10"),
        )
        result = BacktestEngine(history, BacktestConfig(initial_cash="2000", commission_per_order="1", slippage_bps="10")).run(
            [ScheduledIntent("2025-01-01", intent(quantity=10))]
        )
        fill = result.fills[0]
        self.assertEqual(fill.timestamp.date(), date(2025, 1, 2))
        self.assertEqual(fill.price, Decimal("110.110"))
        self.assertEqual(result.final_cash, Decimal("897.900"))
        self.assertEqual(result.final_equity, Decimal("2047.900"))
        self.assertEqual(result.total_return, Decimal("0.02395"))
        self.assertEqual(result.ledger, (fill,))

    def test_limit_fill_requires_cross_and_never_claims_gap_price_improvement(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            # Opening below 100 must not give an optimistic 90 fill.
            ("2025-01-02", "AAPL", "90", "105", "80", "95", "10"),
        )
        buy_limit = intent(order_type="LMT", limit_price="100")
        result = BacktestEngine(history, BacktestConfig(initial_cash="1000", slippage_bps="100")).run(
            [ScheduledIntent("2025-01-01", buy_limit)]
        )
        self.assertEqual(result.fills[0].price, Decimal("100"))
        self.assertEqual(result.final_cash, Decimal("900"))

        missed = BacktestEngine(
            bars(
                ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
                ("2025-01-02", "AAPL", "105", "109", "101", "106", "10"),
            )
        ).run([ScheduledIntent("2025-01-01", buy_limit)])
        self.assertEqual(missed.rejected_orders[0].reason, "limit price was not reached on the eligible bar")

    def test_cash_short_and_missing_future_bar_are_rejected_in_ledger(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "100", "101", "99", "100", "10"),
        )
        engine = BacktestEngine(history, BacktestConfig(initial_cash="50"))
        result = engine.run(
            [
                ScheduledIntent("2025-01-01", intent(decision_id="a", quantity=1)),
                ScheduledIntent("2025-01-01", intent(decision_id="b", side="SELL", quantity=1)),
                ScheduledIntent("2025-01-02", intent(decision_id="c", quantity=1)),
            ]
        )
        reasons = {item.decision_id: item.reason for item in result.rejected_orders}
        self.assertEqual(reasons["a"], "insufficient cash")
        self.assertEqual(reasons["b"], "insufficient position; short sales are disabled")
        self.assertEqual(reasons["c"], "no eligible price bar after fill delay")
        self.assertEqual(result.positions, {})

    def test_equity_drawdown_and_win_rate_use_closed_covered_sell(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-03", "AAPL", "80", "81", "79", "80", "10"),
            ("2025-01-04", "AAPL", "120", "121", "119", "120", "10"),
        )
        result = BacktestEngine(history, BacktestConfig(initial_cash="1000")).run(
            [
                ScheduledIntent("2025-01-01", intent(decision_id="buy", quantity=5)),
                ScheduledIntent("2025-01-03", intent(decision_id="sell", side="SELL", quantity=5)),
            ]
        )
        self.assertEqual(result.final_equity, Decimal("1100"))
        self.assertEqual(result.total_return, Decimal("0.1"))
        self.assertEqual(result.max_drawdown, Decimal("-0.1"))
        self.assertEqual(result.metrics["closed_trade_count"], 1)
        self.assertEqual(result.metrics["win_rate"], Decimal("1"))
        self.assertEqual(result.ledger[0].side, "BUY")
        self.assertEqual(result.ledger[1].side, "SELL")

    def test_decision_policy_validation_and_strategy_delay(self) -> None:
        policy = PaperPolicy(account_mode="live")
        with self.assertRaises(ContractError):
            PaperBacktester(policy=policy)

        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "101", "102", "100", "101", "10"),
        )
        decision = Decision.draft(pool_id="pool-1", mandate_version=1, intents=[intent().as_dict()])
        result = BacktestEngine(history, BacktestConfig(initial_cash="500")).run_decisions(
            [ScheduledDecision("2025-01-01", decision)]
        )
        self.assertEqual(result.fills[0].price, Decimal("101"))

        strategy_result = BacktestEngine(history, BacktestConfig(initial_cash="500")).run_strategy(
            lambda bar: intent(decision_id="strategy") if bar.timestamp.date() == date(2025, 1, 1) else None
        )
        self.assertEqual(strategy_result.fills[0].timestamp.date(), date(2025, 1, 2))

    def test_strategy_decisions_validate_atomically_before_any_fill(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "101", "102", "100", "101", "10"),
        )
        malformed = Decision(
            "hedge.decision.v1",
            "decision-parent",
            "pool-1",
            1,
            (intent(decision_id="decision-child"),),
            "2025-01-01T00:00:00+00:00",
        )
        engine = BacktestEngine(history, BacktestConfig(initial_cash="500"))
        with self.assertRaisesRegex(ContractError, "every intent must match"):
            engine.run_strategy(lambda bar: malformed if bar.timestamp.date() == date(2025, 1, 1) else None)
        with self.assertRaisesRegex(ContractError, "every intent must match"):
            PaperBacktester(BacktestConfig(initial_cash="500")).run_decisions(
                [ScheduledDecision("2025-01-01", malformed)], history
            )

    def test_strategy_duplicate_decision_id_fails_before_execution(self) -> None:
        history = bars(
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-02", "AAPL", "101", "102", "100", "101", "10"),
        )
        decision = Decision.draft(pool_id="pool-1", mandate_version=1, intents=[intent().as_dict()])
        with self.assertRaisesRegex(ContractError, "decision_id may appear only once"):
            BacktestEngine(history, BacktestConfig(initial_cash="500")).run_strategy(lambda bar: decision)

    def test_input_normalization_is_deterministic_and_bars_validate(self) -> None:
        with self.assertRaises(ValueError):
            BacktestBar("AAPL", "2025-01-01", 100, 90, 95, 100)
        history = bars(
            ("2025-01-02", "AAPL", "100", "101", "99", "100", "10"),
            ("2025-01-01", "AAPL", "100", "101", "99", "100", "10"),
        )
        scheduled = [ScheduledIntent("2025-01-01", intent(decision_id="z"))]
        first = BacktestEngine(history, BacktestConfig(initial_cash="200")).run(scheduled)
        second = BacktestEngine(list(reversed(history)), BacktestConfig(initial_cash="200")).run(list(reversed(scheduled)))
        self.assertEqual(first, second)

    def test_sandbox_runner_is_local_and_runs_example_files(self) -> None:
        script = ROOT / "sandbox" / "run_backtest.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--config", str(ROOT / "sandbox" / "backtest_config.example.json"), "--bars", str(ROOT / "sandbox" / "bars.example.csv"), "--decisions", str(ROOT / "sandbox" / "decisions.example.json")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn('"mode": "paper"', completed.stdout)
        source = script.read_text()
        self.assertNotIn("hedge.broker", source)
        self.assertNotIn("os.environ", source)


if __name__ == "__main__":
    unittest.main()
