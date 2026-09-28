"""Strict, paper-only import of Yahoo daily OHLCV into Hedge's backtest sandbox.

No network request occurs unless ``load_history`` is called without a fetcher
and cache_only=False. A fetcher receives (symbol, start, end) dates and yields
mapping rows with Date, Open, High, Low, Close, Volume fields. End is exclusive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .backtest import (
    BacktestBar,
    BacktestConfig,
    BacktestResult,
    PaperBacktester,
    ScheduledDecision,
)
from .policy import PaperPolicy


HistoryFetcher = Callable[[str, date, date], Iterable[Mapping[str, object]]]
_SCHEMA = "hedge.yahoo-history.v1"


def _aware(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _day(value: date, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date, not a datetime")
    return value


def _symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    if isinstance(symbols, str):
        symbols = (symbols,)
    items = tuple(str(symbol).strip().upper() for symbol in symbols)
    if not items or len(set(items)) != len(items):
        raise ValueError("symbols must be non-empty and unique")
    for symbol in items:
        # Use BacktestBar's symbol contract, without a second, weaker parser.
        BacktestBar(symbol, "2000-01-01", 1, 1, 1, 1)
    return tuple(sorted(items))


def _row_date(value: object, timezone: ZoneInfo) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "naive Date datetime is ambiguous; use an aware datetime or a date"
            )
        return value.astimezone(timezone).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Date must be an ISO date") from error
    raise ValueError("Date must be a date or timezone-aware datetime")


def _default_fetcher(
    symbol: str, start: date, end: date
) -> Iterable[Mapping[str, object]]:
    try:
        import yfinance as yf
    except ImportError as error:
        raise RuntimeError(
            "Install Yahoo support with uv sync --extra yahoo"
        ) from error
    frame = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=False,
        repair=False,
        raise_errors=True,
    )
    if frame.empty:
        return ()
    # Avoid importing pandas in this module; yfinance is an optional extra.
    return (
        {
            "Date": index.to_pydatetime() if hasattr(index, "to_pydatetime") else index,
            "Open": row["Open"],
            "High": row["High"],
            "Low": row["Low"],
            "Close": row["Close"],
            "Volume": row["Volume"],
        }
        for index, row in frame.iterrows()
    )


def _serialize(bar: BacktestBar) -> dict[str, str]:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _validate_bars(
    bars: Iterable[BacktestBar],
    symbols: tuple[str, ...],
    start: date,
    end: date,
    timezone: ZoneInfo,
    expected_sessions: Iterable[date] | None,
) -> tuple[BacktestBar, ...]:
    result = tuple(sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol)))
    seen: set[tuple[str, date]] = set()
    actual: dict[str, set[date]] = {symbol: set() for symbol in symbols}
    for bar in result:
        session = bar.timestamp.astimezone(timezone).date()
        if bar.symbol not in actual or not start <= session < end:
            raise ValueError(
                f"bar outside requested symbols/range: {bar.symbol} {session}"
            )
        identity = (bar.symbol, session)
        if identity in seen:
            raise ValueError(f"duplicate daily bar: {bar.symbol} {session}")
        seen.add(identity)
        actual[bar.symbol].add(session)
        if bar.volume is None:
            raise ValueError(f"missing volume: {bar.symbol} {session}")
    if any(not dates for dates in actual.values()):
        raise ValueError("missing history for one or more requested symbols")
    union = set().union(*actual.values())
    if any(dates != union for dates in actual.values()):
        raise ValueError(
            "missing sessions in one or more symbols; provide complete aligned history"
        )
    if expected_sessions is not None:
        expected = set()
        for session in expected_sessions:
            day = _day(session, "expected_sessions entry")
            if not start <= day < end:
                raise ValueError("expected session outside requested range")
            expected.add(day)
        if not expected:
            raise ValueError("expected_sessions cannot be empty")
        for symbol, dates in actual.items():
            if dates != expected:
                raise ValueError(
                    f"missing or unexpected sessions for {symbol}: missing={sorted(expected - dates)}, extra={sorted(dates - expected)}"
                )
    return result


@dataclass(frozen=True)
class HistoricalDataset:
    """Validated price bars plus reproducibility metadata (not a PIT snapshot)."""

    bars: tuple[BacktestBar, ...]
    symbols: tuple[str, ...]
    start: date
    end: date
    timezone: str
    as_of: datetime
    fetched_at: datetime
    source: str
    digest: str
    cache_path: Path | None


@dataclass(frozen=True)
class YahooPaperRun:
    """Paper result with its exact input provenance and chart-ready equity points."""

    history: HistoricalDataset
    result: BacktestResult

    @property
    def metrics(self) -> Mapping[str, Decimal | int]:
        return self.result.metrics

    @property
    def equity_points(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "timestamp": point.timestamp.isoformat(),
                "cash": str(point.cash),
                "positions_value": str(point.positions_value),
                "equity": str(point.equity),
            }
            for point in self.result.equity_curve
        )


def load_history(
    symbols: Iterable[str],
    *,
    start: date,
    end: date,
    as_of: datetime,
    timezone: str = "America/New_York",
    fetcher: HistoryFetcher | None = None,
    source: str | None = None,
    cache_dir: Path | None = None,
    cache_only: bool = False,
    max_age: timedelta | None = timedelta(days=7),
    now: datetime | None = None,
    expected_sessions: Iterable[date] | None = None,
) -> HistoricalDataset:
    """Fetch or read historical daily bars, rejecting stale/incomplete inputs.

    The explicit ``as_of`` is a data cutoff, NOT proof Yahoo had these exact
    prices at that historical time. Cache-only mode never invokes the fetcher.
    ``max_age=None`` explicitly allows old pinned cache snapshots.
    """
    names = _symbols(symbols)
    start, end = _day(start, "start"), _day(end, "end")
    cutoff = _aware(as_of, "as_of")
    current = _aware(now if now is not None else datetime.now(UTC), "now")
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, TypeError) as error:
        raise ValueError("unknown timezone") from error
    if start >= end:
        raise ValueError("start must precede exclusive end")
    # The next local midnight has passed; all requested daily sessions are closed.
    if cutoff < datetime.combine(end, datetime.min.time(), tzinfo=zone).astimezone(UTC):
        raise ValueError(
            "as_of precedes exclusive end; daily session may be incomplete"
        )
    if cutoff > current:
        raise ValueError("as_of cannot be in the future")
    if max_age is not None and (
        not isinstance(max_age, timedelta) or max_age < timedelta(0)
    ):
        raise ValueError("max_age must be a nonnegative timedelta or None")
    if source is None:
        if fetcher is not None:
            raise ValueError("injected fetcher requires an explicit source label")
        source = "yahoo:yfinance:unadjusted:1d"
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must name the actual data provider")
    spec = {
        "schema": _SCHEMA,
        "symbols": names,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": timezone,
        "as_of": cutoff.isoformat(),
        "source": source,
    }
    path = (
        None
        if cache_dir is None
        else Path(cache_dir) / (sha256(_canonical(spec)).hexdigest() + ".json")
    )
    if path is not None and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("request") != json.loads(_canonical(spec)):
            raise ValueError("cache metadata does not match request")
        rows = payload["bars"]
        if sha256(_canonical(rows)).hexdigest() != payload.get("digest"):
            raise ValueError("cache digest mismatch")
        fetched = _aware(datetime.fromisoformat(payload["fetched_at"]), "fetched_at")
        if fetched > current or (max_age is not None and current - fetched > max_age):
            raise ValueError("cached Yahoo history is stale or from the future")
        bars = _validate_bars(
            (BacktestBar(**row) for row in rows),
            names,
            start,
            end,
            zone,
            expected_sessions,
        )
        return HistoricalDataset(
            bars,
            names,
            start,
            end,
            timezone,
            cutoff,
            fetched,
            source,
            payload["digest"],
            path,
        )
    if cache_only:
        raise FileNotFoundError(
            "no matching cached Yahoo history; cache_only forbids network"
        )
    provider = fetcher or _default_fetcher
    bars_list: list[BacktestBar] = []
    for symbol in names:
        for row in provider(symbol, start, end):
            if not isinstance(row, Mapping):
                raise TypeError("fetcher must yield OHLCV mappings")
            try:
                session = _row_date(row["Date"], zone)
                timestamp = datetime.combine(session, datetime.min.time(), tzinfo=zone)
                bars_list.append(
                    BacktestBar(
                        symbol,
                        timestamp,
                        row["Open"],
                        row["High"],
                        row["Low"],
                        row["Close"],
                        row["Volume"],
                    )
                )
            except KeyError as error:
                raise ValueError(f"missing OHLCV column: {error.args[0]}") from error
    bars = _validate_bars(bars_list, names, start, end, zone, expected_sessions)
    rows = [_serialize(bar) for bar in bars]
    digest = sha256(_canonical(rows)).hexdigest()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request": spec,
            "fetched_at": current.isoformat(),
            "bars": rows,
            "digest": digest,
        }
        # Atomic replacement avoids exposing a partial JSON cache to another reader.
        import os
        import tempfile

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".yahoo-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(payload, file, sort_keys=True, allow_nan=False)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return HistoricalDataset(
        bars, names, start, end, timezone, cutoff, current, source, digest, path
    )


def run_yahoo_paper_backtest(
    decisions: Iterable[ScheduledDecision],
    history: HistoricalDataset,
    *,
    config: BacktestConfig | None = None,
    policy: PaperPolicy | None = None,
) -> YahooPaperRun:
    """Run pre-reviewed decisions against supplied history; no fetch/broker calls."""
    if not isinstance(history, HistoricalDataset) or not history.bars:
        raise ValueError("a non-empty HistoricalDataset is required")
    bars = _validate_bars(
        history.bars,
        history.symbols,
        history.start,
        history.end,
        ZoneInfo(history.timezone),
        None,
    )
    if (
        sha256(_canonical([_serialize(bar) for bar in bars])).hexdigest()
        != history.digest
    ):
        raise ValueError("historical dataset digest mismatch")
    scheduled = tuple(decisions)
    if any(not isinstance(item, ScheduledDecision) for item in scheduled):
        raise TypeError("decisions must be ScheduledDecision instances")
    for item in scheduled:
        if item.timestamp > history.as_of:
            raise ValueError("decision occurs after historical data cutoff")
        if (
            not history.start
            <= item.timestamp.astimezone(ZoneInfo(history.timezone)).date()
            < history.end
        ):
            raise ValueError("decision outside requested date range")
        for intent in item.decision.intents:
            if intent.symbol not in history.symbols:
                raise ValueError(f"missing history for decision symbol {intent.symbol}")
    return YahooPaperRun(
        history,
        PaperBacktester(config=config, policy=policy).run_decisions(
            scheduled, history.bars
        ),
    )


__all__ = [
    "HistoryFetcher",
    "HistoricalDataset",
    "YahooPaperRun",
    "load_history",
    "run_yahoo_paper_backtest",
]
