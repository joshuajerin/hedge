from __future__ import annotations

import json
import sys

import pytest
from types import ModuleType

import hedge.slack_bridge as slack_bridge
from hedge.slack_bridge import SlackRequest, _request_from_event, format_cio_task
from hedge.slack_state import SlackState


@pytest.fixture(autouse=True)
def local_fund_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HEDGE_SLACK_FUND_DRAFT_DB", str(tmp_path / "draft.sqlite3"))
    monkeypatch.setenv("HEDGE_PAPER_FUND_DB", str(tmp_path / "fund.sqlite3"))
    monkeypatch.setenv("HEDGE_PAPER_PORTFOLIO_DIR", str(tmp_path / "paper-portfolios"))


class FakeClient:
    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["channel"] == "C1"
        assert kwargs["ts"] == "100.200"
        return {
            "messages": [
                {"user": "U1", "ts": "100.200", "text": "<@B1> investigate"},
                {"bot_id": "B1", "ts": "100.300", "text": "ignore bot reply"},
            ]
        }


def test_request_keeps_root_thread_and_trusted_delivery_correlation() -> None:
    request = _request_from_event(
        FakeClient(),
        "Ev1",
        {"channel": "C1", "user": "U1", "ts": "100.200", "text": "<@B1> investigate"},
        run_id="run-1",
        workspace_id="T1",
    )
    assert request.thread_ts == "100.200"
    assert request.run_id == "run-1"
    assert request.workspace_id == "T1"
    assert request.thread == [
        {"user_id": "U1", "timestamp": "100.200", "text": "<@B1> investigate"}
    ]


def test_cio_task_contains_explicit_thread_affinity_without_credentials() -> None:
    request = SlackRequest(
        event_id="Ev1",
        channel_id="C1",
        user_id="U1",
        user_name="U1",
        message_ts="100.200",
        thread_ts="100.200",
        text="investigate",
        thread=[],
        run_id="run-1",
        workspace_id="T1",
    )
    task = format_cio_task(request)
    payload = json.loads(task.rsplit("\n", 1)[1])
    assert payload["delivery"] == {
        "run_id": "run-1",
        "workspace_id": "T1",
        "channel_id": "C1",
        "root_thread_ts": "100.200",
    }
    assert "x" + "oxb-" not in task


class FakeBridgeClient:
    def __init__(self) -> None:
        self.reactions: list[dict[str, object]] = []
        self.reply_requests: list[dict[str, object]] = []
        self.views: list[dict[str, object]] = []
        self.posts: list[dict[str, object]] = []

    def auth_test(self) -> dict[str, str]:
        return {"user_id": "B1", "team_id": "T1"}

    def reactions_add(self, **kwargs: object) -> None:
        self.reactions.append(kwargs)

    def conversations_replies(self, **kwargs: object) -> dict[str, object]:
        self.reply_requests.append(kwargs)
        return {"messages": [{"user": "U1", "ts": "100.200", "text": "message"}]}

    def views_open(self, **kwargs: object) -> None:
        self.views.append(kwargs)

    def chat_postMessage(self, **kwargs: object) -> None:
        self.posts.append(kwargs)


class FakeBoltApp:
    instance: "FakeBoltApp"

    def __init__(self, *, token: str) -> None:
        assert token == "test-token"
        self.client = FakeBridgeClient()
        self.handlers: dict[str, object] = {}
        FakeBoltApp.instance = self

    def event(self, name: str):
        return self._register(name)

    def action(self, name: str):
        return self._register(name)

    def view(self, name: str):
        return self._register(name)

    def _register(self, name: str):
        def register(handler: object) -> object:
            self.handlers[name] = handler
            return handler

        return register


class InlineExecutor:
    def __init__(self, **kwargs: object) -> None:
        pass

    def submit(self, function: object) -> None:
        function()  # type: ignore[operator]


def _bridge_handler(monkeypatch):
    bolt_module = ModuleType("slack_bolt")
    bolt_module.App = FakeBoltApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt_module)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("HEDGE_APPROVED_WORKSPACE_IDS", "T1")
    monkeypatch.setenv("HEDGE_APPROVED_CHANNEL_IDS", "C1")
    monkeypatch.setenv("HEDGE_SLACK_ACTION_SECRET", "a-test-action-secret")
    monkeypatch.setattr(slack_bridge, "ThreadPoolExecutor", InlineExecutor)
    app = slack_bridge.build_app(state=SlackState(":memory:"))
    return app.handlers["app_mention"], app.client


