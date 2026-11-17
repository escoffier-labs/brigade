"""Node consumer for campaign members (#1128 slice 3).

One pass takes the next eligible member through the generation-fenced ownership ``take``,
launches a local task for that member's repository through ``work dispatch``, then records
the attempt and checkpoints the run on the ownership record. The Hub stays the authority on
eligibility, the offer bounds and fencing: every refusal it makes is handled here, and none
is worked around. A successful run is an attempt and never completes the member.
"""

from __future__ import annotations

import contextlib
import io
import json
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .worklore_client import WorkloreClientError, new_ownership_nonce

_HELD = frozenset({"owned", "handoff-pending"})
# A checkpoint with one of these next actions means the run finished and a person or a
# later slice must act. The consumer never reclaims such a member on its own.
_PARKED = frozenset({"await-review", "await-checks", "await-merge", "blocked"})
# Hub refusals that only rule out this member; the pass moves on to the next one.
_SKIP_CODES = frozenset({"campaign-ineligible", "ownership-conflict", "version-conflict", "idempotency-conflict"})
_STOP_CODE = "campaign-concurrency-limit"
# Only an explicit completed state may be parked as a finished run. Anything else after a launch
# is a failed attempt.
_COMPLETED = "completed"
# Outcomes that need a person or a retry. ``consume --json`` exits non-zero on any of them.
FAILING = frozenset({"failed", "unlinked", "refused", "lost-grant", "error", "previous-run"})
# The local run stops this long before the Hub's expiry so the attempt writes still land.
LEASE_MARGIN_SECONDS = 15.0
# A run budget shorter than this cannot do useful work, so the member is handed back unlaunched.
MIN_RUN_SECONDS = 30.0


