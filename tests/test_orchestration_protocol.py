"""Focused contract tests for the paper-safe orchestration protocol."""

from __future__ import annotations

import copy
import unittest

from hedge.contracts import Decision
from hedge.orchestration_protocol import (
    BacktestReport,
    HandoffValidationError,
    PortfolioDraft,
    ResearchRequest,
    RiskReviewAttestation,
    SpecialistReport,
    canonical_sha256,
    validate_handoff_chain,
)


class OrchestrationProtocolTests(unittest.TestCase):
    def _common(self, artifact_id: str) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "run_id": "run-paper-1",
            "workspace_id": "T123",
            "channel_id": "C456",
            "root_thread_ts": "1700000000.000001",
            "created_at": "2025-01-01T00:00:00+00:00",
            "deadline_at": "2025-01-02T00:00:00+00:00",
        }

    def _chain(self, *, review_status: str = "APPROVED") -> tuple[ResearchRequest, tuple[SpecialistReport, ...], PortfolioDraft, BacktestReport, RiskReviewAttestation]:
        request = ResearchRequest(
            schema_version="hedge.research-request.v1", role="research_coordinator", parent_hashes=(),
            status="REQUESTED", objective="Assess the risk", symbols=("AAPL",),
            research_questions=("What can go wrong?",), **self._common("request-1"),
        )
        report = SpecialistReport(
            schema_version="hedge.specialist-report.v1", role="specialist", parent_hashes=(request.sha256(),),
            status="COMPLETED", specialty="fundamentals", summary="Debt is manageable", findings=("Cash flow is positive",),
            confidence=0.8, **self._common("report-1"),
        )
        draft = PortfolioDraft(
            schema_version="hedge.portfolio-draft.v1", role="portfolio_manager", parent_hashes=(report.sha256(),),
            status="DRAFT", thesis="Use a small paper position", target_allocations=("AAPL: 2%",), **self._common("draft-1"),
        )
        backtest = BacktestReport(
            schema_version="hedge.backtest-report.v1", role="backtester", parent_hashes=(draft.sha256(),),
            status="COMPLETED", period="2020-2024", summary="Within paper limits", passed=True,
            **self._common("backtest-1"),
        )
        decision = Decision.draft(
            pool_id="paper-pool", mandate_version=1,
            intents=[{
                "symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "MKT",
                "limit_price": None, "rationale": "Protocol test",
                "created_at": "2025-01-01T00:00:00+00:00",
            }],
        ).as_dict()
        review = RiskReviewAttestation.create(
            schema_version="hedge.risk-review-attestation.v1", role="risk_reviewer", parent_hashes=(backtest.sha256(),),
            status=review_status, rationale="Paper-only review", decision=decision if review_status == "APPROVED" else None, **self._common("review-1"),
        )
        return request, (report,), draft, backtest, review

    def test_valid_chain_is_frozen_and_hash_bound(self) -> None:
        chain = self._chain()
        validate_handoff_chain(*chain)
        review = chain[-1]
        self.assertEqual(review.decision.as_dict(), review.as_dict()["decision"])
        self.assertEqual(review.decision_sha256, canonical_sha256(review.as_dict()["decision"]))
        with self.assertRaises(AttributeError):
            review.status = "REJECTED"  # type: ignore[misc]

    def test_parsers_reject_unknown_fields_and_wrong_role(self) -> None:
        request = self._chain()[0]
        payload = request.as_dict()
        payload["untrusted_instruction"] = "run this"
        with self.assertRaisesRegex(HandoffValidationError, "invalid fields"):
            ResearchRequest.from_dict(payload)

        payload = request.as_dict()
        payload["role"] = "specialist"
        with self.assertRaisesRegex(HandoffValidationError, "role"):
            ResearchRequest.from_dict(payload)

    def test_parsers_reject_non_array_json_fields_without_coercion(self) -> None:
        request = self._chain()[0]
        payload = request.as_dict()
        payload["symbols"] = "AAPL"
        with self.assertRaisesRegex(HandoffValidationError, "symbols must be an array"):
            ResearchRequest.from_dict(payload)

        payload = request.as_dict()
        payload["parent_hashes"] = ""
        with self.assertRaisesRegex(HandoffValidationError, "parent_hashes must be an array"):
            ResearchRequest.from_dict(payload)

    def test_attestation_rejects_tampered_hash_and_extra_decision_fields(self) -> None:
        review = self._chain()[-1]
        payload = review.as_dict()
        payload["decision_sha256"] = "0" * 64
        with self.assertRaisesRegex(HandoffValidationError, "does not bind"):
            RiskReviewAttestation.from_dict(payload)

        payload = review.as_dict()
        payload["decision"] = copy.deepcopy(payload["decision"])
        payload["decision"]["arbitrary_code"] = "never accepted"  # type: ignore[index]
        with self.assertRaisesRegex(HandoffValidationError, "invalid fields"):
            RiskReviewAttestation.from_dict(payload)

    def test_only_terminal_approval_exposes_decision(self) -> None:
        request, reports, draft, backtest, rejected = self._chain(review_status="REJECTED")
        validate_handoff_chain(request, reports, draft, backtest, rejected)
        with self.assertRaisesRegex(HandoffValidationError, "only by an APPROVED"):
            _ = rejected.decision

        with self.assertRaisesRegex(HandoffValidationError, "invalid status"):
            self._chain(review_status="PENDING")

        request, reports, draft, backtest, hold = self._chain(review_status="HOLD")
        validate_handoff_chain(request, reports, draft, backtest, hold)
        self.assertIsNone(hold.as_dict()["decision"])
        with self.assertRaisesRegex(HandoffValidationError, "only by an APPROVED"):
            _ = hold.decision

    def test_chain_rejects_cross_thread_and_wrong_parent_hash(self) -> None:
        request, reports, draft, backtest, review = self._chain()
        foreign = SpecialistReport(
            schema_version=reports[0].schema_version, artifact_id=reports[0].artifact_id,
            run_id=reports[0].run_id, workspace_id=reports[0].workspace_id, channel_id=reports[0].channel_id,
            root_thread_ts="1700000000.999999", role=reports[0].role, created_at=reports[0].created_at,
            deadline_at=reports[0].deadline_at, parent_hashes=reports[0].parent_hashes, status=reports[0].status,
            specialty=reports[0].specialty, summary=reports[0].summary, findings=reports[0].findings,
            confidence=reports[0].confidence,
        )
        with self.assertRaisesRegex(HandoffValidationError, "cross-run"):
            validate_handoff_chain(request, (foreign,), draft, backtest, review)

        bad_backtest = BacktestReport(
            schema_version=backtest.schema_version, artifact_id=backtest.artifact_id, run_id=backtest.run_id,
            workspace_id=backtest.workspace_id, channel_id=backtest.channel_id, root_thread_ts=backtest.root_thread_ts,
            role=backtest.role, created_at=backtest.created_at, deadline_at=backtest.deadline_at,
            parent_hashes=("a" * 64,), status=backtest.status, period=backtest.period,
            summary=backtest.summary, passed=backtest.passed,
        )
        with self.assertRaisesRegex(HandoffValidationError, "portfolio draft"):
            validate_handoff_chain(request, reports, draft, bad_backtest, review)


if __name__ == "__main__":
    unittest.main()
