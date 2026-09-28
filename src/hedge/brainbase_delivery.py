"""Bounded, fail-closed Brainbase task result consumer.

Registration is trusted launch-time state, never inferred from transcript text.
The CLI offers a narrowly labeled, non-executable CIO status draft when no typed
artifact exists; transcript text is never a handoff, approval, or decision.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from threading import Event, Lock, Thread
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .orchestration_protocol import (
    BacktestReport, HandoffValidationError, PortfolioDraft, ResearchRequest,
    RiskReviewAttestation, SpecialistReport, validate_handoff_chain,
)
from .slack_delivery import DeliveryRequest, DeliveryStatus, HedgeRole, SlackResultDelivery, profile_for_role
from .slack_state import SlackState, ThreadCorrelation


class UnsupportedResult(RuntimeError):
    """The transport does not provide a structured, attributable result."""


class BrainbaseClient(Protocol):
    def get_task(self, task_id: str) -> Mapping[str, Any]: ...
    def get_result(self, task_id: str) -> Mapping[str, Any] | None: ...


class BrainbaseCliClient:
    """Local CLI status with an explicitly untrusted CIO-only research draft.

    `task logs --json` has no typed artifact schema. Never authenticate a
    specialist report, Risk approval, or executable decision from its events.
    """
    def __init__(self, executable: str = "brainbase", *, run: Callable[..., Any] = subprocess.run,
                 expected_agent_id: str | None = None) -> None:
        self.executable, self.run = executable, run
        self.expected_agent_id = expected_agent_id

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        completed = self.run(
            [self.executable, "task", "get", task_id, "--json"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        raw = json.loads(completed.stdout)
        # The installed CLI emits {task: {id, agent_id, status}, eval_runs}.
        # Ignore evaluator text; never interpret transcript as an artifact.
        task = raw.get("task") if isinstance(raw, dict) else None
        if (not isinstance(task, dict) or task.get("id") != task_id
                or not isinstance(task.get("agent_id"), str)
                or (self.expected_agent_id is not None and task["agent_id"] != self.expected_agent_id)):
            raise ValueError("Brainbase task identity mismatch")
        status = {
            "initializing": "pending", "running": "running", "success": "succeeded",
            "fail": "failed", "need_more_info": "needs_input",
        }.get(task.get("status"))
        if status is None:
            raise ValueError("unknown Brainbase task status")
        parent = task.get("parent_task_id")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("invalid Brainbase parent task")
        return {"task_id": task_id, "status": status,
                "agent_id": task["agent_id"], "parent_task_id": parent}

    def get_result(self, task_id: str) -> Mapping[str, Any] | None:
        raise UnsupportedResult("Brainbase CLI does not specify a structured terminal artifact contract")

    def get_research_draft(self, task_id: str, task: Mapping[str, Any]) -> str | None:
        """Best-effort *untrusted* CIO text; never a typed artifact or approval.

        The limit includes a sentinel event. More events mean the transcript is
        truncated, so no candidate may be selected from that prefix.
        """
        if (task.get("task_id") != task_id or task.get("status") != "succeeded"
                or task.get("agent_id") != self.expected_agent_id
                or not self.expected_agent_id or task.get("parent_task_id") is not None):
            return None
        completed = self.run(
            [self.executable, "task", "logs", task_id, "--json", "--limit", "501"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        if len(completed.stdout) > 256_000:
            return None
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"items"}:
            return None
        events = payload["items"]
        if not isinstance(events, list) or len(events) > 500:
            return None
        return _terminal_research_text(events, task_id, self.expected_agent_id)


# Transcript text is never promoted to a handoff, approval, or decision.
# Reject broadly rather than trying to sanitize instructions or Slack markup.
_DRAFT_DENY = re.compile(
    r"(?i)(hedge\.[a-z_-]*(?:decision|risk|specialist|handoff)|schema_version|"
    r"risk[- ]?reviewed|risk (?:approved|approval)|approved (?:by )?risk|"
    r"recommend(?:ation|ed)?\s+(?:buy|sell|trade|allocat)|target weights?|"
    r"proposal|proposed (?:allocation|trade|portfolio)|[{}\[\]]|"
    r"(?:^|[\s])(?:buy|sell|execute|place an? order|rebalance|allocate)[\s:]+(?:now|shares?|\d)|"
    r"broker(?:age)?|ibkr|ignore (?:all )?(?:previous|prior) instructions|"
    r"system prompt|developer message|\bSYSTEM:|\bDEVELOPER:|"
    r"(?:ignore|disregard|override|do not follow) (?:the |all )?(?:rules|instructions|policy)|"
    r"follow these instructions|you are now|(?:use|call) (?:the )?tool|"
    r"(?:post|send|forward|route|reply) (?:this|it|the answer|your answer) to|"
    r"password|secret|api[_ -]?key|access[_ -]?token|bearer\s|"
    r"xox[baprs]-|sk-[A-Za-z0-9]{12}|slack://|slack\.com/archives|"
    r"<[@!#]|(?<!\w)@[A-Za-z0-9_]+|(?:^|\s)#[A-Za-z][\w-]*|"
    r"(?-i:\b[CGD][A-Z0-9]{8,}\b)|https?://[^\s]*\.slack\.com)"
)


def _terminal_research_text(events: list[Any], task_id: str, agent_id: str) -> str | None:
    """Accept only one final main-lane message after every tool event.

    CLI events are raw, unauthenticated transcript data. Identity is required on
    the candidate itself; text, tool outputs and evaluator summaries cannot set it.
    """
    candidates: list[tuple[int, str]] = []
    last_tool = -1
    open_tools: dict[str | None, int] = {}
    prior_ts: datetime | None = None
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        try:
            ts = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None or (prior_ts is not None and ts < prior_ts):
                return None
            prior_ts = ts
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if event.get("task_id") not in (None, task_id) or event.get("agent_id") not in (None, agent_id):
            return None
        kind = event["type"]
        if kind.endswith("tool_call.start") or kind.endswith("tool_call.end"):
            last_tool = index
            lane = event.get("subagent_id")
            if lane is not None and not isinstance(lane, str):
                return None
            if kind.endswith("tool_call.start"):
                open_tools[lane] = open_tools.get(lane, 0) + 1
            else:
                open_tools[lane] = open_tools.get(lane, 0) - 1
                if open_tools[lane] < 0:
                    return None
        if kind != "assistant.message":
            continue
        if (event.get("task_id") != task_id or event.get("agent_id") != agent_id
                or event.get("subagent_id") is not None or event.get("parent_task_id") is not None):
            return None
        data = event.get("data")
        blocks = data.get("content") if isinstance(data, dict) else None
        if (not isinstance(blocks, list) or not blocks or len(blocks) > 20
                or any(not isinstance(block, dict) or block.get("type") != "text"
                       or not isinstance(block.get("content"), str) for block in blocks)):
            return None
        text = "\n".join(block["content"] for block in blocks).strip()
        candidates.append((index, text))
    if any(open_tools.values()):
        return None
    terminal = [(index, text) for index, text in candidates if index > last_tool]
    if len(terminal) != 1:
        return None
    index, text = terminal[0]
    # No later assistant/subagent message: a later utterance makes the terminal
    # candidate unclear even if it was on a different lane.
    if (not events or events[-1]["type"] != "idle"
            or not isinstance(events[-1].get("data"), dict)
            or events[-1]["data"].get("status") not in {"success", "succeeded"}
            or any(event["type"] != "idle" for event in events[index + 1:])):
        return None
    if (not text or len(text) > 5_000 or any(ord(ch) < 32 and ch not in "\n\t" for ch in text)
            or text.lstrip().startswith(("{", "[", "```")) or _DRAFT_DENY.search(text)):
        return None
    try:
        json.loads(text)
    except ValueError:
        return text
    return None  # Even scalar JSON is not a CIO research draft.


@dataclass(frozen=True)
class PollResult:
    status: str
    detail: str = ""


class BrainbaseResultConsumer:
    """One finite observation per poll; persisted deadline and terminal claims.

    The registry contains IDs and status only, not tokens, task text or artifacts.
    A reserved result is never retried after restart: Slack may have accepted it.
    """
    def __init__(self, db_path: str | Path, state: SlackState, delivery: SlackResultDelivery,
                 client: BrainbaseClient, *, now: Callable[[], datetime] | None = None) -> None:
        self.state, self.delivery, self.client = state, delivery, client
        self.now = now or (lambda: datetime.now(UTC))
        path = str(Path(db_path).expanduser())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS brainbase_tasks (
            task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, root_thread_ts TEXT NOT NULL,
            role TEXT NOT NULL, parent_task_id TEXT, deadline_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN
                ('registered','reserved','delivered','failed','timed_out','unsupported')),
            notice_handled INTEGER NOT NULL DEFAULT 0
        )""")
        try:
            self.db.execute("ALTER TABLE brainbase_tasks ADD COLUMN notice_handled INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # existing table already has the additive column

    def close(self) -> None:
        self.db.close()

    def register(self, task_id: str, correlation: ThreadCorrelation, *, role: HedgeRole | str = HedgeRole.CIO,
                 parent_task_id: str | None = None, timeout_seconds: int = 300) -> bool:
        """Bind a task ID from a trusted create response before polling it.

        Children inherit the registered parent's canonical root and cannot set
        their own root, deadline beyond their parent, or CIO identity.
        """
        if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200:
            raise ValueError("invalid task ID")
        role = profile_for_role(role).role
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
            raise ValueError("invalid task timeout")
        if not self.state.has_canonical_correlation(correlation):
            raise ValueError("unregistered Slack run correlation")
        deadline = self.now() + timedelta(seconds=timeout_seconds)
        if parent_task_id is not None:
            parent = self.db.execute("SELECT * FROM brainbase_tasks WHERE task_id=?", (parent_task_id,)).fetchone()
            if parent is None or parent["status"] not in {"registered", "delivered"}:
                raise ValueError("unknown or failed parent task")
            if role is HedgeRole.CIO or parent["role"] != HedgeRole.CIO.value:
                raise ValueError("child task needs a CIO parent and specialist role")
            if (parent["run_id"], parent["workspace_id"], parent["channel_id"], parent["root_thread_ts"]) != (
                correlation.run_id, correlation.workspace_id, correlation.channel_id, correlation.root_thread_ts
            ):
                raise ValueError("child task root differs from parent")
            deadline = min(deadline, datetime.fromisoformat(parent["deadline_at"]))
        elif role is not HedgeRole.CIO:
            raise ValueError("specialist task needs a registered CIO parent")
        try:
            self.db.execute("""INSERT INTO brainbase_tasks (
                task_id,run_id,workspace_id,channel_id,root_thread_ts,role,parent_task_id,deadline_at,status
            ) VALUES (?,?,?,?,?,?,?,?,?)""", (
                task_id, correlation.run_id, correlation.workspace_id, correlation.channel_id,
                correlation.root_thread_ts, role.value, parent_task_id, deadline.isoformat(), "registered",
            ))
        except sqlite3.IntegrityError:
            row = self.db.execute("SELECT * FROM brainbase_tasks WHERE task_id=?", (task_id,)).fetchone()
            if (row["run_id"], row["workspace_id"], row["channel_id"], row["root_thread_ts"], row["role"], row["parent_task_id"]) != (
                correlation.run_id, correlation.workspace_id, correlation.channel_id,
                correlation.root_thread_ts, role.value, parent_task_id
            ):
                raise ValueError("task ID already bound to another run or role")
            return False
        return True

    def tracked_tasks(self) -> list[tuple[str, str]]:
        """Include terminal tasks so interrupted notice delivery resumes on restart."""
        return [(row["task_id"], row["status"]) for row in self.db.execute(
            "SELECT task_id,status FROM brainbase_tasks WHERE parent_task_id IS NULL "
            "AND (status='registered' OR (notice_handled=0 AND "
            "status IN ('delivered','failed','timed_out','unsupported')))"
        )]

    def mark_handled(self, task_id: str) -> None:
        self.db.execute("UPDATE brainbase_tasks SET notice_handled=1 WHERE task_id=?", (task_id,))

    def correlation_for_task(self, task_id: str) -> ThreadCorrelation | None:
        row = self.db.execute(
            "SELECT run_id,workspace_id,channel_id,root_thread_ts "
            "FROM brainbase_tasks WHERE task_id=? AND parent_task_id IS NULL", (task_id,)
        ).fetchone()
        return ThreadCorrelation(*tuple(row)) if row is not None else None

    def _finish(self, task_id: str, status: str) -> PollResult:
        self.db.execute("UPDATE brainbase_tasks SET status=? WHERE task_id=? AND status='registered'", (status, task_id))
        return PollResult(status)

    def poll(self, task_id: str) -> PollResult:
        row = self.db.execute("SELECT * FROM brainbase_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return PollResult("rejected", "unregistered task")
        if row["status"] != "registered":
            return PollResult(row["status"])
        correlation = ThreadCorrelation(row["run_id"], row["workspace_id"], row["channel_id"], row["root_thread_ts"])
        if not self.state.has_canonical_correlation(correlation):
            return self._finish(task_id, "failed")
        if self.now() >= datetime.fromisoformat(row["deadline_at"]):
            return self._finish(task_id, "timed_out")
        if row["parent_task_id"] is not None:
            parent = self.db.execute(
                "SELECT status FROM brainbase_tasks WHERE task_id=?", (row["parent_task_id"],)
            ).fetchone()
            if parent is None or parent["status"] in {"failed", "timed_out", "unsupported"}:
                return self._finish(task_id, "failed")
        try:
            task = self.client.get_task(task_id)
            if not isinstance(task, Mapping) or task.get("task_id") != task_id:
                return self._finish(task_id, "failed")
            status = task.get("status")
            if status in {"failed", "cancelled", "canceled", "stopped", "needs_input"}:
                return self._finish(task_id, "failed")
            if status in {"pending", "queued", "running", "started"}:
                return PollResult("pending")
            if status not in {"succeeded", "completed"}:
                return self._finish(task_id, "failed")
            try:
                result = self.client.get_result(task_id)
            except UnsupportedResult:
                result = None
            if result is None:
                # Never infer a Risk approval or a typed result from logs.
                # Only the installed CLI client exposes the strict draft reader.
                if (row["role"] != HedgeRole.CIO.value or row["parent_task_id"] is not None
                        or not isinstance(self.client, BrainbaseCliClient)):
                    return self._finish(task_id, "unsupported")
                draft = self.client.get_research_draft(task_id, task)
                if draft is None:
                    return self._finish(task_id, "unsupported")
                # Recheck the verified task envelope after the transcript read;
                # a status change or identity change invalidates the snapshot.
                if self.client.get_task(task_id) != task:
                    return self._finish(task_id, "unsupported")
                role, text = HedgeRole.CIO, (
                    "CIO research draft (not risk-reviewed)\n"
                    "Unverified task transcript; not investment advice or a trading instruction.\n\n"
                    + draft
                )
            else:
                role, text = self._validate_result(task_id, row, result, correlation)
        except UnsupportedResult:
            return self._finish(task_id, "unsupported")
        except (ValueError, TypeError, KeyError, HandoffValidationError):
            return self._finish(task_id, "failed")
        except Exception:
            # Transient transport errors are retried only before the deadline;
            # a terminal Slack send is never retried.
            return PollResult("pending", "transport unavailable")
        if self.now() >= datetime.fromisoformat(row["deadline_at"]):
            return self._finish(task_id, "timed_out")
        # SQLite CAS guards concurrent pollers, including distinct processes.
        claimed = self.db.execute(
            "UPDATE brainbase_tasks SET status='reserved' WHERE task_id=? AND status='registered'", (task_id,)
        )
        if claimed.rowcount != 1:
            return PollResult("duplicate")
        sent = self.delivery.deliver(DeliveryRequest(
            delivery_id="brainbase:" + task_id, role=role, correlation=correlation, text=text,
        ))
        final = "delivered" if sent.status is DeliveryStatus.DELIVERED else "failed"
        self.db.execute("UPDATE brainbase_tasks SET status=? WHERE task_id=? AND status='reserved'", (final, task_id))
        return PollResult(final)

    @staticmethod
    def _validate_result(task_id: str, row: sqlite3.Row, result: Mapping[str, Any],
                         correlation: ThreadCorrelation) -> tuple[HedgeRole, str]:
        if not isinstance(result, Mapping) or set(result) != {"task_id", "role", "artifact"} or result["task_id"] != task_id:
            raise ValueError("invalid result envelope")
        role = profile_for_role(result["role"]).role
        if role.value != row["role"]:
            raise ValueError("task role mismatch")
        artifact = result["artifact"]
        if not isinstance(artifact, Mapping):
            raise ValueError("missing structured artifact")
        affinity = (correlation.run_id, correlation.workspace_id, correlation.channel_id, correlation.root_thread_ts)
        if role is HedgeRole.CIO:
            if artifact.get("schema_version") == "hedge.cio-result.v1":
                if set(artifact) != {"schema_version", "run_id", "workspace_id", "channel_id", "root_thread_ts", "role", "status", "text"}:
                    raise ValueError("invalid CIO result fields")
                if artifact["role"] != "cio" or artifact["status"] != "COMPLETED":
                    raise ValueError("invalid CIO result role/status")
                text = artifact["text"]
            elif artifact.get("schema_version") == "hedge.handoff-chain.v1":
                if set(artifact) != {"schema_version", "request", "reports", "draft", "backtest", "attestation", "text"}:
                    raise ValueError("invalid handoff fields")
                request = ResearchRequest.from_dict(artifact["request"])
                if not isinstance(artifact["reports"], list):
                    raise ValueError("invalid reports")
                reports = tuple(SpecialistReport.from_dict(v) for v in artifact["reports"])
                draft = PortfolioDraft.from_dict(artifact["draft"])
                backtest = BacktestReport.from_dict(artifact["backtest"])
                attestation = RiskReviewAttestation.from_dict(artifact["attestation"])
                validate_handoff_chain(request, reports, draft, backtest, attestation)
                for node in (request, *reports, draft, backtest, attestation):
                    if (node.run_id, node.workspace_id, node.channel_id, node.root_thread_ts) != affinity:
                        raise ValueError("handoff root mismatch")
                text = artifact["text"]
            else:
                raise ValueError("unsupported CIO artifact")
        else:
            if set(artifact) != {"schema_version", "request", "report"} or artifact["schema_version"] != "hedge.specialist-result.v1":
                raise ValueError("missing specialist handoff")
            request = ResearchRequest.from_dict(artifact["request"])
            report = SpecialistReport.from_dict(artifact["report"])
            if request.sha256() not in report.parent_hashes or report.specialty != role.value:
                raise ValueError("invalid specialist handoff/role")
            if any((node.run_id, node.workspace_id, node.channel_id, node.root_thread_ts) != affinity for node in (request, report)):
                raise ValueError("specialist root mismatch")
            text = report.summary + "\n" + "\n".join("• " + value for value in report.findings)
        if not isinstance(text, str) or not text.strip() or len(text) > 40_000:
            raise ValueError("empty or oversized result")
        if role is HedgeRole.CIO and artifact["schema_version"] == "hedge.cio-result.v1":
            if tuple(artifact[k] for k in ("run_id", "workspace_id", "channel_id", "root_thread_ts")) != affinity:
                raise ValueError("CIO root mismatch")
        return role, text


# Fixed, non-investment notices. Never quote a CLI response, transcript or task text.
TASK_NOTICES = {
    "failed": "Hedge CIO could not complete this research task. Please try a new mention later.",
    "timed_out": "Hedge CIO research timed out. No research answer was delivered. Please try a new mention later.",
    "unsupported": (
        "Hedge CIO finished, but this integration cannot verify a final research answer "
        "from Brainbase. No answer or investment recommendation was delivered; "
        "an operator must review the task separately."
    ),
}


class BrainbaseTaskMonitor:
    """Periodic bounded status checks, with durable at-most-once CIO notices.

    Each cycle fetches at most one status per registered task. The run loop is
    started by the Socket Mode entrypoint, never by constructing the bridge.
    """

    def __init__(self, consumer: BrainbaseResultConsumer, *, interval: float = 5.0) -> None:
        if not 1 <= interval <= 60:
            raise ValueError("invalid monitor interval")
        self.consumer = consumer
        self.interval = interval
        self._lock = Lock()
        self._wake = Event()
        self._stop = Event()
        self._thread: Thread | None = None

    def register(self, task_id: str, correlation: ThreadCorrelation, *, timeout_seconds: int = 90) -> bool:
        with self._lock:
            registered = self.consumer.register(task_id, correlation, timeout_seconds=timeout_seconds)
        self._wake.set()
        return registered

    def tick(self) -> None:
        with self._lock:
            for task_id, status in self.consumer.tracked_tasks():
                if status == "registered":
                    status = self.consumer.poll(task_id).status
                if status == "delivered":
                    correlation = self.consumer.correlation_for_task(task_id)
                    if correlation is not None:
                        self.consumer.state.set_run_status(correlation.run_id, "completed")
                    self.consumer.mark_handled(task_id)
                    continue
                if status not in TASK_NOTICES:
                    continue
                correlation = self.consumer.correlation_for_task(task_id)
                if correlation is None or not self.consumer.state.has_canonical_correlation(correlation):
                    self.consumer.mark_handled(task_id)
                    continue
                # The verified artifact delivery, if present, owns this task's
                # response. Never post a fallback after its reservation.
                result_status = self.consumer.state.delivery_status("brainbase:" + task_id)
                if result_status is not None:
                    # A failed reservation may still have reached Slack, so
                    # neither retry nor send a competing fallback notice.
                    self.consumer.state.set_run_status(
                        correlation.run_id, "completed" if result_status == "delivered" else "failed"
                    )
                    self.consumer.mark_handled(task_id)
                    continue
                self.consumer.delivery.deliver(DeliveryRequest(
                    delivery_id="brainbase-status:" + task_id,
                    role=HedgeRole.CIO, correlation=correlation,
                    text=TASK_NOTICES[status],
                ))
                self.consumer.state.set_run_status(correlation.run_id, "failed")
                self.consumer.mark_handled(task_id)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="hedge-brainbase-status", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import logging
        logger = logging.getLogger(__name__)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # Do not log transport exceptions: a CLI exception can contain
                # the command, captured output, or sensitive payloads.
                logger.error("Brainbase status monitor cycle failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