def test_tier_zero_classifier_only_accepts_safe_complete_messages() -> None:
    assert (
        slack_bridge._tier_zero_reply("Hello Hedge!") == slack_bridge.TIER_ZERO_GREETING
    )
    assert (
        slack_bridge._tier_zero_reply("What is Hedge?")
        == slack_bridge.TIER_ZERO_HEDGE_EXPLAINER
    )
    assert (
        slack_bridge._tier_zero_reply("How does an investing club work?")
        == slack_bridge.TIER_ZERO_CLUB_EXPLAINER
    )
    assert (
        slack_bridge._tier_zero_reply("How does Hedge work with a portfolio?") is None
    )
    assert slack_bridge._tier_zero_reply("Hi Hedge, should we buy AAPL?") is None


def test_tier_zero_reply_is_immediate_idempotent_and_skips_brainbase(
    monkeypatch,
) -> None:
    handler, client = _bridge_handler(monkeypatch)
    brainbase_messages: list[str] = []
    monkeypatch.setattr(
        slack_bridge,
        "create_brainbase_task",
        lambda message: brainbase_messages.append(message),
    )
    replies: list[dict[str, object]] = []
    event = {
        "channel": "C1",
        "user": "U1",
        "ts": "100.200",
        "text": "<@B1> How does Hedge work?",
    }
    body = {"event_id": "Ev-tier-zero", "team_id": "T1"}

    handler(event, body, client, lambda **kwargs: replies.append(kwargs))  # type: ignore[operator]
    handler(event, body, client, lambda **kwargs: replies.append(kwargs))  # type: ignore[operator]

    assert brainbase_messages == []
    assert replies == [
        {"text": slack_bridge.TIER_ZERO_HEDGE_EXPLAINER, "thread_ts": "100.200"}
    ]
    assert client.reply_requests == []
    assert [reaction["name"] for reaction in client.reactions] == ["eyes"]


def test_tier_one_request_invokes_brainbase_once(monkeypatch) -> None:
    handler, client = _bridge_handler(monkeypatch)
    brainbase_messages: list[str] = []
    replies: list[dict[str, object]] = []
    def create_after_ack(message):
        assert len(replies) == 1  # visible before the task-create CLI starts
        brainbase_messages.append(message)
        return json.dumps({
            "task_id": "task-1", "agent_id": slack_bridge.HEDGE_CIO_ID, "status": "running",
        })
    monkeypatch.setattr(slack_bridge, "create_brainbase_task", create_after_ack)
    event = {
        "channel": "C1",
        "user": "U1",
        "ts": "100.200",
        "text": "<@B1> analyze AAPL risk",
    }
    body = {"event_id": "Ev-tier-one", "team_id": "T1"}

    handler(event, body, client, lambda **kwargs: replies.append(kwargs))  # type: ignore[operator]
    handler(event, body, client, lambda **kwargs: replies.append(kwargs))  # type: ignore[operator]

    assert len(brainbase_messages) == 1
    assert "analyze AAPL risk" in brainbase_messages[0]
    assert len(replies) == 1
    assert replies[0]["thread_ts"] == "100.200"
    assert replies[0]["text"] == "I'm starting research."
    assert len(client.reply_requests) == 1
    assert [reaction["name"] for reaction in client.reactions] == ["eyes", "brain"]


def _ui_app(monkeypatch, state: SlackState):
    bolt_module = ModuleType("slack_bolt")
    bolt_module.App = FakeBoltApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt_module)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("HEDGE_APPROVED_WORKSPACE_IDS", "T1")
    monkeypatch.setenv("HEDGE_APPROVED_CHANNEL_IDS", "C1")
    monkeypatch.setenv("HEDGE_SLACK_ACTION_SECRET", "a-test-action-secret")
    monkeypatch.setattr(slack_bridge, "ThreadPoolExecutor", InlineExecutor)
    return slack_bridge.build_app(state=state)


