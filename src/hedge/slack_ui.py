"""Safe, dependency-free Slack Block Kit UI for virtual budget split planning.

This module only builds and validates data.  A trusted Slack bridge owns Slack
I/O and must verify Slack signatures separately.  It never creates a payment,
collects money, or executes a trade.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping, Sequence

MAX_MEMBERS = 20
MAX_NAME_CHARS = 80
MAX_GROUP_NAME_CHARS = 80
MAX_IDENTIFIER_CHARS = 200
MAX_FORM_TEXT_CHARS = 3_000
MAX_NUMERIC_CHARS = 64
MAX_TOTAL_CENTS = 100_000_000_000  # $1,000,000,000.00 equivalent
MAX_WEIGHT = Decimal("1000000")
_ACTION_PREFIX = "hedge.budget_split."
_METADATA_VERSION = "hedge-ui-v1"
_LEGACY_METADATA_VERSIONS = frozenset({_METADATA_VERSION, "hedge-budget-split-v1"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ALLOWED_ACTIONS = frozenset({"launch", "modal", "preview", "confirm", "cancel", "portfolio", "my_stake", "positions"})
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class SlackThreadContext:
    """Canonical inbound thread affinity, supplied by trusted bridge code only."""

    workspace_id: str
    channel_id: str
    thread_ts: str
    run_id: str

    def __post_init__(self) -> None:
        for field in ("workspace_id", "channel_id", "thread_ts", "run_id"):
            _validate_identifier(field, getattr(self, field))

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class BudgetMember:
    """One named planner.  A missing weight is treated as weight one if weighted."""

    name: str
    weight: Decimal | str | int | float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_text("member name", self.name, MAX_NAME_CHARS))
        if self.weight is not None:
            object.__setattr__(self, "weight", _parse_weight(self.weight))

    def to_dict(self) -> dict[str, str | None]:
        return {"name": self.name, "weight": None if self.weight is None else format(self.weight, "f")}


@dataclass(frozen=True)
class BudgetSplit:
    """Immutable, validated planning input.  Amounts are restricted to whole cents."""

    total: Decimal | str | int | float
    currency: str
    group_name: str
    members: Sequence[BudgetMember]

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", _parse_total(self.total))
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ValueError("currency must be an uppercase ISO-4217-style three-letter code")
        object.__setattr__(self, "group_name", _validate_text("group name", self.group_name, MAX_GROUP_NAME_CHARS))
        if isinstance(self.members, (str, bytes)):
            raise ValueError("members must be a sequence of BudgetMember values")
        members = tuple(self.members)
        if not 1 <= len(members) <= MAX_MEMBERS:
            raise ValueError(f"member count must be between 1 and {MAX_MEMBERS}")
        if any(not isinstance(member, BudgetMember) for member in members):
            raise ValueError("members must contain only BudgetMember values")
        if len({member.name.casefold() for member in members}) != len(members):
            raise ValueError("member names must be unique")
        object.__setattr__(self, "members", members)

    @property
    def total_cents(self) -> int:
        return int((self.total * 100).to_integral_exact())

    @property
    def is_weighted(self) -> bool:
        return any(member.weight is not None for member in self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": format(self.total, ".2f"),
            "currency": self.currency,
            "group_name": self.group_name,
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class Allocation:
    member_name: str
    cents: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int) or self.cents < 0:
            raise ValueError("allocation cents must be a non-negative integer")
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ValueError("invalid currency")

    @property
    def amount(self) -> Decimal:
        return Decimal(self.cents) / 100

    def to_dict(self) -> dict[str, str | int]:
        return {"member_name": self.member_name, "cents": self.cents, "amount": format(self.amount, ".2f"), "currency": self.currency}


@dataclass(frozen=True)
class BudgetSplitPreview:
    split: BudgetSplit
    allocations: tuple[Allocation, ...]

    def __post_init__(self) -> None:
        if len(self.allocations) != len(self.split.members):
            raise ValueError("allocations must match members")
        if sum(item.cents for item in self.allocations) != self.split.total_cents:
            raise ValueError("allocations must sum exactly to total cents")

    @property
    def total_cents(self) -> int:
        return self.split.total_cents

    def to_dict(self) -> dict[str, Any]:
        return {"split": self.split.to_dict(), "allocations": [item.to_dict() for item in self.allocations]}


@dataclass(frozen=True)
class ActionMetadata:
    """Validated action intent bound to a canonical inbound Slack context."""

    action: str
    context: SlackThreadContext

    def __post_init__(self) -> None:
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("action is not approved")

    def to_dict(self) -> dict[str, Any]:
        return {"v": _METADATA_VERSION, "a": self.action, "c": self.context.to_dict()}


def _validate_identifier(name: str, value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a short opaque identifier")
    return value


def _validate_text(name: str, value: object, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    if not value or len(value) > maximum or value != value.strip():
        raise ValueError(f"{name} must be non-empty, trimmed text up to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError(f"{field} must be a finite number")
    if isinstance(value, str) and (not value or len(value) > MAX_NUMERIC_CHARS):
        raise ValueError(f"{field} must be a short finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _parse_total(value: Decimal | str | int | float) -> Decimal:
    parsed = _decimal(value, "total")
    if parsed <= 0 or parsed > Decimal(MAX_TOTAL_CENTS) / 100:
        raise ValueError("total is outside the allowed planning range")
    if parsed.quantize(_CENT) != parsed:
        raise ValueError("total must use no more than two decimal places")
    return parsed.quantize(_CENT)


def _parse_weight(value: Decimal | str | int | float) -> Decimal:
    parsed = _decimal(value, "weight")
    if parsed <= 0 or parsed > MAX_WEIGHT:
        raise ValueError("weight is outside the allowed range")
    return parsed


def escape_slack_text(value: str) -> str:
    """Escape untrusted text before placing it in a Slack mrkdwn text object."""

    if not isinstance(value, str):
        raise ValueError("Slack text must be a string")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(kind: str, text: str, *, emoji: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": kind, "text": text}
    if emoji is not None:
        result["emoji"] = emoji
    return result


def _metadata_key(signing_key: bytes | str) -> bytes:
    if isinstance(signing_key, str):
        signing_key = signing_key.encode("utf-8")
    if not isinstance(signing_key, bytes) or len(signing_key) < 16:
        raise ValueError("signing_key must contain at least 16 bytes")
    return signing_key


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("malformed action metadata")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as error:
        raise ValueError("malformed action metadata") from error


def sign_action_metadata(action: str, context: SlackThreadContext, signing_key: bytes | str) -> str:
    """Create a compact signed token.  No destination can be supplied separately."""

    metadata = ActionMetadata(action, context)
    payload = json.dumps(metadata.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded = _b64encode(payload)
    signature = hmac.new(_metadata_key(signing_key), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def validate_action_metadata(
    token: str, *, signing_key: bytes | str, expected_context: SlackThreadContext, expected_action: str | None = None
) -> ActionMetadata:
    """Verify a token and require its context to equal trusted inbound state.

    Callers must use ``expected_context`` from their durable run/thread record,
    never from interactive payload fields or model output.
    """

    if not isinstance(token, str) or token.count(".") != 1:
        raise ValueError("malformed action metadata")
    encoded, supplied_signature = token.split(".")
    key = _metadata_key(signing_key)
    expected_signature = _b64encode(hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("invalid action metadata signature")
    try:
        raw = json.loads(_b64decode(encoded).decode("utf-8"))
        context_raw = raw["c"]
        parsed = ActionMetadata(
            str(raw["a"]),
            SlackThreadContext(
                workspace_id=context_raw["workspace_id"], channel_id=context_raw["channel_id"],
                thread_ts=context_raw["thread_ts"], run_id=context_raw["run_id"],
            ),
        )
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("malformed action metadata") from error
    if raw.get("v") not in _LEGACY_METADATA_VERSIONS or parsed.context != expected_context:
        raise ValueError("action metadata context does not match canonical context")
    if expected_action is not None and parsed.action != expected_action:
        raise ValueError("unexpected action")
    return parsed


def parse_budget_split_form(values: Mapping[str, object]) -> BudgetSplit:
    """Parse trusted modal values after Slack request-signature verification.

    Expected values are ``total``, ``currency``, ``group_name``, ``member_names``
    and optional ``weights``. Member names and weights are newline-separated;
    blank weight lines mean weight one when any other weight is supplied.
    """

    if not isinstance(values, Mapping):
        raise ValueError("form values must be a mapping")
    if any(isinstance(value, str) and len(value) > MAX_FORM_TEXT_CHARS for value in values.values()):
        raise ValueError("form value exceeds the allowed size")
    allowed = {"total", "currency", "group_name", "member_names", "weights"}
    if set(values) - allowed:
        raise ValueError("unexpected form value")
    required = {"total", "currency", "group_name", "member_names"}
    if not required <= set(values):
        raise ValueError("missing form value")
    total, currency, group_name, names_value = (values[name] for name in ("total", "currency", "group_name", "member_names"))
    if not isinstance(names_value, str):
        raise ValueError("member_names must be text")
    names = names_value.splitlines()
    if not names or any(not name.strip() or name != name.strip() for name in names):
        raise ValueError("member names must be one trimmed name per line")
    weights_value = values.get("weights", "")
    if not isinstance(weights_value, str):
        raise ValueError("weights must be text")
    weights = weights_value.splitlines() if weights_value else []
    if len(weights) > len(names):
        raise ValueError("weights must not exceed member count")
    weights += [""] * (len(names) - len(weights))
    members = tuple(BudgetMember(name, weight or None) for name, weight in zip(names, weights, strict=True))
    return BudgetSplit(total=total, currency=currency, group_name=group_name, members=members)


def allocate_budget(split: BudgetSplit) -> BudgetSplitPreview:
    """Allocate cents exactly, with stable member-order ties for leftover cents."""

    if not isinstance(split, BudgetSplit):
        raise ValueError("split must be a BudgetSplit")
    total = split.total_cents
    weights = tuple(member.weight if member.weight is not None else Decimal(1) for member in split.members)
    if not split.is_weighted:
        cents, remainder = divmod(total, len(split.members))
        portions = [cents + (1 if index < remainder else 0) for index in range(len(split.members))]
    else:
        weight_total = sum(weights)
        exact = [Decimal(total) * weight / weight_total for weight in weights]
        portions = [int(value.to_integral_value(rounding=ROUND_DOWN)) for value in exact]
        remaining = total - sum(portions)
        # Sort by remainder descending, retaining member order for exact ties.
        order = sorted(range(len(portions)), key=lambda index: (-(exact[index] - portions[index]), index))
        for index in order[:remaining]:
            portions[index] += 1
    allocations = tuple(Allocation(member.name, cents, split.currency) for member, cents in zip(split.members, portions, strict=True))
    return BudgetSplitPreview(split, allocations)


def build_budget_split_launch_card(context: SlackThreadContext, signing_key: bytes | str) -> list[dict[str, Any]]:
    """Build an in-thread launch card for a virtual contribution plan."""

    return [
        {"type": "header", "text": _text("plain_text", "Plan a budget split", emoji=True)},
        {"type": "section", "text": _text("mrkdwn", "Create a *virtual planning and contribution* split for a group. This tool does *not* collect payments or execute trades.")},
        {"type": "actions", "elements": [{
            "type": "button", "action_id": _ACTION_PREFIX + "open", "text": _text("plain_text", "Create budget split", emoji=True),
            "style": "primary", "value": sign_action_metadata("launch", context, signing_key),
        }]},
        {"type": "context", "elements": [_text("mrkdwn", "You will review a cents-exact preview before confirming the plan.")]},
    ]


def build_budget_split_modal(context: SlackThreadContext, signing_key: bytes | str) -> dict[str, Any]:
    """Build the data-entry modal. Private metadata is a signed context token."""

    return {
        "type": "modal", "callback_id": _ACTION_PREFIX + "modal", "private_metadata": sign_action_metadata("modal", context, signing_key),
        "title": _text("plain_text", "Budget split", emoji=True), "submit": _text("plain_text", "Preview split", emoji=True),
        "close": _text("plain_text", "Cancel", emoji=True),
        "blocks": [
            {"type": "section", "text": _text("mrkdwn", "This is a *virtual planning and contribution* tool only. It does not collect payments or execute trades.")},
            _input_block("total", "Total budget", "e.g. 120.00", "120.00"),
            _input_block("currency", "Currency", "e.g. USD", "USD"),
            _input_block("group_name", "Group name", "e.g. Cabin weekend", "Cabin weekend"),
            _input_block("member_names", "Member names", "One trimmed name per line", "Avery\nBlair", multiline=True),
            _input_block("weights", "Optional weights", "One positive weight per line; blank means 1", "1\n1", multiline=True, optional=True),
        ],
    }


def _input_block(block_id: str, label: str, placeholder: str, initial: str, *, multiline: bool = False, optional: bool = False) -> dict[str, Any]:
    return {"type": "input", "block_id": block_id, "optional": optional, "label": _text("plain_text", label, emoji=True), "element": {
        "type": "plain_text_input", "action_id": "value", "multiline": multiline,
        "placeholder": _text("plain_text", placeholder, emoji=True), "initial_value": initial,
    }}


def build_allocation_preview_blocks(
    preview: BudgetSplitPreview, context: SlackThreadContext, signing_key: bytes | str
) -> list[dict[str, Any]]:
    """Build review blocks with confirm/cancel actions bound to the same thread/run."""

    if not isinstance(preview, BudgetSplitPreview):
        raise ValueError("preview must be a BudgetSplitPreview")
    lines = "\n".join(
        f"• {escape_slack_text(item.member_name)} — *{item.currency} {item.amount:.2f}*" for item in preview.allocations
    )
    mode = "weighted" if preview.split.is_weighted else "equal"
    return [
        {"type": "header", "text": _text("plain_text", "Review budget split", emoji=True)},
        {"type": "section", "text": _text("mrkdwn", f"*{escape_slack_text(preview.split.group_name)}* · {mode.title()} allocation · Total: *{preview.split.currency} {preview.split.total:.2f}*")},
        {"type": "section", "text": _text("mrkdwn", lines)},
        {"type": "context", "elements": [_text("mrkdwn", "Virtual planning/contribution record only. No payment collection. No trade execution.")]},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": _ACTION_PREFIX + "confirm", "style": "primary", "text": _text("plain_text", "Confirm plan", emoji=True), "value": sign_action_metadata("confirm", context, signing_key)},
            {"type": "button", "action_id": _ACTION_PREFIX + "cancel", "text": _text("plain_text", "Cancel", emoji=True), "value": sign_action_metadata("cancel", context, signing_key)},
        ]},
    ]


def build_confirm_result_blocks(preview: BudgetSplitPreview) -> list[dict[str, Any]]:
    """Return a safe, non-transactional confirmation message."""

    return [
        {"type": "section", "text": _text("mrkdwn", f":white_check_mark: Recorded the *virtual* budget-split plan for *{escape_slack_text(preview.split.group_name)}*.")},
        {"type": "context", "elements": [_text("mrkdwn", "This confirms planning only. No payment was collected and no trade was executed.")]},
    ]


def build_cancel_result_blocks(group_name: str) -> list[dict[str, Any]]:
    """Return a safe cancellation message without retaining a transaction state."""

    clean_name = _validate_text("group name", group_name, MAX_GROUP_NAME_CHARS)
    return [
        {"type": "section", "text": _text("mrkdwn", f":no_entry: Cancelled the virtual budget-split plan for *{escape_slack_text(clean_name)}*.")},
        {"type": "context", "elements": [_text("mrkdwn", "No payment was collected and no trade was executed.")]},
    ]


# Paper-portfolio presentation -------------------------------------------------
# These builders are deliberately read-only.  They turn already-reviewed,
# integer-cent snapshots into Slack blocks; they do not calculate prices, take
# contributions, or create orders.
_PORTFOLIO_ACTION_PREFIX = "hedge.portfolio."
MAX_DISPLAY_NAME_CHARS = 80
MAX_RISK_CHARS = 40
MAX_CENTS = MAX_TOTAL_CENTS
MAX_ABSOLUTE_BPS = 1_000_000  # +/- 10,000.00%; rejects accidental unit changes.
_BPS_DENOMINATOR = Decimal(10_000)
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,14}$")


def _parse_cents(value: object, field: str, *, signed: bool = False) -> int:
    """Accept only bounded integer cents; floats and decimal dollars are unsafe."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer number of cents")
    lower = -MAX_CENTS if signed else 0
    if not lower <= value <= MAX_CENTS:
        raise ValueError(f"{field} is outside the allowed range")
    return value


