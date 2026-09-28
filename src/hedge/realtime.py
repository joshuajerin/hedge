"""Bounded, deterministic market-event intake for paper-only Hedge inference.

This module intentionally has no market-data client and no broker dependency. A
feed adapter turns its received data into :class:`MarketEvent` objects and hands
them to ``MarketEventStream``. The stream only accepts fresh, monotonically newer
quotes and uses a bounded queue so an upstream burst cannot consume unbounded
memory.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Mapping


MARKET_EVENT_SCHEMA = "hedge.market-event.v1"
_SYMBOL = re.compile(r"[A-Z0-9]+(?:\.[A-Z0-9]+)?")


class MarketEventError(ValueError):
    """A market event does not meet the local, deterministic wire contract."""


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketEventError(f"{field} must be an RFC 3339 timestamp")
    if value.tzinfo is None:
        raise MarketEventError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise MarketEventError(f"{field} must be a non-empty RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketEventError(f"{field} must be an RFC 3339 timestamp") from exc
    return _utc(parsed, field)


def _required_string(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MarketEventError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class MarketEvent:
    """A single timestamped quote from an already-authorized feed adapter."""

    event_id: str
    symbol: str
    price: float
    quoted_at: datetime
    received_at: datetime
    source: str
    schema_version: str = MARKET_EVENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != MARKET_EVENT_SCHEMA:
            raise MarketEventError("unsupported market event schema_version")
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise MarketEventError("event_id must be a non-empty string")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise MarketEventError("symbol must be an uppercase ASCII equity ticker")
        if isinstance(self.price, bool) or not isinstance(self.price, (int, float)) or not math.isfinite(self.price) or self.price <= 0:
            raise MarketEventError("price must be a finite positive number")
        if not isinstance(self.source, str) or not self.source.strip():
            raise MarketEventError("source must be a non-empty string")
        object.__setattr__(self, "quoted_at", _utc(self.quoted_at, "quoted_at"))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        object.__setattr__(self, "symbol", self.symbol.upper())
        object.__setattr__(self, "price", float(self.price))

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MarketEvent":
        expected = {"schema_version", "event_id", "symbol", "price", "quoted_at", "received_at", "source"}
        if not isinstance(data, Mapping) or set(data) != expected:
            missing = expected - set(data) if isinstance(data, Mapping) else expected
            extra = set(data) - expected if isinstance(data, Mapping) else set()
            raise MarketEventError(f"market event fields must match schema (missing={sorted(missing)}, extra={sorted(extra)})")
        price = data["price"]
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise MarketEventError("price must be a number")
        return cls(
            schema_version=_required_string(data, "schema_version"),
            event_id=_required_string(data, "event_id"),
            symbol=_required_string(data, "symbol").upper(),
            price=float(price),
            quoted_at=_parse_timestamp(data["quoted_at"], "quoted_at"),
            received_at=_parse_timestamp(data["received_at"], "received_at"),
            source=_required_string(data, "source"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "symbol": self.symbol,
            "price": self.price,
            "quoted_at": self.quoted_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "source": self.source,
        }

    def is_stale(self, *, now: datetime, max_age: timedelta) -> bool:
        return self.quoted_at < _utc(now, "now") - max_age


class IngestStatus(StrEnum):
    QUEUED = "QUEUED"
    STALE_QUOTE = "STALE_QUOTE"
    FUTURE_QUOTE = "FUTURE_QUOTE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    QUEUE_FULL = "QUEUE_FULL"


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    status: IngestStatus
    reason: str

    @property
    def accepted(self) -> bool:
        return self.status is IngestStatus.QUEUED


class LatestQuoteBook:
    """A capacity-bounded, per-symbol latest quote cache.

    A quote at the same timestamp as the cached quote is rejected. This avoids
    arbitrary arrival-order outcomes when two feeds disagree.
    """

    def __init__(self, *, capacity: int = 2_048, max_quote_age: timedelta = timedelta(seconds=30), max_future_skew: timedelta = timedelta(seconds=2)) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if max_quote_age <= timedelta(0) or max_future_skew < timedelta(0):
            raise ValueError("quote age and future skew must be valid durations")
        self._capacity = capacity
        self.max_quote_age = max_quote_age
        self.max_future_skew = max_future_skew
        self._quotes: OrderedDict[str, MarketEvent] = OrderedDict()

    def accept(self, event: MarketEvent, *, now: datetime | None = None) -> IngestResult:
        now = _utc(now or datetime.now(UTC), "now")
        if event.is_stale(now=now, max_age=self.max_quote_age):
            return IngestResult(event.event_id, IngestStatus.STALE_QUOTE, "quote is older than max_quote_age")
        if event.quoted_at > now + self.max_future_skew:
            return IngestResult(event.event_id, IngestStatus.FUTURE_QUOTE, "quote timestamp exceeds max_future_skew")
        previous = self._quotes.get(event.symbol)
        if previous is not None and event.quoted_at <= previous.quoted_at:
            return IngestResult(event.event_id, IngestStatus.OUT_OF_ORDER, "quote is not newer than the cached quote")
        self._quotes[event.symbol] = event
        self._quotes.move_to_end(event.symbol)
        if len(self._quotes) > self._capacity:
            self._quotes.popitem(last=False)
        return IngestResult(event.event_id, IngestStatus.QUEUED, "fresh quote accepted")

    def get_fresh(self, symbol: str, *, now: datetime | None = None) -> MarketEvent | None:
        event = self._quotes.get(symbol.strip().upper())
        if event is None:
            return None
        now = _utc(now or datetime.now(UTC), "now")
        if event.is_stale(now=now, max_age=self.max_quote_age):
            return None
        return event


class MarketEventStream:
    """Async handoff with bounded memory; it never opens a network connection."""

    def __init__(self, *, queue_capacity: int = 1_024, quote_book: LatestQuoteBook | None = None) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.quote_book = quote_book or LatestQuoteBook()
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=queue_capacity)

    async def submit(self, event: MarketEvent | Mapping[str, object], *, now: datetime | None = None) -> IngestResult:
        """Validate and enqueue without waiting; overload is an explicit reject."""
        normalized = MarketEvent.from_dict(event) if isinstance(event, Mapping) else event
        if not isinstance(normalized, MarketEvent):
            raise MarketEventError("event must be a MarketEvent or mapping")
        if self._queue.full():
            return IngestResult(normalized.event_id, IngestStatus.QUEUE_FULL, "event queue is full")
        result = self.quote_book.accept(normalized, now=now)
        if not result.accepted:
            return result
        self._queue.put_nowait(normalized)
        return result

    async def next_event(self) -> MarketEvent:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def queued(self) -> int:
        return self._queue.qsize()
