from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from hedge.inference import InferenceService, InferenceStatus, validate_cio_response
from hedge.realtime import IngestStatus, LatestQuoteBook, MarketEvent, MarketEventStream


NOW = datetime(2025, 1, 2, 15, 30, tzinfo=UTC)


def event(*, event_id: str = "evt-1", symbol: str = "AAPL", quoted_at: datetime = NOW) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        symbol=symbol,
        price=200.0,
        quoted_at=quoted_at,
        received_at=NOW,
        source="test-feed",
    )


def approved_response(market_event: MarketEvent) -> dict[str, object]:
    intent = {
        "schema_version": "hedge.trade-intent.v1",
        "decision_id": "dec-1",
        "pool_id": "pool-1",
        "mandate_version": 1,
        "symbol": market_event.symbol,
        "side": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": 199.0,
        "rationale": "Paper test only",
        "created_at": NOW.isoformat(),
    }
    return {
        "schema_version": "hedge.cio-inference-response.v1",
        "outcome": "PAPER_PROPOSAL",
        "reason": "Risk Reviewer approved this paper proposal.",
        "risk_reviewer_approved": True,
        "decision": {
            "schema_version": "hedge.decision.v1",
            "decision_id": "dec-1",
            "pool_id": "pool-1",
            "mandate_version": 1,
            "intents": [intent],
            "created_at": NOW.isoformat(),
        },
    }


class RealtimeIngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_rejects_stale_and_full_queue_without_overwriting_quote(self) -> None:
        stream = MarketEventStream(
            queue_capacity=1,
            quote_book=LatestQuoteBook(max_quote_age=timedelta(seconds=5)),
        )
        stale = event(event_id="stale", quoted_at=NOW - timedelta(seconds=6))
        self.assertEqual((await stream.submit(stale, now=NOW)).status, IngestStatus.STALE_QUOTE)

        first = event(event_id="first")
        self.assertTrue((await stream.submit(first, now=NOW)).accepted)
        full = event(event_id="full", quoted_at=NOW + timedelta(seconds=1))
        self.assertEqual((await stream.submit(full, now=NOW)).status, IngestStatus.QUEUE_FULL)
        self.assertEqual(stream.quote_book.get_fresh("AAPL", now=NOW).event_id, "first")

    async def test_quote_book_rejects_same_or_older_timestamp(self) -> None:
        book = LatestQuoteBook(max_quote_age=timedelta(seconds=5))
        self.assertTrue(book.accept(event(event_id="new"), now=NOW).accepted)
        self.assertEqual(book.accept(event(event_id="same"), now=NOW).status, IngestStatus.OUT_OF_ORDER)
        self.assertEqual(
            book.accept(event(event_id="old", quoted_at=NOW - timedelta(seconds=1)), now=NOW).status,
            IngestStatus.OUT_OF_ORDER,
        )


class InferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_response_returns_broker_disabled_paper_proposal(self) -> None:
        market_event = event()

        async def router(_request):
            return approved_response(market_event)

        service = InferenceService(router, wall_clock=lambda: NOW)
        outcome = await service.infer(market_event)
        self.assertEqual(outcome.status, InferenceStatus.PAPER_PROPOSAL)
        self.assertTrue(outcome.accepted)
        self.assertTrue(outcome.broker_submission_disabled)
        self.assertEqual(outcome.decision.intents[0].symbol, "AAPL")

    async def test_stale_quote_fails_closed_without_calling_router(self) -> None:
        called = False

        async def router(_request):
            nonlocal called
            called = True
            return approved_response(event())

        service = InferenceService(router, max_quote_age=timedelta(seconds=5), wall_clock=lambda: NOW)
        outcome = await service.infer(event(quoted_at=NOW - timedelta(seconds=6)))
        self.assertEqual(outcome.status, InferenceStatus.HOLD_STALE_QUOTE)
        self.assertFalse(called)
        self.assertIsNone(outcome.decision)

    async def test_invalid_extra_decision_field_fails_closed(self) -> None:
        market_event = event()
        response = approved_response(market_event)
        response["decision"]["unexpected"] = "not allowed"  # type: ignore[index]

        async def router(_request):
            return response

        outcome = await InferenceService(router, wall_clock=lambda: NOW).infer(market_event)
        self.assertEqual(outcome.status, InferenceStatus.HOLD_INVALID_RESPONSE)
        self.assertIsNone(outcome.decision)

    async def test_mismatched_symbol_fails_closed(self) -> None:
        market_event = event()
        response = approved_response(market_event)
        response["decision"]["intents"][0]["symbol"] = "MSFT"  # type: ignore[index]

        async def router(_request):
            return response

        outcome = await InferenceService(router, wall_clock=lambda: NOW).infer(market_event)
        self.assertEqual(outcome.status, InferenceStatus.HOLD_INVALID_RESPONSE)

    async def test_concurrency_contention_has_one_deadline_and_holds_closed(self) -> None:
        gate = asyncio.Event()
        started = asyncio.Event()

        async def router(_request):
            started.set()
            await gate.wait()
            return approved_response(event())

        service = InferenceService(router, max_concurrency=1, timeout=timedelta(milliseconds=100), wall_clock=lambda: NOW)
        first = asyncio.create_task(service.infer(event(event_id="first")))
        await started.wait()
        second = await service.infer(event(event_id="second"))
        # Under scheduler contention the first call can release the slot just
        # before the second deadline. Both safe terminal outcomes are valid:
        # timeout while waiting, or timeout after acquiring the freed slot.
        self.assertIn(second.status, {InferenceStatus.HOLD_OVERLOADED, InferenceStatus.HOLD_TIMEOUT})
        self.assertIsNone(second.decision)
        self.assertEqual((await first).status, InferenceStatus.HOLD_TIMEOUT)

    async def test_queued_event_that_becomes_stale_does_not_route_to_cio(self) -> None:
        gate = asyncio.Event()
        first_started = asyncio.Event()
        second_checked_freshness = asyncio.Event()
        requests = []
        clock_now = NOW
        second_task = None

        def wall_clock() -> datetime:
            if asyncio.current_task() is second_task:
                second_checked_freshness.set()
            return clock_now

        async def router(request):
            requests.append(request)
            if request.event.event_id == "first":
                first_started.set()
                await gate.wait()
            return approved_response(request.event)

        service = InferenceService(
            router,
            max_concurrency=1,
            timeout=timedelta(seconds=60),
            max_quote_age=timedelta(seconds=5),
            wall_clock=wall_clock,
        )
        first = asyncio.create_task(service.infer(event(event_id="first")))
        await first_started.wait()
        second_task = asyncio.create_task(service.infer(event(event_id="second")))
        await second_checked_freshness.wait()
        clock_now = NOW + timedelta(seconds=6)
        gate.set()

        self.assertEqual((await first).status, InferenceStatus.PAPER_PROPOSAL)
        second = await second_task
        self.assertEqual(second.status, InferenceStatus.HOLD_STALE_QUOTE)
        self.assertEqual([request.event.event_id for request in requests], ["first"])
        self.assertEqual(requests[0].deadline_at, NOW + timedelta(seconds=5))

    def test_response_validator_rejects_hold_with_decision(self) -> None:
        response = approved_response(event())
        response["outcome"] = "HOLD"
        response["risk_reviewer_approved"] = False
        with self.assertRaises(ValueError):
            validate_cio_response(response, event=event())
