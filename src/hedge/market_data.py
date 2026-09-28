"""Read-only Yahoo Finance market-data adapter for Hedge research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class YahooQuote:
    """A timestamped last close, with a public Yahoo Finance source URL."""

    symbol: str
    close: float
    as_of: datetime
    source_url: str


def latest_close(symbol: str) -> YahooQuote:
    """Return Yahoo Finance's latest daily close for one US equity symbol."""

    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("A non-empty ticker symbol is required.")
    try:
        import yfinance as yf
    except ImportError as error:  # pragma: no cover - depends on optional extra
        raise RuntimeError("Install Yahoo Finance support: uv sync --extra yahoo") from error

    history = yf.Ticker(normalized).history(period="5d", interval="1d", auto_adjust=False)
    if history.empty or "Close" not in history:
        raise LookupError(f"Yahoo Finance returned no recent daily close for {normalized}.")
    row = history.dropna(subset=["Close"]).iloc[-1]
    timestamp = row.name.to_pydatetime()
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    return YahooQuote(
        symbol=normalized,
        close=float(row["Close"]),
        as_of=timestamp,
        source_url=f"https://finance.yahoo.com/quote/{normalized}",
    )