def _parse_bps(value: object, field: str, *, signed: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer number of basis points")
    lower, upper = (-MAX_ABSOLUTE_BPS, MAX_ABSOLUTE_BPS) if signed else (0, 10_000)
    if not lower <= value <= upper:
        raise ValueError(f"{field} is outside the allowed range")
    return value


def _parse_currency(value: object) -> str:
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError("currency must be an uppercase ISO-4217-style three-letter code")
    return value


def format_cents(cents: int, currency: str, *, signed: bool = False) -> str:
    """Format validated integer cents without accepting ambiguous dollar inputs."""

    amount = _parse_cents(cents, "cents", signed=signed)
    code = _parse_currency(currency)
    sign = "+" if signed and amount > 0 else "-" if amount < 0 else ""
    absolute = abs(amount)
    return f"{sign}{code} {absolute // 100:,}.{absolute % 100:02d}"


def format_basis_points(bps: int, *, signed: bool = False) -> str:
    """Format integer basis points as a bounded, locale-independent percentage."""

    value = _parse_bps(bps, "basis points", signed=signed)
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}%"


@dataclass(frozen=True)
class FundDashboard:
    """A read-only paper-fund summary. Monetary values are integer cents."""

    fund_name: str
    nav_cents: int
    return_bps: int
    cash_cents: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fund_name", _validate_text("fund name", self.fund_name, MAX_DISPLAY_NAME_CHARS))
        object.__setattr__(self, "nav_cents", _parse_cents(self.nav_cents, "NAV cents"))
        object.__setattr__(self, "return_bps", _parse_bps(self.return_bps, "return basis points", signed=True))
        object.__setattr__(self, "cash_cents", _parse_cents(self.cash_cents, "cash cents"))
        object.__setattr__(self, "currency", _parse_currency(self.currency))

    def to_dict(self) -> dict[str, str | int]:
        return {
            "fund_name": self.fund_name, "nav_cents": self.nav_cents,
            "return_bps": self.return_bps, "cash_cents": self.cash_cents,
            "currency": self.currency,
        }


