"""Strict, paper-safe handoff artifacts for a Hedge research thread.

This module is deliberately stdlib-only.  It is an orchestration boundary, not an
execution interface: the only executable-shaped value it handles is the existing
``hedge.decision.v1`` document, and that value is released only by an approved
terminal risk attestation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from typing import Any, ClassVar, Mapping

from hedge.contracts import ContractError, Decision


class HandoffValidationError(ValueError):
    """A handoff document is malformed, untrusted, or belongs to another thread."""


_SHA256_LENGTH = 64
_COMMON_FIELDS = frozenset({
    "schema_version", "artifact_id", "run_id", "workspace_id", "channel_id",
    "root_thread_ts", "role", "created_at", "deadline_at", "parent_hashes", "status",
})


def _fail(message: str) -> None:
    raise HandoffValidationError(message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{field} must include a timezone")
    return text


def _hash(value: object, field: str) -> str:
    text = _string(value, field)
    if len(text) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in text):
        _fail(f"{field} must be a lowercase SHA-256 hex digest")
    return text


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(f"{field} must be an array")
    result = tuple(_string(item, f"{field}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not result:
        _fail(f"{field} must not be empty")
    if len(set(result)) != len(result):
        _fail(f"{field} must not contain duplicates")
    return result


def _json_array(value: object, field: str) -> tuple[Any, ...]:
    """Accept JSON arrays at parse boundaries; never coerce strings or mappings."""
    if not isinstance(value, list):
        _fail(f"{field} must be an array")
    return tuple(value)


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(f"{field} must be a finite number")
    return float(value)


def _exact_keys(data: object, expected: frozenset[str], name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _fail(f"{name} must be an object")
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _fail(f"{name} has invalid fields (missing={missing}, extra={extra})")
    return data


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return the one JSON representation used for all protocol hashes."""
    if not isinstance(value, Mapping):
        _fail("canonical JSON value must be an object")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HandoffValidationError(f"value is not canonical JSON: {exc}") from exc


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _strict_decision_document(value: object) -> dict[str, Any]:
    """Validate the existing Decision schema without weakening it via coercion."""
    decision = _exact_keys(value, frozenset({
        "schema_version", "decision_id", "pool_id", "mandate_version", "intents", "created_at"
    }), "decision")
    if decision["schema_version"] != "hedge.decision.v1":
        _fail("decision must use hedge.decision.v1")
    for field in ("decision_id", "pool_id"):
        _string(decision[field], f"decision.{field}")
    if isinstance(decision["mandate_version"], bool) or not isinstance(decision["mandate_version"], int):
        _fail("decision.mandate_version must be an integer")
    _timestamp(decision["created_at"], "decision.created_at")
    intents = decision["intents"]
    if not isinstance(intents, list) or not intents:
        _fail("decision.intents must be a non-empty array")
    intent_keys = frozenset({
        "schema_version", "decision_id", "pool_id", "mandate_version", "symbol", "side", "quantity",
        "order_type", "limit_price", "rationale", "created_at"
    })
    clean_intents: list[dict[str, Any]] = []
    for index, raw in enumerate(intents):
        intent = _exact_keys(raw, intent_keys, f"decision.intents[{index}]")
        for field in ("schema_version", "decision_id", "pool_id", "symbol", "side", "order_type", "rationale"):
            _string(intent[field], f"decision.intents[{index}].{field}")
        if isinstance(intent["mandate_version"], bool) or not isinstance(intent["mandate_version"], int):
            _fail(f"decision.intents[{index}].mandate_version must be an integer")
        if isinstance(intent["quantity"], bool) or not isinstance(intent["quantity"], int):
            _fail(f"decision.intents[{index}].quantity must be an integer")
        if intent["limit_price"] is not None:
            _finite_number(intent["limit_price"], f"decision.intents[{index}].limit_price")
        _timestamp(intent["created_at"], f"decision.intents[{index}].created_at")
        clean_intents.append(dict(intent))
    clean = dict(decision)
    clean["intents"] = clean_intents
    try:
        Decision.from_dict(clean)
    except ContractError as exc:
        raise HandoffValidationError(f"invalid hedge decision: {exc}") from exc
    return clean


