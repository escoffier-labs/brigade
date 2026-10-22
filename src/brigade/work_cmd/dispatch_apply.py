"""Resumable local dispatch apply, child supervision, and terminal closeout."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import localio, proc, run_lane_state, run_reap, runs_cmd, runguard, worker_failure
from ..aboyeur.run_io import make_run_dir
from ..isolated_exec import module_argv, trusted_cwd
from . import claiming, dispatch_state as store
from .ledger import locking
from .session.lifecycle import _render_task_run_prompt

CONTRACT = "\n\nDispatch contract:\n- Leave changes uncommitted.\n- Do not push.\n- Do not touch other tasks.\n"


class ChildJournalError(ValueError):
    """A lane needs inspection before its terminal receipt can be trusted."""


def _git(cwd: Path, *args: str) -> str:
    result = proc.run(["git", *args], cwd=cwd, timeout=120)
    if result.code != 0 or result.output_limit_exceeded:
        raise ValueError(result.stderr.strip() or "git dispatch operation failed")
    return result.stdout.strip()


def _new_intent(
    target: Path, directory: Path, session: dict[str, Any], plan: dict[str, Any], item: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    attempt = 1 + max(
        (row["attempt_no"] for _, row in store.intents(target) if row["task_id"] == item["task_id"]), default=0
    )
    key = f"{plan['plan_digest']}|{item['task_id']}|{item['task_item_revision']}|{attempt}"
    intent_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    slug = localio.slugify(item["task_id"], fallback="task")
    path = directory / "intents" / f"{intent_id}.json"
    now = localio.utc_now_iso()
    row = {
        "intent_id": intent_id,
        "task_id": item["task_id"],
        "task_item_revision": item["task_item_revision"],
        "attempt_no": attempt,
        "claim_id": "dispatch-" + intent_id,
        "seat": item["seat"],
        "base_sha": session["base_sha"],
        "branch": f"brigade/dispatch/{slug}-{intent_id[:8]}",
        "worktree": str(
            runguard.brigade_worktree_root() / f"{runguard.git_root(target).name}-task-{slug}-{intent_id[:8]}"
        ),
        "run_id": None,
        "run_dir": None,
        "pid": None,
        "state": "planned",
        "state_seq": 0,
        "created_at": now,
        "updated_at": now,
        "terminal": None,
        "landing": None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    localio.write_json_exclusive(path, row)
    store.transition(path, row, "planned")
    return path, row


def _release(target: Path, row: dict[str, Any]) -> None:
    locking._release_tasks(target, task_ids=[row["task_id"]], claim_ids=[row["claim_id"]])


_RELEASING = frozenset({"failed", "orphaned", "canceled"})
_TERMINAL_TEXT_LIMIT = 16 * 1024


def _bounded_text(value: Any, limit: int = _TERMINAL_TEXT_LIMIT) -> Any:
    """Keep terminal metadata inside the bounded event envelope."""
    if not isinstance(value, str):
        return value
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    kept = raw[:limit].decode("utf-8", errors="ignore")
    return f"{kept}\n[truncated {len(raw) - limit} bytes]"


def _finish(
    target: Path, path: Path, row: dict[str, Any], state: str, meta: dict[str, Any], *, commit: bool = False
) -> None:
    if row["state"] in store.TERMINAL - {"unknown"}:
        # Legacy receipts and crashes before recording release still reconcile.
        # Publish the marker only after the ledger mutation succeeds.
        if row["state"] in _RELEASING and not row.get("released"):
            _release(target, row)
            store.transition(path, row, row["state"], released=True)
        return
    kind, phase = worker_failure.resolve_run_failure_taxonomy(meta)
    infrastructure = worker_failure.run_failure_is_infrastructure_at_read_time(kind, phase) is True
    if kind == "branch-head-drift":
        state = "completed"
    changes: dict[str, Any] = {"branch": row["branch"]}
    if kind == "branch-head-drift":
        changes["needs_review"] = True
    worktree = Path(row["worktree"])
    if state == "completed":
        try:
            changes["diff_stat"] = _bounded_text(_git(worktree, "diff", "--stat", row["base_sha"]))
            changes["untracked"] = _bounded_text(_git(worktree, "ls-files", "--others", "--exclude-standard"))
            head = _git(worktree, "rev-parse", "HEAD")
            if head != row["base_sha"]:
                changes["needs_review"] = True
                changes["head"] = head
            elif commit and _git(worktree, "status", "--porcelain"):
                # Child credentials and sandbox are no longer involved. A crash
                # after this commit is recognized as HEAD drift on recovery.
                paths: set[str] = set()
                for arguments in (
                    ("diff", "--name-only", "-z", "HEAD"),
                    ("ls-files", "--others", "--exclude-standard", "-z"),
                ):
                    result = proc.run(["git", *arguments], cwd=worktree, timeout=30)
                    if result.code != 0 or result.output_limit_exceeded:
                        raise ValueError("could not enumerate dispatch changes for commit")
                    paths.update(
                        name
                        for name in result.stdout.split("\0")
                        if name and name != ".brigade" and not name.startswith(".brigade/")
                    )
                if paths:
                    ordered_paths = sorted(paths)
                    for start in range(0, len(ordered_paths), 100):
                        _git(worktree, "add", "-A", "--", *ordered_paths[start : start + 100])
                    _git(worktree, "commit", "--only", "-m", f"feat: dispatch {row['task_id']}", "--", *ordered_paths)
                    changes["commit"] = _git(worktree, "rev-parse", "HEAD")
        except (ValueError, OSError) as exc:
            changes.update(commit_error=_bounded_text(str(exc)), commit_failed=True, needs_review=True)
    terminal = {
        "run_status": meta.get("status"),
        "failure_kind": kind,
        "failure_phase": phase,
        "infrastructure": infrastructure,
        "changes": changes,
    }
    if meta.get("error"):
        terminal["error"] = _bounded_text(meta["error"])
    if (
        state != "completed"
        and store.event_room(path.parent.parent, reserve_events=1)
        and not store.event_room(path.parent.parent, reserve_events=2)
    ):
        # Only one envelope fits. Release first, then publish the terminal
        # state and the release marker together so the session can settle.
        _release(target, row)
        store.transition(path, row, state, terminal=terminal, released=True)
        return
    store.transition(path, row, state, terminal=terminal)
    if state != "completed":
        _release(target, row)
        store.transition(path, row, state, released=True)


def _session_result(rows: list[tuple[Path, dict[str, Any]]]) -> int:
    latest = {row["task_id"]: row for _, row in sorted(rows, key=lambda pair: pair[1]["attempt_no"])}
    return int(any(row["state"] in {"failed", "orphaned", "unknown"} for row in latest.values()))


def _terminal_state(meta: dict[str, Any]) -> str:
    status = meta.get("status")
    if status in runs_cmd._SUCCESS_STATUSES | {"completed", "complete"}:
        return "completed"
    return "orphaned" if status == "orphaned" else "failed"


def _child_argv(row: dict[str, Any], session: dict[str, Any], prompt: str) -> list[str]:
    argv = module_argv(
        "brigade",
        "run",
        "--worker",
        row["seat"],
        "--cwd",
        row["worktree"],
        "--output-dir",
        row["run_dir"],
        "--roster",
        session["roster_path"],
        "--wait",
        "0",
    )
    if session.get("run_budget"):
        argv += ["--run-budget", session["run_budget"]]
    return [*argv, prompt]


def _terminal_meta(run_dir: Path) -> dict[str, Any] | None:
    meta = localio.read_json_dict(run_dir / "run.json")
    try:
        terminal = runs_cmd._terminal_lifecycle_event(run_dir)
    except ValueError as exc:
        raise ChildJournalError(str(exc)) from exc
    if meta is None or (terminal is None and not runs_cmd._run_display_is_terminal(run_dir, meta)):
        return None
    if terminal is not None:
        label = terminal.event_type.rsplit(".", 1)[-1]
        status = terminal.payload.get("status")
        meta = {
            **meta,
            **dict(terminal.payload),
            "status": status
            or {"completed": "ok", "orphaned": "orphaned", "failed": "failed", "canceled": "canceled"}.get(
                label, label
            ),
        }
    return meta


def _reap(row: dict[str, Any]) -> None:
    run_dir = Path(row["run_dir"])
    bound = run_reap._bind_contained_run_dir(run_dir)
    if bound is None:
        return
    try:
        raw, meta = run_reap._read_bound_run_bytes(bound)
        if raw is not None and meta is not None:
            run_reap._reap_one(
                run_dir,
                meta,
                fingerprint=run_reap._run_fingerprint(raw),
                older_than=timedelta(0),
                now=datetime.now(timezone.utc),
                bound=bound,
            )
    finally:
        bound.close()


class Apply:
    """One dispatcher owns writes while children own independent process groups."""

    def __init__(self, target: Path):
        self.target = target
        self.registry = proc.ProcessRegistry()
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.lanes: dict[str, run_lane_state.Lane] = {}
        self.started = time.monotonic()
        self.last_launch: float | None = None
        self.adopted: set[Path] = set()
        self.settled: set[Path] = set()
        self.continue_on_error = False
        self.can_launch: Callable[[], bool] = lambda: True
        self.reservation_allowed: Callable[[], bool] = lambda: True
        self.pause: Callable[[float], None] = time.sleep

    def observe(self) -> None:
        lanes = run_lane_state.collect_lanes(self.target, limit=run_lane_state.MAX_LIMIT)
        self.lanes = {lane.run_dir.name: lane for lane in lanes if lane.run_dir is not None}

    def refresh(self, path: Path, row: dict[str, Any], session: dict[str, Any]) -> bool:
        try:
            return self._refresh(path, row, session)
        except ChildJournalError as exc:
            if row["state"] != "unknown":
                store.transition(path, row, "unknown", terminal={"error": str(exc)})
            return not store.child_alive(row)
        except (ValueError, OSError) as exc:
            self._intent_error(path, row, "receipt-update-error", exc)
            return True

    def _intent_error(self, path: Path, row: dict[str, Any], kind: str, exc: Exception) -> None:
        child = self.children.get(row["intent_id"])
        if child is not None:
            proc.terminate_process_tree(child)
        elif store.child_alive(row):
            self._cancel_adopted(row)
        _finish(
            self.target,
            path,
            row,
            "failed",
            {"status": "failed", "failure_kind": kind, "failure_phase": "dispatch", "error": str(exc)},
        )

    def _refresh(self, path: Path, row: dict[str, Any], session: dict[str, Any]) -> bool:
        if row["state"] in store.TERMINAL - {"unknown"}:
            _finish(self.target, path, row, row["state"], {}, commit=session.get("commit", False))
            return True
        if row["state"] not in {"workspace_ready", "launching", "running", "unknown"} or not row.get("run_dir"):
            return False
        run_dir = Path(row["run_dir"])
        receipt = localio.read_json_dict(run_dir / "dispatch-launch.json")
        if receipt is not None:
            pid = receipt.get("pid")
            if type(pid) is not int or pid <= 0:
                raise ChildJournalError("dispatch launch receipt has no valid positive integer pid")
            if row.get("pid") != pid:
                store.transition(path, row, "running", pid=pid, process_token=receipt.get("process_token"))
        child = self.children.get(row["intent_id"])
        if child is not None:
            child.poll()  # Reap owned children before checking PID liveness.
        alive = store.child_alive(row)
        meta = _terminal_meta(run_dir)
        if meta is not None:
            if alive:
                return False
            _finish(self.target, path, row, _terminal_state(meta), meta, commit=session.get("commit", False))
            return True
        if receipt is not None and receipt.get("launch_error") and not alive:
            _finish(
                self.target,
                path,
                row,
                "failed",
                {"status": "failed", "failure_kind": "transport-error", "failure_phase": "dispatch"},
            )
            return True
        dead_lane = False
        lane = self.lanes.get(row["run_id"])
        if lane is not None:
            classification = run_lane_state.classify(
                lane, datetime.now(timezone.utc), run_lane_state.Thresholds(older_than=timedelta(0))
            )
            dead_lane = classification.state in {"dead", "orphaned", "lost-worktree"}
            # A live worktree lock is stronger evidence than a launcher PID.
            if lane.owner_live is True and classification.state != "orphaned":
                # Refresh a retained lock observation before trusting it after
                # a process exits. Stale snapshots must not keep a lane alive.
                self.observe()
                current = self.lanes.get(row["run_id"])
                alive |= current is not None and current.owner_live is True
        if not alive and (receipt is not None or row.get("pid") is not None or dead_lane):
            # Re-read after observing exit. A child can publish terminal state
            # between the first snapshot read and poll().
            meta = _terminal_meta(run_dir)
            if meta is not None:
                _finish(
                    self.target,
                    path,
                    row,
                    _terminal_state(meta),
                    meta,
                    commit=session.get("commit", False),
                )
            elif not (run_dir / "run.json").exists():
                _finish(
                    self.target,
                    path,
                    row,
                    "failed",
                    {"status": "failed", "failure_kind": "executable-unavailable", "failure_phase": "dispatch"},
                )
            else:
                _reap(row)
                _finish(
                    self.target,
                    path,
                    row,
                    "orphaned",
                    _terminal_meta(run_dir) or {"status": "orphaned", "failure_kind": "owner-process-exited"},
                )
            return True
        if alive and row["state"] != "running":
            store.transition(path, row, "running")
        return False

    def advance(self, path: Path, row: dict[str, Any], session: dict[str, Any]) -> None:
        if row["state"] in {"planned", "claimed", "workspace_ready", "launching"} and self.last_launch is not None:
            remaining = session.get("spawn_delay", 0) - (time.monotonic() - self.last_launch)
            if remaining > 0:
                self.pause(remaining)
        if not self.can_launch() or (not row.get("pid") and not self.reservation_allowed()):
            return
        try:
            self._advance(path, row, session)
        except (ValueError, OSError) as exc:
            self._intent_error(path, row, "spawn-error", exc)

    def _advance(self, path: Path, row: dict[str, Any], session: dict[str, Any]) -> None:
        if self.refresh(path, row, session):
            return
        if row["state"] in {"planned", "claimed", "workspace_ready", "launching"}:
            try:
                claim = locking._claim_task(self.target, row["task_id"], actor=row["seat"], claim_id=row["claim_id"])
            except claiming.ClaimError as exc:
                if exc.exit_code == 13:
                    store.transition(path, row, "lost_race")
                    return
                _finish(
                    self.target,
                    path,
                    row,
                    "failed",
                    {
                        "status": "failed",
                        "failure_kind": "claim-refused",
                        "failure_phase": "dispatch",
                        "error": str(exc),
                    },
                )
                return
            if row["state"] == "planned":
                store.transition(path, row, "claimed", claimed_item_revision=claim["claim"]["item_revision"])
        worktree = Path(row["worktree"])
        if row["state"] == "claimed":
            worktree.parent.mkdir(parents=True, exist_ok=True)
            if worktree.exists():
                if _git(worktree, "branch", "--show-current") != row["branch"]:
                    raise ValueError("dispatch worktree branch does not match intent")
            else:
                existing = proc.run(
                    ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + row["branch"]], cwd=self.target
                )
                args = (
                    ["worktree", "add", str(worktree), row["branch"]]
                    if existing.code == 0
                    else ["worktree", "add", "-b", row["branch"], str(worktree), row["base_sha"]]
                )
                _git(self.target, *args)
            store.transition(path, row, "workspace_ready")
        if row["state"] == "workspace_ready" and not row.get("run_dir"):
            run_dir = make_run_dir(self.target / ".brigade/runs", workspace=self.target)
            # Publish the chosen name before mkdir or spawn.
            store.transition(path, row, "workspace_ready", run_id=run_dir.name, run_dir=str(run_dir))
        if row["state"] in {"workspace_ready", "launching"}:
            run_dir = Path(row["run_dir"])
            if (run_dir / "run.json").exists() or (run_dir / "dispatch-launch.json").exists():
                self.refresh(path, row, session)
                return
            if not self.can_launch():
                return
            run_dir.mkdir(parents=True, exist_ok=True)
            store.transition(path, row, "launching")
            task = next(
                task for task in locking._read_task_ledger(self.target)["tasks"] if task["id"] == row["task_id"]
            )
            argv = _child_argv(row, session, _render_task_run_prompt(task) + CONTRACT)
            try:
                with (run_dir / "dispatch-child.log").open("ab") as log:
                    # Worktree setup and prompt rendering take time. Re-check the
                    # watch deadline and launch ceiling at the final gate.
                    if not self.can_launch() or (not row.get("pid") and not self.reservation_allowed()):
                        return
                    child = subprocess.Popen(
                        module_argv("brigade.work_cmd.dispatch_child", str(run_dir), *argv),
                        cwd=trusted_cwd(),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        **proc.process_group_kwargs(),
                    )
            except OSError:
                _finish(
                    self.target,
                    path,
                    row,
                    "failed",
                    {"status": "failed", "failure_kind": "transport-error", "failure_phase": "dispatch"},
                )
                return
            self.registry.register(child, seat=row["seat"])
            self.children[row["intent_id"]] = child
            store.transition(path, row, "running", pid=child.pid, process_token=store.process_identity(child.pid)[1])
            self.last_launch = time.monotonic()

    def cancel(
        self, rows: list[tuple[Path, dict[str, Any]]], session: dict[str, Any], *, timeout: bool = False
    ) -> None:
        remaining = []
        registry = proc.ProcessRegistry()
        for path, row in rows:
            try:
                self.refresh(path, row, session)
            except KeyboardInterrupt:
                pass  # A repeated interrupt must not prevent cancellation.
            if row["state"] not in store.TERMINAL or row["state"] == "unknown":
                remaining.append((path, row))
                child = self.children.get(row["intent_id"])
                if child is not None:
                    registry.register(child, seat=row["seat"])
        registry.cancel()
        for path, row in remaining:
            if row["intent_id"] not in self.children and store.child_alive(row):
                self._cancel_adopted(row)
            # A child can publish success while cancellation is in flight.
            # Only read terminal evidence here. A dead nonterminal child was
            # canceled by us and must not be classified as an orphan.
            try:
                meta = _terminal_meta(Path(row["run_dir"])) if row.get("run_dir") else None
            except ChildJournalError:
                meta = None
            _finish(
                self.target,
                path,
                row,
                _terminal_state(meta) if meta is not None else ("failed" if timeout else "canceled"),
                meta
                if meta is not None
                else {
                    "status": "timeout" if timeout else "canceled",
                    "failure_kind": "budget-exhausted" if timeout else "operator-cancelled",
                },
                commit=session.get("commit", False),
            )

    def _cancel_adopted(self, row: dict[str, Any]) -> None:
        receipt = localio.read_json_dict(Path(row["run_dir"]) / "dispatch-launch.json")
        pid = receipt.get("pid") if receipt else row.get("pid")
        if type(pid) is not int or pid <= 0:
            pid = row.get("pid")
        if type(pid) is not int or pid <= 0 or not store.child_alive(row):
            return
        if os.name == "nt":
            result = proc.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=5)
            if result.code != 0 and store.child_alive(row):
                raise ValueError("could not cancel adopted dispatch child")
        elif proc.process_group_id(pid) == pid:
            try:
                os.killpg(pid, signal.SIGTERM)
                stop = time.monotonic() + 0.5
                while store.child_alive(row) and time.monotonic() < stop:
                    time.sleep(0.02)
                # The group can contain descendants after its leader exits.
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            raise ValueError("adopted child process group identity changed")
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass  # A resumed dispatcher generally is not the child's parent.
        if store.child_alive(row):
            raise ValueError("adopted child is still active, claim retained")

    def session(self, directory: Path, *, no_wait: bool = False, launch: bool = True) -> int:
        store.events(directory, repair_tail=True)
        session, plan = store.read(directory / "session.json"), store.read(directory / "plan.json")
        rows = [(path, store.read(path)) for path in sorted((directory / "intents").glob("*.json"))]
        denied = False
        elapsed_before = max(
            0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(session["created_at"])).total_seconds()
        )
        resumed_at = time.monotonic()

        def elapsed_seconds() -> float:
            return elapsed_before + time.monotonic() - resumed_at

        def budget_allows() -> bool:
            nonlocal denied
            launched = sum(row.get("run_dir") is not None for _, row in rows)
            elapsed = elapsed_seconds()
            deadline = session.get("deadline")
            ceiling = session.get("max_total")
            dimension = (
                "wall_clock_seconds"
                if deadline is not None and elapsed >= deadline
                else ("worker_dispatch_count" if ceiling is not None and launched >= ceiling else None)
            )
            if dimension is None:
                return True
            if not denied:
                store.event(
                    directory,
                    "run_budget.reservation_denied",
                    {
                        "dimension": dimension,
                        "mode": "enforceable",
                        "declared": int((deadline or 0) if dimension == "wall_clock_seconds" else (ceiling or 0)),
                        "used": int(elapsed) if dimension == "wall_clock_seconds" else launched,
                        "remaining": 0,
                        "request_id": session["dispatch_id"],
                        "reason_class": "reservation_denied",
                    },
                )
                denied = True
            return False

        def launch_item(item: dict[str, Any]) -> None:
            delay = session.get("spawn_delay", 0)
            if self.last_launch is not None and delay:
                self.pause(max(0, delay - (time.monotonic() - self.last_launch)))
            if not self.can_launch() or not self.reservation_allowed() or not budget_allows():
                return
            if item["rank"] >= plan["dispatch_count"] and not any(
                existing["task_id"] == item["task_id"] for _, existing in rows
            ):
                # Publish promotion before creating an intent that recovery
                # can launch without returning through this function.
                store.event(directory, "work.dispatch.backfill", {"task_id": item["task_id"]})
            path, row = _new_intent(self.target, directory, session, plan, item)
            rows.append((path, row))
            self.advance(path, row, session)

        try:
            # Existing intents are resumed from their saved plan, not the now
            # claimed ledger. Even a plan-only interrupted apply gets here.
            for path, row in rows:
                if store.supervised(row):
                    if self.refresh(path, row, session):
                        continue
                    if launch and session.get("deadline") is not None and elapsed_seconds() >= session["deadline"]:
                        budget_allows()
                        self.cancel(rows, session, timeout=True)
                        break
                    if launch:
                        self.advance(path, row, session)
                else:
                    self.refresh(path, row, session)
            attempted = {row["task_id"] for _, row in rows}
            accepted = sum(row["state"] != "lost_race" for _, row in rows)
            for item in plan["candidates"]:
                if not launch or not self.can_launch():
                    break
                if accepted >= plan["dispatch_count"]:
                    break
                if item["task_id"] in attempted:
                    continue
                launch_item(item)
                attempted.add(item["task_id"])
                accepted = sum(row["state"] != "lost_race" for _, row in rows)
            if no_wait:
                if launch:
                    self._retry(directory, session, plan, rows, launch_item)
                if all(not store.supervised(row) for _, row in rows):
                    session["finished_at"] = localio.utc_now_iso()
                    localio.write_json(directory / "session.json", session)
                return _session_result(rows)
            prior_result = 0
            while True:
                for previous in sorted(self.adopted):
                    previous_result = self.session(previous, no_wait=True)
                    if store.read(previous / "session.json").get("finished_at"):
                        prior_result = max(prior_result, previous_result)
                        self.adopted.discard(previous)
                if session.get("deadline") is not None and elapsed_seconds() >= session["deadline"]:
                    self.cancel(rows, session, timeout=True)
                    if not self.adopted:
                        break
                for path, row in list(rows):
                    self.refresh(path, row, session)
                retried = self._retry(directory, session, plan, rows, launch_item) if launch else False
                if not retried and not self.adopted and all(not store.supervised(row) for _, row in rows):
                    break
                time.sleep(0.05)
        except KeyboardInterrupt:
            self.cancel(rows, session)
            self._cancel_previous()
            return 130
        except Exception:
            if not self.continue_on_error:
                self.cancel(rows, session)
                self._cancel_previous()
            raise
        if all(not store.supervised(row) for _, row in rows):
            session["finished_at"] = localio.utc_now_iso()
            localio.write_json(directory / "session.json", session)
        return max(prior_result, _session_result(rows))

    def _cancel_previous(self) -> None:
        for directory in sorted(self.adopted):
            session = store.read(directory / "session.json")
            rows = [(path, store.read(path)) for path in (directory / "intents").glob("*.json")]
            self.cancel(rows, session)

    def _retry(
        self,
        directory: Path,
        session: dict[str, Any],
        plan: dict[str, Any],
        rows: list[tuple[Path, dict[str, Any]]],
        launch: Callable[[dict[str, Any]], None],
    ) -> bool:
        before = len(rows)
        for item in plan["candidates"]:
            attempts = [row for _, row in rows if row["task_id"] == item["task_id"]]
            if not attempts:
                continue
            latest = max(attempts, key=lambda row: row["attempt_no"])
            terminal = latest.get("terminal") or {}
            if (
                latest["state"] == "failed"
                and terminal.get("infrastructure") is True
                and terminal.get("failure_kind") != "receipt-update-error"
            ):
                if len(attempts) < 3:
                    launch(item)
                elif not latest.get("retry_exhausted"):
                    store.transition(
                        next(path for path, row in rows if row is latest), latest, "failed", retry_exhausted=True
                    )
                    store.event(
                        directory,
                        "work.dispatch.skipped",
                        {"task_id": item["task_id"], "reason": "infra-retries-exhausted"},
                    )
        return len(rows) > before

    def reconcile(self, *, no_wait: bool, launch: bool = True) -> int:
        self.observe()
        result = 0
        for directory in store.sessions(self.target):
            if directory in self.settled:
                continue
            session = store.read(directory / "session.json")
            if session.get("dry_run") or session.get("watch"):
                continue
            try:
                # Only failed, orphaned, and canceled rows have a claim-release
                # side effect that can still be pending.
                pending = False
                for path in (directory / "intents").glob("*.json"):
                    row = store.read(path)
                    if row["state"] in _RELEASING and not row.get("released"):
                        pending = True
                        self.refresh(path, row, session)
                if session.get("finished_at") and not pending:
                    # A finished session with nothing left to release stays settled.
                    self.settled.add(directory)
                if not session.get("finished_at"):
                    result = max(result, self.session(directory, no_wait=no_wait, launch=launch))
                    if not store.read(directory / "session.json").get("finished_at"):
                        self.adopted.add(directory)
            except Exception as exc:
                if not self.continue_on_error:
                    raise
                store.event(
                    directory,
                    "work.dispatch.tick_error",
                    {"dispatch_id": directory.name, "error_type": type(exc).__name__, "error": str(exc)},
                )
                result = max(result, 1)
        return result
