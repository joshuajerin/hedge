"""Stable boundary between Brainbase decisions and the local broker runner."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import math
from typing import Any, Literal
from uuid import uuid4


Side = Literal["BUY", "SELL"]
OrderType = Literal["MKT", "LMT"]


class ContractError(ValueError):
    """An agent result does not meet the local runner's executable contract."""


def _nonempty(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{field} is required")
    return result


def _required_string(value: object, field: str) -> None:
    """Reject malformed directly-constructed contract values."""

    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} is required")


@dataclass(frozen=True)
class TradeIntent:
    """A proposal only. The local runner decides whether it can be submitted."""

    schema_version: Literal["hedge.trade-intent.v1"]
    decision_id: str
    pool_id: str
    mandate_version: int
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None
    rationale: str
    created_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeIntent":
        try:
            intent = cls(
                schema_version=data["schema_version"],
                decision_id=_nonempty(data.get("decision_id"), "decision_id"),
                pool_id=_nonempty(data.get("pool_id"), "pool_id"),
                mandate_version=int(data["mandate_version"]),
                symbol=_nonempty(data.get("symbol"), "symbol").upper(),
                side=data["side"],
                quantity=int(data["quantity"]),
                order_type=data["order_type"],
                limit_price=float(data["limit_price"]) if data.get("limit_price") is not None else None,
                rationale=_nonempty(data.get("rationale"), "rationale"),
                created_at=_nonempty(data.get("created_at"), "created_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"invalid trade intent: {exc}") from exc
        intent.validate()
        return intent

    def validate(self) -> None:
        if self.schema_version != "hedge.trade-intent.v1":
            raise ContractError("unsupported schema_version")
        for value, field in (
            (self.decision_id, "decision_id"),
            (self.pool_id, "pool_id"),
            (self.symbol, "symbol"),
            (self.rationale, "rationale"),
            (self.created_at, "created_at"),
        ):
            _required_string(value, field)
        if self.side not in ("BUY", "SELL"):
            raise ContractError("side must be BUY or SELL")
        if self.order_type not in ("MKT", "LMT"):
            raise ContractError("order_type must be MKT or LMT")
        if not self.symbol.isascii() or not self.symbol.replace(".", "").isalnum():
            raise ContractError("symbol must be an ASCII equity ticker")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ContractError("quantity must be positive")
        if isinstance(self.mandate_version, bool) or not isinstance(self.mandate_version, int) or self.mandate_version < 1:
            raise ContractError("mandate_version must be at least 1")
        if self.limit_price is not None:
            try:
                finite_limit_price = math.isfinite(self.limit_price)
            except (TypeError, ValueError) as error:
                raise ContractError("limit_price must be finite") from error
            if not finite_limit_price:
                raise ContractError("limit_price must be finite")
        if self.order_type == "LMT" and (self.limit_price is None or self.limit_price <= 0):
            raise ContractError("limit orders require a positive limit_price")
        if self.order_type == "MKT" and self.limit_price is not None:
            raise ContractError("market orders cannot set limit_price")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """The only output Brainbase agents may hand to the executor."""

    schema_version: Literal["hedge.decision.v1"]
    decision_id: str
    pool_id: str
    mandate_version: int
    intents: tuple[TradeIntent, ...]
    created_at: str

    @classmethod
    def draft(cls, *, pool_id: str, mandate_version: int, intents: list[dict[str, Any]]) -> "Decision":
        decision_id = f"dec_{uuid4().hex}"
        now = datetime.now(UTC).isoformat()
        normalized = []
        for raw in intents:
            candidate = dict(raw)
            candidate.setdefault("schema_version", "hedge.trade-intent.v1")
            candidate["decision_id"] = decision_id
            candidate["pool_id"] = pool_id
            candidate["mandate_version"] = mandate_version
            candidate.setdefault("created_at", now)
            normalized.append(TradeIntent.from_dict(candidate))
        if not normalized:
            raise ContractError("a decision needs at least one intent")
        decision = cls("hedge.decision.v1", decision_id, pool_id, mandate_version, tuple(normalized), now)
        decision.validate()
        return decision

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        try:
            if data.get("schema_version") != "hedge.decision.v1":
                raise ContractError("unsupported decision schema_version")
            decision = cls(
                "hedge.decision.v1",
                _nonempty(data.get("decision_id"), "decision_id"),
                _nonempty(data.get("pool_id"), "pool_id"),
                int(data["mandate_version"]),
                tuple(TradeIntent.from_dict(item) for item in data.get("intents", [])),
                _nonempty(data.get("created_at"), "created_at"),
            )
        except ContractError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"invalid decision: {exc}") from exc
        decision.validate()
        return decision

    def validate(self) -> None:
        """Validate direct construction as strictly as parsed decision documents."""

        if self.schema_version != "hedge.decision.v1":
            raise ContractError("unsupported decision schema_version")
        for value, field in (
            (self.decision_id, "decision_id"),
            (self.pool_id, "pool_id"),
            (self.created_at, "created_at"),
        ):
            _required_string(value, field)
        if isinstance(self.mandate_version, bool) or not isinstance(self.mandate_version, int) or self.mandate_version < 1:
            raise ContractError("mandate_version must be at least 1")
        if not isinstance(self.intents, tuple) or not self.intents:
            raise ContractError("a decision needs at least one intent")
        for intent in self.intents:
            if not isinstance(intent, TradeIntent):
                raise ContractError("decision intents must be TradeIntent values")
            intent.validate()
            if (intent.decision_id, intent.pool_id, intent.mandate_version) != (
                self.decision_id,
                self.pool_id,
                self.mandate_version,
            ):
                raise ContractError("every intent must match the decision identity and mandate version")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "pool_id": self.pool_id,
            "mandate_version": self.mandate_version,
            "intents": [intent.as_dict() for intent in self.intents],
            "created_at": self.created_at,
        }
