"""Route Slack @Hedge mentions into a managed Brainbase CIO task.

The bridge is intentionally narrow: it never reads IBKR configuration, calls a
broker, or treats a Slack message as trading authority.  It packages the
mention and its Slack thread into a research request for the already-deployed
Hedge CIO, which owns delegation through the Brainbase orchestration.

Credentials are read only when this process starts, from the operating-system
environment.  They are never logged, written to disk, or passed to Brainbase.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from .slack_state import SlackState, ThreadCorrelation
from .brainbase_delivery import BrainbaseCliClient, BrainbaseResultConsumer, BrainbaseTaskMonitor
from .slack_delivery import EnvironmentSlackSender, SlackResultDelivery
from .fund_service import PaperFundService, FundConsistencyError
from .paper_portfolio import PaperPortfolio
from .virtual_pool import VirtualPool, PoolStatus
from .slack_ui import (
    BudgetSplitPreview,
    SlackThreadContext,
    allocate_budget,
    build_allocation_preview_blocks,
    build_budget_split_launch_card,
    build_budget_split_modal,
    build_cancel_result_blocks,
    build_confirm_result_blocks,
    parse_budget_split_form,
    validate_action_metadata,
    sign_action_metadata,
    build_portfolio_navigation_actions,
    escape_slack_text,
    format_cents,
)

LOGGER = logging.getLogger(__name__)
HEDGE_CIO_ID = "4ac44fd1-b386-49aa-8b39-a3ba7037fdc8"
MAX_THREAD_MESSAGES = 16
MAX_MESSAGE_CHARS = 3_000
MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>")
# Tier-0 must stay deliberately narrow. Anything that can be investment work is
# routed to the CIO instead of receiving an overly broad canned response.
TIER_ONE_PATTERN = re.compile(
    r"(?:\$[A-Z]{1,5}\b|\b(?:ticker|symbol|research|analyse|analyze|analysis|portfolio|backtest|trade|trading|buy|sell|hold|proposal|risk|allocation|position|thesis|price(?: target)?|valuation|forecast|market|stock|etf|fund|bond|crypto|option)s?\b)",
    re.IGNORECASE,
)
GREETING_PATTERN = re.compile(
    r"^(?:hi|hello|hey|yo|good (?:morning|afternoon|evening))(?:[,! .-]*(?:hedge|everyone|all|team))?[! .]*$",
    re.IGNORECASE,
)
THANKS_PATTERN = re.compile(
    r"^(?:thanks|thank you|thx)(?:[,! .-]*(?:hedge|everyone|all|team))?[! .]*$",
    re.IGNORECASE,
)
HEDGE_EXPLAINERS = {
    "what is hedge",
    "what does hedge do",
    "how does hedge work",
    "how does this work",
}
CLUB_EXPLAINERS = {
    "what is an investing club",
    "what is this investing club",
    "how does an investing club work",
    "how does this investing club work",
    "what is a virtual pool",
    "how do virtual pools work",
}
TIER_ZERO_GREETING = "Hi! I’m Hedge, the investing-club assistant. Ask a question or start a club discussion here."
TIER_ZERO_THANKS = "You’re welcome. I’m here for club discussion and education."
TIER_ZERO_HEDGE_EXPLAINER = "Hedge is an investing-club assistant. It helps turn member questions into research and structured, reviewed proposals; it does not place trades."
TIER_ZERO_CLUB_EXPLAINER = "An investing club is a member discussion group that learns together, sets a mandate, and reviews ideas before any proposal. Hedge’s virtual pool is for discussion and simulation, not execution."
# Explicit planning words launch the local virtual UI before Brainbase. Tier-1
# investment work still takes precedence, so a request such as "buy AAPL" is
# never mistaken for a contribution plan.
BUDGET_SPLIT_PATTERN = re.compile(
    r"\b(?:budget|split|contribution(?:s)?|pool)\b", re.IGNORECASE
)
BUDGET_SPLIT_UNAVAILABLE = (
    "The virtual budget-split planner is unavailable because its action-signing key is not configured. "
    "It cannot collect payments or execute trades."
)


# A deliberately explicit command; no natural-language extraction guesses money
# amounts, mandates, or participant identities. Slack user IDs must be mentions.
CREATE_POOL_PATTERN = re.compile(
    r"^create virtual pool (?P<name>[A-Za-z0-9][A-Za-z0-9 ._-]{0,79})"
    r" \| (?P<currency>[A-Z]{3}) (?P<amount>[0-9]{1,12}\.[0-9]{2})"
    r" \| mandate: (?P<mandate>[A-Za-z0-9][A-Za-z0-9 .,;:()_/-]{0,199})"
    r" \| risk: (?P<risk>low|medium|high)"
    r"(?: \| members: (?P<members><@[UW][A-Z0-9]+>(?:, ?<@[UW][A-Z0-9]+>)*))?$",
    re.IGNORECASE,
)
CREATE_POOL_PREFIX = re.compile(r"^create virtual pool\b", re.IGNORECASE)
JOIN_POOL_PATTERN = re.compile(
    r"^join virtual pool (?P<pool_id>pool-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r" \| (?P<currency>[A-Z]{3}) (?P<amount>[0-9]{1,12}\.[0-9]{2})$"
)
JOIN_POOL_PREFIX = re.compile(r"^join virtual pool\b", re.IGNORECASE)
JOIN_POOL_USAGE = "Use: `join virtual pool pool-<id> | USD 25.00` to review an existing invitation. Only your own virtual contribution can be confirmed. No payment is made."


def _join_pool_fields(text: str) -> dict[str, Any]:
    match = JOIN_POOL_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(JOIN_POOL_USAGE)
    fields = match.groupdict()
    cents = int(fields["amount"].replace(".", ""))
    if not 0 < cents <= 100_000_000_000:
        raise ValueError(
            "Virtual contribution must be from 0.01 through 1,000,000,000.00."
        )
    return {
        "pool_id": fields["pool_id"],
        "currency": fields["currency"],
        "cents": cents,
    }


PORTFOLIO_QUERY = re.compile(r"^(portfolio|my stake|positions)[?.! ]*$", re.IGNORECASE)
DASHBOARD_QUERY = re.compile(
    r"^(?:(?:how(?:'s|s| is)|what(?:'s|s| is))\s+(?:(?:the|my)\s+)?"
    r"dashboard(?:\s+looking(?:\s+like)?)?|"
    r"(?:show(?:\s+me)?\s+(?:(?:the|my)\s+)?)?dashboard)[?.! ]*$",
    re.IGNORECASE,
)
CREATE_POOL_USAGE = (
    "Use: `create virtual pool Name | USD 500.00 | mandate: Learn about index funds "
    "| risk: low | members: <@U123>` (members optional). This creates a paper-only "
    "virtual pool after you press Confirm. No payments or trades."
)


def _create_pool_fields(text: str) -> dict[str, Any]:
    match = CREATE_POOL_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(CREATE_POOL_USAGE)
    raw = match.groupdict()
    members = re.findall(r"<@([UW][A-Z0-9]+)>", raw["members"] or "")
    if len(members) > 20 or len(set(members)) != len(members):
        raise ValueError("Members must be at most 20 distinct explicit Slack mentions.")
    amount = raw["amount"]
    cents = int(amount.replace(".", ""))
    if not 0 < cents <= 100_000_000_000:
        raise ValueError(
            "Virtual starting balance must be from 0.01 through 1,000,000,000.00."
        )
    VirtualPool(
        "preview",
        raw["name"],
        raw["mandate"],
        raw["currency"],
        cents,
        raw["risk"].upper(),
        "creator",
        PoolStatus.DRAFT,
        datetime.now(UTC).isoformat(),
    )
    return {
        "name": raw["name"],
        "mandate": raw["mandate"],
        "currency": raw["currency"],
        "cents": cents,
        "risk": raw["risk"].upper(),
        "members": members,
    }


class LocalFundBridge:
    """Durable command drafts plus paper-only lifecycle and virtual ledger."""

    def __init__(self, *, draft_path: str, fund_path: str, portfolio_dir: str) -> None:
        paths = [
            str(Path(path).expanduser().resolve()) for path in (draft_path, fund_path)
        ]
        if len(set(paths)) != 2:
            raise ValueError(
                "Slack draft and virtual fund SQLite paths must be distinct"
            )
        for path in paths:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(paths[0], check_same_thread=False)
        self._connection.execute("""CREATE TABLE IF NOT EXISTS pending_pool_creations (
            run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL, creator_id TEXT NOT NULL, fields_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending')""")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS pending_pool_joins (
            run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL, invitee_id TEXT NOT NULL, pool_id TEXT NOT NULL,
            cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending')""")
        self._connection.commit()
        self._lock = Lock()
        self._portfolio_dir = Path(portfolio_dir).expanduser().resolve()
        self._portfolio_dir.mkdir(parents=True, exist_ok=True)
        if self._portfolio_dir in (Path(paths[0]), Path(paths[1])):
            raise ValueError("paper portfolio directory cannot be a SQLite file")
        self.fund = PaperFundService(paths[1])

    def close(self) -> None:
        self._connection.close()
        self.fund.close()

    def prepare(
        self, context: SlackThreadContext, creator_id: str, fields: dict[str, Any]
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pending_pool_creations(run_id,workspace_id,channel_id,thread_ts,creator_id,fields_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    context.run_id,
                    context.workspace_id,
                    context.channel_id,
                    context.thread_ts,
                    creator_id,
                    json.dumps(fields, sort_keys=True),
                ),
            )

    def confirm(self, context: SlackThreadContext, actor_id: str) -> tuple[Any, bool]:
        # A stable pool/event ID makes retries (including process restart) safe.
        with self._lock:
            row = self._connection.execute(
                "SELECT workspace_id,channel_id,thread_ts,creator_id,fields_json,status "
                "FROM pending_pool_creations WHERE run_id=?",
                (context.run_id,),
            ).fetchone()
            if (
                row is None
                or tuple(row[:3])
                != (context.workspace_id, context.channel_id, context.thread_ts)
                or row[3] != actor_id
            ):
                raise ValueError("This pool request belongs to another user or thread.")
            fields = json.loads(row[4])
            if actor_id in fields["members"]:
                raise ValueError("The creator cannot also be an invited member.")
            pool_id = "pool-" + context.run_id
            pool = self.fund.create_virtual_pool(
                pool_id=pool_id,
                actor_id=actor_id,
                name=fields["name"],
                mandate=fields["mandate"],
                base_currency=fields["currency"],
                starting_balance_cents=fields["cents"],
                mandate_risk=fields["risk"],
            )
            for member in fields["members"]:
                self.fund.invite_member(
                    invitation_id="invite-" + context.run_id + "-" + member,
                    pool_id=pool_id,
                    member_id=member,
                    actor_id=actor_id,
                )
            if pool.status is PoolStatus.DRAFT:
                pool = self.fund.activate_pool(pool_id=pool_id, actor_id=actor_id)
            paper = PaperPortfolio(self.portfolio_file(pool_id))
            try:
                paper.initialize(pool_id, fields["cents"])
            finally:
                paper.close()
            with self._connection:
                claimed = self._connection.execute(
                    "UPDATE pending_pool_creations SET status='confirmed' WHERE run_id=? AND status='pending'",
                    (context.run_id,),
                ).rowcount
            return pool, claimed != 1

    def prepare_join(
        self, context: SlackThreadContext, actor_id: str, fields: dict[str, Any]
    ) -> None:
        pool = self.fund.pools.pool(fields["pool_id"])
        if (
            pool.status is not PoolStatus.ACTIVE
            or pool.base_currency != fields["currency"]
        ):
            raise ValueError(
                "The virtual pool is not active or its currency does not match."
            )
        invitation_id = "invite-" + pool.pool_id.removeprefix("pool-") + "-" + actor_id
        invitation = self.fund.pools.invitation(invitation_id)
        if (
            invitation.pool_id != pool.pool_id
            or invitation.invitee_id != actor_id
            or invitation.status.value != "PENDING"
        ):
            raise ValueError(
                "No pending invitation exists for this Slack user and pool."
            )
        # No capital changes occur until the invitee clicks the signed button.
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO pending_pool_joins(run_id,workspace_id,channel_id,thread_ts,invitee_id,pool_id,cents) VALUES (?,?,?,?,?,?,?)",
                (
                    context.run_id,
                    context.workspace_id,
                    context.channel_id,
                    context.thread_ts,
                    actor_id,
                    pool.pool_id,
                    fields["cents"],
                ),
            )

    def confirm_join(
        self, context: SlackThreadContext, actor_id: str
    ) -> tuple[str, int, bool]:
        with self._lock:
            row = self._connection.execute(
                "SELECT workspace_id,channel_id,thread_ts,invitee_id,pool_id,cents,status FROM pending_pool_joins WHERE run_id=?",
                (context.run_id,),
            ).fetchone()
            if (
                row is None
                or tuple(row[:3])
                != (context.workspace_id, context.channel_id, context.thread_ts)
                or row[3] != actor_id
            ):
                raise ValueError(
                    "This invitation confirmation belongs to another user or thread."
                )
            pool_id, cents = str(row[4]), int(row[5])
            invitation_id = "invite-" + pool_id.removeprefix("pool-") + "-" + actor_id
            event_id = "join-" + context.run_id
            self.fund.join_with_virtual_contribution(
                invitation_id=invitation_id,
                actor_id=actor_id,
                contribution_cents=cents,
                event_id=event_id,
            )
            # The authoritative capital event must match before paper cash is
            # credited. A partial cross-file write is repaired by exact retry.
            events = self.fund.capital.virtual_contribution_history(
                pool_id=pool_id, member_id=actor_id
            )
            if not any(
                e.event_id == event_id
                and e.cents == cents
                and e.action == "VIRTUAL_CONTRIBUTION"
                for e in events
            ):
                raise FundConsistencyError(
                    "virtual contribution is not verified in fund ledger"
                )
            paper = PaperPortfolio(self.portfolio_file(pool_id))
            try:
                paper.record_contribution(pool_id, event_id=event_id, cents=cents)
            finally:
                paper.close()
            with self._connection:
                claimed = self._connection.execute(
                    "UPDATE pending_pool_joins SET status='confirmed' WHERE run_id=? AND status='pending'",
                    (context.run_id,),
                ).rowcount
            return pool_id, cents, claimed != 1

    def visible_pools(self, member_id: str) -> list[Any]:
        return list(self.fund.member_pools(member_id=member_id))

    def portfolio_file(self, pool_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", pool_id):
            raise ValueError("invalid virtual pool ID for paper portfolio")
        path = self._portfolio_dir / (pool_id + ".sqlite3")
        if path.is_symlink():
            raise ValueError("paper portfolio path cannot be a symlink")
        return path

    def priced_snapshot(
        self, pool: VirtualPool, total_contributions_cents: int
    ) -> tuple[Any, Any] | None:
        # Verify *every* fund capital event is mirrored exactly in paper cash.
        # A crash between the two SQLite commits leaves NAV unavailable.
        history = self.fund.capital.virtual_contribution_history(pool_id=pool.pool_id)
        if sum(event.cents for event in history) != total_contributions_cents:
            return None
        expected = {
            event.event_id: event.cents
            for event in history
            if event.action == "VIRTUAL_CONTRIBUTION"
        }
        start = [
            event for event in history if event.action == "VIRTUAL_STARTING_BALANCE"
        ]
        if (
            len(start) != 1
            or start[0].event_id != "start:" + pool.pool_id
            or start[0].cents != pool.starting_nav_cents
        ):
            return None
        path = self.portfolio_file(pool.pool_id)
        if not path.is_file():
            return None
        paper = PaperPortfolio(path)
        try:
            if dict(paper.contribution_events(pool.pool_id)) != expected:
                return None
            if paper.initialized_cash_cents(pool.pool_id) != pool.starting_nav_cents:
                return None
            recorded = paper.snapshot(pool.pool_id)  # checks held quote age/provenance
            if set(recorded.cost_basis_cents) != set(recorded.positions):
                return (
                    None  # fractional FIFO basis cannot be expressed as per-share cost
                )
            valued = self.fund.snapshot(
                pool_id=pool.pool_id,
                cash_cents=recorded.cash_cents,
                positions=recorded.positions,
                prices_cents=recorded.prices_cents,
                cost_basis_cents=recorded.cost_basis_cents,
                as_of=recorded.as_of,
            )
            if (
                self.fund.capital.virtual_contribution_history(pool_id=pool.pool_id)
                != history
                or dict(paper.contribution_events(pool.pool_id)) != expected
            ):
                return None  # a concurrent join changed capital during valuation
            return recorded, valued
        except ValueError:
            return None  # stale/missing/future quote or incomplete paper account
        finally:
            paper.close()


def _simple_card(
    title: str, message: str, context: SlackThreadContext, key: bytes, *, active: str
) -> list[dict[str, Any]]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": message[:2800]}},
        build_portfolio_navigation_actions(context, key, active=active),
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Paper-only local records. No live NAV, quote, payment, or brokerage action.",
                }
            ],
        },
    ]