def _launch_budget_card(
    app, client, *, event_id: str = "Ev-budget"
) -> dict[str, object]:
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> create a $500 budget split among people",
        },
        {"event_id": event_id, "team_id": "T1"},
        client,
        lambda **kwargs: replies.append(kwargs),
    )
    assert len(replies) == 1
    return replies[0]


def _action_body(value: str) -> dict[str, object]:
    return {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"thread_ts": "100.200"},
        "trigger_id": "trigger-1",
        "actions": [{"value": value}],
    }


def test_budget_words_launch_local_card_before_brainbase(monkeypatch) -> None:
    app = _ui_app(monkeypatch, SlackState(":memory:"))
    brainbase_messages: list[str] = []
    monkeypatch.setattr(
        slack_bridge,
        "create_brainbase_task",
        lambda message: brainbase_messages.append(message),
    )
    card = _launch_budget_card(app, app.client)

    assert brainbase_messages == []
    assert card["thread_ts"] == "100.200"
    assert card["blocks"][2]["elements"][0]["action_id"] == "hedge.budget_split.open"  # type: ignore[index]
    assert all(
        "Classification" not in str(value) and "HOLD" not in str(value)
        for value in [card]
    )


def test_missing_action_secret_shows_unavailable_budget_ui(monkeypatch) -> None:
    bolt_module = ModuleType("slack_bolt")
    bolt_module.App = FakeBoltApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "slack_bolt", bolt_module)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("HEDGE_APPROVED_WORKSPACE_IDS", "T1")
    monkeypatch.setenv("HEDGE_APPROVED_CHANNEL_IDS", "C1")
    monkeypatch.delenv("HEDGE_SLACK_ACTION_SECRET", raising=False)
    monkeypatch.setattr(slack_bridge, "ThreadPoolExecutor", InlineExecutor)
    app = slack_bridge.build_app(state=SlackState(":memory:"))
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> contribution pool",
        },
        {"event_id": "Ev-no-secret", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    assert replies == [
        {"text": slack_bridge.BUDGET_SPLIT_UNAVAILABLE, "thread_ts": "100.200"}
    ]


def test_budget_actions_validate_open_preview_and_confirm_in_root_thread(
    monkeypatch,
) -> None:
    app = _ui_app(monkeypatch, SlackState(":memory:"))
    card = _launch_budget_card(app, app.client)
    launch_value = card["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    acks: list[dict[str, object]] = []
    app.handlers["hedge.budget_split.open"](
        lambda **kwargs: acks.append(kwargs), _action_body(launch_value), app.client
    )  # type: ignore[operator]
    assert acks == [{}]
    modal = app.client.views[0]["view"]
    modal["state"] = {
        "values": {
            "total": {"value": {"value": "500.00"}},
            "currency": {"value": {"value": "USD"}},
            "group_name": {"value": {"value": "Friends"}},
            "member_names": {"value": {"value": "Avery\nBlair"}},
            "weights": {"value": {"value": ""}},
        }
    }
    app.handlers["hedge.budget_split.modal"](
        lambda **kwargs: acks.append(kwargs),
        {"team": {"id": "T1"}, "view": modal},
        app.client,
    )  # type: ignore[operator]
    preview_post = app.client.posts[0]
    assert preview_post["channel"] == "C1" and preview_post["thread_ts"] == "100.200"
    confirm_value = preview_post["blocks"][-1]["elements"][0]["value"]  # type: ignore[index]
    app.handlers["hedge.budget_split.confirm"](
        lambda **kwargs: acks.append(kwargs), _action_body(confirm_value), app.client
    )  # type: ignore[operator]
    assert app.client.posts[-1]["thread_ts"] == "100.200"
    assert "virtual" in app.client.posts[-1]["text"]


def test_budget_action_rejects_tampering_and_validates_after_restart(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "slack-state.sqlite3"
    state = SlackState(path)
    app = _ui_app(monkeypatch, state)
    card = _launch_budget_card(app, app.client)
    launch_value = card["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    tampered = launch_value[:-1] + ("A" if launch_value[-1] != "A" else "B")
    app.handlers["hedge.budget_split.open"](
        lambda **kwargs: None, _action_body(tampered), app.client
    )  # type: ignore[operator]
    assert app.client.views == []

    # A new bridge process reads the run correlation from SQLite and accepts the
    # original signed launch token, proving no payload-selected destination.
    state.close()
    restarted = _ui_app(monkeypatch, SlackState(path))
    restarted.handlers["hedge.budget_split.open"](
        lambda **kwargs: None, _action_body(launch_value), restarted.client
    )  # type: ignore[operator]
    assert restarted.client.views[0]["view"]["private_metadata"]


def test_virtual_pool_confirmation_persists_creator_and_restart_navigation(
    monkeypatch, tmp_path
) -> None:
    state_path = tmp_path / "slack.sqlite3"
    app = _ui_app(monkeypatch, SlackState(state_path))
    brainbase: list[str] = []
    monkeypatch.setattr(
        slack_bridge, "create_brainbase_task", lambda message: brainbase.append(message)
    )
    replies: list[dict[str, object]] = []
    mention = {
        "channel": "C1",
        "user": "U1",
        "ts": "100.200",
        "text": "<@B1> create virtual pool Friends | USD 500.00 | mandate: Learn index funds | risk: low | members: <@U2>",
    }
    envelope = {"event_id": "Ev-create", "team_id": "T1"}
    app.handlers["app_mention"](
        mention, envelope, app.client, lambda **kwargs: replies.append(kwargs)
    )  # type: ignore[operator]
    assert len(replies) == 1 and "500.00" in str(replies[0]["blocks"])
    from hedge.fund_service import PaperFundService

    with_fund = PaperFundService(tmp_path / "fund.sqlite3")
    assert with_fund.member_pools(member_id="U1") == ()
    with_fund.close()
    token = replies[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    state = SlackState(state_path)
    restarted = _ui_app(monkeypatch, state)
    restarted.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), restarted.client
    )  # type: ignore[operator]
    assert len(restarted.client.posts) == 1
    restarted.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), restarted.client
    )  # type: ignore[operator]
    assert len(restarted.client.posts) == 1
    another_restart = _ui_app(monkeypatch, SlackState(state_path))
    another_restart.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), another_restart.client
    )  # type: ignore[operator]
    assert another_restart.client.posts == []
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    pools = fund.member_pools(member_id="U1")
    assert len(pools) == 1 and pools[0].starting_nav_cents == 50_000
    assert fund.member_pools(member_id="U2") == ()  # invitation is not membership
    history = fund.capital.virtual_contribution_history(pool_id=pools[0].pool_id)
    assert [(entry.member_id, entry.cents, entry.action) for entry in history] == [
        ("U1", 50_000, "VIRTUAL_STARTING_BALANCE")
    ]
    fund.close()
    stake_replies: list[dict[str, object]] = []
    restarted.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U1", "ts": "101.200", "text": "<@B1> my stake"},
        {"event_id": "Ev-stake", "team_id": "T1"},
        restarted.client,
        lambda **kwargs: stake_replies.append(kwargs),
    )
    assert "500.00" in str(stake_replies[0]["blocks"])
    assert "Simulated paper value" in str(stake_replies[0]["blocks"])
    assert len(brainbase) == 0
    navigation = stake_replies[0]["blocks"][2]["elements"][2]["value"]  # type: ignore[index]
    wrong = _action_body(navigation)
    wrong["user"] = {"id": "U2"}
    wrong["message"] = {"thread_ts": "101.200"}
    restarted.handlers["hedge.portfolio.positions"](
        lambda **kwargs: None, wrong, restarted.client
    )  # type: ignore[operator]
    assert "No active virtual pool membership" in str(
        restarted.client.posts[-1]["blocks"]
    )
    own = _action_body(navigation)
    own["message"] = {"thread_ts": "101.200"}
    restarted.handlers["hedge.portfolio.positions"](
        lambda **kwargs: None, own, restarted.client
    )  # type: ignore[operator]
    assert "No persisted paper positions" in str(restarted.client.posts[-1]["blocks"])
    assert "hedge.portfolio.positions" in str(restarted.client.posts[-1]["blocks"])


