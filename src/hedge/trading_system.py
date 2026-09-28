"""Paper-only decision runtime with an explicit auditable state machine.

The runtime deliberately has no broker adapter, socket, HTTP client, or live
execution hook. It accepts a reviewed :class:`~hedge.contracts.Decision`, a
caller-owned deterministic price snapshot, and updates only in-memory paper
state after all local controls approve it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping

from .contracts import ContractError, Decision
from .policy import PaperPolicy
from .risk_controls import PaperAccount, RiskApproval, RiskGate, RiskLimits, RiskViolation


class TradingState(str, Enum):
    """All possible decision states; ``COMPLETED``, ``REJECTED`` and ``HALTED`` are terminal."""

    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    RISK_APPROVED = "RISK_APPROVED"
    SIMULATED = "SIMULATED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    HALTED = "HALTED"


TERMINAL_STATES = frozenset({TradingState.COMPLETED, TradingState.REJECTED, TradingState.HALTED})
_TRANSITIONS = {
    TradingState.PROPOSED: frozenset({TradingState.VALIDATED, TradingState.REJECTED, TradingState.HALTED}),
    TradingState.VALIDATED: frozenset({TradingState.RISK_APPROVED, TradingState.REJECTED, TradingState.HALTED}),
    TradingState.RISK_APPROVED: frozenset({TradingState.SIMULATED, TradingState.REJECTED, TradingState.HALTED}),
    TradingState.SIMULATED: frozenset({TradingState.COMPLETED, TradingState.REJECTED}),
}


class LiveExecutionProhibited(RuntimeError):
    """Raised for every attempted live-execution path."""


class IdempotencyConflict(ContractError):
    """A decision ID was reused with different immutable decision contents."""


@dataclass(frozen=True)
class LedgerEvent:
    """An append-only transition record suitable for persistence by an integration."""

    sequence: int
    at: datetime
    decision_id: str | None
    previous_state: TradingState | None
    state: TradingState
    action: str
    reason: str | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp = self.at if self.at.tzinfo else self.at.replace(tzinfo=UTC)
        object.__setattr__(self, "at", timestamp.astimezone(UTC))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "at": self.at.isoformat(),
            "decision_id": self.decision_id,
            "previous_state": self.previous_state.value if self.previous_state else None,
            "state": self.state.value,
            "action": self.action,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class PaperFill:
    """The deterministic result of one locally simulated intent."""

    order_index: int
    symbol: str
    side: str
    quantity: int
    status: str
    price: Decimal | None
    reference: str | None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "order_index": self.order_index,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "status": self.status,
            "price": str(self.price) if self.price is not None else None,
            "reference": self.reference,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProcessingResult:
    """Immutable terminal result. A duplicate returns this result with ``idempotent=True``."""

    decision_id: str
    state: TradingState
    account: PaperAccount
    fills: tuple[PaperFill, ...]
    events: tuple[LedgerEvent, ...]
    reason: str | None = None
    idempotent: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "state": self.state.value,
            "account": self.account.as_dict(),
            "fills": [fill.as_dict() for fill in self.fills],
            "events": [event.as_dict() for event in self.events],
            "reason": self.reason,
            "idempotent": self.idempotent,
        }


class PaperTradingSystem:
    """A synchronous, all-or-nothing, paper simulation runtime.

    The caller owns price collection and persistence. ``process`` does not
    perform I/O and cannot submit an order to IBKR or any other venue.
    """

    account_mode = "paper"

    def __init__(
        self,
        *,
        initial_cash: Decimal | int | float | str,
        initial_positions: Mapping[str, int] | None = None,
        policy: PaperPolicy | None = None,
        limits: RiskLimits | None = None,
        mode: str = "paper",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if mode != "paper":
            raise LiveExecutionProhibited("Hedge trading runtime is paper-only; live execution is prohibited")
        self._policy = policy or PaperPolicy()
        # Do not trust a replacement policy to turn this runtime live.
        if self._policy.account_mode != "paper":
            raise LiveExecutionProhibited("Hedge trading runtime is paper-only; live execution is prohibited")
        self._gate = RiskGate(limits)
        self._account = PaperAccount(initial_cash, initial_positions or {})
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._kill_switch_reason: str | None = None
        self._events: list[LedgerEvent] = []
        self._results: dict[str, ProcessingResult] = {}
        self._fingerprints: dict[str, str] = {}
        self._append(None, None, TradingState.PROPOSED, "RUNTIME_STARTED", details={"account_mode": "paper"})

    @property
    def account(self) -> PaperAccount:
        """Return the current immutable paper account snapshot."""
        with self._lock:
            return self._account

    @property
    def kill_switch_active(self) -> bool:
        with self._lock:
            return self._kill_switch_reason is not None

    def activate_kill_switch(self, reason: str) -> None:
        """Stop future decisions. It never alters completed paper fills."""
        cleaned = str(reason).strip()
        if not cleaned:
            raise ValueError("kill switch reason is required")
        with self._lock:
            if self._kill_switch_reason is None:
                self._kill_switch_reason = cleaned
                self._append(None, None, TradingState.HALTED, "KILL_SWITCH_ACTIVATED", cleaned)

    def deactivate_kill_switch(self, reason: str) -> None:
        """Record a deliberate operator reset; it does not replay rejected work."""
        cleaned = str(reason).strip()
        if not cleaned:
            raise ValueError("kill switch reset reason is required")
        with self._lock:
            if self._kill_switch_reason is not None:
                old_reason = self._kill_switch_reason
                self._kill_switch_reason = None
                self._append(None, TradingState.HALTED, TradingState.PROPOSED, "KILL_SWITCH_DEACTIVATED", cleaned, {"previous_reason": old_reason})

    def ledger(self, decision_id: str | None = None) -> tuple[LedgerEvent, ...]:
        """Read the append-only ledger, optionally filtered to one decision."""
        with self._lock:
            if decision_id is None:
                return tuple(self._events)
            return tuple(event for event in self._events if event.decision_id == decision_id)

    def live_execute(self, *_args: object, **_kwargs: object) -> None:
        """Explicit non-escape hatch retained so integrations fail closed."""
        raise LiveExecutionProhibited("Hedge trading runtime is paper-only; live execution is prohibited")

    def process(self, decision: Decision, prices: Mapping[str, object]) -> ProcessingResult:
        """Run proposal -> validation -> risk gate -> simulation -> terminal result.

        A matching repeated ``decision_id`` returns the original terminal
        outcome and never changes cash, positions, or the ledger. Reusing that
        ID with different contents raises :class:`IdempotencyConflict`.
        """
        try:
            # Reconstructing from the stable boundary validates even a
            # manually constructed dataclass and keeps validation local.
            canonical = self._canonical_decision(decision)
            fingerprint = self._fingerprint(canonical)
        except Exception as exc:
            # Canonicalization is part of processing, not a caller-side
            # precondition. Record malformed hand-built Decision instances as
            # explicit terminal rejections before any policy or risk work.
            return self._reject_malformed(decision, exc)

        with self._lock:
            prior = self._results.get(canonical.decision_id)
            if prior is not None:
                if self._fingerprints[canonical.decision_id] != fingerprint:
                    raise IdempotencyConflict("decision_id already belongs to a different decision payload")
                return replace(prior, idempotent=True)

            events: list[LedgerEvent] = []
            current = TradingState.PROPOSED
            events.append(self._append(canonical.decision_id, None, current, "PROPOSAL_RECEIVED", details={"fingerprint": fingerprint}))
            if self._kill_switch_reason is not None:
                current = self._transition(events, canonical.decision_id, current, TradingState.HALTED, "KILL_SWITCH_BLOCKED", self._kill_switch_reason)
                return self._finish(canonical.decision_id, fingerprint, current, events, (), self._kill_switch_reason)

            try:
                self._policy.validate(canonical)
                current = self._transition(events, canonical.decision_id, current, TradingState.VALIDATED, "POLICY_VALIDATED")
                approval = self._gate.approve(canonical, self._account, prices, open_orders=0)
                current = self._transition(
                    events,
                    canonical.decision_id,
                    current,
                    TradingState.RISK_APPROVED,
                    "RISK_APPROVED",
                    details={"projected_gross_exposure": str(approval.gross_exposure)},
                )
                fills, updated_account = self._simulate(canonical, approval, self._account)
                current = self._transition(
                    events,
                    canonical.decision_id,
                    current,
                    TradingState.SIMULATED,
                    "PAPER_SIMULATED",
                    details={"filled_orders": str(sum(fill.status == "FILLED" for fill in fills)), "unfilled_orders": str(sum(fill.status == "NOT_FILLED" for fill in fills))},
                )
                self._account = updated_account
                current = self._transition(events, canonical.decision_id, current, TradingState.COMPLETED, "TERMINAL_COMPLETED")
                return self._finish(canonical.decision_id, fingerprint, current, events, fills, None)
            except (ContractError, RiskViolation, ValueError, TypeError) as exc:
                reason = str(exc) or exc.__class__.__name__
            except Exception as exc:  # Fail closed; no exception may cause execution.
                reason = f"internal safety failure: {exc.__class__.__name__}"
            current = self._transition(events, canonical.decision_id, current, TradingState.REJECTED, "TERMINAL_REJECTED", reason)
            return self._finish(canonical.decision_id, fingerprint, current, events, (), reason)

    def _reject_malformed(self, decision: object, error: Exception) -> ProcessingResult:
        """Record a malformed input as an idempotent terminal rejection."""
        decision_id = self._untrusted_decision_id(decision)
        fingerprint = self._malformed_fingerprint(decision)
        reason = f"invalid decision: {str(error) or error.__class__.__name__}"
        with self._lock:
            prior = self._results.get(decision_id)
            if prior is not None:
                if self._fingerprints[decision_id] != fingerprint:
                    raise IdempotencyConflict("decision_id already belongs to a different decision payload")
                return replace(prior, idempotent=True)
            events: list[LedgerEvent] = []
            current = TradingState.PROPOSED
            events.append(self._append(decision_id, None, current, "PROPOSAL_RECEIVED", details={"fingerprint": fingerprint}))
            current = self._transition(events, decision_id, current, TradingState.REJECTED, "TERMINAL_REJECTED", reason)
            return self._finish(decision_id, fingerprint, current, events, (), reason)

    def _finish(
        self,
        decision_id: str,
        fingerprint: str,
        state: TradingState,
        events: list[LedgerEvent],
        fills: tuple[PaperFill, ...],
        reason: str | None,
    ) -> ProcessingResult:
        result = ProcessingResult(decision_id, state, self._account, fills, tuple(events), reason)
        self._results[decision_id] = result
        self._fingerprints[decision_id] = fingerprint
        return result

    def _simulate(self, decision: Decision, approval: RiskApproval, account: PaperAccount) -> tuple[tuple[PaperFill, ...], PaperAccount]:
        cash = account.cash
        positions = dict(account.positions)
        fills: list[PaperFill] = []
        for index, planned in enumerate(approval.orders):
            intent = planned.intent
            if not planned.will_fill:
                fills.append(PaperFill(index, intent.symbol, intent.side, intent.quantity, "NOT_FILLED", None, None, "limit price not reached"))
                continue
            notional = planned.market_price * intent.quantity
            if intent.side == "BUY":
                # The gate reserved the less favourable limit price first.
                # This check is defensive and must remain true after approval.
                if cash < notional:
                    raise RiskViolation("insufficient cash during paper simulation")
                cash -= notional
                positions[intent.symbol] = positions.get(intent.symbol, 0) + intent.quantity
            else:
                held = positions.get(intent.symbol, 0)
                if held < intent.quantity:
                    raise RiskViolation(f"uncovered sell during paper simulation for {intent.symbol}")
                positions[intent.symbol] = held - intent.quantity
                cash += notional
            fills.append(PaperFill(index, intent.symbol, intent.side, intent.quantity, "FILLED", planned.market_price, f"paper:{decision.decision_id}:{index}"))
        return tuple(fills), PaperAccount(cash, positions)

    @staticmethod
    def _canonical_decision(decision: Decision) -> Decision:
        if not isinstance(decision, Decision):
            raise ContractError("process requires a hedge.decision.v1 Decision")
        return Decision.from_dict(decision.as_dict())

    @staticmethod
    def _untrusted_decision_id(decision: object) -> str:
        """Return a safe ledger key without trusting malformed input fields."""
        try:
            decision_id = getattr(decision, "decision_id", None)
        except Exception:
            decision_id = None
        if isinstance(decision_id, str) and decision_id.strip():
            return decision_id.strip()
        return "<invalid-decision-id>"

    @staticmethod
    def _malformed_fingerprint(decision: object) -> str:
        """Fingerprint raw malformed input without calling its contract methods."""
        try:
            payload = repr(decision)
        except Exception:
            payload = type(decision).__qualname__
        return "malformed:" + hashlib.sha256(payload.encode("utf-8", "backslashreplace")).hexdigest()

    @staticmethod
    def _fingerprint(decision: Decision) -> str:
        payload = json.dumps(decision.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _transition(
        self,
        events: list[LedgerEvent],
        decision_id: str,
        previous: TradingState,
        target: TradingState,
        action: str,
        reason: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> TradingState:
        if target not in _TRANSITIONS.get(previous, frozenset()):
            raise RuntimeError(f"illegal state transition: {previous.value} -> {target.value}")
        events.append(self._append(decision_id, previous, target, action, reason, details or {}))
        return target

    def _append(
        self,
        decision_id: str | None,
        previous_state: TradingState | None,
        state: TradingState,
        action: str,
        reason: str | None = None,
        details: Mapping[str, str] | None = None,
    ) -> LedgerEvent:
        event = LedgerEvent(len(self._events) + 1, self._clock(), decision_id, previous_state, state, action, reason, details or {})
        self._events.append(event)
        return event
