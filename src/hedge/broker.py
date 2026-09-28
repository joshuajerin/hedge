"""Compatibility boundary that rejects all direct IBKR submission.

Hedge is paper-simulation-only.  It intentionally has no IBKR socket path.
"""

from __future__ import annotations

from dataclasses import dataclass
from .contracts import ContractError, TradeIntent


@dataclass(frozen=True)
class IbkrConfig:
    host: str = "127.0.0.1"
    port: int = 7497  # TWS paper default; IB Gateway paper commonly uses 4002.
    client_id: int = 71
    account: str | None = None


class IbkrPaperBroker:
    """Deprecated compatibility type; every submission attempt is rejected."""

    def __init__(self, config: IbkrConfig) -> None:
        self.config = config

    def submit(self, intent: TradeIntent) -> str:
        """Reject all direct broker submission in this paper-only release.

        The former IBKR socket implementation is intentionally absent: a
        configurable endpoint cannot reliably prove that it is a paper account.
        Callers must use the local paper simulation runtime instead.
        """
        del intent
        raise ContractError(
            "Direct IBKR submission is disabled in Hedge. "
            "This release supports paper simulation only."
        )

    def assert_sell_is_covered(self, intent: TradeIntent) -> None:
        if intent.side == "SELL":
            raise ContractError("sell orders require a position check adapter before submission")
