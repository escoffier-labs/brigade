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
_FAILED_STATES = frozenset({"failed", "orphaned", "canceled", "unknown", "lost_race"})


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
    members: Sequence[Mapping[str, Any]], repos: Mapping[str, Path], tried: set[str]
) -> list[Mapping[str, Any]]:
    """Unowned members first, then in-flight ones whose holder may have died; local repos only."""
    rows = [m for m in members if m["repo"] in repos and m["work_id"] not in tried]
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
    now: Callable[[], datetime] = _now,
) -> Pass:
    """Take and work the next eligible member. Selection is Hub state, so a rerun is a resume."""
    shown = hub.get_campaign(campaign_key)
    members = [m for m in shown.get("members", []) if isinstance(m, dict)]
    campaign = shown.get("campaign", {})
    for member in _candidates(members, repos, tried):
        work_id, repo = str(member["work_id"]), str(member["repo"])
        ownership = hub.get_ownership(work_id)["ownership"]
        if ownership.get("state") in _HELD and not _reclaimable(ownership, now()):
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
        return _work(hub, campaign, member, grant, nonce, node_id=node_id, repos=repos, launcher=launcher, emit=emit)
    return Pass("idle")


def _work(
    hub: Hub,
    campaign: Mapping[str, Any],
    member: Mapping[str, Any],
    grant: Mapping[str, Any],
    nonce: str,
    *,
    node_id: str,
    repos: Mapping[str, Path],
    launcher: Launcher,
    emit: Emit,
) -> Pass:
    work_id, repo, generation = str(member["work_id"]), str(member["repo"]), int(grant["generation"])
    item = hub.get_item(work_id)["item"]
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
        )
    except (OSError, ValueError) as exc:
        launch = Launch("refused", detail=type(exc).__name__)
    emit({"event": "launch", "repo": repo, "work_id": work_id, "generation": generation, **_launch_fields(launch)})
    try:
        if launch.run_id is None:
            # Nothing ran, so there is no attempt to record. Hand the member back.
            _release(hub, work_id, nonce, generation)
            return Pass("refused", repo, work_id, generation)
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
        if launch.state in _FAILED_STATES:
            _attempt(hub, work_id, "failed", launch.run_id, nonce, generation)
            emit({"event": "attempt", "repo": repo, "work_id": work_id, "action": "failed", "run_id": launch.run_id})
            _release(hub, work_id, nonce, generation)
            return Pass("failed", repo, work_id, generation, launch.run_id)
        _park(hub, repo, work_id, launch, nonce, generation, "await-review", f"run {launch.run_id} on {launch.branch}")
        emit({"event": "checkpoint", "repo": repo, "work_id": work_id, "run_id": launch.run_id})
        return Pass("parked", repo, work_id, generation, launch.run_id)
    except WorkloreClientError as exc:
        # Expiry, reclaim or cancellation fenced this holder. The local run is not lost,
        # but the Hub no longer accepts writes from this generation.
        if _code(exc) not in {"stale-generation", "holder-mismatch", "campaign-ineligible", "ownership-conflict"}:
            raise
        emit({"event": "lost-grant", "repo": repo, "work_id": work_id, "generation": generation, "code": _code(exc)})
        return Pass("lost-grant", repo, work_id, generation, launch.run_id)


def _launch_fields(launch: Launch) -> dict[str, Any]:
    return {"state": launch.state, "run_id": launch.run_id, "branch": launch.branch}


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
    while True:
        step = consume_once(hub, **kwargs)
        if step.outcome == "idle":
            if not passes:
                passes.append(step)
            return passes
        passes.append(step)
        if not watch:
            return passes


def launch_local(
    repo_path: Path, *, title: str, repo: str, work_id: str, description: str, acceptance: Sequence[str], worker: str
) -> Launch:
    """Materialize one local task and run it through ``work dispatch`` apply, and only it."""
    from .work_cmd import dispatch as dispatching
    from .work_cmd import dispatch_state
    from .work_cmd.ledger import tasks_plans

    text = f"{title} [{repo}]\n\n{description}".strip()
    task, _ = tasks_plans._add_task(
        repo_path,
        text,
        source="campaign",
        metadata={"campaign_work_id": work_id},
        acceptance=list(acceptance) or None,
    )
    task_id = str(task["id"])
    seen = {row["intent_id"] for _, row in dispatch_state.intents(repo_path)}
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = dispatching.dispatch(
            target=repo_path, worker=worker, max_workers=1, json_output=True, only_tasks=[task_id]
        )
    fresh = [
        row
        for _, row in dispatch_state.intents(repo_path)
        if row["task_id"] == task_id and row["intent_id"] not in seen
    ]
    if not fresh:
        return Launch("refused", detail=f"dispatch exit {code}")
    row = max(fresh, key=lambda r: r["attempt_no"])
    changes = (row.get("terminal") or {}).get("changes") or {}
    revision = changes.get("commit") or changes.get("head") or row.get("base_sha")
    return Launch(row["state"], row.get("run_id"), row.get("branch"), revision)


def receipt_writer(target: Path, campaign_key: str) -> Emit:
    """Append-only JSONL run receipt under the workspace, one line per take, launch and attempt."""
    path = target / ".brigade" / "campaign-consume" / f"{campaign_key}.jsonl"

    def emit(event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now().isoformat(), **event}, sort_keys=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    return emit
