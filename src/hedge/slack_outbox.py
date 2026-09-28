"""Typed-artifact to fixed-profile Slack delivery.

Specialist delivery is disabled until a trusted Brainbase transport can attest
that a terminal artifact belongs to a registered task. Typed objects and Slack
thread affinity alone are not evidence of provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from .orchestration_protocol import (
    BacktestReport,
    HandoffValidationError,
    PortfolioDraft,
    ResearchRequest,
    RiskReviewAttestation,
    SpecialistReport,
    validate_handoff_chain,
)
from .slack_delivery import DeliveryRequest, DeliveryResult, HedgeRole, SlackResultDelivery
from .slack_state import SlackState, ThreadCorrelation


_SPECIALTY_ROLE = {
    "market_scout": HedgeRole.MARKET_SCOUT,
    "trend_analyst": HedgeRole.TREND_ANALYST,
    "news_analyst": HedgeRole.NEWS_ANALYST,
}


class UnsupportedSpecialistDelivery(RuntimeError):
    """No authenticated terminal Brainbase result is available to this outbox."""


def _correlation(artifact: object) -> ThreadCorrelation:
    return ThreadCorrelation(
        run_id=artifact.run_id,
        workspace_id=artifact.workspace_id,
        channel_id=artifact.channel_id,
        root_thread_ts=artifact.root_thread_ts,
    )


@dataclass(frozen=True)
class ArtifactOutbox:
    """Durably deliver only validated, role-bound protocol results."""

    state: SlackState
    delivery: SlackResultDelivery

    def deliver_specialist_report(
        self,
        *,
        delivery_id: str,
        report: SpecialistReport,
        request: ResearchRequest | None = None,
        task_id: str | None = None,
    ) -> DeliveryResult:
        """Fail closed: typed artifacts alone cannot prove terminal task origin.

        A future adapter must check an authenticated, task-bound result envelope,
        a trusted launch-time task registry, and terminal status before any send.
        It must reserve with a task-derived delivery ID, not a caller-chosen ID.
        These proofs are not available through this outbox or the current CLI.
        """
        if not isinstance(report, SpecialistReport):
            raise HandoffValidationError("specialist report must be a typed artifact")
        report.__post_init__()
        if _SPECIALTY_ROLE.get(report.specialty) is None:
            raise HandoffValidationError("specialist report specialty is not an approved Slack profile")
        if request is not None:
            if not isinstance(request, ResearchRequest):
                raise HandoffValidationError("research request must be a typed artifact")
            request.__post_init__()
            if request.sha256() not in report.parent_hashes:
                raise HandoffValidationError("specialist report does not bind the research request")
            if _correlation(request) != _correlation(report):
                raise HandoffValidationError("specialist report and request have different Slack affinity")
        # A caller-supplied task ID, request, delivery ID, or report is not an
        # authenticated terminal Brainbase result. Do not reserve or send.
        raise UnsupportedSpecialistDelivery("authenticated terminal specialist transport is unavailable")

    def deliver_cio_partial(self, *, delivery_id: str, correlation: ThreadCorrelation, reason: str) -> DeliveryResult:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1_000:
            raise HandoffValidationError("partial-result reason is invalid")
        return self.delivery.deliver(
            DeliveryRequest(
                delivery_id=delivery_id,
                role=HedgeRole.CIO,
                correlation=correlation,
                text=f"Partial research update: {reason.strip()}",
            )
        )

    def deliver_terminal_proposal(
        self,
        *,
        delivery_id: str,
        request: ResearchRequest,
        reports: tuple[SpecialistReport, ...],
        draft: PortfolioDraft,
        backtest: BacktestReport,
        attestation: RiskReviewAttestation,
    ) -> DeliveryResult:
        validate_handoff_chain(request, reports, draft, backtest, attestation)
        if attestation.status != "APPROVED":
            raise HandoffValidationError("only APPROVED risk attestations may expose a paper proposal")
        decision = attestation.decision
        text = (
            f"Risk Reviewer APPROVED a paper proposal ({decision.decision_id}). "
            f"Decision hash: {attestation.decision_sha256}. {attestation.rationale}"
        )
        return self.delivery.deliver(
            DeliveryRequest(
                delivery_id=delivery_id,
                role=HedgeRole.RISK_REVIEWER,
                correlation=_correlation(attestation),
                text=text,
            )
        )