# The long name is useful at the bridge boundary and keeps the fund model clear.
PortfolioDashboard = FundDashboard


@dataclass(frozen=True)
class PositionSnapshot:
    """One read-only paper position. Allocation is in basis points of the NAV."""

    ticker: str
    display_name: str
    market_value_cents: int
    pnl_cents: int
    allocation_bps: int
    risk: str
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not _TICKER.fullmatch(self.ticker):
            raise ValueError("ticker must be an uppercase market-style symbol up to 15 characters")
        object.__setattr__(self, "display_name", _validate_text("position name", self.display_name, MAX_DISPLAY_NAME_CHARS))
        object.__setattr__(self, "market_value_cents", _parse_cents(self.market_value_cents, "market value cents"))
        object.__setattr__(self, "pnl_cents", _parse_cents(self.pnl_cents, "P&L cents", signed=True))
        object.__setattr__(self, "allocation_bps", _parse_bps(self.allocation_bps, "allocation basis points"))
        object.__setattr__(self, "risk", _validate_text("risk", self.risk, MAX_RISK_CHARS))
        object.__setattr__(self, "currency", _parse_currency(self.currency))

    def to_dict(self) -> dict[str, str | int]:
        return {
            "ticker": self.ticker, "display_name": self.display_name,
            "market_value_cents": self.market_value_cents, "pnl_cents": self.pnl_cents,
            "allocation_bps": self.allocation_bps, "risk": self.risk, "currency": self.currency,
        }


