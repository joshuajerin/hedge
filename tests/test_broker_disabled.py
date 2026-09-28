from __future__ import annotations

import unittest
from datetime import UTC, datetime

from hedge.broker import IbkrConfig, IbkrPaperBroker
from hedge.contracts import ContractError, TradeIntent


class BrokerDisabledTests(unittest.TestCase):
    def test_direct_ibkr_submission_is_unconditionally_disabled(self) -> None:
        intent = TradeIntent(
            schema_version="hedge.trade-intent.v1",
            decision_id="dec-test",
            pool_id="pool-test",
            mandate_version=1,
            symbol="AAPL",
            side="BUY",
            quantity=1,
            order_type="MKT",
            limit_price=None,
            rationale="regression test",
            created_at=datetime.now(UTC).isoformat(),
        )
        # Even a caller-supplied live-looking endpoint cannot reach IBKR.
        broker = IbkrPaperBroker(IbkrConfig(host="127.0.0.1", port=7496))
        with self.assertRaisesRegex(ContractError, "Direct IBKR submission is disabled"):
            broker.submit(intent)


if __name__ == "__main__":
    unittest.main()
