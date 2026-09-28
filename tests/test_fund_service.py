"""Contract checks for durable paper fund orchestration."""
from fractions import Fraction

import pytest

from hedge.fund_service import FundConsistencyError, PaperFundService
from hedge.dashboard import render_dashboard


CREATE = dict(pool_id="demo", actor_id="owner", name="Paper Alpha",
              starting_balance_cents=10000, mandate="Long only simulated equities")


def test_create_restart_duplicate_and_member_nav(tmp_path):
    path = tmp_path / "fund.sqlite"
    service = PaperFundService(path)
    initial = service.create_virtual_pool(**CREATE)
    assert initial.status.value == "DRAFT"
    assert service.create_virtual_pool(**CREATE) == initial
    assert len(service.capital.contribution_history(pool_id="demo")) == 1
    with pytest.raises(ValueError, match="different"):
        service.create_virtual_pool(**{**CREATE, "starting_balance_cents": 20000})
    service.activate_pool(pool_id="demo", actor_id="owner")
    service.invite_member(invitation_id="invite.1", pool_id="demo", actor_id="owner", member_id="guest")
    assert service.join_with_virtual_contribution(invitation_id="invite.1", actor_id="guest",
                                                  contribution_cents=30000, event_id="join.1").units == 0
    service.close()

    service = PaperFundService(path)
    assert service.create_virtual_pool(**CREATE).status.value == "ACTIVE"
    assert service.join_with_virtual_contribution(invitation_id="invite.1", actor_id="guest",
                                                  contribution_cents=30000, event_id="join.1").units == 0
    assert len(service.capital.contribution_history(pool_id="demo")) == 2
    assert [p.pool_id for p in service.member_pools(member_id="guest")] == ["demo"]
    assert service.pools.unit_ownership("demo") == {"guest": 0, "owner": 10000}
    snapshot = service.snapshot(pool_id="demo", cash_cents=20000,
                                positions={"AAPL": 10}, prices_cents={"AAPL": 2500},
                                cost_basis_cents={"AAPL": 2000})
    assert snapshot.nav.fund_nav_cents == 45000
    assert [(a.member_id, a.ownership, a.nav_cents) for a in snapshot.nav.allocations] == [
        ("guest", Fraction(3, 4), 33750), ("owner", Fraction(1, 4), 11250)]
    assert snapshot.dashboard.nav_cents == 45000
    assert snapshot.positions[0].pnl_cents == 5000
    assert [stake.ownership_bps for stake in snapshot.stakes] == [7500, 2500]
    assert "<html" in render_dashboard(snapshot.report).lower()
    assert snapshot.report["summary"]["nav"] == 450
    assert snapshot.report["breakdowns"]["capital_account_nav_cents"] == {"guest": 33750, "owner": 11250}
    with pytest.raises(ValueError, match="different"):
        service.join_with_virtual_contribution(invitation_id="invite.1", actor_id="guest",
                                               contribution_cents=40000, event_id="join.1")
    service.close()


def test_create_partial_write_fails_closed_and_exact_retry_repairs(tmp_path, monkeypatch):
    service = PaperFundService(tmp_path / "fund.sqlite")
    record = service.capital.record_virtual_starting_balance
    def unavailable(**kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(service.capital, "record_virtual_starting_balance", unavailable)
    with pytest.raises(FundConsistencyError, match="incomplete"):
        service.create_virtual_pool(**CREATE)
    with pytest.raises(FundConsistencyError):
        service.snapshot(pool_id="demo", cash_cents=10000, positions={}, prices_cents={})
    with pytest.raises(ValueError, match="different"):
        service.create_virtual_pool(**{**CREATE, "starting_balance_cents": 20000})
    monkeypatch.setattr(service.capital, "record_virtual_starting_balance", record)
    service.close()
    service = PaperFundService(tmp_path / "fund.sqlite")
    assert service.create_virtual_pool(**CREATE).pool_id == "demo"
    service.close()


def test_join_partial_write_fails_closed_and_exact_retry_repairs(tmp_path, monkeypatch):
    service = PaperFundService(tmp_path / "fund.sqlite")
    service.create_virtual_pool(**CREATE)
    service.activate_pool(pool_id="demo", actor_id="owner")
    service.invite_member(invitation_id="invite.1", pool_id="demo", actor_id="owner", member_id="guest")
    record = service.capital.record_virtual_contribution
    def unavailable(**kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(service.capital, "record_virtual_contribution", unavailable)
    with pytest.raises(FundConsistencyError, match="incomplete"):
        service.join_with_virtual_contribution(invitation_id="invite.1", actor_id="guest",
                                               contribution_cents=30000, event_id="join.1")
    with pytest.raises(FundConsistencyError):
        service.member_pools(member_id="guest")
    monkeypatch.setattr(service.capital, "record_virtual_contribution", record)
    service.close()
    service = PaperFundService(tmp_path / "fund.sqlite")
    assert service.join_with_virtual_contribution(invitation_id="invite.1", actor_id="guest",
                                                  contribution_cents=30000, event_id="join.1").member_id == "guest"
    service.close()


def test_prices_are_required_and_paper_only(tmp_path):
    service = PaperFundService(tmp_path / "fund.sqlite")
    service.create_virtual_pool(**CREATE)
    with pytest.raises(ValueError, match="cover exactly"):
        service.snapshot(pool_id="demo", cash_cents=0, positions={"AAPL": 1}, prices_cents={})
    with pytest.raises(ValueError, match="positive"):
        service.snapshot(pool_id="demo", cash_cents=0, positions={"AAPL": 1}, prices_cents={"AAPL": 0})
    with pytest.raises(ValueError):
        service.snapshot(pool_id="demo", cash_cents=0, positions={"AAPL": -1}, prices_cents={"AAPL": 100})
    service.close()