def test_creation_fails_closed_on_invalid_amount_member_tamper_and_allowlist(
    monkeypatch, tmp_path
) -> None:
    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    replies: list[dict[str, object]] = []
    for index, command in enumerate(
        (
            "create virtual pool Friends | USD 500 | mandate: Learn | risk: low",
            "create virtual pool Friends | USD 500.00 | mandate: Learn | risk: low | members: Avery",
            "create virtual pool Friends | USD 500.00 | mandate: Learn | risk: low | members: <@U1>",
        )
    ):
        app.handlers["app_mention"](  # type: ignore[operator]
            {
                "channel": "C1",
                "user": "U1",
                "ts": f"{index + 200}.200",
                "text": "<@B1> " + command,
            },
            {"event_id": f"Ev-invalid-{index}", "team_id": "T1"},
            app.client,
            lambda **kwargs: replies.append(kwargs),
        )
    assert len(replies) == 3
    assert app.client.posts == []
    valid: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C2", "user": "U1", "ts": "300.200", "text": "<@B1> portfolio"},
        {"event_id": "Ev-deny-channel", "team_id": "T1"},
        app.client,
        lambda **kwargs: valid.append(kwargs),
    )
    app.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U1", "ts": "301.200", "text": "<@B1> portfolio"},
        {"event_id": "Ev-deny-team", "team_id": "T2"},
        app.client,
        lambda **kwargs: valid.append(kwargs),
    )
    assert valid == []
    from hedge.fund_service import PaperFundService

    fund = PaperFundService(tmp_path / "fund.sqlite3")
    assert fund.member_pools(member_id="U1") == ()
    fund.close()