@dataclass(frozen=True)
class _Artifact:
    schema_version: str
    artifact_id: str
    run_id: str
    workspace_id: str
    channel_id: str
    root_thread_ts: str
    role: str
    created_at: str
    deadline_at: str
    parent_hashes: tuple[str, ...]
    status: str

    EXPECTED_SCHEMA: ClassVar[str]
    EXPECTED_ROLE: ClassVar[str]
    ALLOWED_STATUS: ClassVar[frozenset[str]]

    def __post_init__(self) -> None:
        if self.schema_version != self.EXPECTED_SCHEMA:
            _fail(f"unsupported schema_version for {self.__class__.__name__}")
        for field in ("artifact_id", "run_id", "workspace_id", "channel_id", "root_thread_ts"):
            _string(getattr(self, field), field)
        if self.role != self.EXPECTED_ROLE:
            _fail(f"role must be {self.EXPECTED_ROLE}")
        created = _timestamp(self.created_at, "created_at")
        deadline = _timestamp(self.deadline_at, "deadline_at")
        if datetime.fromisoformat(deadline.replace("Z", "+00:00")) < datetime.fromisoformat(created.replace("Z", "+00:00")):
            _fail("deadline_at must not precede created_at")
        if not isinstance(self.parent_hashes, tuple):
            _fail("parent_hashes must be a tuple")
        for index, parent in enumerate(self.parent_hashes):
            _hash(parent, f"parent_hashes[{index}]")
        if len(set(self.parent_hashes)) != len(self.parent_hashes):
            _fail("parent_hashes must not contain duplicates")
        if self.status not in self.ALLOWED_STATUS:
            _fail(f"invalid status for {self.__class__.__name__}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "artifact_id": self.artifact_id,
            "run_id": self.run_id, "workspace_id": self.workspace_id, "channel_id": self.channel_id,
            "root_thread_ts": self.root_thread_ts, "role": self.role, "created_at": self.created_at,
            "deadline_at": self.deadline_at, "parent_hashes": list(self.parent_hashes), "status": self.status,
        }

    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True)
