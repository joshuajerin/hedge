from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from hedge.slack_ui import (
    BudgetMember,
    BudgetSplit,
    FundDashboard,
    MemberStake,
    PositionSnapshot,
    SlackThreadContext,
    allocate_budget,
    build_allocation_preview_blocks,
    build_budget_split_launch_card,
    build_budget_split_modal,
    build_cancel_result_blocks,
    build_confirm_result_blocks,
    build_fund_dashboard_card,
    build_member_stake_card,
    build_position_card,
    build_portfolio_navigation_actions,
    format_basis_points,
    format_cents,
    parse_budget_split_form,
    sign_action_metadata,
    validate_action_metadata,
)

KEY = b"a-testing-only-signing-key-that-is-long-enough"
CONTEXT = SlackThreadContext(workspace_id="T123", channel_id="C456", thread_ts="171234.5678", run_id="run-abc_123")


def _assert_block_kit_shape(blocks: list[dict[str, object]]) -> None:
    assert isinstance(blocks, list) and blocks
    assert json.loads(json.dumps(blocks)) == blocks
    for block in blocks:
        assert block["type"] in {"header", "section", "actions", "context", "input"}
        if "text" in block:
            text = block["text"]
            assert isinstance(text, dict)
            assert text["type"] in {"plain_text", "mrkdwn"}
            assert isinstance(text["text"], str) and text["text"]
        if block["type"] == "actions":
            assert 1 <= len(block["elements"]) <= 5
            for element in block["elements"]:
                assert element["type"] == "button"
                assert element["action_id"].startswith("hedge.budget_split.")
                assert isinstance(element["value"], str) and len(element["value"]) < 2000
        if block["type"] == "input":
            assert isinstance(block["block_id"], str)
            assert block["element"]["type"] == "plain_text_input"


def test_launch_card_and_modal_are_valid_serializable_block_kit_shapes() -> None:
    launch = build_budget_split_launch_card(CONTEXT, KEY)
    _assert_block_kit_shape(launch)
    assert "virtual planning" in launch[1]["text"]["text"]
    assert "collect payments" in launch[1]["text"]["text"]
    modal = build_budget_split_modal(CONTEXT, KEY)
    assert modal["type"] == "modal"
    assert modal["callback_id"] == "hedge.budget_split.modal"
    assert modal["title"]["type"] == "plain_text"
    _assert_block_kit_shape(modal["blocks"])
    assert [block["block_id"] for block in modal["blocks"] if block["type"] == "input"] == [
        "total", "currency", "group_name", "member_names", "weights"
    ]
    assert validate_action_metadata(modal["private_metadata"], signing_key=KEY, expected_context=CONTEXT, expected_action="modal").context == CONTEXT


def test_equal_allocation_is_deterministic_and_cents_exact() -> None:
    split = BudgetSplit("10.00", "USD", "Camp", (BudgetMember("Avery"), BudgetMember("Blair"), BudgetMember("Casey")))
    preview = allocate_budget(split)
    assert [item.cents for item in preview.allocations] == [334, 333, 333]
    assert sum(item.cents for item in preview.allocations) == split.total_cents == 1000
    assert [item.amount for item in preview.allocations] == [Decimal("3.34"), Decimal("3.33"), Decimal("3.33")]
    assert allocate_budget(split) == preview


def test_weighted_allocation_uses_largest_remainder_and_member_order_ties() -> None:
    split = BudgetSplit(
        "0.10", "USD", "Snack",
        (BudgetMember("Avery", "1"), BudgetMember("Blair", "1"), BudgetMember("Casey", "1")),
    )
    preview = allocate_budget(split)
    assert [item.cents for item in preview.allocations] == [4, 3, 3]
    assert sum(item.cents for item in preview.allocations) == 10
    mixed = allocate_budget(BudgetSplit("10.00", "USD", "Trip", (BudgetMember("Avery", "2"), BudgetMember("Blair"))))
    assert [item.cents for item in mixed.allocations] == [667, 333]


