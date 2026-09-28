"""Read-only, JSON-safe data builders for Hedge's paper dashboard.

This module has no broker imports and cannot submit an order. It converts
already-normalized research and paper-account records into a stable report
contract that a local dashboard can render.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from .contracts import Decision

DASHBOARD_SCHEMA = "hedge.dashboard.v1"
DEFAULT_STALE_AFTER_SECONDS = 15 * 60


def _as_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp without guessing a local timezone."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)


def _timestamp(value: object) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _number(value: object, default: float = 0.0) -> float:
    """Return a finite JSON number, including when input uses ``Decimal``."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        return default
    return result if isfinite(result) else default


def _integer(value: object, default: int = 0) -> int:
    """Return an integral, finite value without letting Decimal leak into JSON."""
    if isinstance(value, bool):
        return default
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return default
        return int(value)
    number = _number(value, float(default))
    return int(number) if number.is_integer() else default


def _money(value: object, cents: object | None = None, default: float = 0.0) -> float:
    """Read USD first, then an integer-cent fallback, and round for JSON."""
    if value is not None:
        return round(_number(value, default), 6)
    return round(_integer(cents) / 100, 6) if cents is not None else round(default, 6)


def _text(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _record(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _point(at: str | None, value: float, **extra: object) -> dict[str, Any]:
    point: dict[str, Any] = {"at": at, "value": round(_number(value), 6)}
    point.update({key: item for key, item in extra.items() if item is not None})
    return point


def _age_seconds(at: str | None, now: datetime) -> int | None:
    parsed = _as_utc(at)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _freshness(name: str, at: str | None, now: datetime, stale_after_seconds: int) -> dict[str, Any]:
    age = _age_seconds(at, now)
    status = "unknown" if age is None else ("stale" if age > stale_after_seconds else "fresh")
    return {"name": name, "at": at, "age_seconds": age, "status": status}


def _period_values(value: object) -> dict[str, float]:
    """Whitelist a normalized period-to-percent mapping."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for raw_period, raw_return in value.items():
        period = _text(raw_period)
        if period:
            result[period] = round(_number(raw_return), 6)
    return dict(sorted(result.items()))


def normalize_decisions(decisions: Iterable[Decision | Mapping[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return whitelisted decision/order-flow records."""
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    normalized: list[dict[str, Any]] = []
    for item in decisions:
        source: Mapping[str, Any] = item.as_dict() if isinstance(item, Decision) else _record(item)
        decision_id = _text(source.get("decision_id"), "unknown")
        created_at = _timestamp(source.get("created_at"))
        intents = source.get("intents")
        safe_intents: list[dict[str, Any]] = []
        if isinstance(intents, (list, tuple)):
            for raw_intent in intents:
                intent = _record(raw_intent)
                side = _text(intent.get("side")).upper()
                order_type = _text(intent.get("order_type")).upper()
                if side not in {"BUY", "SELL"} or order_type not in {"MKT", "LMT"}:
                    continue
                safe_intents.append({
                    "symbol": _text(intent.get("symbol")).upper() or "UNKNOWN",
                    "side": side,
                    "quantity": max(0, _integer(intent.get("quantity"))),
                    "order_type": order_type,
                    "limit_price": _money(intent.get("limit_price")) if intent.get("limit_price") is not None else None,
                })
        status = _text(source.get("status") or source.get("review_status"), "proposed").lower()
        normalized.append({
            "decision_id": decision_id, "pool_id": _text(source.get("pool_id"), "unknown"),
            "mandate_version": max(0, _integer(source.get("mandate_version"))),
            "created_at": created_at,
            "reviewed_at": _timestamp(source.get("reviewed_at") or source.get("updated_at")),
            "age_seconds": _age_seconds(created_at, reference), "status": status,
            "intents": safe_intents, "intent_count": len(safe_intents),
        })
    return sorted(normalized, key=lambda item: (item["created_at"] is None, item["created_at"] or "", item["decision_id"]))


def decision_flow(decisions: Iterable[Decision | Mapping[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Build cumulative decision/order counts and a review-safe event flow."""
    items = normalize_decisions(decisions, now=now)
    cumulative_decisions = cumulative_orders = 0
    decision_points: list[dict[str, Any]] = []
    order_points: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for decision in items:
        cumulative_decisions += 1
        cumulative_orders += decision["intent_count"]
        at = decision["created_at"]
        decision_points.append(_point(at, cumulative_decisions, decision_id=decision["decision_id"]))
        order_points.append(_point(at, cumulative_orders, decision_id=decision["decision_id"]))
        status_counts[decision["status"]] = status_counts.get(decision["status"], 0) + 1
        events.append({
            "at": at, "kind": "decision", "decision_id": decision["decision_id"], "pool_id": decision["pool_id"],
            "status": decision["status"], "intent_count": decision["intent_count"], "age_seconds": decision["age_seconds"],
            "orders": decision["intents"],
        })
    return {"events": events, "status_counts": dict(sorted(status_counts.items())), "series": [
        {"name": "Decisions", "unit": "count", "points": decision_points},
        {"name": "Proposed orders", "unit": "count", "points": order_points},
    ]}


def contribution_flow(contributions: Iterable[Mapping[str, Any]] | Mapping[str, Any]) -> dict[str, Any]:
    """Build a virtual-pool contribution series in cents, never real cash flow."""
    rows: list[Mapping[str, Any]] = []
    if isinstance(contributions, Mapping):
        for pool_id, members in contributions.items():
            if isinstance(members, Mapping):
                for member_id, cents in members.items():
                    rows.append({"pool_id": pool_id, "member_id": member_id, "cents": cents})
    else:
        rows = [_record(row) for row in contributions]
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        cents = _integer(row.get("cents", row.get("amount_cents")))
        cleaned.append({"at": _timestamp(row.get("at") or row.get("created_at") or row.get("timestamp")),
                        "pool_id": _text(row.get("pool_id"), "unknown"), "member_id": _text(row.get("member_id"), "unknown"), "cents": cents})
    cleaned.sort(key=lambda row: (row["at"] is None, row["at"] or "", row["pool_id"], row["member_id"]))
    total = 0
    points: list[dict[str, Any]] = []
    by_member: dict[str, int] = {}
    by_pool: dict[str, int] = {}
    for row in cleaned:
        total += row["cents"]
        points.append(_point(row["at"], total / 100, pool_id=row["pool_id"], member_id=row["member_id"]))
        by_member[row["member_id"]] = by_member.get(row["member_id"], 0) + row["cents"]
        by_pool[row["pool_id"]] = by_pool.get(row["pool_id"], 0) + row["cents"]
    return {"total_cents": total, "by_member_cents": dict(sorted(by_member.items())), "by_pool_cents": dict(sorted(by_pool.items())),
            "series": [{"name": "Virtual contributions", "unit": "USD", "points": points}]}


def equity_flow(snapshots: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate paper equity, cumulative P&L, and high-water drawdown."""
    cleaned: list[dict[str, Any]] = []
    for raw in snapshots:
        row = _record(raw)
        at = _timestamp(row.get("at") or row.get("as_of") or row.get("timestamp"))
        equity = _money(row.get("equity"), row.get("equity_cents"))
        benchmark = _number(row.get("benchmark", row.get("benchmark_value"))) if ("benchmark" in row or "benchmark_value" in row) else None
        cleaned.append({"at": at, "equity": equity, "benchmark": benchmark})
    cleaned.sort(key=lambda row: (row["at"] is None, row["at"] or ""))
    baseline = cleaned[0]["equity"] if cleaned else 0.0
    high_water = baseline
    equity_points: list[dict[str, Any]] = []
    pnl_points: list[dict[str, Any]] = []
    drawdown_points: list[dict[str, Any]] = []
    benchmark_points: list[dict[str, Any]] = []
    for row in cleaned:
        equity = row["equity"]
        high_water = max(high_water, equity)
        pnl = equity - baseline
        drawdown = ((equity - high_water) / high_water * 100) if high_water > 0 else 0.0
        equity_points.append(_point(row["at"], equity))
        pnl_points.append(_point(row["at"], pnl))
        drawdown_points.append(_point(row["at"], drawdown))
        if row["benchmark"] is not None:
            benchmark_points.append(_point(row["at"], row["benchmark"]))
    series = [
        {"name": "Paper equity", "unit": "USD", "points": equity_points},
        {"name": "Paper P&L", "unit": "USD", "points": pnl_points},
        {"name": "Drawdown", "unit": "percent", "points": drawdown_points},
    ]
    if benchmark_points:
        series.append({"name": "Benchmark", "unit": "index", "points": benchmark_points})
    return {"baseline_equity": round(baseline, 6), "latest_equity": round(cleaned[-1]["equity"], 6) if cleaned else None,
            "pnl": round(cleaned[-1]["equity"] - baseline, 6) if cleaned else 0.0,
            "max_drawdown_pct": round(min((point["value"] for point in drawdown_points), default=0.0), 6),
            "latest_at": cleaned[-1]["at"] if cleaned else None, "series": series}


def fund_summary(fund: Mapping[str, Any] | None = None, *, latest_equity: object | None = None,
                 benchmark: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize paper-fund NAV, cash, returns, and benchmark deltas.

    Return values are percentages (for example ``1.25`` means 1.25%), not
    decimal fractions. ``fund`` and ``benchmark`` are caller-supplied records;
    this helper does not fetch a price or a benchmark.
    """
    source = _record(fund)
    benchmark_source = _record(benchmark) or _record(source.get("benchmark"))
    nav_source = source.get("nav", source.get("nav_usd"))
    if nav_source is None and source.get("nav_cents") is None:
        nav_source = latest_equity
    nav = _money(nav_source, source.get("nav_cents"), _number(latest_equity) if latest_equity is not None else 0.0)
    cash = _money(source.get("cash", source.get("cash_usd")), source.get("cash_cents"))
    fund_returns = _period_values(source.get("return_periods", source.get("returns")))
    benchmark_returns = _period_values(benchmark_source.get("return_periods", benchmark_source.get("returns")))
    rows = []
    for period in sorted(set(fund_returns) | set(benchmark_returns)):
        value = fund_returns.get(period)
        benchmark_value = benchmark_returns.get(period)
        rows.append({"period": period, "return_pct": value, "benchmark_return_pct": benchmark_value,
                     "benchmark_delta_pct": round(value - benchmark_value, 6) if value is not None and benchmark_value is not None else None})
    nav_cents = _integer(source.get("nav_cents"), _integer(round(nav * 100)))
    cash_cents = _integer(source.get("cash_cents"), _integer(round(cash * 100)))
    return {"nav": nav, "nav_cents": nav_cents, "cash": cash, "cash_cents": cash_cents,
            "return_periods": rows, "benchmark": {"name": _text(benchmark_source.get("name") or source.get("benchmark_name"), "Benchmark"),
                                                       "return_periods": benchmark_returns}}


def position_snapshot(positions: Iterable[Mapping[str, Any]], *, nav: object | None = None) -> list[dict[str, Any]]:
    """Return calculated paper-position cards, omitting arbitrary source fields."""
    result: list[dict[str, Any]] = []
    for raw in positions:
        row = _record(raw)
        quantity = _number(row.get("quantity"))
        current_price = _money(row.get("current_price", row.get("mark_price", row.get("price"))), row.get("current_price_cents", row.get("mark_price_cents")))
        entry_price = _money(row.get("entry_price", row.get("avg_cost", row.get("cost_basis", row.get("average_cost")))), row.get("entry_price_cents", row.get("avg_cost_cents")))
        market_value = _money(row.get("market_value"), row.get("market_value_cents"), quantity * current_price)
        unrealized_pnl = _money(row.get("unrealized_pnl"), row.get("unrealized_pnl_cents"), (current_price - entry_price) * quantity)
        realized_pnl = _money(row.get("realized_pnl"), row.get("realized_pnl_cents"))
        result.append({
            "symbol": _text(row.get("symbol"), "UNKNOWN").upper(), "quantity": quantity,
            "entry_price": entry_price, "current_price": current_price,
            # Previous field names remain public aliases for existing renderers.
            "avg_cost": entry_price, "mark_price": current_price,
            "market_value": market_value, "unrealized_pnl": unrealized_pnl, "realized_pnl": realized_pnl,
            "allocation_pct": _number(row.get("allocation_pct")) if row.get("allocation_pct") is not None else None,
            "thesis_status": _text(row.get("thesis_status"), "unknown").lower(),
            "risk_status": _text(row.get("risk_status"), "unknown").lower(),
            "as_of": _timestamp(row.get("as_of") or row.get("at") or row.get("timestamp")),
        })
    total_value = sum(row["market_value"] for row in result)
    supplied_nav = _number(nav) if nav is not None else 0.0
    denominator = supplied_nav if supplied_nav > 0 else total_value
    for row in result:
        if row["allocation_pct"] is None:
            row["allocation_pct"] = round(row["market_value"] / denominator * 100, 6) if denominator > 0 else 0.0
    return sorted(result, key=lambda row: row["symbol"])


def capital_account_summary(accounts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize caller-supplied, virtual per-member capital-account records."""
    result: list[dict[str, Any]] = []
    for raw in accounts:
        row = _record(raw)
        contributed = _integer(row.get("contributed_cents", row.get("contributions_cents", row.get("contribution_cents"))))
        withdrawn = _integer(row.get("withdrawn_cents", row.get("withdrawals_cents", row.get("withdrawal_cents"))))
        realized = _integer(row.get("realized_pnl_cents"))
        unrealized = _integer(row.get("unrealized_pnl_cents"))
        nav_cents = _integer(row.get("nav_cents", row.get("capital_cents", row.get("balance_cents"))))
        total_pnl = _integer(row.get("total_pnl_cents"), realized + unrealized)
        result.append({
            "member_id": _text(row.get("member_id"), "unknown"), "display_name": _text(row.get("display_name") or row.get("name"), ""),
            "contributed_cents": contributed, "withdrawn_cents": withdrawn, "net_contributions_cents": contributed - withdrawn,
            "nav_cents": nav_cents, "realized_pnl_cents": realized, "unrealized_pnl_cents": unrealized, "total_pnl_cents": total_pnl,
            "allocation_pct": round(_number(row.get("allocation_pct")), 6) if row.get("allocation_pct") is not None else None,
            "as_of": _timestamp(row.get("as_of") or row.get("at")),
        })
    total_nav = sum(row["nav_cents"] for row in result)
    for row in result:
        if row["allocation_pct"] is None:
            row["allocation_pct"] = round(row["nav_cents"] / total_nav * 100, 6) if total_nav > 0 else 0.0
    return sorted(result, key=lambda row: row["member_id"])


def research_flow(stages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize research-agent stage timing into a serializable timeline."""
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(stages):
        row = _record(raw)
        started_at = _timestamp(row.get("started_at") or row.get("created_at"))
        completed_at = _timestamp(row.get("completed_at") or row.get("updated_at") or row.get("at"))
        start, finish = _as_utc(started_at), _as_utc(completed_at)
        result.append({"order": _integer(row.get("order"), index + 1), "agent": _text(row.get("agent") or row.get("stage"), "unknown"),
                       "status": _text(row.get("status"), "unknown").lower(), "started_at": started_at, "completed_at": completed_at,
                       "duration_seconds": max(0, int((finish - start).total_seconds())) if start and finish else None})
    return {"stages": sorted(result, key=lambda row: (row["order"], row["agent"]))}


def build_report(
    *, decisions: Iterable[Decision | Mapping[str, Any]] = (), contributions: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    equity: Iterable[Mapping[str, Any]] = (), positions: Iterable[Mapping[str, Any]] = (), research_stages: Iterable[Mapping[str, Any]] = (),
    fund: Mapping[str, Any] | None = None, benchmark: Mapping[str, Any] | None = None,
    capital_accounts: Iterable[Mapping[str, Any]] = (), generated_at: datetime | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS, max_drawdown_pct: float = 10.0,
) -> dict[str, Any]:
    """Build the complete paper-only dashboard/report document.

    ``fund``, ``benchmark``, and ``capital_accounts`` are normalized, read-only
    inputs. This function performs no I/O and has no order, broker, payment,
    environment, or credential parameters.
    """
    if generated_at is not None and generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    decisions_list = list(decisions)
    decision_data = decision_flow(decisions_list, now=now)
    contribution_data = contribution_flow(contributions)
    equity_data = equity_flow(equity)
    fund_data = fund_summary(fund, latest_equity=equity_data["latest_equity"], benchmark=benchmark)
    position_data = position_snapshot(positions, nav=fund_data["nav"])
    capital_data = capital_account_summary(capital_accounts)
    research_data = research_flow(research_stages)
    latest_decision_at = max((event["at"] for event in decision_data["events"] if event["at"]), default=None)
    latest_stage_at = max((stage["completed_at"] for stage in research_data["stages"] if stage["completed_at"]), default=None)
    freshness = [_freshness("equity", equity_data["latest_at"], now, stale_after_seconds),
                 _freshness("decision flow", latest_decision_at, now, stale_after_seconds),
                 _freshness("research flow", latest_stage_at, now, stale_after_seconds)]
    reasons: list[str] = []
    if equity_data["max_drawdown_pct"] <= -abs(max_drawdown_pct):
        reasons.append(f"drawdown exceeds {abs(max_drawdown_pct):g}% limit")
    stale_names = [item["name"] for item in freshness if item["status"] == "stale"]
    if stale_names:
        reasons.append("stale " + ", ".join(stale_names))
    blocked = [stage["agent"] for stage in research_data["stages"] if stage["status"] in {"blocked", "failed", "rejected"}]
    if blocked:
        reasons.append("research gate not clear: " + ", ".join(blocked))
    risk_status = "blocked" if blocked else ("warning" if reasons else "ok")
    return {
        "schema_version": DASHBOARD_SCHEMA, "generated_at": now.isoformat().replace("+00:00", "Z"), "paper_only": True,
        "summary": {"virtual_contributions_cents": contribution_data["total_cents"], "latest_equity": equity_data["latest_equity"],
                    "pnl": equity_data["pnl"], "max_drawdown_pct": equity_data["max_drawdown_pct"], "fund": fund_data,
                    "nav": fund_data["nav"], "cash": fund_data["cash"], "return_periods": fund_data["return_periods"],
                    "decision_count": len(decision_data["events"]), "proposed_order_count": sum(event["intent_count"] for event in decision_data["events"]),
                    "risk": {"status": risk_status, "reasons": reasons}},
        "freshness": freshness,
        "charts": {"contributions": contribution_data["series"], "equity": equity_data["series"], "equity_curve": equity_data["series"], "decision_flow": decision_data["series"]},
        "flows": {"decisions": decision_data["events"], "research": research_data["stages"]}, "positions": position_data,
        "capital_accounts": capital_data,
        "breakdowns": {"contributions_by_member_cents": contribution_data["by_member_cents"], "contributions_by_pool_cents": contribution_data["by_pool_cents"],
                       "decision_statuses": decision_data["status_counts"], "capital_account_nav_cents": {row["member_id"]: row["nav_cents"] for row in capital_data}},
    }
