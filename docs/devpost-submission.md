# Devpost submission copy — Hedge

## Tagline

A Slack-native investment committee that turns group research into reviewed, paper-only portfolio proposals.

## What it does

Hedge brings the investment-club workflow into one Slack thread. A member mentions Hedge with a question such as “research AAPL.” The CIO agent starts Market Scout, Trend Analyst, and News Analyst in parallel, collects typed reports, and passes them to a Portfolio Manager. A Backtester checks the draft, and a terminal Risk Reviewer validates a SHA-256-bound decision attestation before a paper proposal can appear.

For group planning, Hedge also offers a Block Kit budget-split flow. Members can create a virtual pool, specify contributors and optional weights, see a cents-exact preview, and confirm the plan in the original Slack thread. No payments, account creation, transfers, or trades occur.

## Why it matters

Group investing discussions are fast, but the reasoning behind a decision is usually fragmented across chat messages and spreadsheets. Hedge makes the process legible. It keeps the conversation where the group already works, separates responsibilities across specialist agents, stores a durable correlation record for every run, and fails closed when outputs are stale, malformed, duplicated, or associated with the wrong thread.

## How we built it

- **Slack:** a CIO Socket Mode app handles ingress, thread affinity, reactions, and the budget UI. Specialist bot profiles are outbound-only.
- **Brainbase + Kafka:** seven internal agents run a fan-out/fan-in research graph with the Risk Reviewer as the terminal gate.
- **Python:** typed Pydantic-style contracts, durable SQLite state/outbox delivery, virtual pool accounting, paper portfolio storage, dashboards, and tests.
- **Yahoo Finance:** read-only market data for a simple deployment path.

## What makes it safe

Hedge is intentionally paper-only. Live broker submission is disabled. Models cannot select Slack tokens, profile identities, channels, or thread timestamps. Every handoff uses the same `run_id`, workspace, channel, root thread, and role envelope. The Risk Reviewer attests to the exact canonical `hedge.decision.v1` JSON object instead of modifying it.

## Challenges

The hard part was designing multi-agent collaboration without letting a model impersonate a role, redirect a post, or manufacture specialist work. We solved this with a trusted local delivery layer, an immutable role-to-profile map, schema validation at every handoff, parent-hash checks, durable delivery reservations, and timeout behavior that posts a concise partial result instead of inventing a report.

## Accomplishments

- Parallel specialist research with typed fan-in.
- Risk-gated paper proposals with canonical decision hashing.
- Cents-exact virtual budget splits in Slack.
- Durable, idempotent Slack ingestion and delivery.
- Local portfolio dashboard and paper-only data model.
- 192 passing tests plus nine subtests.

## What is next

We will finish the remaining dedicated specialist Slack profiles, add richer source attribution to research reports, and expand the local dashboard into a lightweight virtual-pool cockpit. The paper-only policy and terminal risk gate remain fixed.
