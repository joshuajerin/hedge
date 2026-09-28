"""Deterministic contract tests for the paper-only flows and dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hedge.dashboard import render_dashboard, write_dashboard
from hedge.flows import build_report, contribution_flow, equity_flow

NOW = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)


class FlowReportTests(unittest.TestCase):
    def report(self):
        return build_report(
            generated_at=NOW,
            stale_after_seconds=300,
            decisions=[
                {
                    "decision_id": "dec-later",
                    "pool_id": "pool-a",
                    "mandate_version": 2,
                    "created_at": "2025-01-02T11:59:00Z",
                    "reviewed_at": "2025-01-02T11:59:30Z",
                    "status": "approved",
                    "untrusted_secret": "must-not-leak",
                    "intents": [
                        {"symbol": "MSFT", "side": "SELL", "quantity": 2, "order_type": "LMT", "limit_price": 410},
                        {"symbol": "invalid", "side": "HOLD", "quantity": 99, "order_type": "MKT"},
                    ],
                },
                {
                    "decision_id": "dec-first",
                    "pool_id": "pool-a",
                    "mandate_version": 2,
                    "created_at": "2025-01-02T11:58:00Z",
                    "status": "proposed",
                    "intents": [{"symbol": "AAPL", "side": "BUY", "quantity": 3, "order_type": "MKT"}],
                },
            ],
            contributions=[
                {"created_at": "2025-01-02T11:55:00Z", "pool_id": "pool-a", "member_id": "alice", "cents": 12500},
                {"created_at": "2025-01-02T11:56:00Z", "pool_id": "pool-a", "member_id": "bob", "amount_cents": 7500},
            ],
            equity=[
                {"as_of": "2025-01-02T11:55:00Z", "equity": 200.0},
                {"as_of": "2025-01-02T11:59:00Z", "equity": 180.0},
                {"as_of": "2025-01-02T12:00:00Z", "equity": 190.0},
            ],
            positions=[{"symbol": "AAPL", "quantity": 3, "avg_cost": 50, "mark_price": 55, "api_key": "never-export"}],
            research_stages=[
                {"order": 1, "agent": "Market Scout", "status": "complete", "started_at": "2025-01-02T11:50:00Z", "completed_at": "2025-01-02T11:52:00Z"},
                {"order": 2, "agent": "Risk Reviewer", "status": "blocked", "started_at": "2025-01-02T11:58:00Z", "completed_at": "2025-01-02T11:59:00Z"},
            ],
        )

    def test_report_has_cumulative_charts_risk_and_whitelisted_flow(self):
        report = self.report()
        self.assertTrue(report["paper_only"])
        self.assertEqual(report["summary"]["virtual_contributions_cents"], 20000)
        self.assertEqual(report["summary"]["pnl"], -10.0)
        self.assertEqual(report["summary"]["max_drawdown_pct"], -10.0)
        self.assertEqual(report["summary"]["risk"]["status"], "blocked")
        self.assertIn("research gate not clear: Risk Reviewer", report["summary"]["risk"]["reasons"])
        self.assertEqual([item["decision_id"] for item in report["flows"]["decisions"]], ["dec-first", "dec-later"])
        self.assertEqual(report["charts"]["decision_flow"][1]["points"][-1]["value"], 2)
        self.assertEqual(report["charts"]["contributions"][0]["points"][-1]["value"], 200.0)
        encoded = json.dumps(report)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("never-export", encoded)
        self.assertEqual(report["positions"][0]["unrealized_pnl"], 15.0)
        self.assertEqual(report["freshness"][0]["status"], "fresh")

    def test_snapshot_contributions_and_cents_equity_are_supported(self):
        self.assertEqual(contribution_flow({"pool-a": {"alice": 20, "bob": 30}})["total_cents"], 50)
        flow = equity_flow([{"as_of": "2025-01-01T00:00:00Z", "equity_cents": "12345"}])
        self.assertEqual(flow["latest_equity"], 123.45)

    def test_fund_positions_and_capital_accounts_accept_decimal_and_cents(self):
        report = build_report(
            generated_at=NOW,
            equity=[{"as_of": "2025-01-02T12:00:00Z", "equity_cents": Decimal("120050")}],
            fund={
                "nav_cents": Decimal("120050"),
                "cash_cents": Decimal("25050"),
                "return_periods": {"1M": Decimal("4.25"), "YTD": Decimal("8.5")},
                "benchmark": {"name": "S&P <500>", "return_periods": {"1M": Decimal("3.0"), "YTD": Decimal("9.0")}},
            },
            positions=[{
                "symbol": "AAPL", "quantity": Decimal("5"), "entry_price": Decimal("100.10"),
                "current_price": Decimal("110.10"), "realized_pnl_cents": Decimal("125"),
                "thesis_status": "active <review>", "risk_status": "warning",
            }],
            capital_accounts=[{
                "member_id": "alice", "display_name": "Alice <admin>", "contributed_cents": Decimal("100000"),
                "withdrawn_cents": Decimal("1000"), "nav_cents": Decimal("110050"),
                "realized_pnl_cents": Decimal("2000"), "unrealized_pnl_cents": Decimal("9050"),
            }, {
                "member_id": "bob", "contributed_cents": Decimal("10000"), "nav_cents": Decimal("10000"),
            }],
        )
        self.assertEqual(report["summary"]["fund"]["nav"], 1200.5)
        self.assertEqual(report["summary"]["fund"]["cash_cents"], 25050)
        self.assertEqual(report["summary"]["return_periods"][0]["benchmark_delta_pct"], 1.25)
        self.assertEqual(report["positions"][0]["entry_price"], 100.1)
        self.assertEqual(report["positions"][0]["current_price"], 110.1)
        self.assertEqual(report["positions"][0]["realized_pnl"], 1.25)
        self.assertAlmostEqual(report["positions"][0]["allocation_pct"], 45.856, places=3)
        self.assertEqual(report["capital_accounts"][0]["net_contributions_cents"], 99000)
        self.assertAlmostEqual(report["capital_accounts"][0]["allocation_pct"], 91.67, places=2)
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False))["capital_accounts"][1]["nav_cents"], 10000)
        html = render_dashboard(report)
        self.assertIn("Fund return periods", html)
        self.assertIn("Capital accounts", html)
        self.assertNotIn("S&P <500>", html)
        self.assertNotIn("Alice <admin>", html)
        document = html.split('<script id="hedge-report" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(document)["summary"]["fund"]["benchmark"]["name"], "S&P <500>")

    def test_html_is_self_contained_safe_and_writable(self):
        report = self.report()
        report["flows"]["decisions"][0]["decision_id"] = "</script><img src=x>"
        html = render_dashboard(report, title="Hedge <paper>")
        self.assertIn("Hedge &lt;paper&gt;", html)
        self.assertIn("PAPER ONLY", html)
        self.assertNotIn("https://", html)
        document = html.split('<script id="hedge-report" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertEqual(json.loads(document)["flows"]["decisions"][0]["decision_id"], "</script><img src=x>")
        with TemporaryDirectory() as directory:
            path = write_dashboard(Path(directory) / "report.html", report, title="Hedge <paper>")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), html)


if __name__ == "__main__":
    unittest.main()