# A concise compatibility name for callers that treat the model as a card input.
PositionCard = PositionSnapshot


@dataclass(frozen=True)
class MemberStake:
    """A member's virtual, non-custodial paper-fund stake."""

    member_name: str
    virtual_contribution_cents: int
    ownership_bps: int
    current_value_cents: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_name", _validate_text("member name", self.member_name, MAX_DISPLAY_NAME_CHARS))
        object.__setattr__(self, "virtual_contribution_cents", _parse_cents(self.virtual_contribution_cents, "virtual contribution cents"))
        object.__setattr__(self, "ownership_bps", _parse_bps(self.ownership_bps, "ownership basis points"))
        object.__setattr__(self, "current_value_cents", _parse_cents(self.current_value_cents, "current value cents"))
        object.__setattr__(self, "currency", _parse_currency(self.currency))

    def to_dict(self) -> dict[str, str | int]:
        return {
            "member_name": self.member_name,
            "virtual_contribution_cents": self.virtual_contribution_cents,
            "ownership_bps": self.ownership_bps,
            "current_value_cents": self.current_value_cents,
            "currency": self.currency,
        }


def _field(label: str, value: str) -> dict[str, str]:
    """Use plain text fields so input text cannot become Slack mrkdwn."""

    return _text("plain_text", f"{label}\n{value}", emoji=True)