def test_preview_and_result_blocks_escape_text_and_state_non_transactional_scope() -> None:
    split = BudgetSplit("9.99", "USD", "A < B & C", (BudgetMember("A < B"), BudgetMember("C & D")))
    preview = allocate_budget(split)
    blocks = build_allocation_preview_blocks(preview, CONTEXT, KEY)
    _assert_block_kit_shape(blocks)
    rendered = json.dumps(blocks)
    assert "A &lt; B" in rendered and "C &amp; D" in rendered
    assert "No payment collection" in rendered and "No trade execution" in rendered
    assert validate_action_metadata(blocks[-1]["elements"][0]["value"], signing_key=KEY, expected_context=CONTEXT, expected_action="confirm").action == "confirm"
    confirmed = build_confirm_result_blocks(preview)
    cancelled = build_cancel_result_blocks("A < B & C")
    _assert_block_kit_shape(confirmed)
    _assert_block_kit_shape(cancelled)
    assert "No payment was collected" in confirmed[1]["elements"][0]["text"]
    assert "no trade was executed" in cancelled[1]["elements"][0]["text"]


def test_action_metadata_is_signed_bound_and_rejects_tampering_or_wrong_destination() -> None:
    token = sign_action_metadata("confirm", CONTEXT, KEY)
    parsed = validate_action_metadata(token, signing_key=KEY, expected_context=CONTEXT, expected_action="confirm")
    assert parsed.context == CONTEXT
    with pytest.raises(ValueError):
        validate_action_metadata(token + "x", signing_key=KEY, expected_context=CONTEXT)
    with pytest.raises(ValueError):
        validate_action_metadata(token, signing_key=KEY, expected_context=SlackThreadContext("T123", "C999", "171234.5678", "run-abc_123"))
    with pytest.raises(ValueError):
        validate_action_metadata(token, signing_key=KEY, expected_context=CONTEXT, expected_action="cancel")


def test_form_parser_and_immutable_input_models() -> None:
    split = parse_budget_split_form({
        "total": "12.00", "currency": "USD", "group_name": "Cabin", "member_names": "Avery\nBlair", "weights": "2\n",
    })
    assert split.members[0].weight == Decimal("2")
    assert split.members[1].weight is None
    assert isinstance(split.members, tuple)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        split.currency = "EUR"  # type: ignore[misc]
    with pytest.raises(ValueError):
        parse_budget_split_form({"total": "1", "currency": "USD", "group_name": "X", "member_names": "Avery", "extra": "bad"})


@pytest.mark.parametrize("total", ["0", "-1", "1.001", "NaN", "Infinity", float("nan"), float("inf")])
def test_invalid_totals_are_rejected(total: object) -> None:
    with pytest.raises(ValueError):
        BudgetSplit(total, "USD", "Group", (BudgetMember("Avery"),))  # type: ignore[arg-type]


@pytest.mark.parametrize("weight", ["0", "-1", "NaN", "Infinity", float("nan"), float("inf")])
def test_invalid_weights_are_rejected(weight: object) -> None:
    with pytest.raises(ValueError):
        BudgetMember("Avery", weight)  # type: ignore[arg-type]


def test_input_limits_and_identifiers_fail_closed() -> None:
    with pytest.raises(ValueError):
        SlackThreadContext("T 1", "C1", "1.2", "run")
    with pytest.raises(ValueError):
        BudgetSplit("1", "usd", "Group", (BudgetMember("Avery"),))
    with pytest.raises(ValueError):
        BudgetSplit("1", "USD", "Group", tuple(BudgetMember(str(index)) for index in range(21)))
    with pytest.raises(ValueError):
        BudgetMember(" bad")
    with pytest.raises(ValueError):
        parse_budget_split_form({"total": "1", "currency": "USD", "group_name": "Group", "member_names": "A" * 3001})
    with pytest.raises(ValueError):
        BudgetSplit("1" * 65, "USD", "Group", (BudgetMember("Avery"),))



def _assert_paper_portfolio_shape(blocks: list[dict[str, object]]) -> None:
    """Assert the subset of Block Kit used by all read-only portfolio cards."""

    assert isinstance(blocks, list) and len(blocks) == 4
    assert json.loads(json.dumps(blocks)) == blocks
    assert [block["type"] for block in blocks] == ["header", "section", "actions", "context"]
    assert blocks[0]["text"]["type"] == "plain_text"
    fields = blocks[1]["fields"]
    assert 1 <= len(fields) <= 10
    assert all(field["type"] == "plain_text" and field["text"] for field in fields)
    actions = blocks[2]["elements"]
    assert [button["action_id"] for button in actions] == [
        "hedge.portfolio.portfolio", "hedge.portfolio.my_stake", "hedge.portfolio.positions"
    ]
    assert all(button["type"] == "button" and len(button["value"]) < 2_000 for button in actions)
    assert blocks[3]["elements"][0]["text"] == "Paper portfolio only. Informational snapshot; no funds, orders, or trading."


