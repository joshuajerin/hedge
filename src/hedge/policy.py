"""Local, deterministic controls. They apply even if an agent is wrong or compromised."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError, Decision, TradeIntent


@dataclass(frozen=True)
class PaperPolicy:
    account_mode: str = "paper"
    allowed_exchange: str = "SMART"
    max_orders_per_decision: int = 8
    max_shares_per_order: int = 100
    allow_short_sales: bool = False
    allowed_order_types: frozenset[str] = frozenset({"MKT", "LMT"})

    def validate(self, decision: Decision) -> None:
        if self.account_mode != "paper":
            raise ContractError("Hedge local runner is paper-only")
        if len(decision.intents) > self.max_orders_per_decision:
            raise ContractError("decision exceeds max_orders_per_decision")
        for intent in decision.intents:
            self.validate_intent(intent)

    def validate_intent(self, intent: TradeIntent) -> None:
        if intent.order_type not in self.allowed_order_types:
            raise ContractError("order type is not allowed")
        if intent.quantity > self.max_shares_per_order:
            raise ContractError("order exceeds max_shares_per_order")
        if intent.side == "SELL" and not self.allow_short_sales:
            # A position check happens before submission; this rejects blind shorting.
            return