def build_portfolio_navigation_actions(
    context: SlackThreadContext, signing_key: bytes | str, *, active: str
) -> dict[str, Any]:
    """Build the only interactive controls: signed, read-only navigation."""

    if not isinstance(context, SlackThreadContext):
        raise ValueError("context must be a SlackThreadContext")
    if active not in {"portfolio", "my_stake", "positions"}:
        raise ValueError("active navigation target is not approved")
    labels = (("portfolio", "Portfolio"), ("my_stake", "My Stake"), ("positions", "Positions"))
    elements: list[dict[str, Any]] = []
    for action, label in labels:
        button: dict[str, Any] = {
            "type": "button",
            "action_id": _PORTFOLIO_ACTION_PREFIX + action,
            "text": _text("plain_text", label, emoji=True),
            "value": sign_action_metadata(action, context, signing_key),
        }
        if action == active:
            button["style"] = "primary"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _paper_only_context() -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [_text("mrkdwn", "Paper portfolio only. Informational snapshot; no funds, orders, or trading.")],
    }


def build_fund_dashboard_card(
    dashboard: FundDashboard, context: SlackThreadContext, signing_key: bytes | str
) -> list[dict[str, Any]]:
    """Build a compact paper-fund NAV card with signed navigation controls."""

    if not isinstance(dashboard, FundDashboard):
        raise ValueError("dashboard must be a FundDashboard")
    return [
        {"type": "header", "text": _text("plain_text", dashboard.fund_name, emoji=True)},
        {"type": "section", "fields": [
            _field("NAV", format_cents(dashboard.nav_cents, dashboard.currency)),
            _field("Return", format_basis_points(dashboard.return_bps, signed=True)),
            _field("Cash", format_cents(dashboard.cash_cents, dashboard.currency)),
        ]},
        build_portfolio_navigation_actions(context, signing_key, active="portfolio"),
        _paper_only_context(),
    ]