def test_portfolio_empty_routes_never_call_brainbase_or_infer_nav(
    monkeypatch, tmp_path
) -> None:
    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(
        slack_bridge,
        "create_brainbase_task",
        lambda message: (_ for _ in ()).throw(AssertionError(message)),
    )
    for index, route in enumerate((
        "portfolio", "my stake", "positions", "dashboard",
        "hows the dashboard looking like", "what's my dashboard looking like",
    )):
        replies: list[dict[str, object]] = []
        app.handlers["app_mention"](  # type: ignore[operator]
            {
                "channel": "C1",
                "user": "U1",
                "ts": f"{index + 400}.200",
                "text": f"<@B1> {route}",
            },
            {"event_id": f"Ev-empty-{index}", "team_id": "T1"},
            app.client,
            lambda **kwargs: replies.append(kwargs),
        )
        assert len(replies) == 1
        assert "No active virtual pool membership" in str(replies[0]["blocks"])
        assert "NAV" in str(replies[0]["blocks"])
        assert "hedge.portfolio." in str(replies[0]["blocks"])


def test_pool_confirmation_rejects_tampered_token_wrong_actor_and_thread(
    monkeypatch, tmp_path
) -> None:
    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> create virtual pool Friends | USD 100.00 | mandate: Study funds | risk: medium",
        },
        {"event_id": "Ev-confirm-guard", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    token = replies[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    app.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(tampered), app.client
    )  # type: ignore[operator]
    wrong_actor = _action_body(token)
    wrong_actor["user"] = {"id": "U2"}
    app.handlers["hedge.pool.confirm"](lambda **kwargs: None, wrong_actor, app.client)  # type: ignore[operator]
    wrong_thread = _action_body(token)
    wrong_thread["message"] = {"thread_ts": "999.000"}
    app.handlers["hedge.pool.confirm"](lambda **kwargs: None, wrong_thread, app.client)  # type: ignore[operator]
    assert app.client.posts == []
    from hedge.fund_service import PaperFundService

    fund = PaperFundService(tmp_path / "fund.sqlite3")
    assert fund.member_pools(member_id="U1") == ()
    fund.close()


def test_missing_allowlist_rejects_all_inbound_mentions(monkeypatch, tmp_path) -> None:
    _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    monkeypatch.delenv("HEDGE_APPROVED_CHANNEL_IDS")
    blocked = slack_bridge.build_app(state=SlackState(tmp_path / "state3.sqlite3"))
    replies: list[dict[str, object]] = []
    blocked.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U1", "ts": "100.200", "text": "<@B1> portfolio"},
        {"event_id": "Ev-no-allowlist", "team_id": "T1"},
        blocked.client,
        lambda **kwargs: replies.append(kwargs),
    )
    assert replies == []


