"""Safe, role-bound outbound Slack result delivery.

The sender is injected by the runtime integration.  This module knows profile
names and token *environment variable names* only; it never accepts or logs a
token.  It also does not accept a model-selected destination.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from .slack_state import SlackState, ThreadCorrelation

LOGGER = logging.getLogger(__name__)
MAX_RESULT_CHARS = 40_000


class HedgeRole(StrEnum):
    CIO = "cio"
    MARKET_SCOUT = "market_scout"
    TREND_ANALYST = "trend_analyst"
    NEWS_ANALYST = "news_analyst"
    PORTFOLIO_MANAGER = "portfolio_manager"
    BACKTESTER = "backtester"
    RISK_REVIEWER = "risk_reviewer"


@dataclass(frozen=True)
class SlackProfile:
    """An approved bot identity, represented without a credential value."""

    role: HedgeRole
    display_name: str
    token_env: str


ROLE_PROFILES = MappingProxyType({
    HedgeRole.CIO: SlackProfile(HedgeRole.CIO, "Hedge CIO", "SLACK_BOT_TOKEN"),
    HedgeRole.MARKET_SCOUT: SlackProfile(HedgeRole.MARKET_SCOUT, "Market Scout", "HEDGE_MARKET_SCOUT_BOT_TOKEN"),
    HedgeRole.TREND_ANALYST: SlackProfile(HedgeRole.TREND_ANALYST, "Trend Analyst", "HEDGE_TREND_ANALYST_BOT_TOKEN"),
    HedgeRole.NEWS_ANALYST: SlackProfile(HedgeRole.NEWS_ANALYST, "News Analyst", "HEDGE_NEWS_ANALYST_BOT_TOKEN"),
    HedgeRole.PORTFOLIO_MANAGER: SlackProfile(HedgeRole.PORTFOLIO_MANAGER, "Portfolio Manager", "HEDGE_PORTFOLIO_MANAGER_BOT_TOKEN"),
    HedgeRole.BACKTESTER: SlackProfile(HedgeRole.BACKTESTER, "Backtester", "HEDGE_BACKTESTER_BOT_TOKEN"),
    HedgeRole.RISK_REVIEWER: SlackProfile(HedgeRole.RISK_REVIEWER, "Risk Reviewer", "HEDGE_RISK_REVIEWER_BOT_TOKEN"),
})


def profile_for_role(role: HedgeRole | str) -> SlackProfile:
    """Resolve only one of Hedge's fixed, deployer-approved profiles."""

    try:
        return ROLE_PROFILES[HedgeRole(role)]
    except (ValueError, KeyError) as error:
        raise ValueError("role is not approved for Slack delivery") from error


class SlackSender(Protocol):
    """Runtime adapter. It resolves a profile locally and owns Slack I/O."""

    def send(
        self, *, profile: SlackProfile, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> None: ...


class EnvironmentSlackSender:
    """Production sender that resolves only a fixed profile's env variable.

    Tokens are intentionally read at send time, never included in requests,
    SQLite, exceptions, or log messages.  The deployment process supplies fresh
    rotated values outside this checkout.
    """

    def __init__(self) -> None:
        self._clients: dict[HedgeRole, object] = {}

    def _client_for(self, profile: SlackProfile) -> object:
        cached = self._clients.get(profile.role)
        if cached is not None:
            return cached
        token = os.environ.get(profile.token_env)
        if not token:
            raise RuntimeError("configured Slack profile token is unavailable")
        try:
            from slack_sdk import WebClient
        except ImportError as error:  # pragma: no cover - deployment dependency
            raise RuntimeError("slack_sdk is required for outbound delivery") from error
        client = WebClient(token=token, timeout=10)
        self._clients[profile.role] = client
        return client

    def send(
        self, *, profile: SlackProfile, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> None:
        # Slack chat.postMessage has no idempotency field.  The durable
        # reservation in SlackResultDelivery is the exactly-once boundary.
        client = self._client_for(profile)
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)


@dataclass(frozen=True)
class DeliveryRequest:
    """An untrusted result with the correlation copied from its owning run."""

    delivery_id: str
    role: HedgeRole | str
    correlation: ThreadCorrelation
    text: str


class DeliveryStatus(StrEnum):
    DELIVERED = "delivered"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus

    @property
    def delivered(self) -> bool:
        return self.status is DeliveryStatus.DELIVERED


class SlackResultDelivery:
    """Send results using only durable canonical state and an injected sender."""

    def __init__(self, state: SlackState, sender: SlackSender) -> None:
        self._state = state
        self._sender = sender

    def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        try:
            profile = profile_for_role(request.role)
        except ValueError:
            return DeliveryResult(DeliveryStatus.REJECTED)
        if not isinstance(request.text, str) or not request.text.strip() or len(request.text) > MAX_RESULT_CHARS:
            return DeliveryResult(DeliveryStatus.REJECTED)
        # Do this explicit check before reserving to make a bad correlation a
        # rejection, not a duplicate of a valid result.
        if not self._state.has_canonical_correlation(request.correlation):
            return DeliveryResult(DeliveryStatus.REJECTED)
        if not self._state.reserve_delivery(request.delivery_id, request.correlation, profile.role.value):
            return DeliveryResult(DeliveryStatus.DUPLICATE)
        try:
            self._sender.send(
                profile=profile,
                channel_id=request.correlation.channel_id,
                thread_ts=request.correlation.root_thread_ts,
                text=request.text,
                idempotency_key=request.delivery_id,
            )
        except Exception:
            # Do not include text, channel IDs, or exception values in logs.
            # A timeout may have posted already, so the reserved event stays
            # terminal and no automatic retry can duplicate a result.
            self._state.finish_delivery(request.delivery_id, "failed")
            LOGGER.error("Slack result delivery failed for run %s", request.correlation.run_id)
            return DeliveryResult(DeliveryStatus.FAILED)
        self._state.finish_delivery(request.delivery_id, "delivered")
        return DeliveryResult(DeliveryStatus.DELIVERED)