class ResearchRequest(_Artifact):
    objective: str
    symbols: tuple[str, ...]
    research_questions: tuple[str, ...]

    EXPECTED_SCHEMA = "hedge.research-request.v1"
    EXPECTED_ROLE = "research_coordinator"
    ALLOWED_STATUS = frozenset({"REQUESTED"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.parent_hashes:
            _fail("ResearchRequest must not have parent hashes")
        _string(self.objective, "objective")
        _strings(self.symbols, "symbols")
        _strings(self.research_questions, "research_questions")

    @classmethod
    def from_dict(cls, data: object) -> "ResearchRequest":
        raw = _exact_keys(data, _COMMON_FIELDS | {"objective", "symbols", "research_questions"}, "research request")
        return cls(**dict(raw, parent_hashes=_json_array(raw["parent_hashes"], "parent_hashes"), symbols=_json_array(raw["symbols"], "symbols"), research_questions=_json_array(raw["research_questions"], "research_questions")))

    def as_dict(self) -> dict[str, Any]:
        return dict(super().as_dict(), objective=self.objective, symbols=list(self.symbols), research_questions=list(self.research_questions))


@dataclass(frozen=True)
class SpecialistReport(_Artifact):
    specialty: str
    summary: str
    findings: tuple[str, ...]
    confidence: float

    EXPECTED_SCHEMA = "hedge.specialist-report.v1"
    EXPECTED_ROLE = "specialist"
    ALLOWED_STATUS = frozenset({"COMPLETED"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.parent_hashes:
            _fail("SpecialistReport requires parent hashes")
        _string(self.specialty, "specialty")
        _string(self.summary, "summary")
        _strings(self.findings, "findings")
        confidence = _finite_number(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            _fail("confidence must be between 0 and 1")

    @classmethod
    def from_dict(cls, data: object) -> "SpecialistReport":
        raw = _exact_keys(data, _COMMON_FIELDS | {"specialty", "summary", "findings", "confidence"}, "specialist report")
        return cls(**dict(raw, parent_hashes=_json_array(raw["parent_hashes"], "parent_hashes"), findings=_json_array(raw["findings"], "findings")))

    def as_dict(self) -> dict[str, Any]:
        return dict(super().as_dict(), specialty=self.specialty, summary=self.summary, findings=list(self.findings), confidence=self.confidence)


@dataclass(frozen=True)
class PortfolioDraft(_Artifact):
    thesis: str
    target_allocations: tuple[str, ...]

    EXPECTED_SCHEMA = "hedge.portfolio-draft.v1"
    EXPECTED_ROLE = "portfolio_manager"
    ALLOWED_STATUS = frozenset({"DRAFT"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.parent_hashes:
            _fail("PortfolioDraft requires parent hashes")
        _string(self.thesis, "thesis")
        _strings(self.target_allocations, "target_allocations")

    @classmethod
    def from_dict(cls, data: object) -> "PortfolioDraft":
        raw = _exact_keys(data, _COMMON_FIELDS | {"thesis", "target_allocations"}, "portfolio draft")
        return cls(**dict(raw, parent_hashes=_json_array(raw["parent_hashes"], "parent_hashes"), target_allocations=_json_array(raw["target_allocations"], "target_allocations")))

    def as_dict(self) -> dict[str, Any]:
        return dict(super().as_dict(), thesis=self.thesis, target_allocations=list(self.target_allocations))


@dataclass(frozen=True)
class BacktestReport(_Artifact):
    period: str
    summary: str
    passed: bool

    EXPECTED_SCHEMA = "hedge.backtest-report.v1"
    EXPECTED_ROLE = "backtester"
    ALLOWED_STATUS = frozenset({"COMPLETED"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.parent_hashes:
            _fail("BacktestReport requires parent hashes")
        _string(self.period, "period")
        _string(self.summary, "summary")
        if not isinstance(self.passed, bool):
            _fail("passed must be a boolean")

    @classmethod
    def from_dict(cls, data: object) -> "BacktestReport":
        raw = _exact_keys(data, _COMMON_FIELDS | {"period", "summary", "passed"}, "backtest report")
        return cls(**dict(raw, parent_hashes=_json_array(raw["parent_hashes"], "parent_hashes")))

    def as_dict(self) -> dict[str, Any]:
        return dict(super().as_dict(), period=self.period, summary=self.summary, passed=self.passed)


@dataclass(frozen=True)
class RiskReviewAttestation(_Artifact):
    decision_json: str | None
    decision_sha256: str | None
    rationale: str

    EXPECTED_SCHEMA = "hedge.risk-review-attestation.v1"
    EXPECTED_ROLE = "risk_reviewer"
    ALLOWED_STATUS = frozenset({"APPROVED", "HOLD", "REJECTED"})

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.parent_hashes:
            _fail("RiskReviewAttestation requires parent hashes")
        _string(self.rationale, "rationale")
        if self.status != "APPROVED":
            if self.decision_json is not None or self.decision_sha256 is not None:
                _fail("only an APPROVED attestation may carry a decision")
            return
        if not isinstance(self.decision_sha256, str):
            _fail("decision_sha256 is required for APPROVED")
        _hash(self.decision_sha256, "decision_sha256")
        if not isinstance(self.decision_json, str):
            _fail("decision_json is required for APPROVED")
        try:
            raw = json.loads(self.decision_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HandoffValidationError("decision_json must be valid JSON") from exc
        clean = _strict_decision_document(raw)
        if self.decision_json != canonical_json(clean):
            _fail("decision_json must use canonical JSON")
        if self.decision_sha256 != canonical_sha256(clean):
            _fail("decision_sha256 does not bind decision_json")

    @classmethod
    def create(cls, *, decision: Mapping[str, Any] | None, **fields: Any) -> "RiskReviewAttestation":
        if fields.get("status") != "APPROVED":
            if decision is not None:
                _fail("only an APPROVED attestation may carry a decision")
            return cls(decision_json=None, decision_sha256=None, **fields)
        if decision is None:
            _fail("APPROVED attestation requires a decision")
        clean = _strict_decision_document(decision)
        return cls(decision_json=canonical_json(clean), decision_sha256=canonical_sha256(clean), **fields)

    @classmethod
    def from_dict(cls, data: object) -> "RiskReviewAttestation":
        raw = _exact_keys(data, _COMMON_FIELDS | {"decision", "decision_sha256", "rationale"}, "risk review attestation")
        fields = dict(raw)
        fields["parent_hashes"] = _json_array(raw["parent_hashes"], "parent_hashes")
        decision = fields.pop("decision")
        supplied_hash = fields.pop("decision_sha256")
        if fields["status"] != "APPROVED":
            if decision is not None or supplied_hash is not None:
                _fail("only an APPROVED attestation may carry a decision")
            fields["decision_json"] = None
            fields["decision_sha256"] = None
            return cls(**fields)
        clean = _strict_decision_document(decision)
        supplied_hash = _hash(supplied_hash, "decision_sha256")
        if supplied_hash != canonical_sha256(clean):
            _fail("decision_sha256 does not bind decision")
        fields["decision_json"] = canonical_json(clean)
        fields["decision_sha256"] = supplied_hash
        return cls(**fields)

    @property
    def decision(self) -> Decision:
        """Return the parsed Decision only after a terminal approval."""
        if self.status != "APPROVED":
            _fail("a decision is exposed only by an APPROVED attestation")
        if self.decision_json is None:
            _fail("APPROVED attestation is missing a decision")
        return Decision.from_dict(json.loads(self.decision_json))

    def as_dict(self) -> dict[str, Any]:
        decision = json.loads(self.decision_json) if self.decision_json is not None else None
        return dict(super().as_dict(), decision=decision, decision_sha256=self.decision_sha256, rationale=self.rationale)


def validate_handoff_chain(
    request: ResearchRequest,
    reports: tuple[SpecialistReport, ...],
    draft: PortfolioDraft,
    backtest: BacktestReport,
    attestation: RiskReviewAttestation,
) -> None:
    """Reject a chain with mismatched Slack context, roles, or parent hashes."""
    artifacts: tuple[_Artifact, ...] = (request, *reports, draft, backtest, attestation)
    if not reports:
        _fail("a handoff chain needs at least one specialist report")
    for artifact in artifacts:
        artifact.__post_init__()
        if (artifact.run_id, artifact.workspace_id, artifact.channel_id, artifact.root_thread_ts) != (
            request.run_id, request.workspace_id, request.channel_id, request.root_thread_ts
        ):
            _fail("cross-run, workspace, channel, or root-thread artifact")
    request_hash = request.sha256()
    report_hashes = {report.sha256() for report in reports}
    if any(request_hash not in report.parent_hashes for report in reports):
        _fail("each specialist report must bind the research request hash")
    if not report_hashes.issubset(set(draft.parent_hashes)):
        _fail("portfolio draft must bind every specialist report hash")
    if draft.sha256() not in backtest.parent_hashes:
        _fail("backtest report must bind the portfolio draft hash")
    if backtest.sha256() not in attestation.parent_hashes:
        _fail("risk attestation must bind the backtest report hash")
    if attestation.status not in RiskReviewAttestation.ALLOWED_STATUS:
        _fail("risk attestation must be terminal")


__all__ = [
    "BacktestReport", "HandoffValidationError", "PortfolioDraft", "ResearchRequest",
    "RiskReviewAttestation", "SpecialistReport", "canonical_json", "canonical_sha256",
    "validate_handoff_chain",
]