def _portfolio_card(
    route: str,
    context: SlackThreadContext,
    user_id: str,
    fund: LocalFundBridge,
    key: bytes,
) -> list[dict[str, Any]]:
    pools = fund.visible_pools(user_id)[:10]
    if not pools:
        return _simple_card(
            "Virtual portfolio",
            "No active virtual pool membership is recorded for you. No NAV or positions are available.",
            context,
            key,
            active=route,
        )
    rows = []
    for pool in pools:
        accounts = {
            account.member_id: account
            for account in fund.fund.capital.virtual_accounts(pool_id=pool.pool_id)
        }
        account = accounts.get(user_id)
        total = sum(a.contributed_cents for a in accounts.values())
        priced = fund.priced_snapshot(pool, total)
        name = escape_slack_text(pool.name)
        if route == "positions":
            if priced is None:
                rows.append(
                    f"*{name}*: Paper positions unavailable (missing/stale quotes or unreconciled virtual capital). No price or NAV inferred."
                )
            else:
                recorded, valued = priced
                if not valued.positions:
                    rows.append(
                        f"*{name}*: No persisted paper positions. No market quote needed for cash-only simulation."
                    )
                for position in valued.positions:
                    quote = recorded.quotes[position.ticker]
                    source = escape_slack_text(" ".join(quote.source.split())[:80])
                    rows.append(
                        f"*{name} · {position.ticker}*: {recorded.positions[position.ticker]} paper units; "
                        f"value {format_cents(position.market_value_cents, pool.base_currency)}; "
                        f"persisted quote {format_cents(quote.price_cents, pool.base_currency)} "
                        f"at {quote.observed_at.isoformat()} ({source})."
                    )
        elif route == "my_stake":
            if account is None:
                rows.append(
                    f"*{name}*: No virtual capital recorded for you. No current value is available."
                )
            else:
                fraction = account.ownership
                row = (
                    f"*{name}*: virtual contribution {format_cents(account.contributed_cents, pool.base_currency)}; "
                    f"ownership {fraction.numerator}/{fraction.denominator}."
                )
                if priced is None:
                    row += " Current value unavailable without fresh, reconciled paper NAV."
                else:
                    stake = next(
                        (s for s in priced[1].stakes if s.member_name == user_id), None
                    )
                    row += (
                        f" Simulated paper value {format_cents(stake.current_value_cents, pool.base_currency)}."
                        if stake is not None
                        else " Current value unavailable."
                    )
                rows.append(row)
        else:
            row = (
                f"*{name}* ({pool.status.value}): recorded virtual contributions "
                f"{format_cents(total, pool.base_currency)}."
            )
            if priced is None:
                row += " NAV, returns, cash and prices unavailable (missing/stale quotes or unreconciled virtual capital)."
            else:
                recorded, valued = priced
                label = (
                    "Simulated starting NAV"
                    if not recorded.positions
                    and recorded.cash_cents == pool.starting_nav_cents
                    else "Paper NAV"
                )
                row += (
                    f" {label} {format_cents(valued.dashboard.nav_cents, pool.base_currency)}; "
                    f"paper cash {format_cents(recorded.cash_cents, pool.base_currency)}. "
                    "Holdings use persisted fresh quotes only; no live price fetch."
                )
            rows.append(row)
    title = {
        "my_stake": "My virtual stake",
        "positions": "Paper positions",
        "portfolio": "Virtual portfolio",
    }[route]
    return _simple_card(title, "\n".join(rows), context, key, active=route)


