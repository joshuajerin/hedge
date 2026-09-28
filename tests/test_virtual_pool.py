from __future__ import annotations

from fractions import Fraction

import pytest

from hedge.virtual_pool import (
    AuditAction,
    InvitationStatus,
    LifecycleError,
    MandateRisk,
    NotFoundError,
    PoolStatus,
    SqliteVirtualPoolStore,
    VirtualPoolError,
    VirtualPoolService,
)


CREATED = "2025-01-01T00:00:00+00:00"
ACTIVATED = "2025-01-02T00:00:00+00:00"


def _service(path: str = ":memory:") -> tuple[SqliteVirtualPoolStore, VirtualPoolService]:
    store = SqliteVirtualPoolStore(path)
    return store, VirtualPoolService(store)


def _create(service: VirtualPoolService) -> None:
    service.create_pool(
        pool_id="pool.alpha",
        name="Alpha simulation",
        mandate="Long-term diversified equities simulation",
        base_currency="USD",
        starting_nav_cents=10_001,
        mandate_risk=MandateRisk.MEDIUM,
        creator_id="creator-1",
        created_at=CREATED,
    )


def test_creation_mints_exact_virtual_units_for_creator_and_audits() -> None:
    _, service = _service()
    _create(service)

    pool = service.pool("pool.alpha")
    creator = service.members("pool.alpha")[0]
    assert pool.status is PoolStatus.DRAFT
    assert pool.total_units == 10_001
    assert creator.member_id == "creator-1"
    assert creator.role == "CREATOR"
    assert creator.units == 10_001
    assert creator.ownership_fraction(pool.total_units) == Fraction(1, 1)
    assert service.unit_ownership("pool.alpha") == {"creator-1": 10_001}
    events = service.audit_events("pool.alpha")
    assert [(event.sequence, event.action, event.actor_id, event.subject_id) for event in events] == [
        (1, AuditAction.POOL_CREATED, "creator-1", "pool.alpha")
    ]


def test_repeated_creations_are_idempotent_and_conflicting_ids_fail_closed() -> None:
    _, service = _service()
    _create(service)
    original = service.pool("pool.alpha")

    repeated_pool = service.create_pool(
        pool_id="pool.alpha",
        name="Alpha simulation",
        mandate="Long-term diversified equities simulation",
        base_currency="USD",
        starting_nav_cents=10_001,
        mandate_risk="MEDIUM",
        creator_id="creator-1",
        created_at=CREATED,
    )
    assert repeated_pool == original
    assert [event.action for event in service.audit_events("pool.alpha")] == [AuditAction.POOL_CREATED]
    with pytest.raises(VirtualPoolError, match="different virtual pool creation"):
        service.create_pool(
            pool_id="pool.alpha",
            name="Alpha simulation",
            mandate="Different simulated strategy",
            base_currency="USD",
            starting_nav_cents=10_001,
            mandate_risk="MEDIUM",
            creator_id="creator-1",
            created_at=CREATED,
        )

    first_invitation = service.invite_member(
        invitation_id="invite-1",
        pool_id="pool.alpha",
        invitee_id="member-2",
        actor_id="creator-1",
        created_at=ACTIVATED,
    )
    repeated_invitation = service.invite_member(
        invitation_id="invite-1",
        pool_id="pool.alpha",
        invitee_id="member-2",
        actor_id="creator-1",
        created_at=ACTIVATED,
    )
    assert repeated_invitation == first_invitation
    assert [event.action for event in service.audit_events("pool.alpha")] == [
        AuditAction.POOL_CREATED,
        AuditAction.INVITATION_CREATED,
    ]
    with pytest.raises(VirtualPoolError, match="different invitation"):
        service.invite_member(
            invitation_id="invite-1",
            pool_id="pool.alpha",
            invitee_id="member-3",
            actor_id="creator-1",
            created_at=ACTIVATED,
        )


def test_invitation_activation_adds_zero_unit_member_and_full_audit_trail() -> None:
    _, service = _service()
    _create(service)
    service.activate_pool(pool_id="pool.alpha", actor_id="creator-1", activated_at=ACTIVATED)
    invitation = service.invite_member(
        invitation_id="invite-1",
        pool_id="pool.alpha",
        invitee_id="member-2",
        actor_id="creator-1",
        created_at="2025-01-03T00:00:00+00:00",
    )
    member = service.activate_invitation(
        invitation_id=invitation.invitation_id,
        actor_id="member-2",
        activated_at="2025-01-04T00:00:00+00:00",
    )

    assert member.units == 0
    assert member.ownership_fraction(10_001) == Fraction(0, 1)
    assert service.unit_ownership("pool.alpha") == {"creator-1": 10_001, "member-2": 0}
    assert [event.action for event in service.audit_events("pool.alpha")] == [
        AuditAction.POOL_CREATED,
        AuditAction.POOL_ACTIVATED,
        AuditAction.INVITATION_CREATED,
        AuditAction.INVITATION_ACTIVATED,
    ]
    assert service.invitation("invite-1").status is InvitationStatus.ACTIVATED