def test_wildcard_channels_still_require_approved_workspace(monkeypatch, tmp_path) -> None:
    _ui_app(monkeypatch, SlackState(tmp_path / "setup.sqlite3"))
    monkeypatch.setenv("HEDGE_APPROVED_CHANNEL_IDS", "*")
    app = slack_bridge.build_app(state=SlackState(tmp_path / "wildcard.sqlite3"))
    replies: list[dict[str, object]] = []
    for event_id, channel, team in [
        ("Ev-new-channel", "C2", "T1"),
        ("Ev-private-channel", "G3", "T1"),
        ("Ev-wrong-workspace", "C4", "T2"),
    ]:
        app.handlers["app_mention"](  # type: ignore[operator]
            {"channel": channel, "user": "U1", "ts": "100.200", "text": "<@B1> hi"},
            {"event_id": event_id, "team_id": team},
            app.client,
            lambda **kwargs: replies.append(kwargs),
        )
    assert len(replies) == 2
    assert all("thread_ts" in reply for reply in replies)


def test_portfolio_research_question_remains_cio_route(monkeypatch, tmp_path) -> None:
    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    tasks: list[str] = []
    monkeypatch.setattr(
        slack_bridge, "create_brainbase_task", lambda message: (tasks.append(message) or json.dumps({
            "task_id": "task-1", "agent_id": slack_bridge.HEDGE_CIO_ID, "status": "running",
        }))
    )
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> analyze my portfolio risk",
        },
        {"event_id": "Ev-research", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    assert len(tasks) == 1 and "analyze my portfolio risk" in tasks[0]
    assert len(replies) == 1 and replies[0]["thread_ts"] == "100.200"
    assert replies[0]["text"] == "I'm starting research."


def test_persisted_fresh_quote_positions_and_stale_quote_fail_closed(
    monkeypatch, tmp_path
) -> None:
    from datetime import UTC, datetime, timedelta
    from hedge.contracts import Decision
    from hedge.fund_service import PaperFundService
    from hedge.paper_portfolio import PaperPortfolio, PriceQuote

    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> create virtual pool Friends | USD 500.00 | mandate: Study funds | risk: low",
        },
        {"event_id": "Ev-paper", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    token = replies[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    app.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), app.client
    )  # type: ignore[operator]
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    pool = fund.member_pools(member_id="U1")[0]
    fund.close()
    paper = PaperPortfolio(tmp_path / "paper-portfolios" / f"{pool.pool_id}.sqlite3")
    now = datetime.now(UTC)
    decision = Decision.draft(
        pool_id=pool.pool_id,
        mandate_version=1,
        intents=[
            {
                "symbol": "ABC",
                "side": "BUY",
                "quantity": 2,
                "order_type": "MKT",
                "rationale": "paper only",
            }
        ],
    )
    paper.process(decision, {"ABC": PriceQuote(1000, now, "test-fixture")})
    paper.close()
    positions: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U1", "ts": "101.200", "text": "<@B1> positions"},
        {"event_id": "Ev-priced", "team_id": "T1"},
        app.client,
        lambda **kwargs: positions.append(kwargs),
    )
    assert "ABC" in str(positions[0]["blocks"])
    assert "test-fixture" in str(positions[0]["blocks"])
    assert "20.00" in str(positions[0]["blocks"])
    # Use a future read time by replacing the bridge's PaperPortfolio constructor
    # with the real class configured with a deterministic future clock.
    monkeypatch.setattr(
        slack_bridge,
        "PaperPortfolio",
        lambda path: PaperPortfolio(path, clock=lambda: now + timedelta(hours=2)),
    )
    stale: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U1", "ts": "102.200", "text": "<@B1> positions"},
        {"event_id": "Ev-stale", "team_id": "T1"},
        app.client,
        lambda **kwargs: stale.append(kwargs),
    )
    assert "Paper positions unavailable" in str(stale[0]["blocks"])
    assert "20.00" not in str(stale[0]["blocks"])


