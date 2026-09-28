"""The specialist outbox must not promote caller-authored reports into Slack results."""
from dataclasses import replace

import pytest

from hedge.orchestration_protocol import HandoffValidationError, ResearchRequest, SpecialistReport
from hedge.slack_delivery import SlackResultDelivery
from hedge.slack_outbox import ArtifactOutbox, UnsupportedSpecialistDelivery
from hedge.slack_state import SlackState, ThreadCorrelation


class Sender:
    def __init__(self):
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def boundary():
    state = SlackState(":memory:")
    root = ThreadCorrelation("run-1", "workspace-1", "channel-1", "1710000000.000100")
    assert state.record_run(root)
    sender = Sender()
    yield ArtifactOutbox(state, SlackResultDelivery(state, sender)), state, sender
    state.close()


@pytest.fixture
def handoff():
    common = dict(run_id="run-1", workspace_id="workspace-1", channel_id="channel-1",
                  root_thread_ts="1710000000.000100", created_at="2026-01-01T00:00:00+00:00",
                  deadline_at="2026-01-01T00:01:00+00:00")
    request = ResearchRequest(schema_version="hedge.research-request.v1", artifact_id="request-1",
                              role="research_coordinator", parent_hashes=(), status="REQUESTED",
                              objective="Observe market conditions", symbols=("SPY",),
                              research_questions=("What changed?",), **common)
    report = SpecialistReport(schema_version="hedge.specialist-report.v1", artifact_id="report-1",
                              role="specialist", parent_hashes=(request.sha256(),), status="COMPLETED",
                              specialty="market_scout", summary="Observed data", findings=("One fact",),
                              confidence=0.5, **common)
    return request, report


def test_bare_or_forged_task_id_is_unsupported_without_reservation(boundary, handoff):
    outbox, state, sender = boundary
    request, report = handoff
    for task_id in (None, "caller-invented-task-id"):
        with pytest.raises(UnsupportedSpecialistDelivery, match="authenticated terminal"):
            outbox.deliver_specialist_report(delivery_id="arbitrary-id", request=request,
                                             report=report, task_id=task_id)
    assert sender.calls == []
    assert state.delivery_status("arbitrary-id") is None


def test_wrong_parent_hash_rejected_before_delivery(boundary, handoff):
    outbox, state, sender = boundary
    request, report = handoff
    forged = replace(report, parent_hashes=("a" * 64,))
    with pytest.raises(HandoffValidationError, match="does not bind"):
        outbox.deliver_specialist_report(delivery_id="forged", request=request, report=forged, task_id="child-1")
    assert sender.calls == []
    assert state.delivery_status("forged") is None


def test_nonterminal_or_cross_thread_artifact_rejected(boundary, handoff):
    outbox, state, sender = boundary
    request, report = handoff
    # Frozen dataclasses are not authenticated envelopes; revalidate even if
    # an object bypasses its constructor after creation.
    object.__setattr__(report, "status", "RUNNING")
    with pytest.raises(HandoffValidationError, match="invalid status"):
        outbox.deliver_specialist_report(delivery_id="nonterminal", request=request, report=report)
    other = replace(request, channel_id="wrong-channel")
    valid_report = replace(report, status="COMPLETED", parent_hashes=(other.sha256(),))
    with pytest.raises(HandoffValidationError, match="different Slack affinity"):
        outbox.deliver_specialist_report(delivery_id="cross-thread", request=other, report=valid_report)
    assert sender.calls == []
    assert state.delivery_status("nonterminal") is None
    assert state.delivery_status("cross-thread") is None


def test_repeated_delivery_and_alternate_ids_cannot_duplicate(boundary, handoff):
    outbox, state, sender = boundary
    request, report = handoff
    for delivery_id in ("id-1", "id-1", "id-2"):
        with pytest.raises(UnsupportedSpecialistDelivery):
            outbox.deliver_specialist_report(delivery_id=delivery_id, request=request,
                                             report=report, task_id="same-task")
    assert sender.calls == []
    assert state.delivery_status("id-1") is None
    assert state.delivery_status("id-2") is None
