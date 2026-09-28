"""Deterministic, paper-only risk checks for the Hedge trading runtime.

This module has no broker, network, clock, or market-data dependency.  Callers
must provide a complete price snapshot, which makes every approval reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from .contracts import ContractError, Decision, TradeIntent


class RiskViolation(ContractError):
    """A decision cannot safely be simulated against the supplied account."""


def money(value: object, *, field_name: str = "amount") -> Decimal:
    """Convert a caller supplied USD value without accepting non-finite values."""

    if isinstance(value, bool):
        raise RiskViolation(f"{field_name} must be a finite positive number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RiskViolation(f"{field_name} must be a finite positive number") from exc
    if not result.is_finite() or result <= 0:
        raise RiskViolation(f"{field_name} must be a finite positive number")
    return result


def _normalized_positions(positions: Mapping[str, int]) -> Mapping[str, int]:
    normalized: dict[str, int] = {}
    for raw_symbol, raw_quantity in positions.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol or not symbol.isascii() or not symbol.replace(".", "").isalnum():
            raise RiskViolation("position symbol must be an ASCII equity ticker")
        if isinstance(raw_quantity, bool) or int(raw_quantity) != raw_quantity:
            raise RiskViolation("position quantity must be an integer")
        quantity = int(raw_quantity)
        if quantity < 0:
            raise RiskViolation("paper accounts cannot contain short positions")
        if quantity:
            normalized[symbol] = quantity
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class PaperAccount:
    """An immutable USD cash and long-only position snapshot."""

    cash: Decimal | int | float | str
    positions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.cash, bool):
            raise RiskViolation("cash must be a finite non-negative number")
        try:
            cash = Decimal(str(self.cash))
        except (InvalidOperation, ValueError) as exc:
            raise RiskViolation("cash must be a finite non-negative number") from exc
        if not cash.is_finite() or cash < 0:
            raise RiskViolation("cash must be a finite non-negative number")
        object.__setattr__(self, "cash", cash)
        object.__setattr__(self, "positions", _normalized_positions(self.positions))

    def as_dict(self) -> dict[str, object]:
        return {"cash": str(self.cash), "positions": dict(self.positions)}


@dataclass(frozen=True)
class RiskLimits:
    """Hard upper bounds.  They apply before any paper state changes."""

    max_orders_per_decision: int = 8
    max_open_orders: int = 8
    max_shares_per_order: int = 100
    max_notional_per_order: Decimal | int | float | str = Decimal("10000")
    max_position_shares: int = 500
    max_gross_exposure: Decimal | int | float | str = Decimal("100000")

    def __post_init__(self) -> None:
        for field_name in (
            "max_orders_per_decision",
            "max_open_orders",
            "max_shares_per_order",
            "max_position_shares",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        object.__setattr__(self, "max_notional_per_order", money(self.max_notional_per_order, field_name="max_notional_per_order"))
        object.__setattr__(self, "max_gross_exposure", money(self.max_gross_exposure, field_name="max_gross_exposure"))


@dataclass(frozen=True)
class PlannedOrder:
    """One fully priced order, safe for deterministic local simulation."""

    intent: TradeIntent
    market_price: Decimal
    worst_case_price: Decimal
    will_fill: bool


@dataclass(frozen=True)
class RiskApproval:
    """The all-or-nothing result of a risk gate evaluation."""

    orders: tuple[PlannedOrder, ...]
    projected_account: PaperAccount
    gross_exposure: Decimal


class RiskGate:
    """Checks a whole decision atomically against a supplied price snapshot."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    @staticmethod
    def _prices_for(account: PaperAccount, decision: Decision, prices: Mapping[str, object]) -> Mapping[str, Decimal]:
        required = set(account.positions) | {intent.symbol for intent in decision.intents}
        normalized: dict[str, Decimal] = {}
        for raw_symbol, raw_price in prices.items():
            symbol = str(raw_symbol).strip().upper()
            if symbol:
                normalized[symbol] = money(raw_price, field_name=f"price for {symbol}")
        missing = sorted(required - set(normalized))
        if missing:
            raise RiskViolation("missing deterministic price(s): " + ", ".join(missing))
        return MappingProxyType(normalized)

    @staticmethod
    def _will_fill(intent: TradeIntent, market_price: Decimal) -> bool:
        if intent.order_type == "MKT":
            return True
        assert intent.limit_price is not None  # guaranteed by TradeIntent.validate
        limit_price = money(intent.limit_price, field_name="limit_price")
        return market_price <= limit_price if intent.side == "BUY" else market_price >= limit_price

    def approve(
        self,
        decision: Decision,
        account: PaperAccount,
        prices: Mapping[str, object],
        *,
        open_orders: int = 0,
    ) -> RiskApproval:
        """Approve only if every potential order is safe.

        Limit buys reserve their limit price rather than a favourable current
        quote.  This makes the cash and exposure checks conservative even when
        a simulation later receives price improvement.
        """

        if isinstance(open_orders, bool) or not isinstance(open_orders, int) or open_orders < 0:
            raise RiskViolation("open_orders must be a non-negative integer")
        count = len(decision.intents)
        if count > self.limits.max_orders_per_decision:
            raise RiskViolation("decision exceeds max_orders_per_decision")
        if open_orders + count > self.limits.max_open_orders:
            raise RiskViolation("decision exceeds max_open_orders")

        market_prices = self._prices_for(account, decision, prices)
        positions = dict(account.positions)
        cash = account.cash
        planned: list[PlannedOrder] = []
        for intent in decision.intents:
            if intent.quantity > self.limits.max_shares_per_order:
                raise RiskViolation("order exceeds max_shares_per_order")
            market_price = market_prices[intent.symbol]
            limit_price = money(intent.limit_price, field_name="limit_price") if intent.limit_price is not None else None
            worst_case_price = limit_price if intent.side == "BUY" and limit_price is not None else market_price
            notional = worst_case_price * intent.quantity
            if notional > self.limits.max_notional_per_order:
                raise RiskViolation("order exceeds max_notional_per_order")

            will_fill = self._will_fill(intent, market_price)
            # Check every requested sell, including a limit order that does not
            # fill at this snapshot. A proposal may never rely on a short sale.
            # Only orders that fill change the shadow account, so a later sell
            # cannot borrow shares from an earlier non-filling buy.
            held = positions.get(intent.symbol, 0)
            if intent.side == "SELL":
                if intent.quantity > held:
                    raise RiskViolation(f"uncovered sell for {intent.symbol}")
                if will_fill:
                    positions[intent.symbol] = held - intent.quantity
                    cash += market_price * intent.quantity
            else:
                if cash < notional:
                    raise RiskViolation("insufficient cash for buy order")
                if will_fill:
                    positions[intent.symbol] = held + intent.quantity
                    if positions[intent.symbol] > self.limits.max_position_shares:
                        raise RiskViolation(f"position exceeds max_position_shares for {intent.symbol}")
                    # Reserve the limit price even when the simulation receives
                    # a better current quote.
                    cash -= notional
            planned.append(PlannedOrder(intent, market_price, worst_case_price, will_fill))

        gross_exposure = sum(Decimal(quantity) * market_prices[symbol] for symbol, quantity in positions.items())
        if gross_exposure > self.limits.max_gross_exposure:
            raise RiskViolation("projected gross exposure exceeds max_gross_exposure")
        return RiskApproval(tuple(planned), PaperAccount(cash, positions), gross_exposure)