def test_join_without_paper_cash_reconciliation_never_shows_nav(
    monkeypatch, tmp_path
) -> None:
    from hedge.fund_service import PaperFundService

    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> create virtual pool Friends | USD 100.00 | mandate: Study funds | risk: low | members: <@U2>",
        },
        {"event_id": "Ev-join", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    token = replies[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    app.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), app.client
    )  # type: ignore[operator]
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    pool = fund.member_pools(member_id="U1")[0]
    invitation_id = "invite-" + pool.pool_id.removeprefix("pool-") + "-U2"
    fund.join_with_virtual_contribution(
        invitation_id=invitation_id,
        actor_id="U2",
        contribution_cents=2500,
        event_id="member-U2-contribution",
    )
    fund.close()
    for user, text, eid in (
        ("U1", "portfolio", "Ev-unreconciled"),
        ("U2", "my stake", "Ev-member-stake"),
    ):
        response: list[dict[str, object]] = []
        app.handlers["app_mention"](  # type: ignore[operator]
            {
                "channel": "C1",
                "user": user,
                "ts": f"{eid}.200",
                "text": "<@B1> " + text,
            },
            {"event_id": eid, "team_id": "T1"},
            app.client,
            lambda **kwargs: response.append(kwargs),
        )
        assert len(response) == 1
        assert "unavailable" in str(response[0]["blocks"])
        assert "25.00" in str(response[0]["blocks"]) or user == "U1"
        assert "simulated starting NAV" not in str(response[0]["blocks"]).lower()


