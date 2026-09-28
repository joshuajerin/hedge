#!/bin/zsh
set -euo pipefail

ENV_FILE="${HEDGE_SLACK_ENV_FILE:-$HOME/.hedge/slack.env}"
if [[ -r "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# Never let stale launchd-wide Slack credentials shadow this service's Keychain app.
# The CIO bot and Socket Mode app token must come from the same selected install.
unset SLACK_BOT_TOKEN SLACK_APP_TOKEN

load_keychain_secret() {
  local variable_name="$1"
  local service_name="$2"
  local existing_value="${(P)variable_name:-}"
  if [[ -n "$existing_value" ]]; then
    return
  fi

  local keychain_value
  keychain_value="$(security find-generic-password -a "$USER" -s "$service_name" -w 2>/dev/null || true)"
  if [[ -n "$keychain_value" ]]; then
    typeset -gx "$variable_name=$keychain_value"
  fi
}

# Credentials may be supplied by a private environment file during migration,
# but production values live in the login Keychain.  Neither location is in
# the checkout and this script never prints a credential value.
load_keychain_secret SLACK_BOT_TOKEN hedge.slack.cio.bot
load_keychain_secret SLACK_APP_TOKEN hedge.slack.cio.app
load_keychain_secret HEDGE_MARKET_SCOUT_BOT_TOKEN hedge.slack.market-scout.bot
load_keychain_secret HEDGE_TREND_ANALYST_BOT_TOKEN hedge.slack.trend-analyst.bot
load_keychain_secret HEDGE_NEWS_ANALYST_BOT_TOKEN hedge.slack.news-analyst.bot
load_keychain_secret HEDGE_PORTFOLIO_MANAGER_BOT_TOKEN hedge.slack.portfolio-manager.bot
load_keychain_secret HEDGE_BACKTESTER_BOT_TOKEN hedge.slack.backtester.bot
load_keychain_secret HEDGE_RISK_REVIEWER_BOT_TOKEN hedge.slack.risk-reviewer.bot
load_keychain_secret HEDGE_SLACK_ACTION_SECRET hedge.slack.action-secret

# A source-controlled bridge must never inherit an arbitrary legacy CLI path.
unset HEDGE_BRAINBASE_BIN
: "${HEDGE_CIO_MODEL:=gemini-3.8-flash}"
: "${HEDGE_SLACK_STATE_DB:=$HOME/.hedge/slack-state.sqlite3}"
# Explicit user-approved temporary exception for previously exposed credentials.
# Do not represent them as rotated; remove this switch once rotation is done.
if [[ "${HEDGE_SLACK_CREDENTIALS_ROTATED:-}" != "1" && "${HEDGE_ALLOW_EXPOSED_SLACK_CREDENTIALS:-}" != "1" ]]; then
  print -u2 "Hedge Slack credentials require rotation or explicit exposed-credential override"
  exit 1
fi
: "${HEDGE_APPROVED_CHANNEL_IDS:?set the approved test channel IDs}"
: "${HEDGE_SLACK_ACTION_SECRET:?set a fresh local action HMAC secret}"
exec /Users/joshuajerin/Desktop/jarvis/hedge/.venv/bin/hedge-slack-bridge
