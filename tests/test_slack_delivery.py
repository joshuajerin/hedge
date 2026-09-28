from __future__ import annotations

from hedge.slack_delivery import (
    DeliveryRequest,
    DeliveryStatus,
    HedgeRole,
    ROLE_PROFILES,
    SlackResultDelivery,
)
from hedge.slack_state import SlackState, ThreadCorrelation
from hedge.orchestration_protocol import SpecialistReport
from hedge.slack_outbox import ArtifactOutbox, UnsupportedSpecialistDelivery


class FakeSender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def send(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def correlation() -> ThreadCorrelation:
    return ThreadCorrelation("run-1", "workspace-1", "channel-1", "1710000000.000100")


def test_profiles_are_fixed_and_include_cio_and_six_specialists() -> None:
    assert set(ROLE_PROFILES) == set(HedgeRole)
    assert ROLE_PROFILES[HedgeRole.CIO].display_name == "Hedge CIO"
    assert all(profile.token_env.endswith("BOT_TOKEN") for profile in ROLE_PROFILES.values())
    try:
        ROLE_PROFILES[HedgeRole.CIO] = ROLE_PROFILES[HedgeRole.CIO]  # type: ignore[index]
    except TypeError:
        pass
    else:  # pragma: no cover - MappingProxyType must be immutable
        raise AssertionError("role profiles must not be mutable")


def test_delivery_is_idempotent_across_state_restart(tmp_path) -> None:
    path = tmp_path / "slack.sqlite3"
    state = SlackState(path)
    state.record_run(correlation())
    sender = FakeSender()
    service = SlackResultDelivery(state, sender)
    request = DeliveryRequest("delivery-1", HedgeRole.MARKET_SCOUT, correlation(), "Research result")

    assert service.deliver(request).status is DeliveryStatus.DELIVERED
    state.close()

    restarted = SlackState(path)
    duplicate = SlackResultDelivery(restarted, sender).deliver(request)
    assert duplicate.status is DeliveryStatus.DUPLICATE
    assert len(sender.calls) == 1
    assert restarted.delivery_status("delivery-1") == "delivered"


def test_inbound_event_idempotency_survives_restart(tmp_path) -> None:
    path = tmp_path / "slack.sqlite3"
    first = SlackState(path)
    assert first.claim_inbound_event("Ev-1")
    assert not first.claim_inbound_event("Ev-1")
    first.close()

    restarted = SlackState(path)
    assert not restarted.claim_inbound_event("Ev-1")


def test_bad_thread_affinity_is_rejected_without_calling_sender() -> None:
    state = SlackState(":memory:")
    state.record_run(correlation())
    sender = FakeSender()
    bad = ThreadCorrelation("run-1", "workspace-1", "other-channel", "1710000000.000100")
    result = SlackResultDelivery(state, sender).deliver(
        DeliveryRequest("delivery-2", HedgeRole.RISK_REVIEWER, bad, "Do this")
    )

    assert result.status is DeliveryStatus.REJECTED
    assert sender.calls == []


def test_role_identity_and_canonical_thread_are_sender_controlled() -> None:
    state = SlackState(":memory:")
    state.record_run(correlation())
    sender = FakeSender()
    result = SlackResultDelivery(state, sender).deliver(
        DeliveryRequest("delivery-3", "risk_reviewer", correlation(), "HOLD")
    )

    assert result.delivered
    call = sender.calls[0]
    assert call["profile"] is ROLE_PROFILES[HedgeRole.RISK_REVIEWER]
    assert call["channel_id"] == "channel-1"
    assert call["thread_ts"] == "1710000000.000100"
    assert "token" not in call


def test_sender_failure_is_terminal_and_does_not_leak_token(caplog) -> None:
    state = SlackState(":memory:")
    state.record_run(correlation())
    sender = FakeSender(RuntimeError("credential-redacted"))
    service = SlackResultDelivery(state, sender)
    request = DeliveryRequest("delivery-4", HedgeRole.CIO, correlation(), "Answer")

    assert service.deliver(request).status is DeliveryStatus.FAILED
    assert state.delivery_status("delivery-4") == "failed"
    assert service.deliver(request).status is DeliveryStatus.DUPLICATE
    assert len(sender.calls) == 1
    assert "credential-redacted" not in caplog.text


def test_unknown_role_fails_closed() -> None:
    state = SlackState(":memory:")
    state.record_run(correlation())
    sender = FakeSender()
    result = SlackResultDelivery(state, sender).deliver(
        DeliveryRequest("delivery-5", "operator_selected_profile", correlation(), "Answer")
    )
    assert result.status is DeliveryStatus.REJECTED
    assert sender.calls == []


def test_one_active_run_per_root_thread_and_workspace_scoped_events() -> None:
    state = SlackState(":memory:")
    first = correlation()
    second = ThreadCorrelation("run-2", "workspace-1", "channel-1", "1710000000.000100")
    assert state.record_run(first, event_id="Ev-1", requester="U-1", deadline_at="2099-01-01T00:00:00+00:00")
    assert not state.record_run(second, event_id="Ev-2", requester="U-2", deadline_at="2099-01-01T00:00:00+00:00")
    state.set_run_status(first.run_id, "completed")
    assert state.record_run(second, event_id="Ev-2", requester="U-2", deadline_at="2099-01-01T00:00:00+00:00")
    assert state.claim_inbound_event("Ev-same", "workspace-1")
    assert state.claim_inbound_event("Ev-same", "workspace-2")


def test_artifact_outbox_uses_only_fixed_specialist_profile() -> None:
    state = SlackState(":memory:")
    state.record_run(correlation())
    sender = FakeSender()
    outbox = ArtifactOutbox(state, SlackResultDelivery(state, sender))
    report = SpecialistReport(
        schema_version="hedge.specialist-report.v1", artifact_id="report-1", run_id="run-1",
        workspace_id="workspace-1", channel_id="channel-1", root_thread_ts="1710000000.000100",
        role="specialist", created_at="2026-01-01T00:00:00+00:00", deadline_at="2026-01-01T00:01:00+00:00",
        parent_hashes=("a" * 64,), status="COMPLETED", specialty="market_scout",
        summary="A factual report", findings=("One observed fact",), confidence=0.5,
    )
    try:
        outbox.deliver_specialist_report(delivery_id="artifact-delivery-1", report=report)
    except UnsupportedSpecialistDelivery:
        pass
    else:
        raise AssertionError("bare specialist reports must not reach Slack")
    assert sender.calls == []
    assert state.delivery_status("artifact-delivery-1") is None
