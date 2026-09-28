"""Network-free contract tests for the Yahoo historical import boundary."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hedge.backtest import BacktestConfig, ScheduledDecision
from hedge.contracts import Decision, TradeIntent
from hedge.yahoo_history import load_history, run_yahoo_paper_backtest

START = date(2025, 1, 2)
END = date(2025, 1, 4)
NOW = datetime(2025, 1, 5, tzinfo=UTC)


def rows(symbol: str, start: date, end: date):
    assert start == START and end == END
    base = 100 if symbol == "AAPL" else 200
    for day, price in ((START, base), (date(2025, 1, 3), base + 10)):
        yield {
            "Date": day,
            "Open": price,
            "High": price + 2,
            "Low": price - 1,
            "Close": price + 1,
            "Volume": 1000,
        }


def decision(symbol="AAPL"):
    intent = TradeIntent.from_dict(
        {
            "schema_version": "hedge.trade-intent.v1",
            "decision_id": "buy-1",
            "pool_id": "pool-1",
            "mandate_version": 1,
            "symbol": symbol,
            "side": "BUY",
            "quantity": 1,
            "order_type": "MKT",
            "limit_price": None,
            "rationale": "reviewed test decision",
            "created_at": "2025-01-02T00:00:00+00:00",
        }
    )
    return Decision.from_dict(
        {
            "schema_version": "hedge.decision.v1",
            "decision_id": "buy-1",
            "pool_id": "pool-1",
            "mandate_version": 1,
            "intents": [intent.as_dict()],
            "rationale": "reviewed test decision",
            "created_at": "2025-01-02T00:00:00+00:00",
        }
    )


class YahooHistoryTests(unittest.TestCase):
    def params(self, **changes):
        params = dict(
            symbols=["AAPL"],
            start=START,
            end=END,
            as_of=datetime(2025, 1, 4, 5, tzinfo=UTC),
            now=NOW,
            fetcher=rows,
            source="fixture:rows",
            expected_sessions=[START, date(2025, 1, 3)],
        )
        params.update(changes)
        return params

    def test_normalize_provenance_cache_offline_and_deterministic_paper_result(self):
        with TemporaryDirectory() as tmp:
            first = load_history(
                **self.params(symbols=["MSFT", "AAPL"], cache_dir=Path(tmp))
            )
            self.assertEqual(
                [b.symbol for b in first.bars], ["AAPL", "MSFT", "AAPL", "MSFT"]
            )
            self.assertEqual(
                first.bars[0].timestamp.isoformat(), "2025-01-02T05:00:00+00:00"
            )
            self.assertEqual(first.bars[0].open, Decimal("100"))
            self.assertEqual(first.bars[0].volume, Decimal("1000"))
            self.assertEqual(first.source, "fixture:rows")
            self.assertTrue(first.cache_path.exists())

            def forbidden(*args):
                raise AssertionError("offline cache read must not call fetcher")

            second = load_history(
                **self.params(
                    symbols=["AAPL", "MSFT"],
                    cache_dir=Path(tmp),
                    cache_only=True,
                    fetcher=forbidden,
                )
            )
            self.assertEqual(first.bars, second.bars)
            self.assertEqual(first.digest, second.digest)
            scheduled = [ScheduledDecision("2025-01-02T21:00:00+00:00", decision())]
            output = run_yahoo_paper_backtest(
                scheduled, second, config=BacktestConfig(initial_cash=1000)
            )
            self.assertEqual(
                output.result.fills[0].timestamp.isoformat(),
                "2025-01-03T05:00:00+00:00",
            )
            self.assertEqual(output.result.final_equity, Decimal("1001"))
            self.assertEqual(output.metrics["trade_count"], 1)
            self.assertEqual(output.equity_points[-1]["equity"], "1001")
            self.assertEqual(
                output,
                run_yahoo_paper_backtest(
                    scheduled, first, config=BacktestConfig(initial_cash=1000)
                ),
            )

    def test_missing_duplicate_null_invalid_and_gaps_rejected(self):
        def check(fetcher, message):
            with self.assertRaisesRegex(ValueError, message):
                load_history(**self.params(fetcher=fetcher))

        check(lambda *args: (), "missing history")
        check(lambda *args: [*rows(*args), next(rows(*args))], "duplicate daily bar")
        check(
            lambda *args: [{**row, "Close": None} for row in rows(*args)],
            "finite number",
        )
        check(
            lambda *args: [{**row, "Volume": None} for row in rows(*args)],
            "missing volume",
        )
        check(lambda *args: [{**row, "High": 1} for row in rows(*args)], "OHLC")
        check(lambda *args: list(rows(*args))[:1], "missing or unexpected sessions")
        check(
            lambda *args: [{**row, "Date": date(2025, 1, 1)} for row in rows(*args)],
            "outside requested",
        )
        check(
            lambda *args: [
                {**row, "Date": datetime(2025, 1, 2)} for row in rows(*args)
            ],
            "naive Date",
        )

        def partial(symbol, start, end):
            return (
                list(rows(symbol, start, end))[:1]
                if symbol == "MSFT"
                else rows(symbol, start, end)
            )

        with self.assertRaisesRegex(ValueError, "missing sessions"):
            load_history(
                **self.params(
                    symbols=["AAPL", "MSFT"], fetcher=partial, expected_sessions=None
                )
            )

    def test_cutoff_cache_freshness_integrity_and_no_fetch_on_failure(self):
        with self.assertRaisesRegex(ValueError, "as_of precedes"):
            load_history(**self.params(as_of=datetime(2025, 1, 4, tzinfo=UTC)))
        with self.assertRaisesRegex(ValueError, "as_of cannot"):
            load_history(**self.params(as_of=NOW + timedelta(days=1)))
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                load_history(
                    **self.params(
                        cache_dir=directory,
                        cache_only=True,
                        fetcher=lambda *args: self.fail("network attempted"),
                    )
                )
            original = load_history(**self.params(cache_dir=directory))
            with self.assertRaisesRegex(ValueError, "stale"):
                load_history(
                    **self.params(
                        cache_dir=directory,
                        cache_only=True,
                        now=NOW + timedelta(days=8),
                    )
                )
            old = load_history(
                **self.params(
                    cache_dir=directory,
                    cache_only=True,
                    now=NOW + timedelta(days=8),
                    max_age=None,
                )
            )
            self.assertEqual(old.digest, original.digest)
            payload = json.loads(original.cache_path.read_text())
            payload["bars"][0]["close"] = "500"
            original.cache_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_history(**self.params(cache_dir=directory, cache_only=True))

    def test_custom_source_required_and_dst_session_midnight(self):
        with self.assertRaisesRegex(ValueError, "explicit source"):
            load_history(**self.params(source=None))
        spring_start = date(2025, 3, 7)
        spring_end = date(2025, 3, 11)

        def spring_rows(symbol, start, end):
            for session in (spring_start, date(2025, 3, 10)):
                yield {
                    "Date": session,
                    "Open": 1,
                    "High": 1,
                    "Low": 1,
                    "Close": 1,
                    "Volume": 0,
                }

        history = load_history(
            symbols=["AAPL"],
            start=spring_start,
            end=spring_end,
            as_of=datetime(2025, 3, 11, 4, tzinfo=UTC),
            now=datetime(2025, 3, 12, tzinfo=UTC),
            fetcher=spring_rows,
            source="fixture:dst",
            expected_sessions=[spring_start, date(2025, 3, 10)],
        )
        self.assertEqual([bar.timestamp.hour for bar in history.bars], [5, 4])

    def test_paper_service_rejects_unavailable_symbol_and_outside_decision(self):
        history = load_history(**self.params())
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            run_yahoo_paper_backtest([], replace(history, digest="not-the-bars"))
        with self.assertRaisesRegex(ValueError, "missing history"):
            run_yahoo_paper_backtest(
                [ScheduledDecision("2025-01-02T21:00:00+00:00", decision("MSFT"))],
                history,
            )
        with self.assertRaisesRegex(ValueError, "outside requested"):
            run_yahoo_paper_backtest(
                [ScheduledDecision("2025-01-04T05:00:00+00:00", decision())], history
            )


if __name__ == "__main__":
    unittest.main()