class Hub(Protocol):
    """The Worklore client calls the consumer uses; ``worklore_client`` satisfies it."""

    def get_campaign(self, campaign_key: str) -> dict[str, Any]: ...
    def get_item(self, work_id: str) -> dict[str, Any]: ...
    def get_ownership(self, work_id: str) -> dict[str, Any]: ...
    def ownership_action(
        self, work_id: str, body: Mapping[str, Any], *, if_match: object, idempotency_key: str, holder_nonce: str
    ) -> dict[str, Any]: ...
    def link_execution(
        self, work_id: str, body: Mapping[str, Any], *, generation: object, holder_nonce: str
    ) -> dict[str, Any]: ...
    def record_attempt(
        self, work_id: str, body: Mapping[str, Any], *, if_match: object, generation: object, holder_nonce: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Launch:
    """What a local launch left behind. ``state`` is a dispatch intent state or ``refused``."""

    state: str
    run_id: str | None = None
    branch: str | None = None
    revision: str | None = None
    detail: str = ""


Launcher = Callable[..., Launch]
Emit = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Pass:
    """One consumer pass. ``outcome`` is idle, parked, failed, lost-grant, unlinked or refused."""

    outcome: str
    repo: str | None = None
    work_id: str | None = None
    generation: int | None = None
    run_id: str | None = None
    detail: str = ""


class HoldsUnreadable(RuntimeError):
    """The holds file exists but cannot be read. Refuse the pass; an unread hold must not relaunch."""


class Holds:
    """Members this node must not take again until an operator clears them.

    A hold records that a run may have outlived its grant, or failed where the Hub could not count
    it. It lives in ``.brigade/campaign-consume/KEY.holds.json``; delete a work id's entry to clear it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.rows: dict[str, Any] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                loaded = {}
            except (OSError, ValueError) as exc:
                raise HoldsUnreadable(f"cannot read campaign holds at {path}") from exc
            if not isinstance(loaded, dict):
                raise HoldsUnreadable(f"campaign holds at {path} is not a JSON object")
            self.rows = loaded

    def __contains__(self, work_id: object) -> bool:
        return work_id in self.rows

    def add(self, work_id: str, reason: str, at: datetime) -> None:
        self.rows[work_id] = {"reason": reason, "at": at.isoformat()}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.path.with_suffix(".tmp")
            scratch.write_text(json.dumps(self.rows, sort_keys=True, indent=2), encoding="utf-8")
            scratch.replace(self.path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code(exc: WorkloreClientError) -> str:
    return exc.code or ""


def _expired(ownership: Mapping[str, Any], now: datetime) -> bool:
    deadline = ownership.get("expires_at")
    if not isinstance(deadline, str):
        return False
    try:
        stamp = datetime.fromisoformat(deadline)
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp <= now


def _reclaimable(ownership: Mapping[str, Any], now: datetime) -> bool:
    """Whether a held member may be retried: an expired holder that did not park a finished run."""
    if ownership.get("state") not in _HELD or not _expired(ownership, now):
        return False
    action = ownership.get("next_action")
    return not (isinstance(action, Mapping) and action.get("kind") in _PARKED)


def _candidates(
    members: Sequence[Mapping[str, Any]], repos: Mapping[str, Path], skip: Callable[[str], bool]
) -> list[Mapping[str, Any]]:
    """Unowned members first, then in-flight ones whose holder may have died; local repos only."""
    rows = [m for m in members if m["repo"] in repos and not skip(m["work_id"])]
    order = {"unowned": 0, "in_flight": 1}
    return sorted((m for m in rows if m["bucket"] in order), key=lambda m: (order[m["bucket"]], m["repo"]))


def _checkpoint(repo: str, launch: Launch, revision: str, kind: str, why: str) -> dict[str, Any]:
    evidence = []
    if launch.run_id and launch.revision:
        evidence.append({"kind": "receipt", "ref": launch.run_id, "source_revision": launch.revision})
    return {
        "action": "checkpoint",
        "repo_identity": repo,
        "write_scope": [],
        "source_revision": revision,
        "next_action": {"kind": kind, "resume_condition": why[:500]},
        "evidence_refs": evidence,
    }


def consume_once(
    hub: Hub,
    *,
    campaign_key: str,
    node_id: str,
    repos: Mapping[str, Path],
    launcher: Launcher,
    emit: Emit,
    tried: set[str],
    holds: Holds,
    now: Callable[[], datetime] = _now,
) -> Pass:
    """Take and work the next eligible member. Selection is Hub state, so a rerun is a resume."""
    shown = hub.get_campaign(campaign_key)
    members = [m for m in shown.get("members", []) if isinstance(m, dict)]
    campaign = shown.get("campaign", {})
    for member in _candidates(members, repos, lambda work_id: work_id in tried or work_id in holds):
        work_id, repo = str(member["work_id"]), str(member["repo"])
        ownership = hub.get_ownership(work_id)["ownership"]
        reclaim = ownership.get("state") in _HELD
        if reclaim and not _reclaimable(ownership, now()):
            continue
        nonce = new_ownership_nonce()
        try:
            grant = hub.ownership_action(
                work_id,
                {"action": "take", "generation": ownership["generation"]},
                if_match=ownership["revision"],
                idempotency_key="take-" + secrets.token_hex(16),
                holder_nonce=nonce,
            )["ownership"]
        except WorkloreClientError as exc:
            if _code(exc) == _STOP_CODE:
                return Pass("idle")
            if _code(exc) in _SKIP_CODES:
                continue
            raise
        tried.add(work_id)
        generation = int(grant["generation"])
        emit({"event": "take", "repo": repo, "work_id": work_id, "generation": generation})
        progress: dict[str, Launch] = {}
        try:
            return _work(
                hub, campaign, member, grant, nonce, reclaim=reclaim, progress=progress,
                node_id=node_id, repos=repos, launcher=launcher, emit=emit, holds=holds, now=now,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - one member's failure must not strand it or stop the pass
            return _recover(
                hub, member, nonce, generation, progress.get("launch"), exc, emit=emit, holds=holds, now=now
            )
    return Pass("idle")


def _recover(
    hub: Hub,
    member: Mapping[str, Any],
    nonce: str,
    generation: int,
    launch: Launch | None,
    exc: Exception,
    *,
    emit: Emit,
    holds: Holds,
    now: Callable[[], datetime],
) -> Pass:
    """Hand an unexpectedly failed member back, or park it when a run may have happened."""
    work_id, repo = str(member["work_id"]), str(member["repo"])
    reason = type(exc).__name__ + (f" {_code(exc)}" if isinstance(exc, WorkloreClientError) else "")
    ran = launch is not None and launch.state != "refused"
    # Every step is best effort and independent: the receipt writer may be what failed.
    try:
        if launch is not None and ran:
            with contextlib.suppress(OSError):
                holds.add(work_id, f"error {reason}", now())
            _park(hub, repo, work_id, launch, nonce, generation, "blocked", f"consumer error {reason}")
        else:
            _release(hub, work_id, nonce, generation)
    except Exception:  # noqa: BLE001 - the lease bounds what a failed write leaves behind
        pass
    try:
        emit({"event": "error", "repo": repo, "work_id": work_id, "generation": generation, "detail": reason})
    except OSError:
        pass
    return Pass("error", repo, work_id, generation, launch.run_id if launch else None, reason)


def _budget_seconds(campaign: Mapping[str, Any], grant: Mapping[str, Any], at: datetime) -> tuple[float, str]:
    """Seconds a run may use and the offer lease it came from. The Hub's clock decides expiry."""
    offer = campaign.get("offer")
    lease = offer.get("lease_seconds") if isinstance(offer, Mapping) else None
    lease_text = f"campaign offer lease_seconds={lease}"
    left = [float(lease)] if isinstance(lease, int) else []
    deadline = grant.get("expires_at")
    if isinstance(deadline, str):
        try:
            left.append((datetime.fromisoformat(deadline) - at).total_seconds())
        except ValueError:
            pass
    return (min(left) - LEASE_MARGIN_SECONDS if left else 0.0), lease_text


def _hand_back(hub: Hub, member: Mapping[str, Any], nonce: str, generation: int, emit: Emit, detail: str) -> Pass:
    work_id, repo = str(member["work_id"]), str(member["repo"])
    emit(
        {
            "event": "launch",
            "repo": repo,
            "work_id": work_id,
            "generation": generation,
            "state": "refused",
            "detail": detail,
        }
    )
    _release(hub, work_id, nonce, generation)
    return Pass("refused", repo, work_id, generation, detail=detail)


def _work(
    hub: Hub,
    campaign: Mapping[str, Any],
    member: Mapping[str, Any],
    grant: Mapping[str, Any],
    nonce: str,
    *,
    reclaim: bool,
    progress: dict[str, Launch],
    node_id: str,
    repos: Mapping[str, Path],
    launcher: Launcher,
    emit: Emit,
    holds: Holds,
    now: Callable[[], datetime],
) -> Pass:
    work_id, repo, generation = str(member["work_id"]), str(member["repo"]), int(grant["generation"])
    payload = hub.get_item(work_id)
    item = payload["item"]
    if reclaim:
        # The previous holder expired. If it linked a run the Hub never counted, that run may still
        # be going or may have finished. Do not start a second one; an operator decides.
        runs = [x for x in payload.get("links", []) if isinstance(x, dict) and x.get("link_type") == "fleet-run"]
        if len(runs) > int(item.get("attempt_count", 0)):
            detail = "a previous generation linked a run with no failed attempt recorded; it may be running or finished"
            emit(
                {"event": "previous-run", "repo": repo, "work_id": work_id, "generation": generation, "detail": detail}
            )
            _park(
                hub,
                repo,
                work_id,
                Launch("refused"),
                nonce,
                generation,
                "blocked",
                detail + "; review it before rerunning",
            )
            return Pass("previous-run", repo, work_id, generation, detail=detail)
    budget, lease_text = _budget_seconds(campaign, grant, now())
    if budget < MIN_RUN_SECONDS:
        needed = int(MIN_RUN_SECONDS + LEASE_MARGIN_SECONDS)
        detail = (
            f"lease too short to start: {lease_text}, need at least {needed} seconds; raise lease_seconds in the offer"
        )
        return _hand_back(hub, member, nonce, generation, emit, detail)
    try:
        launch = launcher(
            repos[repo],
            title=str(campaign.get("title", "")),
            repo=repo,
            work_id=work_id,
            description="\n\n".join(
                t for t in (str(campaign.get("description", "")), str(item.get("description", ""))) if t
            ),
            acceptance=list(campaign.get("acceptance", [])),
            deadline_seconds=budget,
        )
    except (OSError, ValueError) as exc:
        launch = Launch("refused", detail=type(exc).__name__)
    progress["launch"] = launch
    emit({"event": "launch", "repo": repo, "work_id": work_id, "generation": generation, **_launch_fields(launch)})
    try:
        if launch.run_id is None:
            if launch.state == "refused":
                # Nothing ran, so there is no attempt to record. Hand the member back.
                _release(hub, work_id, nonce, generation)
                return Pass("refused", repo, work_id, generation, detail=launch.detail)
            # An intent failed before any run existed. The Hub only counts an attempt against a linked
            # run, so count it here and keep this node from repeating it until an operator clears it.
            emit(
                {
                    "event": "attempt",
                    "repo": repo,
                    "work_id": work_id,
                    "action": "failed",
                    "run_id": None,
                    "local": True,
                }
            )
            holds.add(work_id, f"dispatch {launch.state} before a run existed", now())
            _release(hub, work_id, nonce, generation)
            return Pass("failed", repo, work_id, generation, detail=launch.detail or launch.state)
        try:
            hub.link_execution(
                work_id, {"node_id": node_id, "run_id": launch.run_id}, generation=generation, holder_nonce=nonce
            )
        except WorkloreClientError as exc:
            if _code(exc) != "execution-mismatch":
                raise
            # The Hub never saw this run, so it cannot count an attempt. Park the member for
            # an operator instead of letting the next pass repeat an unrecorded run.
            _park(hub, repo, work_id, launch, nonce, generation, "blocked", "run is not visible to the hub")
            return Pass("unlinked", repo, work_id, generation, launch.run_id)
        _attempt(hub, work_id, "started", launch.run_id, nonce, generation)
        emit({"event": "attempt", "repo": repo, "work_id": work_id, "action": "started", "run_id": launch.run_id})
        if launch.state != _COMPLETED:
            _attempt(hub, work_id, "failed", launch.run_id, nonce, generation)
            emit({"event": "attempt", "repo": repo, "work_id": work_id, "action": "failed", "run_id": launch.run_id})
            _release(hub, work_id, nonce, generation)
            return Pass("failed", repo, work_id, generation, launch.run_id, launch.detail or launch.state)
        _park(hub, repo, work_id, launch, nonce, generation, "await-review", f"run {launch.run_id} on {launch.branch}")
        emit({"event": "checkpoint", "repo": repo, "work_id": work_id, "run_id": launch.run_id})
        return Pass("parked", repo, work_id, generation, launch.run_id)
    except WorkloreClientError as exc:
        # Expiry, reclaim or cancellation fenced this holder. The local run is not lost, but the
        # Hub no longer accepts writes from this generation and has no checkpoint or counted attempt
        # for it, so any later pass would take the member again. Hold it on this node.
        if _code(exc) not in {"stale-generation", "holder-mismatch", "campaign-ineligible", "ownership-conflict"}:
            raise
        emit({"event": "lost-grant", "repo": repo, "work_id": work_id, "generation": generation, "code": _code(exc)})
        if launch.state != "refused":
            holds.add(work_id, f"lost grant ({_code(exc)}) after a run started", now())
            emit({"event": "hold", "repo": repo, "work_id": work_id, "reason": "lost-grant"})
        return Pass("lost-grant", repo, work_id, generation, launch.run_id)


def _launch_fields(launch: Launch) -> dict[str, Any]:
    return {"state": launch.state, "run_id": launch.run_id, "branch": launch.branch, "detail": launch.detail}


def _attempt(hub: Hub, work_id: str, action: str, run_id: str, nonce: str, generation: int) -> None:
    version = hub.get_item(work_id)["item"]["version"]
    hub.record_attempt(
        work_id, {"action": action, "run_id": run_id}, if_match=version, generation=generation, holder_nonce=nonce
    )


def _park(hub: Hub, repo: str, work_id: str, launch: Launch, nonce: str, generation: int, kind: str, why: str) -> None:
    ownership = hub.get_ownership(work_id)["ownership"]
    body = {**_checkpoint(repo, launch, launch.revision or "0" * 40, kind, why), "generation": generation}
    hub.ownership_action(
        work_id,
        body,
        if_match=ownership["revision"],
        idempotency_key="checkpoint-" + secrets.token_hex(16),
        holder_nonce=nonce,
    )


def _release(hub: Hub, work_id: str, nonce: str, generation: int) -> None:
    ownership = hub.get_ownership(work_id)["ownership"]
    hub.ownership_action(
        work_id,
        {"action": "release", "generation": generation},
        if_match=ownership["revision"],
        idempotency_key="release-" + secrets.token_hex(16),
        holder_nonce=nonce,
    )


def consume(
    hub: Hub,
    *,
    watch: bool,
    **kwargs: Any,
) -> list[Pass]:
    """One pass, or passes until no member is eligible. A member is tried once per call."""
    passes: list[Pass] = []
    kwargs.setdefault("tried", set())
    kwargs.setdefault("holds", Holds())
    while True:
        step = consume_once(hub, **kwargs)
        if step.outcome == "idle":
            if not passes:
                passes.append(step)
            return passes
        passes.append(step)
        if not watch:
            return passes


def _prerequisites(repo_path: Path, worker: str) -> str | None:
    """The refusals dispatch would make, checked before a task is added to the ledger."""
    from . import roster
    from .work_cmd import dispatch as dispatching

    if sys.platform == "win32":
        return "work dispatch apply is unsupported on Windows"
    try:
        configured = roster.load_roster(roster.resolve_roster_path(repo_path))
    except (OSError, ValueError) as exc:
        return f"no usable roster: {type(exc).__name__}"
    if worker not in configured.agents:
        return f"unknown roster seat: {worker}"
    if dispatching._base_sha(repo_path, "HEAD") is None:
        return "no committed Git base"
    return None


def _local_task(repo_path: Path, text: str, work_id: str, acceptance: Sequence[str]) -> tuple[str | None, str]:
    """The one local task for this campaign work id, reopened if an earlier pass cancelled it."""
    from .work_cmd import helpers
    from .work_cmd.ledger import locking, tasks_plans

    with locking._task_ledger_lock(repo_path):
        ledger = locking._read_task_ledger(repo_path)
        for task in ledger["tasks"]:
            if isinstance(task, dict) and (task.get("metadata") or {}).get("campaign_work_id") == work_id:
                if task.get("status") == "pending":
                    return str(task["id"]), ""
                if task.get("status") != "cancelled":
                    return None, f"local task for this member is {task.get('status')}"
                task["status"], task["updated_at"] = "pending", helpers._now().isoformat()
                locking._write_task_ledger(repo_path, ledger)
                return str(task["id"]), ""
        task, _ = tasks_plans._apply_add_task_to_ledger(
            repo_path,
            ledger,
            text,
            source="campaign",
            metadata={"campaign_work_id": work_id},
            acceptance=list(acceptance) or None,
            dedupe=False,
        )
        locking._write_task_ledger(repo_path, ledger)
        return str(task["id"]), ""


def _retire_task(repo_path: Path, task_id: str) -> None:
    """Cancel the local task unless it is still running, so a plain dispatch cannot pick it up."""
    from .work_cmd import helpers
    from .work_cmd.ledger import locking

    with locking._task_ledger_lock(repo_path):
        ledger = locking._read_task_ledger(repo_path)
        for task in ledger["tasks"]:
            if isinstance(task, dict) and task.get("id") == task_id and task.get("status") == "pending":
                task["status"], task["updated_at"] = "cancelled", helpers._now().isoformat()
                locking._write_task_ledger(repo_path, ledger)


def _dispatch_detail(out: str, err: str, code: int, task_id: str) -> str:
    detail = ""
    try:
        payload = json.loads(out)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = str(payload.get("error") or "")
        plan = payload.get("plan")
        rows = plan.get("skipped", []) if isinstance(plan, dict) else []
        skipped = [s for s in rows if isinstance(s, dict) and s.get("task_id") == task_id]
        detail = detail or ", ".join(str(s.get("reason")) for s in skipped)
    lines = (err or out).strip().splitlines()
    return f"dispatch exit {code}: {detail or (lines[-1] if lines else 'no intent was created')}"[:300]


def _retire_quietly(repo_path: Path, task_id: str) -> None:
    """Cancel the local task, best effort. A cleanup failure must never hide a run that happened."""
    with contextlib.suppress(Exception):
        _retire_task(repo_path, task_id)


def launch_local(
    repo_path: Path,
    *,
    title: str,
    repo: str,
    work_id: str,
    description: str,
    acceptance: Sequence[str],
    worker: str,
    deadline_seconds: float | None = None,
) -> Launch:
    """Run one local task for the member through ``work dispatch`` apply, and only it.

    The task is reused per work id and cancelled again unless it is still running, so a failed,
    refused or timed-out pass leaves nothing a plain ``work dispatch`` could run outside the fence.
    ``deadline_seconds`` is dispatch's own wall-clock budget: it stops the child and fails the intent.
    """
    from .work_cmd import dispatch as dispatching
    from .work_cmd import dispatch_state

    started = time.monotonic()
    refusal = _prerequisites(repo_path, worker)
    if refusal:
        return Launch("refused", detail=refusal)
    text = f"{title} [{repo}]\n\n{description}".strip()
    task_id, busy = _local_task(repo_path, text, work_id, acceptance)
    if task_id is None:
        return Launch("refused", detail=busy)
    if deadline_seconds is not None:
        # Setup (prerequisites, the ledger lock) spends lease too; the child gets only what is left.
        deadline_seconds -= time.monotonic() - started
        if deadline_seconds <= 0:
            _retire_quietly(repo_path, task_id)
            return Launch("refused", detail="lease used up during setup")
    try:
        seen = {row["intent_id"] for _, row in dispatch_state.intents(repo_path)}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatching.dispatch(
                target=repo_path,
                worker=worker,
                max_workers=1,
                json_output=True,
                only_tasks=[task_id],
                deadline=deadline_seconds,
            )
        fresh = [
            row
            for _, row in dispatch_state.intents(repo_path)
            if row["task_id"] == task_id and row["intent_id"] not in seen
        ]
    finally:
        _retire_quietly(repo_path, task_id)
    if not fresh:
        return Launch("refused", detail=_dispatch_detail(out.getvalue(), err.getvalue(), code, task_id))
    row = max(fresh, key=lambda r: r["attempt_no"])
    terminal = row.get("terminal") or {}
    changes = terminal.get("changes") or {}
    revision = changes.get("commit") or changes.get("head") or row.get("base_sha")
    detail = "stopped at the lease deadline" if terminal.get("run_status") == "timeout" else ""
    return Launch(row["state"], row.get("run_id"), row.get("branch"), revision, detail)


def holds_path(target: Path, campaign_key: str) -> Path:
    return target / ".brigade" / "campaign-consume" / f"{campaign_key}.holds.json"


def receipt_writer(target: Path, campaign_key: str) -> Emit:
    """Append-only JSONL run receipt under the workspace, one line per take, launch and attempt."""
    path = target / ".brigade" / "campaign-consume" / f"{campaign_key}.jsonl"

    def emit(event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now().isoformat(), **event}, sort_keys=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    return emit