def test_invitee_signed_virtual_join_reconciles_paper_cash_after_restart(
    monkeypatch, tmp_path
) -> None:
    from hedge.fund_service import PaperFundService
    from hedge.paper_portfolio import PaperPortfolio

    state_path = tmp_path / "state.sqlite3"
    app = _ui_app(monkeypatch, SlackState(state_path))
    replies: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U1",
            "ts": "100.200",
            "text": "<@B1> create virtual pool Friends | USD 100.00 | mandate: Study funds | risk: low | members: <@U2>",
        },
        {"event_id": "Ev-create-for-join", "team_id": "T1"},
        app.client,
        lambda **kwargs: replies.append(kwargs),
    )
    token = replies[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    app.handlers["hedge.pool.confirm"](
        lambda **kwargs: None, _action_body(token), app.client
    )  # type: ignore[operator]
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    pool = fund.member_pools(member_id="U1")[0]
    assert pool.status.value == "ACTIVE"
    fund.close()
    joined: list[dict[str, object]] = []
    app.handlers["app_mention"](  # type: ignore[operator]
        {
            "channel": "C1",
            "user": "U2",
            "ts": "101.200",
            "text": f"<@B1> join virtual pool {pool.pool_id} | USD 25.00",
        },
        {"event_id": "Ev-join-confirm", "team_id": "T1"},
        app.client,
        lambda **kwargs: joined.append(kwargs),
    )
    assert "25.00" in str(joined[0]["blocks"])
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    assert fund.member_pools(member_id="U2") == ()
    fund.close()
    join_token = joined[0]["blocks"][2]["elements"][0]["value"]  # type: ignore[index]
    body = _action_body(join_token)
    body["message"] = {"thread_ts": "101.200"}
    body["user"] = {"id": "U1"}
    app.handlers["hedge.pool.join_confirm"](lambda **kwargs: None, body, app.client)  # type: ignore[operator]
    assert len(app.client.posts) == 1  # original creator confirmation only
    body["user"] = {"id": "U2"}
    restarted = _ui_app(monkeypatch, SlackState(state_path))
    restarted.handlers["hedge.pool.join_confirm"](
        lambda **kwargs: None, body, restarted.client
    )  # type: ignore[operator]
    assert len(restarted.client.posts) == 1 and "No payment" in str(
        restarted.client.posts[0]
    )
    restarted.handlers["hedge.pool.join_confirm"](
        lambda **kwargs: None, body, restarted.client
    )  # type: ignore[operator]
    assert len(restarted.client.posts) == 1
    fund = PaperFundService(tmp_path / "fund.sqlite3")
    assert len(fund.member_pools(member_id="U2")) == 1
    history = fund.capital.virtual_contribution_history(
        pool_id=pool.pool_id, member_id="U2"
    )
    assert len(history) == 1 and history[0].cents == 2500
    fund.close()
    paper = PaperPortfolio(tmp_path / "paper-portfolios" / f"{pool.pool_id}.sqlite3")
    assert dict(paper.contribution_events(pool.pool_id)) == {history[0].event_id: 2500}
    assert paper.snapshot(pool.pool_id).cash_cents == 12500
    paper.close()
    portfolio: list[dict[str, object]] = []
    restarted.handlers["app_mention"](  # type: ignore[operator]
        {"channel": "C1", "user": "U2", "ts": "102.200", "text": "<@B1> my stake"},
        {"event_id": "Ev-joined-stake", "team_id": "T1"},
        restarted.client,
        lambda **kwargs: portfolio.append(kwargs),
    )
    assert "25.00" in str(portfolio[0]["blocks"])
    assert "Simulated paper value" in str(portfolio[0]["blocks"])
    assert "unavailable" not in str(portfolio[0]["blocks"])


@pytest.mark.parametrize("raw", [
    '{}', '{"task_id":"x","agent_id":"wrong","status":"running"}',
    '{"task_id":"x","agent_id":"4ac44fd1-b386-49aa-8b39-a3ba7037fdc8","status":"unknown"}',
    '{"task_id":"x","agent_id":"4ac44fd1-b386-49aa-8b39-a3ba7037fdc8","status":"running","extra":0}',
    '{"task_id":"x\nsecret","agent_id":"4ac44fd1-b386-49aa-8b39-a3ba7037fdc8","status":"running"}',
])
def test_create_response_fails_closed(raw):
    with pytest.raises(ValueError):
        slack_bridge.parse_task_create_response(raw, expected_agent_id=slack_bridge.HEDGE_CIO_ID)


def test_brainbase_create_register_and_retry_after_restart(monkeypatch, tmp_path):
    path = tmp_path / "durable-state.sqlite3"
    state = SlackState(path)
    app = _ui_app(monkeypatch, state)
    created: list[str] = []
    def create(message):
        created.append(message)
        return json.dumps({"task_id": "task-created", "agent_id": slack_bridge.HEDGE_CIO_ID, "status": "initializing"})
    monkeypatch.setattr(slack_bridge, "create_brainbase_task", create)
    event = {"channel": "C1", "user": "U1", "ts": "100.200", "text": "<@B1> analyze AAPL risk"}
    body = {"event_id": "Ev-durable", "team_id": "T1"}
    posts = []
    app.handlers["app_mention"](event, body, app.client, lambda **kw: posts.append(kw))
    assert len(created) == 1 and len(posts) == 1
    assert posts[0]["thread_ts"] == "100.200"
    task = app.hedge_task_monitor.consumer.db.execute("SELECT * FROM brainbase_tasks").fetchone()
    assert task["task_id"] == "task-created" and task["role"] == "cio"
    assert task["run_id"] == state.current_run_for_thread("T1", "C1", "100.200")
    assert (task["workspace_id"], task["channel_id"], task["root_thread_ts"]) == ("T1", "C1", "100.200")
    # Cloud bootstrap and delegation exceeded the old 90-second limit live.
    from datetime import UTC, datetime
    deadline = datetime.fromisoformat(task["deadline_at"])
    seconds_left = (deadline - datetime.now(UTC)).total_seconds()
    assert 230 <= seconds_left <= 240
    assert posts[0]["text"] == "I'm starting research."
    app.hedge_task_monitor.consumer.close()
    state.close()
    restarted = _ui_app(monkeypatch, SlackState(path))
    restarted.handlers["app_mention"](event, body, restarted.client, lambda **kw: posts.append(kw))
    assert len(created) == 1 and len(posts) == 1
    assert restarted.hedge_task_monitor.consumer.tracked_tasks() == [("task-created", "registered")]
    restarted.hedge_task_monitor.consumer.close()


def test_invalid_create_agent_never_registers_or_logs_payload(monkeypatch, tmp_path, caplog):
    app = _ui_app(monkeypatch, SlackState(tmp_path / "state.sqlite3"))
    marker = "private-request-content"
    monkeypatch.setattr(slack_bridge, "create_brainbase_task", lambda message: json.dumps({
        "task_id": "task-1", "agent_id": "wrong", "status": "running",
    }))
    replies = []
    app.handlers["app_mention"](
        {"channel": "C1", "user": "U1", "ts": "100.200", "text": "<@B1> analyze " + marker},
        {"event_id": "Ev-invalid-agent", "team_id": "T1"}, app.client,
        lambda **kw: replies.append(kw),
    )
    assert replies and app.hedge_task_monitor.consumer.tracked_tasks() == []
    assert marker not in caplog.text
    app.hedge_task_monitor.consumer.close()