def test_paper_portfolio_cards_are_valid_read_only_block_kit() -> None:
    dashboard = FundDashboard("Hedge Paper Fund", 123_456_789, 127, 12_345, "USD")
    position = PositionSnapshot("AAPL", "Apple Inc.", 50_000, -1_234, 2_500, "Moderate", "USD")
    stake = MemberStake("Avery", 20_000, 1_250, 22_500, "USD")

    dashboard_blocks = build_fund_dashboard_card(dashboard, CONTEXT, KEY)
    position_blocks = build_position_card(position, CONTEXT, KEY)
    stake_blocks = build_member_stake_card(stake, CONTEXT, KEY)
    for blocks in (dashboard_blocks, position_blocks, stake_blocks):
        _assert_paper_portfolio_shape(blocks)
        for button in blocks[2]["elements"]:
            action = button["action_id"].removeprefix("hedge.portfolio.")
            parsed = validate_action_metadata(button["value"], signing_key=KEY, expected_context=CONTEXT, expected_action=action)
            assert parsed.context == CONTEXT

    assert dashboard_blocks[1]["fields"] == [
        {"type": "plain_text", "text": "NAV\nUSD 1,234,567.89", "emoji": True},
        {"type": "plain_text", "text": "Return\n+1.27%", "emoji": True},
        {"type": "plain_text", "text": "Cash\nUSD 123.45", "emoji": True},
    ]
    assert position_blocks[1]["fields"][1]["text"] == "P&L\n-USD 12.34"
    assert stake_blocks[1]["fields"][1]["text"] == "Ownership\n12.50%"


def test_paper_portfolio_text_is_validated_and_never_interpolated_as_mrkdwn() -> None:
    # The only untrusted strings land in plain_text fields/headers, which do
    # not parse Slack mrkdwn. Explicit mrkdwn escaping remains available for
    # the separate legacy preview path.
    dashboard = FundDashboard("Fund < & > *", 0, 0, 0, "USD")
    position = PositionSnapshot("BRK.B", "Name < & > *", 0, 0, 0, "High < & >", "USD")
    stake = MemberStake("Avery < & > *", 0, 0, 0, "USD")
    rendered = json.dumps([
        build_fund_dashboard_card(dashboard, CONTEXT, KEY),
        build_position_card(position, CONTEXT, KEY),
        build_member_stake_card(stake, CONTEXT, KEY),
    ])
    assert "Fund < & > *" in rendered
    assert "Name < & > *" in rendered
    # User input appears only in plain_text objects, not the mrkdwn disclosure.
    assert all(block["type"] != "mrkdwn" for block in build_position_card(position, CONTEXT, KEY)[1]["fields"])
    with pytest.raises(ValueError):
        FundDashboard("Bad\nname", 1, 0, 0, "USD")
    with pytest.raises(ValueError):
        PositionSnapshot("aapl", "Apple", 1, 0, 0, "Low", "USD")
    with pytest.raises(ValueError):
        MemberStake("Avery", 1.0, 0, 0, "USD")  # type: ignore[arg-type]


@pytest.mark.parametrize("factory", [
    lambda: FundDashboard("Fund", -1, 0, 0, "USD"),
    lambda: FundDashboard("Fund", 0, 1.0, 0, "USD"),
    lambda: PositionSnapshot("AAPL", "Apple", 1, 0, 10_001, "Low", "USD"),
    lambda: PositionSnapshot("AAPL", "Apple", 1, 0, 0, "Bad\nrisk", "USD"),
    lambda: MemberStake("Avery", 0, -1, 0, "USD"),
    lambda: MemberStake("Avery", 0, 0, 0, "usd"),
])
def test_paper_portfolio_rejects_ambiguous_or_out_of_range_values(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_cents_percent_and_navigation_helpers_are_strict() -> None:
    assert format_cents(123_456, "USD") == "USD 1,234.56"
    assert format_cents(-1, "USD", signed=True) == "-USD 0.01"
    assert format_basis_points(-5, signed=True) == "-0.05%"
    with pytest.raises(ValueError):
        format_cents(1.0, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        format_basis_points(10_001)
    with pytest.raises(ValueError):
        build_portfolio_navigation_actions(CONTEXT, KEY, active="buy")
