"""Paper-only, deadline-bounded orchestration through the deployed Hedge CIO.

``InferenceService`` does not select a model, call a broker, or submit an
order. It hands a deterministic request to an injected transport for the
existing Hedge CIO. It accepts only a strict, risk-reviewed paper proposal and
otherwise returns a non-executable HOLD outcome.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .contracts import ContractError, Decision
from .policy import PaperPolicy
from .realtime import MarketEvent, MarketEventError


HEDGE_CIO_AGENT_ID = "4ac44fd1-b386-49aa-8b39-a3ba7037fdc8"
INFERENCE_REQUEST_SCHEMA = "hedge.cio-inference-request.v1"
INFERENCE_RESPONSE_SCHEMA = "hedge.cio-inference-response.v1"
OUTCOME_SCHEMA = "hedge.inference-outcome.v1"


class InferenceStatus(StrEnum):
    PAPER_PROPOSAL = "PAPER_PROPOSAL"
    HOLD = "HOLD"
    HOLD_STALE_QUOTE = "HOLD_STALE_QUOTE"
    HOLD_OVERLOADED = "HOLD_OVERLOADED"
    HOLD_TIMEOUT = "HOLD_TIMEOUT"
    HOLD_INVALID_RESPONSE = "HOLD_INVALID_RESPONSE"
    HOLD_ROUTER_ERROR = "HOLD_ROUTER_ERROR"


class InferenceContractError(ValueError):
    """The CIO transport result is not a safe inference response."""


@dataclass(frozen=True)
class CioInferenceRequest:
    """Exact prompt payload for the existing deployed Hedge CIO router."""

    event: MarketEvent
    deadline_at: datetime
    account_mode: str = "paper"
    agent_id: str = HEDGE_CIO_AGENT_ID
    schema_version: str = INFERENCE_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != INFERENCE_REQUEST_SCHEMA:
            raise ValueError("unsupported inference request schema_version")
        if self.account_mode != "paper":
            raise ValueError("real-time inference is paper-only")
        if self.agent_id != HEDGE_CIO_AGENT_ID:
            raise ValueError("inference must route through the deployed Hedge CIO")
        if self.deadline_at.tzinfo is None:
            raise ValueError("deadline_at must include a timezone")
        object.__setattr__(self, "deadline_at", self.deadline_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "account_mode": self.account_mode,
            "deadline_at": self.deadline_at.isoformat(),
            "market_event": self.event.as_dict(),
            "instructions": (
                "Route this event through the existing Hedge CIO orchestration. "
                "Return only hedge.cio-inference-response.v1. This is PAPER-ONLY: "
                "never execute or request broker access. Return HOLD unless Risk "
                "Reviewer approved the exact hedge.decision.v1 proposal."
            ),
        }

    def as_json(self) -> str:
        """Canonical JSON for transport signing, logging, or replay."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class CioRouter(Protocol):
    """Transport boundary for the deployed Hedge CIO, not a new model API."""

    def __call__(self, request: CioInferenceRequest) -> Awaitable[Mapping[str, object]]: ...


@dataclass(frozen=True)
class InferenceOutcome:
    """A terminal, non-executable result. Only PAPER_PROPOSAL has a decision."""

    event_id: str
    status: InferenceStatus
    reason: str
    decision: Decision | None = None
    schema_version: str = OUTCOME_SCHEMA
    account_mode: str = "paper"
    broker_submission_disabled: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != OUTCOME_SCHEMA or self.account_mode != "paper" or not self.broker_submission_disabled:
            raise ValueError("inference outcomes must remain paper-only and broker-disabled")
        if self.status is InferenceStatus.PAPER_PROPOSAL:
            if self.decision is None:
                raise ValueError("PAPER_PROPOSAL requires a decision")
        elif self.decision is not None:
            raise ValueError("HOLD outcomes must not include a decision")

    @property
    def accepted(self) -> bool:
        return self.status is InferenceStatus.PAPER_PROPOSAL

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "status": self.status.value,
            "reason": self.reason,
            "account_mode": self.account_mode,
            "broker_submission_disabled": self.broker_submission_disabled,
            "decision": self.decision.as_dict() if self.decision else None,
        }


