# Hedge orchestration protocol

`hedge.orchestration_protocol` is the paper-safe boundary for a single Brainbase
and Slack research thread. It complements, and does not change,
[`hedge.decision.v1`](decision-contract.md). It uses only Python standard-library
validation and frozen dataclasses.

## Artifacts and order

The permitted chain is:

1. `ResearchRequest` — `role="research_coordinator"`, `status="REQUESTED"`.
2. One or more `SpecialistReport` values — `role="specialist"`, `status="COMPLETED"`.
3. `PortfolioDraft` — `role="portfolio_manager"`, `status="DRAFT"`.
4. `BacktestReport` — `role="backtester"`, `status="COMPLETED"`.
5. `RiskReviewAttestation` — `role="risk_reviewer"`, with terminal
   `status="APPROVED"`, `status="HOLD"`, or `status="REJECTED"`.

Every artifact includes its schema, artifact ID, `run_id`, `workspace_id`,
`channel_id`, `root_thread_ts`, role, `created_at`, `deadline_at`,
`parent_hashes`, and status. Timestamps must be timezone-aware ISO-8601 values.
`deadline_at` cannot precede `created_at`. The parser rejects missing or extra
fields, unrecognized roles/statuses, non-SHA-256 parent hashes, and duplicate
parents.

`validate_handoff_chain(request, reports, draft, backtest, attestation)` checks
that all artifacts have the same run/workspace/channel/root Slack thread and
that each parent hash binds the preceding output:

- reports include the request hash;
- the draft includes every report hash;
- the backtest includes the draft hash; and
- risk review includes the backtest hash.

Use `artifact.sha256()` for these parent values. It is SHA-256 of the canonical
JSON returned by `artifact.as_dict()`.

## Decision binding and release

The risk attestation carries a strict, exact `hedge.decision.v1` object and its
`decision_sha256`. The decision object and every nested intent reject unknown
fields before being passed to the existing `Decision.from_dict` validator. The
hash is SHA-256 over `canonical_json(decision)`: UTF-8 JSON with sorted keys,
compact separators, UTF-8 characters, and no NaN/Infinity.

Create an approval through `RiskReviewAttestation.create(decision=decision.as_dict(),
...)`. HOLD and REJECTED attestations must pass `decision=None`; they cannot
carry a decision object or decision hash. Read `attestation.decision` only after
`status == "APPROVED"`. `PENDING`, `DRAFT`, and any other nonterminal risk
status are rejected. This module does not submit orders or
contain broker credentials.

## Exact Brainbase task prompt integration

Give each Brainbase task this text, replacing the bracketed values with trusted
Slack/task metadata. Do not let the agent choose those values:

```text
You are one stage of the Hedge paper-research handoff. Return exactly one JSON
object for [ARTIFACT_SCHEMA]. Do not return Markdown, executable code, shell
commands, credentials, or any fields not listed by that schema.

Copy these trusted envelope values exactly; never invent or change them:
run_id=[RUN_ID]
workspace_id=[WORKSPACE_ID]
channel_id=[CHANNEL_ID]
root_thread_ts=[ROOT_THREAD_TS]
created_at=[CREATED_AT]
deadline_at=[DEADLINE_AT]
role=[REQUIRED_ROLE]
status=[REQUIRED_STATUS]
parent_hashes=[CANONICAL_PARENT_SHA256_ARRAY]

Use the required role/status for your stage. Include only stage-specific fields
from the Hedge orchestration protocol. A specialist report must include the
research-request hash. A portfolio draft must include every specialist-report
hash. A backtest report must include the portfolio-draft hash. A risk-review
attestation must include the backtest-report hash, the complete exact
hedge.decision.v1 object, and decision_sha256 equal to the canonical SHA-256 of
that object. Risk review may use only APPROVED, HOLD, or REJECTED. It must never expose
or request brokerage execution.
```

The local adapter must parse with the relevant `from_dict`, not trust the agent
prompt alone, and call `validate_handoff_chain` before it reads an approved
`attestation.decision`. The adapter, rather than Brainbase, must create the
trusted IDs/timestamps and calculate all parent hashes.