@dataclass(frozen=True)
class SlackRequest:
    """Only the Slack context the CIO needs to answer in the correct thread."""

    event_id: str
    channel_id: str
    user_id: str
    user_name: str
    message_ts: str
    thread_ts: str
    text: str
    thread: list[dict[str, str]]
    # These values are generated by the trusted inbound bridge, not by a model.
    run_id: str = ""
    workspace_id: str = ""


class RecentEventIds:
    """A bounded, process-local duplicate guard for Socket Mode retries."""

    def __init__(self, capacity: int = 2_048) -> None:
        self._capacity = capacity
        self._values: OrderedDict[str, None] = OrderedDict()
        self._lock = Lock()

    def add_if_new(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._values:
                return False
            self._values[event_id] = None
            if len(self._values) > self._capacity:
                self._values.popitem(last=False)
            return True


def _clean_text(text: str) -> str:
    return re.sub(r"^\s*<@[A-Z0-9]+>", "", text, count=1).strip()[:MAX_MESSAGE_CHARS]


def _is_budget_split_request(text: str) -> bool:
    """Recognize explicit, non-investment virtual planning requests."""

    normalized = " ".join(text.casefold().split()).strip(" .!?,;:")
    return bool(
        normalized
        and not TIER_ONE_PATTERN.search(text)
        and BUDGET_SPLIT_PATTERN.search(normalized)
    )


def _context_from_correlation(correlation: ThreadCorrelation) -> SlackThreadContext:
    return SlackThreadContext(
        workspace_id=correlation.workspace_id,
        channel_id=correlation.channel_id,
        thread_ts=correlation.root_thread_ts,
        run_id=correlation.run_id,
    )


def _mapping_value(value: object, key: str) -> str:
    return _string(value.get(key)) if isinstance(value, dict) else ""


def _action_run_id(token: object) -> str:
    """Read a candidate run ID only to find a durable record before HMAC validation.

    The result is deliberately untrusted. ``validate_action_metadata`` below
    verifies the entire token and the recovered canonical context before use.
    """

    if not isinstance(token, str) or token.count(".") != 1:
        return ""
    encoded = token.split(".", 1)[0]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    context = payload.get("c") if isinstance(payload, dict) else None
    return _mapping_value(context, "run_id")


def _action_context_from_body(
    body: dict[str, Any], state: SlackState
) -> SlackThreadContext | None:
    """Get the expected context from signed Slack payload affinity plus SQLite.

    Payload values identify the actual Slack message that was clicked. They are
    never accepted as a destination: all three must resolve to a stored run.
    """

    team_id = _mapping_value(body.get("team"), "id") or _string(body.get("team_id"))
    channel = body.get("channel")
    container = body.get("container")
    message = body.get("message")
    channel_id = (
        _mapping_value(channel, "id")
        or _mapping_value(container, "channel_id")
        or _mapping_value(message, "channel")
    )
    root_thread_ts = _mapping_value(message, "thread_ts") or _mapping_value(
        container, "thread_ts"
    )
    if not team_id or not channel_id or not root_thread_ts:
        return None
    run_id = state.current_run_for_thread(team_id, channel_id, root_thread_ts)
    correlation = state.correlation_for_run(run_id) if run_id else None
    return _context_from_correlation(correlation) if correlation is not None else None


def _modal_values(view: dict[str, Any]) -> dict[str, str]:
    """Flatten only the five fixed Block Kit inputs into direct form strings."""

    state = view.get("state")
    values = state.get("values") if isinstance(state, dict) else None
    if not isinstance(values, dict):
        raise ValueError("missing form values")
    names = ("total", "currency", "group_name", "member_names", "weights")
    result: dict[str, str] = {}
    for block_id in names:
        block = values.get(block_id)
        action = block.get("value") if isinstance(block, dict) else None
        value = action.get("value") if isinstance(action, dict) else None
        if block_id == "weights" and value is None:
            result[block_id] = ""
        elif isinstance(value, str):
            result[block_id] = value
        else:
            raise ValueError(f"missing {block_id}")
    return result


def _tier_zero_reply(text: str) -> str | None:
    """Return a fixed reply only for clearly safe, non-investment-work messages.

    This is intentionally conservative. A match must be a complete greeting or
    one of a small set of education questions, and investment-work terms always
    take precedence. ``None`` means the CIO must handle it through Brainbase.
    """

    normalized = " ".join(text.casefold().split()).strip(" .!?,;:")
    if not normalized or TIER_ONE_PATTERN.search(text):
        return None
    if GREETING_PATTERN.fullmatch(normalized):
        return TIER_ZERO_GREETING
    if THANKS_PATTERN.fullmatch(normalized):
        return TIER_ZERO_THANKS
    if normalized in HEDGE_EXPLAINERS:
        return TIER_ZERO_HEDGE_EXPLAINER
    if normalized in CLUB_EXPLAINERS:
        return TIER_ZERO_CLUB_EXPLAINER
    return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _thread_context(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep a compact, human-readable transcript without bot messages."""

    context: list[dict[str, str]] = []
    for message in messages[-MAX_THREAD_MESSAGES:]:
        if message.get("bot_id") or message.get("subtype") == "bot_message":
            continue
        text = _string(message.get("text"))
        if not text:
            continue
        context.append(
            {
                "user_id": _string(message.get("user")),
                "timestamp": _string(message.get("ts")),
                "text": text[:MAX_MESSAGE_CHARS],
            }
        )
    return context


def format_cio_task(request: SlackRequest) -> str:
    """Create the complete, non-executable CIO prompt passed to Brainbase."""

    payload = {
        "schema": "hedge.slack-request.v1",
        "received_at": datetime.now(UTC).isoformat(),
        "slack": {
            "workspace_id": request.workspace_id,
            "channel_id": request.channel_id,
            "requester": {
                "user_id": request.user_id,
                "display_name": request.user_name,
            },
            "message_ts": request.message_ts,
            "thread_ts": request.thread_ts,
            "reply_in_thread": True,
            "mention_text": request.text,
            "thread_context": request.thread,
        },
        # Result adapters must use these opaque values to look up the durable
        # canonical destination. They must not accept a model-supplied token or
        # Slack channel.
        "delivery": {
            "run_id": request.run_id,
            "workspace_id": request.workspace_id,
            "channel_id": request.channel_id,
            "root_thread_ts": request.thread_ts,
        },
    }
    return "\n".join(
        [
            "You are Hedge CIO, the top-level router for one Slack @mention.",
            "Classify the request before acting. Reply directly to greetings, casual conversation, and simple questions without delegating.",
            "For research, portfolio, backtest, or risk requests, use the configured Hedge Brainbase orchestration and delegate only to the specialists needed for that request.",
            "Use Yahoo Finance for any market price or research data; never use IBKR as a data source.",
            "Reply for the original Slack user in the same thread, identify uncertainty and the specialist findings used.",
            "Do not execute, promise, or imply any trade. IBKR is local paper execution only and is outside Brainbase.",
            "Do not send a generic status update. Send the useful final answer in the originating Slack thread.",
            "Slack context JSON follows:",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ]
    )


def create_brainbase_task(
    message: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Start a CIO task without waiting for the agent or invoking a shell."""

    command = [
        _brainbase_cli_path(),
        "task",
        "create",
        "--agent",
        os.environ.get("HEDGE_CIO_AGENT_ID", HEDGE_CIO_ID),
        "--title",
        "Slack Hedge mention",
        "--message",
        message,
        "--json",
    ]
    model = os.environ.get("HEDGE_CIO_MODEL")
    if model:
        command.extend(["--model", model])
    completed = run(command, check=True, capture_output=True, text=True, timeout=45)
    return completed.stdout


def parse_task_create_response(raw: str, *, expected_agent_id: str) -> str:
    """Accept only the installed CLI's trusted, exact task-create envelope."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid Brainbase task-create response") from error
    if not isinstance(value, dict) or set(value) != {"task_id", "agent_id", "status"}:
        raise ValueError("invalid Brainbase task-create fields")
    task_id = value["task_id"]
    if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", task_id)
            or value["agent_id"] != expected_agent_id
            or value["status"] not in {"initializing", "running", "success", "fail", "need_more_info"}):
        raise ValueError("invalid Brainbase task-create identity or status")
    return task_id


def _brainbase_cli_path() -> str:
    """Use the known local Brainbase CLI; never accept a legacy env override."""

    local = Path.home() / ".hermes" / "node" / "bin" / "brainbase"
    return str(local) if local.is_file() else "brainbase"


def _request_from_event(
    client: Any,
    event_id: str,
    event: dict[str, Any],
    *,
    run_id: str = "",
    workspace_id: str = "",
) -> SlackRequest:
    channel_id = _string(event.get("channel"))
    message_ts = _string(event.get("ts"))
    thread_ts = _string(event.get("thread_ts")) or message_ts
    user_id = _string(event.get("user"))
    # The event's user ID is stable Slack identity. Avoid a users.info call so
    # the mention bridge needs no optional users:read scope or reinstall.
    user_name = user_id
    replies = client.conversations_replies(
        channel=channel_id, ts=thread_ts, limit=MAX_THREAD_MESSAGES
    )
    return SlackRequest(
        event_id=event_id,
        channel_id=channel_id,
        user_id=user_id,
        user_name=user_name,
        message_ts=message_ts,
        thread_ts=thread_ts,
        text=_clean_text(_string(event.get("text"))),
        thread=_thread_context(replies.get("messages", [])),
        run_id=run_id,
        workspace_id=workspace_id,
    )


def _is_bot_event(event: dict[str, Any], bot_user_id: str) -> bool:
    return bool(
        event.get("bot_id")
        or event.get("subtype") == "bot_message"
        or (bot_user_id and event.get("user") == bot_user_id)
    )


def _react(client: Any, *, channel_id: str, timestamp: str, name: str) -> None:
    """Add a best-effort lifecycle reaction without delaying a Slack task."""

    try:
        client.reactions_add(channel=channel_id, timestamp=timestamp, name=name)
    except Exception:  # Missing scope and already_reacted are non-fatal.
        LOGGER.debug(
            "Could not add :%s: reaction to Slack message %s",
            name,
            timestamp,
            exc_info=True,
        )


def build_app(
    *, state: SlackState | None = None, fund: LocalFundBridge | None = None,
    task_monitor: BrainbaseTaskMonitor | None = None,
) -> Any:
    """Build the Socket Mode bridge and the bounded virtual split UI.

    Slack Bolt verifies interactive request signatures. The additional UI HMAC
    binds every button and modal to the durable inbound Slack correlation.
    """

    try:
        from slack_bolt import App
    except ImportError as error:  # pragma: no cover - depends on local optional extra
        raise RuntimeError(
            "Install the Slack bridge extra: uv sync --extra slack"
        ) from error

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN is required to run the Socket Mode bridge.")
    app = App(token=bot_token)
    identity = app.client.auth_test()
    bot_user_id = _string(identity.get("user_id"))
    configured_workspace_id = _string(identity.get("team_id"))
    if state is None:
        state = SlackState(
            os.environ.get("HEDGE_SLACK_STATE_DB", "~/.hedge/slack-state.sqlite3")
        )
    if fund is None:
        fund = LocalFundBridge(
            draft_path=os.environ.get(
                "HEDGE_SLACK_FUND_DRAFT_DB", "~/.hedge/slack-fund-drafts.sqlite3"
            ),
            fund_path=os.environ.get(
                "HEDGE_PAPER_FUND_DB", "~/.hedge/paper-fund.sqlite3"
            ),
            portfolio_dir=os.environ.get(
                "HEDGE_PAPER_PORTFOLIO_DIR", "~/.hedge/paper-portfolios"
            ),
        )
    if task_monitor is None:
        cli = BrainbaseCliClient(_brainbase_cli_path(), expected_agent_id=os.environ.get("HEDGE_CIO_AGENT_ID", HEDGE_CIO_ID))
        task_db = os.environ.get("HEDGE_BRAINBASE_TASK_DB", state.path)
        if task_db == ":memory:" and state.path != ":memory:":
            raise ValueError("Brainbase task registry must be durable")
        task_monitor = BrainbaseTaskMonitor(BrainbaseResultConsumer(
            task_db, state, SlackResultDelivery(state, EnvironmentSlackSender()), cli,
        ))
    # main() starts this worker before Socket Mode admits new events. Tests can
    # inject a fake monitor and run status checks without any external calls.
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hedge-slack")
    raw_channels = os.environ.get("HEDGE_APPROVED_CHANNEL_IDS", "")
    approved_channels = frozenset(
        value.strip() for value in raw_channels.split(",") if value.strip()
    )
    approved_workspaces = frozenset(
        value.strip()
        for value in os.environ.get("HEDGE_APPROVED_WORKSPACE_IDS", "").split(",")
        if value.strip()
    )

    def approved(context: SlackThreadContext) -> bool:
        return (
            bool(approved_channels and approved_workspaces)
            and ("*" in approved_channels or context.channel_id in approved_channels)
            and context.workspace_id in approved_workspaces
            and context.workspace_id == configured_workspace_id
        )

    # Do not fall back to any other secret. An absent or too-short value leaves
    # the virtual UI unavailable while keeping all CIO routing operational.
    configured_action_secret = os.environ.get("HEDGE_SLACK_ACTION_SECRET")
    action_signing_key: bytes | None = (
        configured_action_secret.encode("utf-8")
        if configured_action_secret
        and len(configured_action_secret.encode("utf-8")) >= 16
        else None
    )
    previews: dict[str, BudgetSplitPreview] = {}
    finalized_runs: set[str] = set()
    ui_lock = Lock()

    @app.event("app_mention")
    def on_app_mention(
        event: dict[str, Any],
        body: dict[str, Any],
        client: Any,
        say: Callable[..., Any],
    ) -> None:
        event_id = _string(body.get("event_id"))
        workspace_id = (
            _string(body.get("team_id"))
            or _string(event.get("team"))
            or configured_workspace_id
        )
        channel_id = _string(event.get("channel"))
        message_ts = _string(event.get("ts"))
        root_thread_ts = _string(event.get("thread_ts")) or message_ts
        if not event_id or _is_bot_event(event, bot_user_id):
            return
        # A missing workspace/channel/thread is not safe to infer from model
        # output. Do not create a Brainbase task without canonical affinity.
        if not workspace_id or not channel_id or not root_thread_ts:
            LOGGER.warning("Dropped Slack event without canonical correlation")
            return
        if (
            not approved_channels
            or not approved_workspaces
            or ("*" not in approved_channels and channel_id not in approved_channels)
            or workspace_id not in approved_workspaces
            or workspace_id != configured_workspace_id
        ):
            LOGGER.warning("Dropped Slack event from an unapproved channel")
            return
        if not state.claim_inbound_event(event_id, workspace_id):
            return

        # Keep the existing lifecycle reaction for every accepted event. The
        # durable event claim above means Slack retries cannot add a second
        # reaction or a second Tier-0 reply.
        _react(client, channel_id=channel_id, timestamp=message_ts, name="eyes")
        clean_text = _clean_text(_string(event.get("text")))
        correlation = ThreadCorrelation(
            run_id=str(uuid4()),
            workspace_id=workspace_id,
            channel_id=channel_id,
            root_thread_ts=root_thread_ts,
        )
        deadline_at = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
        if CREATE_POOL_PREFIX.match(clean_text):
            if action_signing_key is None:
                say(
                    text="Virtual pool creation is unavailable without the action-signing key.",
                    thread_ts=root_thread_ts,
                )
                return
            try:
                fields = _create_pool_fields(clean_text)
                creator_id = _string(event.get("user"))
                if (
                    not re.fullmatch(r"[UW][A-Z0-9]+", creator_id)
                    or creator_id in fields["members"]
                ):
                    raise ValueError(
                        "The creator must be a Slack user and cannot be invited twice."
                    )
            except ValueError as error:
                say(text=str(error), thread_ts=root_thread_ts)
                return
            if not state.record_run(
                correlation,
                event_id=event_id,
                requester=creator_id,
                deadline_at=deadline_at,
            ):
                return
            context = _context_from_correlation(correlation)
            try:
                fund.prepare(context, creator_id, fields)
            except (sqlite3.Error, OSError):
                state.set_run_status(correlation.run_id, "failed")
                LOGGER.exception("Could not persist virtual pool confirmation request")
                say(
                    text="The virtual pool could not be prepared locally. No pool was created.",
                    thread_ts=root_thread_ts,
                )
                return
            members = (
                ", ".join(f"<@{member}>" for member in fields["members"]) or "None"
            )
            details = (
                f"*{escape_slack_text(fields['name'])}* · "
                f"{format_cents(fields['cents'], fields['currency'])} virtual starting balance "
                f"for <@{creator_id}>\nMandate: {escape_slack_text(fields['mandate'])}"
                f"\nRisk: {fields['risk']} · Invited (zero units until they join): {members}"
            )
            say(
                text="Review and confirm a paper-only virtual pool.",
                thread_ts=root_thread_ts,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "Create virtual pool · review",
                        },
                    },
                    {"type": "section", "text": {"type": "mrkdwn", "text": details}},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "hedge.pool.confirm",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Confirm virtual pool",
                                },
                                "value": sign_action_metadata(
                                    "confirm", context, action_signing_key
                                ),
                            }
                        ],
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Paper only. No payment or brokerage action. No pool is created until you confirm.",
                            }
                        ],
                    },
                ],
            )
            return

        if JOIN_POOL_PREFIX.match(clean_text):
            if action_signing_key is None:
                say(
                    text="Virtual pool joining is unavailable without the action-signing key.",
                    thread_ts=root_thread_ts,
                )
                return
            try:
                fields = _join_pool_fields(clean_text)
                actor_id = _string(event.get("user"))
                if not re.fullmatch(r"[UW][A-Z0-9]+", actor_id):
                    raise ValueError("A Slack user must accept their own invitation.")
            except ValueError as error:
                say(text=str(error), thread_ts=root_thread_ts)
                return
            if not state.record_run(
                correlation,
                event_id=event_id,
                requester=actor_id,
                deadline_at=deadline_at,
            ):
                return
            context = _context_from_correlation(correlation)
            try:
                fund.prepare_join(context, actor_id, fields)
            except (ValueError, sqlite3.Error, OSError):
                state.set_run_status(correlation.run_id, "failed")
                say(
                    text="No eligible pending invitation was found for you and that active virtual pool. Nothing was contributed.",
                    thread_ts=root_thread_ts,
                )
                return
            say(
                text="Review your virtual contribution. No payment is made.",
                thread_ts=root_thread_ts,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "Join virtual pool · review",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Pool:* {escape_slack_text(fields['pool_id'])}\n"
                            f"*Invitee:* <@{actor_id}>\n"
                            f"*Virtual contribution:* {format_cents(fields['cents'], fields['currency'])}",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "hedge.pool.join_confirm",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Confirm virtual join",
                                },
                                "value": sign_action_metadata(
                                    "confirm", context, action_signing_key
                                ),
                            }
                        ],
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Paper only. No money moves. No membership or virtual capital changes until you confirm.",
                            }
                        ],
                    },
                ],
            )
            return

        route_match = PORTFOLIO_QUERY.fullmatch(clean_text)
        if route_match or DASHBOARD_QUERY.fullmatch(clean_text):
            route = route_match.group(1).lower().replace(" ", "_") if route_match else "portfolio"
            if action_signing_key is None:
                say(
                    text="Local portfolio cards are unavailable without the action-signing key.",
                    thread_ts=root_thread_ts,
                )
                return
            if not state.record_run(
                correlation,
                event_id=event_id,
                requester=_string(event.get("user")),
                deadline_at=deadline_at,
            ):
                return
            context = _context_from_correlation(correlation)
            try:
                say(
                    text="Paper-only portfolio records.",
                    blocks=_portfolio_card(
                        route,
                        context,
                        _string(event.get("user")),
                        fund,
                        action_signing_key,
                    ),
                    thread_ts=root_thread_ts,
                )
            except (ValueError, FundConsistencyError, sqlite3.Error, OSError):
                LOGGER.exception(
                    "Local virtual portfolio is inconsistent or unavailable"
                )
                say(
                    text="The local virtual portfolio is unavailable until its persisted records are repaired. No NAV or positions were inferred.",
                    thread_ts=root_thread_ts,
                )
            state.set_run_status(correlation.run_id, "completed")
            return

        if _is_budget_split_request(clean_text):
            if action_signing_key is None:
                say(text=BUDGET_SPLIT_UNAVAILABLE, thread_ts=root_thread_ts)
                return
            # Persist before posting so a click remains valid after a bridge
            # restart. This route never creates a Brainbase task.
            if not state.record_run(
                correlation,
                event_id=event_id,
                requester=_string(event.get("user")),
                deadline_at=deadline_at,
            ):
                return
            context = _context_from_correlation(correlation)
            say(
                text="Open the virtual budget-split planner in this thread.",
                blocks=build_budget_split_launch_card(context, action_signing_key),
                thread_ts=root_thread_ts,
            )
            return

        tier_zero_reply = _tier_zero_reply(clean_text)
        if tier_zero_reply is not None:
            if not state.record_run(
                correlation,
                event_id=event_id,
                requester=_string(event.get("user")),
                deadline_at=deadline_at,
            ):
                return
            say(text=tier_zero_reply, thread_ts=root_thread_ts)
            state.set_run_status(correlation.run_id, "completed")
            return

        def submit() -> None:
            run_recorded = False
            try:
                request = _request_from_event(
                    client,
                    event_id,
                    event,
                    run_id=correlation.run_id,
                    workspace_id=correlation.workspace_id,
                )
                if not request.text:
                    say(
                        text="Tell me what you want the Hedge research team to investigate.",
                        thread_ts=request.thread_ts,
                    )
                    return
                # Research may include cloud bootstrap and specialist delegation.
                # Keep UI action deadlines short, but give the async CIO task time to finish.
                research_deadline_at = (datetime.now(UTC) + timedelta(seconds=240)).isoformat()
                run_recorded = state.record_run(
                    correlation,
                    event_id=event_id,
                    requester=request.user_id,
                    deadline_at=research_deadline_at,
                )
                if not run_recorded:
                    return
                # Claim the inbound event before acknowledging. This is one
                # same-thread status reply, not permission to execute anything.
                # It happens before the slow task-create CLI call.
                say(
                    text="I'm starting research.",
                    thread_ts=root_thread_ts,
                )
                # The Slack event is already durably claimed. If the CLI
                # succeeds but registration fails, never create another task on
                # retry: leave the run failed for manual reconciliation.
                raw_create = create_brainbase_task(format_cio_task(request))
                task_id = parse_task_create_response(
                    raw_create, expected_agent_id=os.environ.get("HEDGE_CIO_AGENT_ID", HEDGE_CIO_ID),
                )
                # Carry the inbound run's original deadline forward; CLI
                # creation time must not silently extend the Slack SLA.
                remaining = max(1, int((datetime.fromisoformat(research_deadline_at) - datetime.now(UTC)).total_seconds()))
                task_monitor.register(task_id, correlation, timeout_seconds=remaining)
                state.set_run_status(correlation.run_id, "started")
                _react(
                    client,
                    channel_id=request.channel_id,
                    timestamp=request.message_ts,
                    name="brain",
                )
            except subprocess.TimeoutExpired:
                if run_recorded:
                    state.set_run_status(correlation.run_id, "failed")
                LOGGER.error("Timed out creating Brainbase task for Slack event %s", event_id)
                say(
                    text="I couldn’t start the research task yet. Please mention me again shortly.",
                    thread_ts=root_thread_ts,
                )
            except subprocess.CalledProcessError:
                if run_recorded:
                    state.set_run_status(correlation.run_id, "failed")
                LOGGER.error("Brainbase rejected Slack event %s", event_id)
                say(
                    text="I couldn’t start the research task. The team has been notified.",
                    thread_ts=root_thread_ts,
                )
            except Exception:
                if run_recorded:
                    state.set_run_status(correlation.run_id, "failed")
                LOGGER.error("Failed to route Slack event %s", event_id)
                say(
                    text="I couldn’t route that mention. Please try again shortly.",
                    thread_ts=root_thread_ts,
                )

        executor.submit(submit)

    @app.action("hedge.pool.confirm")
    def confirm_virtual_pool(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        ack()
        if action_signing_key is None:
            return
        actions = body.get("actions")
        token = (
            _string(actions[0].get("value"))
            if isinstance(actions, list) and actions and isinstance(actions[0], dict)
            else ""
        )
        run_id = _action_run_id(token)
        correlation = state.correlation_for_run(run_id) if run_id else None
        context = _context_from_correlation(correlation) if correlation else None
        if (
            context is None
            or not approved(context)
            or _action_context_from_body(body, state) != context
        ):
            return
        try:
            validate_action_metadata(
                token,
                signing_key=action_signing_key,
                expected_context=context,
                expected_action="confirm",
            )
            actor_id = _mapping_value(body.get("user"), "id")
            pool, repeated = fund.confirm(context, actor_id)
        except (ValueError, FundConsistencyError, sqlite3.Error, OSError):
            LOGGER.info(
                "Virtual pool confirmation rejected or incomplete", exc_info=True
            )
            return
        state.set_run_status(context.run_id, "completed")
        if not repeated:
            client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                text=f"Created paper-only virtual pool {pool.name} ({pool.pool_id}). The creator's virtual starting balance was recorded. Invited members have no stake until they explicitly join.",
            )

    @app.action("hedge.pool.join_confirm")
    def confirm_virtual_join(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        ack()
        if action_signing_key is None:
            return
        actions = body.get("actions")
        token = (
            _string(actions[0].get("value"))
            if isinstance(actions, list) and actions and isinstance(actions[0], dict)
            else ""
        )
        run_id = _action_run_id(token)
        correlation = state.correlation_for_run(run_id) if run_id else None
        context = _context_from_correlation(correlation) if correlation else None
        if (
            context is None
            or not approved(context)
            or _action_context_from_body(body, state) != context
        ):
            return
        try:
            validate_action_metadata(
                token,
                signing_key=action_signing_key,
                expected_context=context,
                expected_action="confirm",
            )
            actor_id = _mapping_value(body.get("user"), "id")
            pool_id, cents, repeated = fund.confirm_join(context, actor_id)
        except (ValueError, FundConsistencyError, sqlite3.Error, OSError):
            LOGGER.info("Virtual pool join rejected or incomplete", exc_info=True)
            return
        state.set_run_status(context.run_id, "completed")
        if not repeated:
            currency = fund.fund.pools.pool(pool_id).base_currency
            client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                text=f"Joined paper-only virtual pool {pool_id} with a recorded virtual contribution of {format_cents(cents, currency)}. No payment was made.",
            )

    for route in ("portfolio", "my_stake", "positions"):

        def navigation(
            ack: Callable[..., Any],
            body: dict[str, Any],
            client: Any,
            *,
            route: str = route,
        ) -> None:
            ack()
            if action_signing_key is None:
                return
            actions = body.get("actions")
            token = (
                _string(actions[0].get("value"))
                if isinstance(actions, list)
                and actions
                and isinstance(actions[0], dict)
                else ""
            )
            run_id = _action_run_id(token)
            correlation = state.correlation_for_run(run_id) if run_id else None
            context = _context_from_correlation(correlation) if correlation else None
            if (
                context is None
                or not approved(context)
                or _action_context_from_body(body, state) != context
            ):
                return
            try:
                validate_action_metadata(
                    token,
                    signing_key=action_signing_key,
                    expected_context=context,
                    expected_action=route,
                )
                actor_id = _mapping_value(body.get("user"), "id")
                blocks = _portfolio_card(
                    route, context, actor_id, fund, action_signing_key
                )
            except (ValueError, FundConsistencyError, sqlite3.Error, OSError):
                LOGGER.info(
                    "Rejected or unavailable local portfolio navigation", exc_info=True
                )
                return
            client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                text="Paper-only portfolio records.",
                blocks=blocks,
            )

        app.action("hedge.portfolio." + route)(navigation)

    @app.action("hedge.budget_split.open")
    def open_budget_split(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        """Validate a root-thread launch button, then open its signed modal."""

        ack()
        if action_signing_key is None:
            return
        actions = body.get("actions")
        action = (
            actions[0]
            if isinstance(actions, list) and actions and isinstance(actions[0], dict)
            else {}
        )
        context = _action_context_from_body(body, state)
        if context is None or not approved(context):
            return
        try:
            validate_action_metadata(
                _string(action.get("value")),
                signing_key=action_signing_key,
                expected_context=context,
                expected_action="launch",
            )
            trigger_id = _string(body.get("trigger_id"))
            if not trigger_id:
                raise ValueError("missing trigger ID")
            client.views_open(
                trigger_id=trigger_id,
                view=build_budget_split_modal(context, action_signing_key),
            )
        except ValueError:
            # Invalid/stale actions must not open a view or select a destination.
            LOGGER.info("Rejected invalid budget-split launch action")

    @app.view("hedge.budget_split.modal")
    def preview_budget_split(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        """Validate one submitted modal and send a review card to its root thread."""

        view = body.get("view") if isinstance(body.get("view"), dict) else {}
        if action_signing_key is None:
            ack(response_action="errors", errors={"total": BUDGET_SPLIT_UNAVAILABLE})
            return
        run_id = _action_run_id(view.get("private_metadata"))
        correlation = state.correlation_for_run(run_id) if run_id else None
        if correlation is None:
            ack(
                response_action="errors",
                errors={
                    "total": "This planner session is no longer valid. Open a new planner from the thread."
                },
            )
            return
        context = _context_from_correlation(correlation)
        # A modal has no channel/thread payload. Its private metadata is HMAC
        # checked against the durable canonical context, while its workspace is
        # checked against the signed Slack submission envelope when supplied.
        body_team_id = _mapping_value(body.get("team"), "id") or _string(
            body.get("team_id")
        )
        if not approved(context) or body_team_id != context.workspace_id:
            ack(
                response_action="errors",
                errors={
                    "total": "This planner session is not valid for this workspace."
                },
            )
            return
        try:
            validate_action_metadata(
                _string(view.get("private_metadata")),
                signing_key=action_signing_key,
                expected_context=context,
                expected_action="modal",
            )
            preview = allocate_budget(parse_budget_split_form(_modal_values(view)))
        except ValueError as error:
            ack(response_action="errors", errors={"total": str(error)})
            return
        with ui_lock:
            previews[context.run_id] = preview
            finalized_runs.discard(context.run_id)
        ack()
        try:
            client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                text="Review the virtual budget split in this thread.",
                blocks=build_allocation_preview_blocks(
                    preview, context, action_signing_key
                ),
            )
        except Exception:
            LOGGER.exception("Could not post budget-split preview")

    def finalize_budget_split(
        ack: Callable[..., Any], body: dict[str, Any], client: Any, *, action_name: str
    ) -> None:
        """Post exactly one safe final plan/cancel result in the canonical thread."""

        ack()
        if action_signing_key is None:
            return
        actions = body.get("actions")
        action = (
            actions[0]
            if isinstance(actions, list) and actions and isinstance(actions[0], dict)
            else {}
        )
        token = _string(action.get("value"))
        run_id = _action_run_id(token)
        correlation = state.correlation_for_run(run_id) if run_id else None
        if correlation is None:
            return
        context = _context_from_correlation(correlation)
        actual_context = _action_context_from_body(body, state)
        if actual_context != context or not approved(context):
            return
        try:
            validate_action_metadata(
                token,
                signing_key=action_signing_key,
                expected_context=context,
                expected_action=action_name,
            )
        except ValueError:
            LOGGER.info("Rejected invalid budget-split %s action", action_name)
            return
        with ui_lock:
            preview = previews.get(context.run_id)
            if preview is None or context.run_id in finalized_runs:
                return
            finalized_runs.add(context.run_id)
        blocks = (
            build_confirm_result_blocks(preview)
            if action_name == "confirm"
            else build_cancel_result_blocks(preview.split.group_name)
        )
        try:
            client.chat_postMessage(
                channel=context.channel_id,
                thread_ts=context.thread_ts,
                text=(
                    "Recorded virtual budget-split plan."
                    if action_name == "confirm"
                    else "Cancelled virtual budget-split plan."
                ),
                blocks=blocks,
            )
        except Exception:
            # Do not retry a possibly accepted Slack post; users can create a
            # fresh virtual plan. This mirrors durable delivery fail-closed UX.
            LOGGER.exception("Could not post budget-split %s result", action_name)

    @app.action("hedge.budget_split.confirm")
    def confirm_budget_split(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        finalize_budget_split(ack, body, client, action_name="confirm")

    @app.action("hedge.budget_split.cancel")
    def cancel_budget_split(
        ack: Callable[..., Any], body: dict[str, Any], client: Any
    ) -> None:
        finalize_budget_split(ack, body, client, action_name="cancel")

    app.hedge_task_monitor = task_monitor
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HEDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = build_app()
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN is required to run the Socket Mode bridge.")
    app.hedge_task_monitor.start()  # resume registered tasks after restart
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