def test_lifecycle_and_actor_authorization_are_fail_closed() -> None:
    _, service = _service()
    _create(service)
    invitation = service.invite_member(
        invitation_id="invite-1", pool_id="pool.alpha", invitee_id="member-2", actor_id="creator-1", created_at=ACTIVATED
    )
    with pytest.raises(LifecycleError, match="active pools"):
        service.activate_invitation(invitation_id=invitation.invitation_id, actor_id="member-2")
    with pytest.raises(LifecycleError, match="only the creator"):
        service.activate_pool(pool_id="pool.alpha", actor_id="member-2")

    service.activate_pool(pool_id="pool.alpha", actor_id="creator-1", activated_at=ACTIVATED)
    with pytest.raises(LifecycleError, match="only the invitee"):
        service.activate_invitation(invitation_id="invite-1", actor_id="creator-1")
    service.archive_pool(pool_id="pool.alpha", actor_id="creator-1", archived_at="2025-01-03T00:00:00+00:00")
    assert service.pool("pool.alpha").status is PoolStatus.ARCHIVED
    with pytest.raises(LifecycleError, match="archived pools"):
        service.invite_member(invitation_id="invite-2", pool_id="pool.alpha", invitee_id="member-3", actor_id="creator-1")


def test_revoke_is_terminal_and_recorded() -> None:
    _, service = _service()
    _create(service)
    invitation = service.invite_member(
        invitation_id="invite-1", pool_id="pool.alpha", invitee_id="member-2", actor_id="creator-1", created_at=ACTIVATED
    )
    revoked = service.revoke_invitation(invitation_id=invitation.invitation_id, actor_id="creator-1", revoked_at=ACTIVATED)
    assert revoked.status is InvitationStatus.REVOKED
    with pytest.raises(LifecycleError, match="only pending"):
        service.revoke_invitation(invitation_id="invite-1", actor_id="creator-1")
    assert service.audit_events("pool.alpha")[-1].action is AuditAction.INVITATION_REVOKED


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_nonpositive_or_noninteger_starting_nav_is_rejected(value: object) -> None:
    _, service = _service()
    with pytest.raises(VirtualPoolError, match="starting_nav_cents"):
        service.create_pool(
            pool_id="pool.alpha", name="Alpha", mandate="Diversified simulation", base_currency="USD", starting_nav_cents=value,
            mandate_risk="LOW", creator_id="creator-1", created_at=CREATED,
        )


def test_strict_models_reject_real_money_language_and_unknown_records() -> None:
    _, service = _service()
    with pytest.raises(VirtualPoolError, match="real-money"):
        service.create_pool(
            pool_id="pool.alpha", name="Real Money Pool", mandate="Diversified simulation", base_currency="USD", starting_nav_cents=1,
            mandate_risk="LOW", creator_id="creator-1", created_at=CREATED,
        )
    with pytest.raises(VirtualPoolError, match="real-money"):
        service.create_pool(
            pool_id="pool.alpha", name="Alpha", mandate="Deposit! all virtual units", base_currency="USD", starting_nav_cents=1,
            mandate_risk="LOW", creator_id="creator-1", created_at=CREATED,
        )
    with pytest.raises(VirtualPoolError, match="real-money"):
        service.create_pool(
            pool_id="pool.alpha", name="Money simulation", mandate="Diversified simulation", base_currency="USD", starting_nav_cents=1,
            mandate_risk="LOW", creator_id="creator-1", created_at=CREATED,
        )
    with pytest.raises(VirtualPoolError, match="base_currency"):
        service.create_pool(
            pool_id="pool.alpha", name="Alpha", mandate="Diversified simulation", base_currency="usd", starting_nav_cents=1,
            mandate_risk="LOW", creator_id="creator-1", created_at=CREATED,
        )
    with pytest.raises(NotFoundError):
        service.pool("missing")


def test_sqlite_repository_is_durable_and_audit_sequences_continue(tmp_path: object) -> None:
    path = tmp_path / "virtual-pools.sqlite"  # type: ignore[operator]
    store, service = _service(str(path))
    _create(service)
    store.close()

    reopened, service = _service(str(path))
    assert service.pool("pool.alpha").name == "Alpha simulation"
    assert service.unit_ownership("pool.alpha") == {"creator-1": 10_001}
    service.activate_pool(pool_id="pool.alpha", actor_id="creator-1", activated_at=ACTIVATED)
    assert [event.sequence for event in service.audit_events("pool.alpha")] == [1, 2]
    reopened.close()