# Explicit dashboard spelling for bridges which do not use the shorter fund name.
build_portfolio_dashboard_card = build_fund_dashboard_card


def build_position_card(
    position: PositionSnapshot, context: SlackThreadContext, signing_key: bytes | str
) -> list[dict[str, Any]]:
    """Build one compact paper-position card, never a buy/sell control."""

    if not isinstance(position, PositionSnapshot):
        raise ValueError("position must be a PositionSnapshot")
    return [
        {"type": "header", "text": _text("plain_text", f"{position.ticker} · {position.display_name}", emoji=True)},
        {"type": "section", "fields": [
            _field("Value", format_cents(position.market_value_cents, position.currency)),
            _field("P&L", format_cents(position.pnl_cents, position.currency, signed=True)),
            _field("Allocation", format_basis_points(position.allocation_bps)),
            _field("Risk", position.risk),
        ]},
        build_portfolio_navigation_actions(context, signing_key, active="positions"),
        _paper_only_context(),
    ]


def build_member_stake_card(
    stake: MemberStake, context: SlackThreadContext, signing_key: bytes | str
) -> list[dict[str, Any]]:
    """Build a member's virtual stake card with no payment or transfer action."""

    if not isinstance(stake, MemberStake):
        raise ValueError("stake must be a MemberStake")
    return [
        {"type": "header", "text": _text("plain_text", f"{stake.member_name}'s virtual stake", emoji=True)},
        {"type": "section", "fields": [
            _field("Virtual contribution", format_cents(stake.virtual_contribution_cents, stake.currency)),
            _field("Ownership", format_basis_points(stake.ownership_bps)),
            _field("Current value", format_cents(stake.current_value_cents, stake.currency)),
        ]},
        build_portfolio_navigation_actions(context, signing_key, active="my_stake"),
        _paper_only_context(),
    ]


__all__ = [
    "ActionMetadata", "Allocation", "BudgetMember", "BudgetSplit", "BudgetSplitPreview", "MAX_FORM_TEXT_CHARS", "MAX_MEMBERS", "SlackThreadContext",
    "allocate_budget", "build_allocation_preview_blocks", "build_budget_split_launch_card", "build_budget_split_modal",
    "build_cancel_result_blocks", "build_confirm_result_blocks", "escape_slack_text", "parse_budget_split_form",
    "sign_action_metadata", "validate_action_metadata",
    "FundDashboard", "PortfolioDashboard", "PositionSnapshot", "PositionCard", "MemberStake",
    "build_fund_dashboard_card", "build_portfolio_dashboard_card", "build_position_card",
    "build_member_stake_card", "build_portfolio_navigation_actions", "format_cents", "format_basis_points",
]
