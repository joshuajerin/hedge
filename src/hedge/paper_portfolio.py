"""Durable paper-only portfolio accounting. No network, broker, or real-money paths."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import MappingProxyType
from typing import Callable, Mapping

from .contracts import Decision
from .policy import PaperPolicy
from .risk_controls import PaperAccount, RiskLimits
from .trading_system import (IdempotencyConflict, LedgerEvent, PaperFill,
                             PaperTradingSystem, ProcessingResult, TradingState)

_MAX = 9_223_372_036_854_775_807


def _cents(value: object, name: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1) or value > _MAX:
        raise ValueError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer cent amount")
    return value


def _instant(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError("pool_id must be a nonempty identifier")
    return value


@dataclass(frozen=True)
class PriceQuote:
    """Caller-supplied, positive USD price in cents with verifiable provenance."""
    price_cents: int
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        _cents(self.price_cents, "price_cents")
        object.__setattr__(self, "observed_at", _instant(self.observed_at, "observed_at"))
        if not isinstance(self.source, str) or not self.source.strip() or len(self.source) > 128:
            raise ValueError("quote source is required")

    def as_dict(self) -> dict[str, object]:
        return dict(price_cents=self.price_cents, observed_at=self.observed_at.isoformat(), source=self.source)


@dataclass(frozen=True)
class PortfolioSnapshot:
    pool_id: str
    cash_cents: int
    positions: Mapping[str, int]
    total_basis_cents: Mapping[str, int]
    cost_basis_cents: Mapping[str, int]
    prices_cents: Mapping[str, int]
    quotes: Mapping[str, PriceQuote]
    unrealized_pnl_cents: int
    nav_cents: int
    as_of: datetime

    def __post_init__(self) -> None:
        for name in ("positions", "total_basis_cents", "cost_basis_cents", "prices_cents", "quotes"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


class PaperPortfolio:
    """Single-pool SQLite serializable paper ledger, safe across process restarts.

    One BEGIN IMMEDIATE covers decision idempotency, policy/risk simulation,
    quotes, audit transitions, and updated cash/holdings/cost lots. Never use
    this store for live orders or as a replacement for a market-data provider.
    """
    account_mode = "paper"

    def __init__(self, path: str | Path, *, max_quote_age_seconds: int = 300,
                 clock: Callable[[], datetime] | None = None, policy: PaperPolicy | None = None,
                 limits: RiskLimits | None = None) -> None:
        if str(path) == ":memory:":
            raise ValueError("paper portfolio requires a durable SQLite file")
        if isinstance(max_quote_age_seconds, bool) or not isinstance(max_quote_age_seconds, int) or max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if policy is not None and policy.account_mode != "paper":
            raise ValueError("paper portfolio cannot use live policy")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=15, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=15000")
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_age = timedelta(seconds=max_quote_age_seconds)
        self._policy = policy
        self._limits = limits
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_portfolio_account (
                    pool_id TEXT PRIMARY KEY, cash_cents INTEGER NOT NULL CHECK(cash_cents >= 0),
                    positions_json TEXT NOT NULL, lots_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_portfolio_decisions (
                    decision_id TEXT PRIMARY KEY, pool_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, decision_json TEXT NOT NULL,
                    quotes_json TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_portfolio_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL,
                    decision_id TEXT, at TEXT NOT NULL, previous_state TEXT,
                    state TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_portfolio_quotes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, price_cents INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, source TEXT NOT NULL,
                    decision_id TEXT, recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_portfolio_contributions (
                    event_id TEXT PRIMARY KEY, pool_id TEXT NOT NULL,
                    cents INTEGER NOT NULL CHECK(cents > 0)
                );
            """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _begin(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def initialize(self, pool_id: str, starting_cash_cents: int) -> None:
        """Seed exactly one pool with simulated cents; identical retries are safe."""
        pool_id = _id(pool_id)
        cash = _cents(starting_cash_cents, "starting_cash_cents", zero=True)
        with self._lock:
            self._begin()
            try:
                row = self._db.execute("SELECT * FROM paper_portfolio_account").fetchone()
                if row is None:
                    self._db.execute("INSERT INTO paper_portfolio_account VALUES (?, ?, ?, ?)", (pool_id, cash, "{}", "{}"))
                    self._audit(pool_id, None, None, TradingState.PROPOSED, "PAPER_PORTFOLIO_INITIALIZED", None,
                                {"starting_cash_cents": str(cash)}, _instant(self._clock(), "clock"))
                else:
                    seeded = self._db.execute(
                        "SELECT details_json FROM paper_portfolio_audit WHERE action='PAPER_PORTFOLIO_INITIALIZED' "
                        "ORDER BY sequence LIMIT 1").fetchone()
                    if (row["pool_id"] != pool_id or seeded is None or
                            int(json.loads(seeded["details_json"])["starting_cash_cents"]) != cash):
                        raise ValueError("portfolio already initialized with different pool or cash")
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def record_contribution(self, pool_id: str, *, event_id: str, cents: int) -> bool:
        """Audit an externally verified virtual contribution exactly once.

        The caller MUST first confirm the same event ID, pool, and amount in
        PaperFundService's durable capital ledger. Cross-store orchestration
        remains the caller's responsibility; this is paper cash, not a payment.
        Returns False for an identical already-applied event.
        """
        pool_id = _id(pool_id)
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip() or len(event_id) > 128:
            raise ValueError("event_id must be a nonempty identifier")
        if event_id == "start:" + pool_id:
            raise ValueError("starting balance is already included; cannot apply it as a contribution")
        cents = _cents(cents, "contribution cents")
        with self._lock:
            self._begin()
            try:
                row = self._account(pool_id)
                prior = self._db.execute(
                    "SELECT pool_id,cents FROM paper_portfolio_contributions WHERE event_id=?", (event_id,)).fetchone()
                if prior is not None:
                    if prior["pool_id"] != pool_id or prior["cents"] != cents:
                        raise ValueError("contribution event_id belongs to a different request")
                    self._db.commit()
                    return False
                updated_cash = row["cash_cents"] + cents
                if updated_cash > _MAX:
                    raise ValueError("paper cash exceeds integer cents range")
                self._db.execute("UPDATE paper_portfolio_account SET cash_cents=? WHERE pool_id=?", (updated_cash, pool_id))
                self._db.execute("INSERT INTO paper_portfolio_contributions VALUES (?,?,?)", (event_id, pool_id, cents))
                self._audit(pool_id, None, None, TradingState.PROPOSED, "VIRTUAL_CONTRIBUTION_APPLIED", None,
                            {"event_id": event_id, "cents": str(cents)}, _instant(self._clock(), "clock"))
                self._db.commit()
                return True
            except BaseException:
                self._db.rollback()
                raise

    def contribution_events(self, pool_id: str) -> Mapping[str, int]:
        """Compare applied event IDs and amounts to the authoritative capital ledger."""
        with self._lock:
            self._account(pool_id)
            rows = self._db.execute("SELECT event_id,cents FROM paper_portfolio_contributions WHERE pool_id=?",
                                    (pool_id,)).fetchall()
            return MappingProxyType({row["event_id"]: row["cents"] for row in rows})

    def initialized_cash_cents(self, pool_id: str) -> int:
        """Read the immutable original seed in one indexed, bounded audit lookup.

        Compare this to the fund's authoritative starting balance before
        presenting a priced fund NAV. Current cash may differ after trades.
        """
        with self._lock:
            self._account(pool_id)
            seed = self._db.execute(
                "SELECT action,details_json FROM paper_portfolio_audit "
                "WHERE pool_id=? ORDER BY sequence LIMIT 1", (pool_id,)).fetchone()
            if seed is None or seed["action"] != "PAPER_PORTFOLIO_INITIALIZED":
                raise ValueError("missing paper portfolio initialization audit")
            raw_cash = json.loads(seed["details_json"])["starting_cash_cents"]
            if not isinstance(raw_cash, str) or not raw_cash.isascii() or not raw_cash.isdecimal():
                raise ValueError("invalid starting cash initialization audit")
            return _cents(int(raw_cash), "starting_cash_cents", zero=True)

    def _account(self, pool_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM paper_portfolio_account").fetchone()
        if row is None or row["pool_id"] != _id(pool_id):
            raise ValueError("unknown or uninitialized paper pool")
        return row

    def _audit(self, pool_id: str, decision_id: str | None, previous: TradingState | None,
               state: TradingState, action: str, reason: str | None, details: Mapping[str, str],
               at: datetime) -> LedgerEvent:
        cur = self._db.execute("INSERT INTO paper_portfolio_audit(pool_id,decision_id,at,previous_state,state,action,reason,details_json) "
                               "VALUES (?,?,?,?,?,?,?,?)", (pool_id, decision_id, at.isoformat(),
                               previous.value if previous else None, state.value, action, reason,
                               json.dumps(dict(details), sort_keys=True)))
        return LedgerEvent(cur.lastrowid, at, decision_id, previous, state, action, reason, details)

    def ledger(self, pool_id: str, decision_id: str | None = None) -> tuple[LedgerEvent, ...]:
        with self._lock:
            self._account(pool_id)
            rows = self._db.execute("SELECT * FROM paper_portfolio_audit WHERE pool_id=? AND (? IS NULL OR decision_id=?) ORDER BY sequence",
                                    (pool_id, decision_id, decision_id)).fetchall()
            return tuple(LedgerEvent(r["sequence"], datetime.fromisoformat(r["at"]), r["decision_id"],
                                     TradingState(r["previous_state"]) if r["previous_state"] else None,
                                     TradingState(r["state"]), r["action"], r["reason"],
                                     json.loads(r["details_json"])) for r in rows)

    def _checked_quotes(self, quotes: Mapping[str, PriceQuote], required: set[str], now: datetime) -> dict[str, PriceQuote]:
        if not isinstance(quotes, Mapping):
            raise ValueError("quotes must be an immutable caller-supplied price snapshot")
        normalized = dict(quotes)
        if set(normalized) != required:
            raise ValueError("quotes must cover exactly held and proposed symbols")
        for symbol, quote in normalized.items():
            if not isinstance(quote, PriceQuote):
                raise ValueError(f"{symbol} requires a PriceQuote with timestamp and source")
            if quote.observed_at > now or now - quote.observed_at > self._max_age:
                raise ValueError(f"stale or future quote for {symbol}")
        return normalized

    def _append_quotes(self, pool_id: str, quotes: Mapping[str, PriceQuote], now: datetime,
                       decision_id: str | None = None) -> None:
        for symbol, quote in sorted(quotes.items()):
            prior = self._db.execute(
                "SELECT observed_at FROM paper_portfolio_quotes WHERE pool_id=? AND symbol=? "
                "ORDER BY sequence DESC LIMIT 1", (pool_id, symbol)).fetchone()
            if prior is not None and datetime.fromisoformat(prior["observed_at"]) > quote.observed_at:
                raise ValueError(f"quote timestamp regressed for {symbol}")
            self._db.execute("INSERT INTO paper_portfolio_quotes(pool_id,symbol,price_cents,observed_at,source,decision_id,recorded_at) VALUES (?,?,?,?,?,?,?)",
                             (pool_id, symbol, quote.price_cents, quote.observed_at.isoformat(), quote.source,
                              decision_id, now.isoformat()))

    def record_prices(self, pool_id: str, quotes: Mapping[str, PriceQuote]) -> None:
        """Append a fresh, complete valuation snapshot for existing holdings."""
        with self._lock:
            self._begin()
            try:
                row = self._account(pool_id)
                now = _instant(self._clock(), "clock")
                checked = self._checked_quotes(quotes, set(json.loads(row["positions_json"])), now)
                if not checked:
                    raise ValueError("no held positions to price")
                self._append_quotes(pool_id, checked, now)
                self._audit(pool_id, None, None, TradingState.PROPOSED, "PAPER_PRICES_RECORDED", None,
                            {"symbols": ",".join(sorted(checked))}, now)
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def process(self, decision: Decision, quotes: Mapping[str, PriceQuote]) -> ProcessingResult:
        """Simulate only after durable account read and complete fresh quote validation."""
        canonical = PaperTradingSystem._canonical_decision(decision)
        fingerprint = PaperTradingSystem._fingerprint(canonical)
        with self._lock:
            self._begin()
            try:
                row = self._account(canonical.pool_id)
                prior = self._db.execute("SELECT * FROM paper_portfolio_decisions WHERE decision_id=?",
                                         (canonical.decision_id,)).fetchone()
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise IdempotencyConflict("decision_id already belongs to a different decision payload")
                    result = self._decode_result(json.loads(prior["result_json"]))
                    self._db.commit()
                    return replace(result, idempotent=True)
                now = _instant(self._clock(), "clock")
                positions = json.loads(row["positions_json"])
                lots = json.loads(row["lots_json"])
                required = set(positions) | {intent.symbol for intent in canonical.intents}
                # A malformed/incomplete quote cannot be interpreted as a market price.
                # Record valid but stale quotes as a terminal rejection below.
                if not isinstance(quotes, Mapping) or set(quotes) != required or any(
                    not isinstance(q, PriceQuote) for q in quotes.values()
                ):
                    raise ValueError("quotes must cover exactly held and proposed symbols with PriceQuote values")
                quote_data = {symbol: quote.as_dict() for symbol, quote in sorted(quotes.items())}
                stale = next((symbol for symbol, q in sorted(quotes.items()) if
                              q.observed_at > now or now - q.observed_at > self._max_age), None)
                if stale is not None:
                    events = (
                        self._audit(canonical.pool_id, canonical.decision_id, None, TradingState.PROPOSED,
                                    "PROPOSAL_RECEIVED", None, {"fingerprint": fingerprint}, now),
                        self._audit(canonical.pool_id, canonical.decision_id, TradingState.PROPOSED,
                                    TradingState.REJECTED, "TERMINAL_REJECTED", f"stale or future quote for {stale}", {}, now),
                    )
                    result = ProcessingResult(canonical.decision_id, TradingState.REJECTED,
                                              PaperAccount(Decimal(row["cash_cents"]) / 100, positions), (), events,
                                              f"stale or future quote for {stale}")
                else:
                    runtime = PaperTradingSystem(initial_cash=Decimal(row["cash_cents"]) / 100,
                                                 initial_positions=positions, policy=self._policy,
                                                 limits=self._limits, clock=lambda: now)
                    simulated = runtime.process(canonical, {symbol: Decimal(q.price_cents) / 100
                                                              for symbol, q in quotes.items()})
                    events = tuple(self._audit(canonical.pool_id, canonical.decision_id,
                                               event.previous_state, event.state, event.action,
                                               event.reason, event.details, event.at)
                                   for event in simulated.events)
                    result = replace(simulated, events=events)
                    if result.state is TradingState.COMPLETED:
                        cash = result.account.cash * 100
                        if cash != cash.to_integral_value() or cash < 0 or cash > _MAX:
                            raise ValueError("paper cash exceeds integer cents range")
                        for fill in result.fills:
                            if fill.status != "FILLED":
                                continue
                            price = quotes[fill.symbol].price_cents
                            if fill.side == "BUY":
                                lots.setdefault(fill.symbol, []).append([fill.quantity, price])
                            else:
                                remaining = fill.quantity
                                held_lots = lots[fill.symbol]
                                while remaining:
                                    quantity, unit = held_lots[0]
                                    used = min(quantity, remaining)
                                    remaining -= used
                                    if used == quantity:
                                        held_lots.pop(0)
                                    else:
                                        held_lots[0][0] -= used
                                if not held_lots:
                                    lots.pop(fill.symbol)
                        if {s: sum(n for n, _ in entries) for s, entries in lots.items()} != dict(result.account.positions):
                            raise ValueError("paper lots and positions disagree")
                        self._db.execute("UPDATE paper_portfolio_account SET cash_cents=?,positions_json=?,lots_json=? WHERE pool_id=?",
                                         (int(cash), json.dumps(dict(result.account.positions), sort_keys=True),
                                          json.dumps(lots, sort_keys=True), canonical.pool_id))
                        self._append_quotes(canonical.pool_id, quotes, now, canonical.decision_id)
                self._db.execute("INSERT INTO paper_portfolio_decisions VALUES (?,?,?,?,?,?)",
                                 (canonical.decision_id, canonical.pool_id, fingerprint,
                                  json.dumps(canonical.as_dict(), sort_keys=True),
                                  json.dumps(quote_data, sort_keys=True),
                                  json.dumps(result.as_dict(), sort_keys=True)))
                self._db.commit()
                return result
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _decode_result(data: dict[str, object]) -> ProcessingResult:
        account = data["account"]
        fills = tuple(PaperFill(f["order_index"], f["symbol"], f["side"], f["quantity"], f["status"],
                                Decimal(f["price"]) if f["price"] is not None else None,
                                f["reference"], f["reason"]) for f in data["fills"])
        events = tuple(LedgerEvent(e["sequence"], datetime.fromisoformat(e["at"]), e["decision_id"],
                                   TradingState(e["previous_state"]) if e["previous_state"] else None,
                                   TradingState(e["state"]), e["action"], e["reason"], e["details"])
                       for e in data["events"])
        return ProcessingResult(data["decision_id"], TradingState(data["state"]),
                                PaperAccount(account["cash"], account["positions"]), fills, events, data["reason"])

    def decision_prices(self, decision_id: str) -> Mapping[str, PriceQuote]:
        """Read the immutable supplied price snapshot bound to a terminal decision."""
        with self._lock:
            row = self._db.execute("SELECT quotes_json FROM paper_portfolio_decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if row is None:
                raise ValueError("unknown decision_id")
            return MappingProxyType({s: PriceQuote(q["price_cents"], datetime.fromisoformat(q["observed_at"]), q["source"])
                                     for s, q in json.loads(row["quotes_json"]).items()})

    def snapshot(self, pool_id: str, *, as_of: datetime | None = None) -> PortfolioSnapshot:
        """Read restart-safe paper state; stale/missing held quotes fail closed."""
        with self._lock:
            row = self._account(pool_id)
            now = _instant(as_of if as_of is not None else self._clock(), "as_of")
            positions = json.loads(row["positions_json"])
            lots = json.loads(row["lots_json"])
            if {s: sum(n for n, _ in entries) for s, entries in lots.items()} != positions:
                raise ValueError("paper lots and positions disagree")
            quotes = {}
            for symbol in positions:
                q = self._db.execute("SELECT * FROM paper_portfolio_quotes WHERE pool_id=? AND symbol=? "
                                     "ORDER BY sequence DESC LIMIT 1", (pool_id, symbol)).fetchone()
                if q is None:
                    raise ValueError(f"missing recorded quote for {symbol}")
                quote = PriceQuote(q["price_cents"], datetime.fromisoformat(q["observed_at"]), q["source"])
                if quote.observed_at > now or now - quote.observed_at > self._max_age:
                    raise ValueError(f"stale or future quote for {symbol}")
                quotes[symbol] = quote
            basis = {s: sum(n * price for n, price in entries) for s, entries in lots.items()}
            unit_basis = {s: amount // positions[s] for s, amount in basis.items()
                          if amount % positions[s] == 0}
            prices = {s: quote.price_cents for s, quote in quotes.items()}
            nav = row["cash_cents"] + sum(positions[s] * prices[s] for s in positions)
            if nav > _MAX:
                raise ValueError("paper NAV exceeds integer cents range")
            return PortfolioSnapshot(pool_id, row["cash_cents"], positions, basis, unit_basis,
                                     prices, quotes, sum(positions[s] * prices[s] - basis[s] for s in positions), nav, now)

    def dashboard_inputs(self, pool_id: str, *, as_of: datetime | None = None) -> dict[str, object]:
        """Exact inputs for PaperFundService.snapshot; never synthesize prices/basis."""
        snap = self.snapshot(pool_id, as_of=as_of)
        if set(snap.cost_basis_cents) != set(snap.positions):
            raise ValueError("fractional average cost basis cannot be represented by fund dashboard")
        return dict(pool_id=pool_id, cash_cents=snap.cash_cents,
                    positions=dict(snap.positions), prices_cents=dict(snap.prices_cents),
                    cost_basis_cents=dict(snap.cost_basis_cents), as_of=snap.as_of)