def _strict_fields(data: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(data)
    if actual != expected:
        raise InferenceContractError(f"{label} fields must match schema (missing={sorted(expected - actual)}, extra={sorted(actual - expected)})")


def _is_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_decision_payload(payload: object, *, event: MarketEvent) -> Decision:
    """Reject coercion, extra keys, invalid numbers, and unrelated symbols."""
    if not isinstance(payload, Mapping):
        raise InferenceContractError("decision must be an object")
    decision_fields = {"schema_version", "decision_id", "pool_id", "mandate_version", "intents", "created_at"}
    intent_fields = {"schema_version", "decision_id", "pool_id", "mandate_version", "symbol", "side", "quantity", "order_type", "limit_price", "rationale", "created_at"}
    _strict_fields(payload, decision_fields, "decision")
    if payload.get("schema_version") != "hedge.decision.v1":
        raise InferenceContractError("decision schema_version must be hedge.decision.v1")
    for field in ("decision_id", "pool_id", "created_at"):
        if not _is_str(payload.get(field)):
            raise InferenceContractError(f"decision {field} must be a non-empty string")
    if isinstance(payload.get("mandate_version"), bool) or not isinstance(payload.get("mandate_version"), int):
        raise InferenceContractError("decision mandate_version must be an integer")
    intents = payload.get("intents")
    if not isinstance(intents, list) or not intents:
        raise InferenceContractError("decision intents must be a non-empty array")
    for index, intent in enumerate(intents):
        if not isinstance(intent, Mapping):
            raise InferenceContractError(f"intent {index} must be an object")
        _strict_fields(intent, intent_fields, f"intent {index}")
        for field in ("schema_version", "decision_id", "pool_id", "symbol", "side", "order_type", "rationale", "created_at"):
            if not _is_str(intent.get(field)):
                raise InferenceContractError(f"intent {index} {field} must be a non-empty string")
        if isinstance(intent.get("mandate_version"), bool) or not isinstance(intent.get("mandate_version"), int):
            raise InferenceContractError(f"intent {index} mandate_version must be an integer")
        if isinstance(intent.get("quantity"), bool) or not isinstance(intent.get("quantity"), int):
            raise InferenceContractError(f"intent {index} quantity must be an integer")
        limit_price = intent.get("limit_price")
        if limit_price is not None and (isinstance(limit_price, bool) or not isinstance(limit_price, (int, float)) or not math.isfinite(limit_price)):
            raise InferenceContractError(f"intent {index} limit_price must be a finite number or null")
        if intent["symbol"] != event.symbol:
            raise InferenceContractError(f"intent {index} symbol must match market event symbol")
    try:
        decision = Decision.from_dict(dict(payload))
        PaperPolicy().validate(decision)
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise InferenceContractError(f"invalid decision: {exc}") from exc
    return decision


def validate_cio_response(payload: object, *, event: MarketEvent) -> Decision | None:
    """Validate the response envelope and return only a safe paper proposal."""
    if not isinstance(payload, Mapping):
        raise InferenceContractError("CIO response must be an object")
    expected = {"schema_version", "outcome", "reason", "risk_reviewer_approved", "decision"}
    _strict_fields(payload, expected, "CIO response")
    if payload.get("schema_version") != INFERENCE_RESPONSE_SCHEMA:
        raise InferenceContractError("unsupported CIO response schema_version")
    if not _is_str(payload.get("reason")):
        raise InferenceContractError("CIO response reason must be a non-empty string")
    approved = payload.get("risk_reviewer_approved")
    if not isinstance(approved, bool):
        raise InferenceContractError("risk_reviewer_approved must be a boolean")
    outcome = payload.get("outcome")
    if outcome == "HOLD":
        if approved or payload.get("decision") is not None:
            raise InferenceContractError("HOLD must have no decision and no approval")
        return None
    if outcome != "PAPER_PROPOSAL":
        raise InferenceContractError("outcome must be HOLD or PAPER_PROPOSAL")
    if not approved:
        raise InferenceContractError("PAPER_PROPOSAL requires Risk Reviewer approval")
    return _validate_decision_payload(payload.get("decision"), event=event)


class InferenceService:
    """Run bounded, deadline-aware CIO calls and fail closed on every error."""

    def __init__(self, router: CioRouter, *, max_concurrency: int = 4, timeout: timedelta = timedelta(seconds=10), max_quote_age: timedelta = timedelta(seconds=30), wall_clock: Callable[[], datetime] | None = None, monotonic_clock: Callable[[], float] = time.monotonic) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if timeout <= timedelta(0) or max_quote_age <= timedelta(0):
            raise ValueError("timeout and max_quote_age must be positive")
        self._router = router
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.timeout = timeout
        self.max_quote_age = max_quote_age
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock

    def _outcome(self, event: MarketEvent, status: InferenceStatus, reason: str, decision: Decision | None = None) -> InferenceOutcome:
        return InferenceOutcome(event.event_id, status, reason, decision)

    async def infer(self, event: MarketEvent) -> InferenceOutcome:
        """Produce a validated paper proposal or an explicit HOLD.

        The single deadline covers semaphore contention and the CIO transport.
        Cancellation is propagated so service shutdown is prompt.
        """
        if not isinstance(event, MarketEvent):
            raise MarketEventError("infer requires a validated MarketEvent")
        now = self._wall_clock().astimezone(UTC)
        if event.is_stale(now=now, max_age=self.max_quote_age):
            return self._outcome(event, InferenceStatus.HOLD_STALE_QUOTE, "quote is older than max_quote_age")
        started = self._monotonic_clock()
        timeout_seconds = self.timeout.total_seconds()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout_seconds)
        except TimeoutError:
            return self._outcome(event, InferenceStatus.HOLD_OVERLOADED, "inference concurrency limit reached before deadline")
        try:
            now = self._wall_clock().astimezone(UTC)
            if event.is_stale(now=now, max_age=self.max_quote_age):
                return self._outcome(event, InferenceStatus.HOLD_STALE_QUOTE, "quote is older than max_quote_age")
            elapsed = self._monotonic_clock() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                # We waited for the entire shared deadline.  A semaphore
                # release at that boundary must remain an overload outcome,
                # not become a misleading router timeout.
                return self._outcome(event, InferenceStatus.HOLD_OVERLOADED, "inference concurrency limit reached before deadline")
            quote_expires_at = event.quoted_at + self.max_quote_age
            deadline_at = min(now + timedelta(seconds=remaining), quote_expires_at)
            router_timeout = min(remaining, max(0.0, (deadline_at - now).total_seconds()))
            if router_timeout <= 0:
                return self._outcome(event, InferenceStatus.HOLD_TIMEOUT, "quote expires before CIO routing can start")
            request = CioInferenceRequest(event=event, deadline_at=deadline_at)
            try:
                result = self._router(request)
                if not inspect.isawaitable(result):
                    return self._outcome(event, InferenceStatus.HOLD_ROUTER_ERROR, "CIO router must return an awaitable")
                response = await asyncio.wait_for(result, timeout=router_timeout)
            except asyncio.TimeoutError:
                return self._outcome(event, InferenceStatus.HOLD_TIMEOUT, "CIO routing exceeded deadline")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._outcome(event, InferenceStatus.HOLD_ROUTER_ERROR, f"CIO router failed: {type(exc).__name__}")
            try:
                decision = validate_cio_response(response, event=event)
            except (InferenceContractError, ContractError, ValueError, TypeError) as exc:
                return self._outcome(event, InferenceStatus.HOLD_INVALID_RESPONSE, str(exc))
            if decision is None:
                return self._outcome(event, InferenceStatus.HOLD, "CIO returned HOLD")
            return self._outcome(event, InferenceStatus.PAPER_PROPOSAL, "risk-reviewed paper proposal", decision)
        finally:
            self._semaphore.release()
