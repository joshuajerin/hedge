"""Deterministic, paper-only backtesting for Hedge decisions.

This module is deliberately self-contained.  It does not import the broker,
market-data adapters, local store, or any third-party package.  A caller passes
immutable historical bars and reviewed :class:`~hedge.contracts.Decision`
objects, so a run cannot contact a broker or fetch data.

Execution model
---------------
A decision is submitted at ``ScheduledDecision.timestamp``.  Each intent is a
DAY paper order: its only eligible fill bar is the ``fill_delay_bars``-th bar
*strictly after* submission for that symbol.  Market orders fill at that bar's
open.  Limit orders fill only when that bar trades through the limit and always use
the specified limit price (no price improvement).  Slippage is adverse and
never makes a limit-order fill worse than its limit.  Positions are marked at each bar close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from .contracts import ContractError, Decision, TradeIntent
from .policy import PaperPolicy


Money = Decimal
_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")


def _decimal(value: Decimal | int | float | str, name: str) -> Decimal:
    """Return a finite Decimal without accepting binary-float artefacts."""

    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return number


def _timestamp(value: datetime | date | str, name: str) -> datetime:
    """Normalize a bar or submission timestamp to an aware UTC datetime."""

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{name} must be an ISO-8601 date or datetime") from error
    else:
        raise TypeError(f"{name} must be a datetime, date, or ISO-8601 string")
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


@dataclass(frozen=True)
class BacktestBar:
    """One fully known historical OHLC bar for a single equity symbol."""

    symbol: str
    timestamp: datetime | date | str
    open: Decimal | int | float | str
    high: Decimal | int | float | str
    low: Decimal | int | float | str
    close: Decimal | int | float | str
    volume: Decimal | int | float | str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol or not symbol.isascii() or not symbol.replace(".", "").isalnum():
            raise ValueError("symbol must be an ASCII equity ticker")
        timestamp = _timestamp(self.timestamp, "timestamp")
        opening = _decimal(self.open, "open")
        high = _decimal(self.high, "high")
        low = _decimal(self.low, "low")
        close = _decimal(self.close, "close")
        volume = None if self.volume is None else _decimal(self.volume, "volume")
        if min(opening, high, low, close) <= _ZERO:
            raise ValueError("OHLC prices must be positive")
        if low > min(opening, close) or high < max(opening, close) or low > high:
            raise ValueError("OHLC values must satisfy low <= open/close <= high")
        if volume is not None and volume < _ZERO:
            raise ValueError("volume cannot be negative")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "open", opening)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", close)
        object.__setattr__(self, "volume", volume)


# Short aliases make the data object convenient in small scripts without
# creating a second incompatible price-bar contract.  ``OHLCVBar`` accepts
# optional volume but execution intentionally uses only OHLC prices.
OHLCVBar = BacktestBar
PriceBar = BacktestBar
Candle = BacktestBar


@dataclass(frozen=True)
class ScheduledDecision:
    """A reviewed decision and the instant it became available to the model."""

    timestamp: datetime | date | str
    decision: Decision

    def __post_init__(self) -> None:
        if not isinstance(self.decision, Decision):
            raise TypeError("decision must be a hedge.contracts.Decision")
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp, "timestamp"))

    @property
    def submitted_at(self) -> datetime:
        """Explicit name for ``timestamp`` when reporting order timing."""

        return self.timestamp


@dataclass(frozen=True)
class ScheduledIntent:
    """One existing ``TradeIntent`` and when it became available.

    This is the low-level sandbox input.  Use :class:`ScheduledDecision` when
    a run should validate a complete Decision and its decision-wide policy.
    """

    timestamp: datetime | date | str
    intent: TradeIntent

    def __post_init__(self) -> None:
        if not isinstance(self.intent, TradeIntent):
            raise TypeError("intent must be a hedge.contracts.TradeIntent")
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp, "timestamp"))

    @property
    def submitted_at(self) -> datetime:
        return self.timestamp


@dataclass(frozen=True)
class BacktestConfig:
    """Paper execution settings.  All values are local and deterministic."""

    initial_cash: Decimal | int | float | str = Decimal("100000")
    commission_per_order: Decimal | int | float | str = _ZERO
    slippage_bps: Decimal | int | float | str = _ZERO
    fill_delay_bars: int = 1

    def __post_init__(self) -> None:
        initial_cash = _decimal(self.initial_cash, "initial_cash")
        commission = _decimal(self.commission_per_order, "commission_per_order")
        slippage = _decimal(self.slippage_bps, "slippage_bps")
        if initial_cash < _ZERO:
            raise ValueError("initial_cash cannot be negative")
        if commission < _ZERO:
            raise ValueError("commission_per_order cannot be negative")
        if slippage < _ZERO:
            raise ValueError("slippage_bps cannot be negative")
        if isinstance(self.fill_delay_bars, bool) or not isinstance(self.fill_delay_bars, int):
            raise ValueError("fill_delay_bars must be an integer")
        if self.fill_delay_bars < 1:
            raise ValueError("fill_delay_bars must be at least 1")
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "commission_per_order", commission)
        object.__setattr__(self, "slippage_bps", slippage)


@dataclass(frozen=True)
class PaperFill:
    """A simulated, non-broker fill."""

    decision_id: str
    intent_index: int
    symbol: str
    side: str
    quantity: int
    timestamp: datetime
    price: Decimal
    commission: Decimal

    @property
    def gross_value(self) -> Decimal:
        return self.price * self.quantity

    @property
    def cash_change(self) -> Decimal:
        """Positive for a sale and negative for a purchase, after commission."""

        return self.gross_value - self.commission if self.side == "SELL" else -self.gross_value - self.commission


@dataclass(frozen=True)
class RejectedOrder:
    """An intent which the paper engine deliberately did not fill."""

    decision_id: str
    intent_index: int
    symbol: str
    side: str
    quantity: int
    timestamp: datetime
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    """End-of-bar paper portfolio valuation."""

    timestamp: datetime
    cash: Decimal
    positions_value: Decimal
    equity: Decimal


@dataclass(frozen=True)
class BacktestResult:
    """Complete reproducible output from one isolated paper simulation."""

    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    positions: Mapping[str, int]
    fills: tuple[PaperFill, ...]
    rejected_orders: tuple[RejectedOrder, ...]
    equity_curve: tuple[EquityPoint, ...]
    total_return: Decimal
    max_drawdown: Decimal
    closed_trade_count: int
    winning_trade_count: int
    win_rate: Decimal

    @property
    def trade_count(self) -> int:
        return len(self.fills)

    @property
    def ending_positions(self) -> Mapping[str, int]:
        """Alias retained for callers that use portfolio terminology."""

        return self.positions

    @property
    def ledger(self) -> tuple[PaperFill | RejectedOrder, ...]:
        """Time-ordered, immutable audit trail of fills and rejections."""

        return tuple(sorted((*self.fills, *self.rejected_orders), key=lambda event: (
            event.timestamp, event.decision_id, event.intent_index, type(event).__name__
        )))

    @property
    def metrics(self) -> Mapping[str, Decimal | int]:
        """Compact metrics suitable for reporting without re-running the model."""

        return MappingProxyType({
            "initial_cash": self.initial_cash,
            "final_cash": self.final_cash,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "max_drawdown": self.max_drawdown,
            "trade_count": self.trade_count,
            "closed_trade_count": self.closed_trade_count,
            "winning_trade_count": self.winning_trade_count,
            "win_rate": self.win_rate,
            "rejected_order_count": len(self.rejected_orders),
        })


@dataclass(frozen=True)
class _PendingIntent:
    due_at: datetime
    decision_id: str
    intent_index: int
    intent: TradeIntent


def _normalize_bars(bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]]) -> tuple[BacktestBar, ...]:
    """Validate and sort bars without modifying a caller-owned collection."""

    if isinstance(bars, Mapping):
        flattened: list[BacktestBar] = []
        for symbol, symbol_bars in bars.items():
            normalized_symbol = str(symbol).strip().upper()
            for bar in symbol_bars:
                if not isinstance(bar, BacktestBar):
                    raise TypeError("bars must contain BacktestBar instances")
                if bar.symbol != normalized_symbol:
                    raise ValueError("mapping key must match each bar.symbol")
                flattened.append(bar)
    else:
        flattened = list(bars)
        if any(not isinstance(bar, BacktestBar) for bar in flattened):
            raise TypeError("bars must contain BacktestBar instances")
    result = tuple(sorted(flattened, key=lambda bar: (bar.timestamp, bar.symbol)))
    seen: set[tuple[str, datetime]] = set()
    for bar in result:
        identity = (bar.symbol, bar.timestamp)
        if identity in seen:
            raise ValueError(f"duplicate bar for {bar.symbol} at {bar.timestamp.isoformat()}")
        seen.add(identity)
    return result


def _validate_decision(decision: Decision, policy: PaperPolicy) -> None:
    """Validate both the external contract and local paper constraints."""

    decision.validate()
    policy.validate(decision)


def _limit_fill_price(intent: TradeIntent, bar: BacktestBar) -> Decimal | None:
    """Return a pre-slippage limit fill price, or ``None`` for an unfilled DAY order."""

    assert intent.limit_price is not None  # guaranteed by TradeIntent.validate()
    limit = _decimal(intent.limit_price, "limit_price")
    if intent.side == "BUY":
        if bar.low > limit:
            return None
        return limit
    if bar.high < limit:
        return None
    return limit


class PaperBacktester:
    """Run only local, deterministic paper simulations.

    The constructor rejects non-paper policies.  ``run`` never imports or
    invokes a broker and only reads its arguments.
    """

    def __init__(self, config: BacktestConfig | None = None, policy: PaperPolicy | None = None) -> None:
        self.config = config or BacktestConfig()
        self.policy = policy or PaperPolicy()
        if self.policy.account_mode != "paper":
            raise ContractError("Hedge backtesting is paper-only")

    def run_decisions(
        self,
        decisions: Iterable[ScheduledDecision],
        bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
    ) -> BacktestResult:
        """Run complete decisions, enforcing their decision-wide policy limits."""

        scheduled = tuple(decisions)
        if any(not isinstance(item, ScheduledDecision) for item in scheduled):
            raise TypeError("decisions must contain ScheduledDecision instances")
        decision_ids: set[str] = set()
        submissions: list[tuple[datetime, str, int, TradeIntent]] = []
        for item in scheduled:
            _validate_decision(item.decision, self.policy)
            if item.decision.decision_id in decision_ids:
                raise ContractError("a decision_id may appear only once in a backtest")
            decision_ids.add(item.decision.decision_id)
            submissions.extend(
                (item.timestamp, item.decision.decision_id, index, intent)
                for index, intent in enumerate(item.decision.intents)
            )
        return self._run_submissions(submissions, bars)

    def run(
        self,
        scheduled_intents: Iterable[ScheduledIntent],
        bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
    ) -> BacktestResult:
        """Run low-level scheduled intents against supplied historical bars.

        Input order does not affect output.  Signals become eligible only on a
        bar strictly later than their timestamp; ``fill_delay_bars=1`` selects
        that first later bar.  At equal fill times, intents are ordered by their
        stable contract fields.  This method validates each intent and the
        individual paper policy limits.  Use ``run_decisions`` to also enforce
        the policy's per-decision order count.
        """

        scheduled = tuple(scheduled_intents)
        if any(not isinstance(item, ScheduledIntent) for item in scheduled):
            raise TypeError("scheduled_intents must contain ScheduledIntent instances")
        for item in scheduled:
            item.intent.validate()
            self.policy.validate_intent(item.intent)
        per_decision: dict[str, int] = {}
        for item in scheduled:
            per_decision[item.intent.decision_id] = per_decision.get(item.intent.decision_id, 0) + 1
        if any(count > self.policy.max_orders_per_decision for count in per_decision.values()):
            raise ContractError("decision exceeds max_orders_per_decision")
        ordered = sorted(
            scheduled,
            key=lambda item: (
                item.timestamp,
                item.intent.decision_id,
                item.intent.symbol,
                item.intent.side,
                item.intent.quantity,
                item.intent.order_type,
                str(item.intent.limit_price),
                item.intent.rationale,
                item.intent.created_at,
            ),
        )
        index_by_decision: dict[str, int] = {}
        submissions: list[tuple[datetime, str, int, TradeIntent]] = []
        for item in ordered:
            index = index_by_decision.get(item.intent.decision_id, 0)
            index_by_decision[item.intent.decision_id] = index + 1
            submissions.append((item.timestamp, item.intent.decision_id, index, item.intent))
        return self._run_submissions(submissions, bars)

    def _run_submissions(
        self,
        submissions: Iterable[tuple[datetime, str, int, TradeIntent]],
        bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
    ) -> BacktestResult:
        """Execute validated submissions.  This method has no external side effects."""

        normalized_bars = _normalize_bars(bars)
        by_symbol: dict[str, list[BacktestBar]] = {}
        for bar in normalized_bars:
            by_symbol.setdefault(bar.symbol, []).append(bar)

        pending: list[_PendingIntent] = []
        rejections: list[RejectedOrder] = []
        for submitted_at, decision_id, index, intent in sorted(
            submissions, key=lambda item: (item[0], item[1], item[2])
        ):
            eligible = [bar for bar in by_symbol.get(intent.symbol, ()) if bar.timestamp > submitted_at]
            if len(eligible) < self.config.fill_delay_bars:
                rejections.append(
                    RejectedOrder(
                        decision_id,
                        index,
                        intent.symbol,
                        intent.side,
                        intent.quantity,
                        submitted_at,
                        "no eligible price bar after fill delay",
                    )
                )
                continue
            due = eligible[self.config.fill_delay_bars - 1]
            pending.append(_PendingIntent(due.timestamp, decision_id, index, intent))

        pending.sort(key=lambda item: (item.due_at, item.decision_id, item.intent_index))
        due_by_time: dict[datetime, list[_PendingIntent]] = {}
        for item in pending:
            due_by_time.setdefault(item.due_at, []).append(item)
        bars_by_time: dict[datetime, list[BacktestBar]] = {}
        for bar in normalized_bars:
            bars_by_time.setdefault(bar.timestamp, []).append(bar)

        cash = self.config.initial_cash
        positions: dict[str, int] = {}
        last_close: dict[str, Decimal] = {}
        fills: list[PaperFill] = []
        curve: list[EquityPoint] = []
        # Total carrying cost for long shares, including buy commissions.
        # A SELL that closes any long shares realizes a trade outcome.
        cost_basis: dict[str, Decimal] = {}
        closed_trade_count = 0
        winning_trade_count = 0
        # Every due intent references exactly one symbol/timestamp bar.
        lookup = {(bar.timestamp, bar.symbol): bar for bar in normalized_bars}
        slip_factor = self.config.slippage_bps / _ONE_HUNDRED / _ONE_HUNDRED

        for timestamp in sorted(bars_by_time):
            for pending_intent in due_by_time.get(timestamp, ()):
                intent = pending_intent.intent
                bar = lookup[(timestamp, intent.symbol)]
                raw_price = bar.open if intent.order_type == "MKT" else _limit_fill_price(intent, bar)
                if raw_price is None:
                    rejections.append(
                        RejectedOrder(
                            pending_intent.decision_id,
                            pending_intent.intent_index,
                            intent.symbol,
                            intent.side,
                            intent.quantity,
                            timestamp,
                            "limit price was not reached on the eligible bar",
                        )
                    )
                    continue
                if intent.side == "BUY":
                    price = raw_price * (Decimal("1") + slip_factor)
                    # Slippage cannot violate the limit the caller supplied.
                    if intent.order_type == "LMT":
                        price = min(price, _decimal(intent.limit_price, "limit_price"))
                    required_cash = price * intent.quantity + self.config.commission_per_order
                    if required_cash > cash:
                        rejections.append(
                            RejectedOrder(pending_intent.decision_id, pending_intent.intent_index, intent.symbol, intent.side, intent.quantity, timestamp, "insufficient cash"))
                        continue
                    cash -= required_cash
                    positions[intent.symbol] = positions.get(intent.symbol, 0) + intent.quantity
                    cost_basis[intent.symbol] = cost_basis.get(intent.symbol, _ZERO) + required_cash
                else:
                    price = raw_price * (Decimal("1") - slip_factor)
                    if intent.order_type == "LMT":
                        price = max(price, _decimal(intent.limit_price, "limit_price"))
                    current = positions.get(intent.symbol, 0)
                    if not self.policy.allow_short_sales and current < intent.quantity:
                        rejections.append(
                            RejectedOrder(pending_intent.decision_id, pending_intent.intent_index, intent.symbol, intent.side, intent.quantity, timestamp, "insufficient position; short sales are disabled"))
                        continue
                    proceeds = price * intent.quantity - self.config.commission_per_order
                    # Count one closed outcome for each covered SELL fill. This
                    # makes win_rate deterministic even for partial exits.
                    if current >= intent.quantity and current > 0:
                        basis = cost_basis.get(intent.symbol, _ZERO)
                        allocated_cost = basis * Decimal(intent.quantity) / Decimal(current)
                        closed_trade_count += 1
                        if proceeds > allocated_cost:
                            winning_trade_count += 1
                        remaining_basis = basis - allocated_cost
                        if remaining_basis:
                            cost_basis[intent.symbol] = remaining_basis
                        else:
                            cost_basis.pop(intent.symbol, None)
                    cash += proceeds
                    positions[intent.symbol] = current - intent.quantity
                fills.append(PaperFill(pending_intent.decision_id, pending_intent.intent_index, intent.symbol, intent.side, intent.quantity, timestamp, price, self.config.commission_per_order))

            for bar in bars_by_time[timestamp]:
                last_close[bar.symbol] = bar.close
            positions_value = sum((Decimal(quantity) * last_close[symbol] for symbol, quantity in positions.items() if quantity and symbol in last_close), _ZERO)
            curve.append(EquityPoint(timestamp, cash, positions_value, cash + positions_value))

        # There are no prices to value a position if the caller supplied no bars.
        final_equity = curve[-1].equity if curve else cash
        peak = self.config.initial_cash
        max_drawdown = _ZERO
        for point in curve:
            peak = max(peak, point.equity)
            if peak > _ZERO:
                max_drawdown = min(max_drawdown, (point.equity - peak) / peak)
        total_return = _ZERO if self.config.initial_cash == _ZERO else (final_equity - self.config.initial_cash) / self.config.initial_cash
        win_rate = _ZERO if not closed_trade_count else Decimal(winning_trade_count) / Decimal(closed_trade_count)
        final_positions = MappingProxyType(dict(sorted((symbol, quantity) for symbol, quantity in positions.items() if quantity)))
        return BacktestResult(
            self.config.initial_cash,
            cash,
            final_equity,
            final_positions,
            tuple(fills),
            tuple(rejections),
            tuple(curve),
            total_return,
            max_drawdown,
            closed_trade_count,
            winning_trade_count,
            win_rate,
        )


class BacktestEngine:
    """Isolated sandbox with fixed historical bars and paper-only execution.

    ``bars`` are normalized once at construction.  ``run`` accepts
    :class:`ScheduledIntent` values.  ``run_decisions`` accepts complete
    :class:`ScheduledDecision` values.  Neither method writes to disk, uses the
    network, imports a broker, or mutates the supplied bars/intents.
    """

    def __init__(
        self,
        bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
        config: BacktestConfig | None = None,
        policy: PaperPolicy | None = None,
    ) -> None:
        self._bars = _normalize_bars(bars)
        self._backtester = PaperBacktester(config=config, policy=policy)

    @property
    def config(self) -> BacktestConfig:
        return self._backtester.config

    @property
    def policy(self) -> PaperPolicy:
        return self._backtester.policy

    def run(self, scheduled_intents: Iterable[ScheduledIntent]) -> BacktestResult:
        return self._backtester.run(scheduled_intents, self._bars)

    def run_decisions(self, decisions: Iterable[ScheduledDecision]) -> BacktestResult:
        return self._backtester.run_decisions(decisions, self._bars)

    def run_strategy(
        self,
        strategy: Callable[[BacktestBar], TradeIntent | Decision | Iterable[TradeIntent | Decision] | None],
    ) -> BacktestResult:
        """Evaluate a strategy once per bar, then execute its emitted signals.

        Bars reach ``strategy`` in ``(timestamp, symbol)`` order.  A signal is
        stamped with the current bar's timestamp, so it can fill no earlier
        than the following bar for its symbol.  The callable itself must be
        pure if reproducibility across runs is required.
        """

        scheduled: list[ScheduledIntent] = []
        decision_ids: set[str] = set()
        for bar in self._bars:
            emitted = strategy(bar)
            if emitted is None:
                continue
            values: Iterable[TradeIntent | Decision]
            if isinstance(emitted, (TradeIntent, Decision)):
                values = (emitted,)
            else:
                try:
                    values = iter(emitted)
                except TypeError as error:
                    raise TypeError("strategy must return a TradeIntent, Decision, iterable, or None") from error
            for value in values:
                if isinstance(value, TradeIntent):
                    scheduled.append(ScheduledIntent(bar.timestamp, value))
                elif isinstance(value, Decision):
                    # Keep a Decision intact long enough to check its identity,
                    # nested intents, and decision-wide policy before flattening.
                    # Collection finishes before execution, so any bad output
                    # fails closed without producing a partial paper fill.
                    _validate_decision(value, self.policy)
                    if value.decision_id in decision_ids:
                        raise ContractError("a decision_id may appear only once in a backtest")
                    decision_ids.add(value.decision_id)
                    scheduled.extend(ScheduledIntent(bar.timestamp, intent) for intent in value.intents)
                else:
                    raise TypeError("strategy output must contain TradeIntent or Decision values")
        return self.run(scheduled)


def run_backtest(
    decisions: Iterable[ScheduledDecision],
    bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
    *,
    config: BacktestConfig | None = None,
    policy: PaperPolicy | None = None,
) -> BacktestResult:
    """Run complete scheduled decisions with decision-wide policy validation."""

    return PaperBacktester(config=config, policy=policy).run_decisions(decisions, bars)


def run_strategy(
    bars: Iterable[BacktestBar] | Mapping[str, Iterable[BacktestBar]],
    strategy: Callable[[BacktestBar], TradeIntent | Decision | Iterable[TradeIntent | Decision] | None],
    *,
    config: BacktestConfig | None = None,
    policy: PaperPolicy | None = None,
) -> BacktestResult:
    """Convenience wrapper for :meth:`BacktestEngine.run_strategy`."""

    return BacktestEngine(bars, config=config, policy=policy).run_strategy(strategy)


__all__ = [
    "BacktestBar",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "Candle",
    "OHLCVBar",
    "EquityPoint",
    "PaperBacktester",
    "PaperFill",
    "PriceBar",
    "RejectedOrder",
    "ScheduledDecision",
    "ScheduledIntent",
    "run_backtest",
    "run_strategy",
]
