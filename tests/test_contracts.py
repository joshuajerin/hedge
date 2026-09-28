"""Regression tests for Hedge decision-contract validation."""

from __future__ import annotations

import unittest

from hedge.contracts import ContractError, Decision, TradeIntent


def limit_intent_payload(limit_price: float) -> dict[str, object]:
    return {
        "schema_version": "hedge.trade-intent.v1",
        "decision_id": "dec-limit",
        "pool_id": "pool-1",
        "mandate_version": 1,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "order_type": "LMT",
        "limit_price": limit_price,
        "rationale": "finite price regression",
        "created_at": "2025-01-01T00:00:00+00:00",
    }


class ContractValidationTests(unittest.TestCase):
    def test_non_finite_limit_prices_are_rejected_by_intent_and_decision_boundaries(self) -> None:
        for limit_price in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(limit_price=limit_price):
                with self.assertRaisesRegex(ContractError, "limit_price must be finite"):
                    TradeIntent.from_dict(limit_intent_payload(limit_price))

                payload = {
                    "schema_version": "hedge.decision.v1",
                    "decision_id": "dec-limit",
                    "pool_id": "pool-1",
                    "mandate_version": 1,
                    "intents": [limit_intent_payload(limit_price)],
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
                with self.assertRaisesRegex(ContractError, "limit_price must be finite"):
                    Decision.from_dict(payload)

                direct_intent = TradeIntent(**limit_intent_payload(limit_price))
                direct_decision = Decision(
                    "hedge.decision.v1",
                    "dec-limit",
                    "pool-1",
                    1,
                    (direct_intent,),
                    "2025-01-01T00:00:00+00:00",
                )
                with self.assertRaisesRegex(ContractError, "limit_price must be finite"):
                    direct_decision.validate()


if __name__ == "__main__":
    unittest.main()
