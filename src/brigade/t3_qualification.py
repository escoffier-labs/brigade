"""Run or summarize the five matched T3 and run-batch qualification pairs."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from statistics import median
from string import Template
from typing import Any

from . import (
    agents,
    inbox_lock,
    localio,
    native_t3_capacity,
    native_t3_enrollment,
    native_t3_lifecycle,
    orca_qualification,
    roster,
    run_batch,
)
from . import t3_qualification_evidence as evidence
from .codex_appserver import AppServer, AppServerError
from .native_child_host import NativeChildCapacityError, NativeChildCustodyError, NativeChildError
from .native_t3_contract import NativeTarget
from .run_journal import RunJournalError
from .run_tasks import TaskJournalError

WORKLOAD = {
    "guide": "docs/run-batch.md",
    "package": "pyproject.toml",
    "runner": "src/brigade/run_batch.py",
}
ARMS = ("t3", "run-batch")
CAMPAIGN_SCHEMA = "brigade.t3_qualification_campaign.v1"
METRICS = {
    "queue_and_startup": "queue",
    "planning": "planning",
    "execution": "execution",
    "synthesis": "synthesis",
    "delivery": "delivery",
}
ERRORS = ("failed_tasks", "nonterminal_tasks", "circuit_broken_tasks", "other")
CLI = (sys.executable, "-m", "brigade")


def schedule() -> list[tuple[int, str]]:
    return [(pair, arm) for pair in range(1, 6) for arm in (ARMS if pair % 2 else ARMS[::-1])]


def envelope(base_sha: str, brigade_commit: str, workload_digest: str) -> dict[str, Any]:
    return {
        "base_sha": base_sha,
        "brigade_commit": brigade_commit,
        "workload_digest": workload_digest,
        "model": evidence.MODEL,
        "effort": evidence.EFFORT,
        "service_tier": evidence.TIER,
        "isolation": "worktree",
        "max_workers": evidence.SLOTS,
        "read_only": True,
        "synthesis": False,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Refuse medians unless all ten batches are verified, matched and sequential."""
    require = evidence.require
    order = schedule()
    require(len(rows) == len(order), "report requires five complete pairs")
    expected = rows[0]["envelope"]
    for key, size in (("base_sha", 40), ("brigade_commit", 40), ("workload_digest", 64)):
        require(re.fullmatch(rf"[0-9a-f]{{{size}}}", expected[key]), "invalid qualification envelope identity")
    require(
        expected == envelope(expected["base_sha"], expected["brigade_commit"], expected["workload_digest"]),
        "invalid qualification envelope",
    )
    previous_close = -1
    requests = set()
    for index, ((pair, arm), row) in enumerate(zip(order, rows, strict=True)):
        record = row["record"]
        require((row["pair"], record["arm"]) == (pair, arm), "batch alternation is broken")
        require(row["envelope"] == expected, "batch envelope differs from the experiment")
        require(
            record["model"] == expected["model"]
            and record["effort"] == expected["effort"]
            and record["source_revision"] == expected["base_sha"]
            and record["brigade_commit"] == expected["brigade_commit"]
            and record["isolation"] == expected["isolation"]
            and record["concurrency"] == expected["max_workers"]
            and record["read_only_enforcement"] == evidence.READ_ONLY_ENFORCEMENT[arm],
            "batch record differs from the matched envelope",
        )
        parent_settings = row["parent"]
        require(
            parent_settings
            == ({"model": evidence.MODEL, "effort": "medium", "service_tier": evidence.TIER} if arm == "t3" else None),
            "unexpected T3 parent settings",
        )
        if index % 2 == 0:
            require(row["request_id"] not in requests, "shared batch request reused in a different pair")
            requests.add(row["request_id"])
        else:
            require(
                all(row[key] == rows[index - 1][key] for key in ("request_id", "batch_fingerprint")),
                "pair does not share one shared batch request and fingerprint",
            )
        require(re.fullmatch(r"[0-9a-f]{64}", row["batch_fingerprint"]), "invalid shared batch fingerprint")
        start, close = (evidence.count(row[key]) for key in ("start_ns", "closed_ns"))
        require(previous_close <= start < close, "batch overlap or invalid monotonic interval")
        previous_close = close
        require(
            record["schema"] == evidence.RECORD_SCHEMA
            and record["configuration_mismatch"] is False
            and record["all_tasks_terminal"] is True
            and evidence.count(record["held_capacity"]) == 0
            and record["slot_check_passed"] is True
            and record["circuit_breaker_check_passed"] is True
            and record["verified_completion_seconds"] is not None,
            "batch has no verified completion",
        )
        elapsed = evidence.seconds(record["verified_completion_seconds"])
        phases = record["timings_seconds"]
        require(
            phases["startup"] is None
            and phases["planning"] == phases["synthesis"] == 0
            and math.isclose(sum(evidence.seconds(phases[key]) for key in METRICS.values()), elapsed)
            and elapsed <= (close - start) / 1e9,
            "batch timings do not match the measured interval",
        )
        require(record["usage"] == dict.fromkeys(evidence.COUNTERS), "usage must be unrecorded")
        evidence.count(record["parent_interventions"])
        for key in ERRORS:
            evidence.count(record["errors"][key])
        require(
            all(record["errors"][key] == 0 for key in ERRORS[:-1]),
            "batch has no verified completion",
        )

    arms = {}
    for arm in ARMS:
        records = [row["record"] for row in rows if row["record"]["arm"] == arm]
        arms[arm] = {
            "median_verified_completion_seconds": median(record["verified_completion_seconds"] for record in records),
            "median_timings_seconds": {
                label: median(record["timings_seconds"][key] for record in records) for label, key in METRICS.items()
            },
            "errors": {key: sum(record["errors"][key] for record in records) for key in ERRORS},
            "parent_interventions": sum(record["parent_interventions"] for record in records),
            "usage": "not recorded",
        }
    return {
        "schema": "brigade.t3_qualification_report.v1",
        "completed_pairs": 5,
        "queue_startup_note": "Queue and startup cannot be separated with the available timestamps.",
        "arms": arms,
        "batches": rows,
    }


def prepare_batch(workspace: Path, base: str, seat: str, request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    answers = {}
    tasks = []
    for task, path in WORKLOAD.items():
        content = subprocess.check_output(["git", "show", f"{base}:{path}"], cwd=workspace)
        answers[task] = {"head": base, "sha256": hashlib.sha256(content).hexdigest()}
        tasks.append(
            {
                "task_id": task,
                "seat": seat,
                "isolation": "worktree",
                "read_only": True,
                "text": (
                    f"In your assigned worktree, run git rev-parse HEAD and sha256sum -- {path}. "
                    'Return only a JSON object with two string fields named "head" and "sha256", '
                    "containing the observed revision and file digest. Do not change any files."
                ),
            }
        )
    return {"schema": run_batch.SCHEMA, "request_id": request_id, "tasks": tasks}, answers


def _preflight(workspace: Path, roster_path: Path, seat: str, base: str) -> NativeTarget:
    require = evidence.require
    require(evidence.clean_revision(workspace) == base, "workload checkout changed since the campaign began")
    evidence.check_native_idle(workspace)
    evidence.require(
        roster.resolve_roster_path(workspace) == roster_path,
        "use the roster that applies to the workload checkout for T3 claims",
    )
    loaded = roster.load_roster(roster_path)
    agent = loaded.agents[seat]
    target = native_t3_enrollment.target_for_seat(loaded, seat, source_revision=base)
    options = dict(target.options)
    max_live = loaded.t3_targets[seat].get("max_live", evidence.SLOTS)
    require(
        loaded.max_workers == evidence.SLOTS
        and seat != loaded.orchestrator
        and agent.cli == "codex"
        and agent.transport == "direct"
        and agent.command is None
        and agent.env is None
        and not agent.fallback
        and agent.model == target.launch_model == evidence.MODEL
        and agent.reasoning == options.get("reasoningEffort") == evidence.EFFORT
        and options.get("serviceTier") == evidence.TIER
        and target.driver == "codex"
        and target.target_environment == target.origin_environment
        and isinstance(max_live, int)
        and max_live >= evidence.SLOTS
        and agents.read_only_enforcement("codex", sandbox=loaded.sandbox) == "hard",
        "roster does not match the qualification envelope",
    )
    return target


def _write_brief(folder: Path, context: dict[str, Any], roster_path: Path) -> None:
    template = Path(__file__).with_name("templates") / "evals" / "t3-qualification-coordinator.txt"
    prefix = (sys.executable, "-m", "brigade.t3_qualification")

    def shell(*parts: str) -> str:
        return shlex.join(parts)

    workspace = context["workspace"]
    run = context["run_ids"]["t3"]
    parent = context["parent_thread"]
    finish = shell(*prefix, "finish-t3", str(folder))
    document = Template(template.read_text(encoding="utf-8")).substitute(
        folder=shell(str(folder)),
        read_expected=shell(str(folder / "expected.json")),
        ready=shell(
            *CLI,
            "roster",
            "ready",
            "--route",
            "t3",
            "--isolation",
            "worktree",
            "--concurrency",
            str(evidence.SLOTS),
            "--worker",
            context["seat"],
            "--roster",
            str(roster_path),
            "--target",
            workspace,
            "--t3-capabilities",
            "capabilities-report.json",
            "--json",
        ),
        mark=shell(*prefix, "mark", str(folder)),
        open=shell(
            *CLI, "t3", "open", str(folder.parent / "batch.json"), "--target", workspace, "--roster", str(roster_path)
        ),
        claim=shell(*CLI, "t3", "claim", run, "--parent-thread", parent, "--target", workspace),
        accepted=shell(*CLI, "t3", "accepted", run, "--parent-thread", parent, "--target", workspace),
        observe=shell(*CLI, "t3", "observe", run, "--parent-thread", parent, "--target", workspace),
        ack=shell(*CLI, "t3", "ack", run, "--target", workspace),
        finalize=shell(*CLI, "t3", "finalize", run, "--target", workspace),
        finish=finish,
        abort=shell(*prefix, "abort-t3", str(folder)),
    )
    localio.write_text_atomic(folder / "coordinator.txt", document)


def _wait_for_parent(folder: Path, timeout: float) -> None:
    print(f"Give this coordinator brief to the expected T3 parent: {folder / 'coordinator.txt'}", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (folder / "failure.json").exists():
            raise ValueError("T3 parent recorded a failed batch. See failure.json in the arm directory.")
        if (folder / "measurements.json").exists():
            return
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    raise ValueError("T3 parent did not publish measurements before the deadline")


def _check_direct_tier(workspace: Path) -> None:
    """Read Codex configuration without opening a thread or starting a turn."""
    server = AppServer(cwd=workspace)
    try:
        server.start(timeout=10)
        settings = server.request("config/read", {"cwd": str(workspace), "includeLayers": False}, timeout=10)
        evidence.require(
            settings["config"]["service_tier"] == evidence.TIER,
            "direct Codex effective service tier must be explicitly default",
        )
    except (AppServerError, KeyError, TypeError) as exc:
        raise ValueError("could not establish the direct Codex service tier") from exc
    finally:
        server.close()


def _direct(workspace: Path, roster_path: Path, folder: Path, sessions: Path, context: dict[str, Any]) -> None:
    _check_direct_tier(workspace)
    run = context["run_ids"]["run-batch"]
    run_dir = native_t3_capacity.runs_root(workspace) / run
    argv = [
        *CLI,
        "run",
        "batch",
        str(folder.parent / "batch.json"),
        "--cwd",
        str(workspace),
        "--roster",
        str(roster_path),
        "--json",
    ]
    with (folder / "status.json").open("xb") as stdout, (folder / "stderr.log").open("xb") as stderr:
        localio.write_json_exclusive(folder / "opened.json", evidence.clock())
        completed = subprocess.run(argv, cwd=workspace, stdout=stdout, stderr=stderr, check=False)
    evidence.require(
        completed.returncode == 0, f"run-batch exited {completed.returncode}. See status.json and stderr.log."
    )
    localio.write_json_exclusive(
        folder / "measurements.json", evidence.measure("run-batch", workspace, run_dir, folder, sessions)
    )


def run(args: argparse.Namespace) -> None:
    workspace, output, roster_path, sessions = (
        p.expanduser().resolve() for p in (args.workspace, args.output, args.roster, args.codex_sessions)
    )
    evidence.require(not output.is_relative_to(workspace), "put campaign evidence outside the workload checkout")
    evidence.require(sessions.is_dir(), "direct Codex session directory does not exist")
    base = evidence.clean_revision(workspace)
    evidence.require(
        re.fullmatch(r"[0-9a-f]{40}", args.brigade_commit), "expected the executable's 40-character commit"
    )
    target = _preflight(workspace, roster_path, args.seat, base)
    runs = native_t3_capacity.runs_root(workspace)
    lock_dir = runs.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    with inbox_lock.held_file_lock(lock_dir / "qualification-2046.lock", deadline_seconds=1):
        output.mkdir(mode=0o700)
        _run_campaign(args, workspace, output, roster_path, sessions, base, target)


def _run_campaign(
    args: argparse.Namespace,
    workspace: Path,
    output: Path,
    roster_path: Path,
    sessions: Path,
    base: str,
    target: NativeTarget,
) -> None:
    rows: list[dict[str, Any]] = []
    campaign = {
        "schema": CAMPAIGN_SCHEMA,
        "campaign_id": uuid.uuid4().hex,
        "workspace": str(workspace),
        "base_sha": base,
        "brigade_commit": args.brigade_commit,
        "seat": args.seat,
    }
    localio.write_json_exclusive(output / "campaign.json", campaign)
    for pair, arm in schedule():
        evidence.require(
            _preflight(workspace, roster_path, args.seat, base) == target, "retained roster target changed"
        )
        pair_dir = output / f"pair-{pair:02d}"
        request, run_ids = _pair_identities(campaign, pair)
        payload, answers = prepare_batch(workspace, base, args.seat, request)
        fingerprint = run_batch.request_fingerprint(payload, base_sha=base)
        context = {
            "qualification": "t3",
            "pair": pair,
            "arm": arm,
            "workspace": str(workspace),
            "request_id": request,
            "base_sha": base,
            "batch_fingerprint": fingerprint,
            "brigade_commit": args.brigade_commit,
            "answers": answers,
            "run_ids": run_ids,
            "seat": args.seat,
            "parent_thread": target.origin_thread,
            "environment": target.origin_environment,
            "project": target.target_project,
            "provider_instance": target.provider_instance,
        }
        if not pair_dir.exists():
            pair_dir.mkdir()
            localio.write_json_exclusive(pair_dir / "batch.json", payload)
        else:
            evidence.require(evidence.read(pair_dir / "batch.json") == payload, "shared batch was altered")
        folder = pair_dir / arm
        folder.mkdir()
        localio.write_json_exclusive(folder / "expected.json", context)
        for log in ("interventions.jsonl", "errors.jsonl"):
            (folder / log).touch(exist_ok=False)
        try:
            localio.write_json_exclusive(folder / "offered.json", evidence.clock())
            if arm == "t3":
                _write_brief(folder, context, roster_path)
                _wait_for_parent(folder, args.parent_timeout)
            else:
                _direct(workspace, roster_path, folder, sessions, context)
            row = _collect_row(output, pair, arm, closing=True)
            evidence.require(not rows or rows[-1]["closed_ns"] <= row["start_ns"], "batch overlap")
            localio.write_json_exclusive(folder / "record.json", row)
            rows.append(row)
        except QUALIFICATION_ERRORS as exc:
            failure = folder / "failure.json"
            if not failure.exists():
                localio.write_json_exclusive(failure, {"error": str(exc), "pair": pair, "arm": arm})
            raise
    localio.write_json_exclusive(output / "report.json", summarize(rows))


def _pair_identities(campaign: dict[str, Any], pair: int) -> tuple[str, dict[str, str]]:
    evidence.require(campaign["schema"] == CAMPAIGN_SCHEMA, "invalid qualification campaign")
    evidence.require(re.fullmatch(r"[0-9a-f]{32}", campaign["campaign_id"]), "invalid campaign identity")
    request = f"qualification-{campaign['campaign_id']}-{pair}"
    digest = hashlib.sha256(request.encode()).hexdigest()
    return request, {"t3": f"t3-{digest}", "run-batch": f"batch-{digest}"}


def _collect_row(output: Path, pair: int, arm: str, *, closing: bool = False) -> dict[str, Any]:
    campaign = evidence.read(output / "campaign.json")
    pair_dir = output / f"pair-{pair:02d}"
    folder = pair_dir / arm
    evidence.require(not (folder / "failure.json").exists(), "qualification arm failed")
    context = evidence.read(folder / "expected.json")
    request, run_ids = _pair_identities(campaign, pair)
    evidence.require(
        type(context["pair"]) is int
        and context["pair"] == pair
        and context["arm"] == arm
        and context["qualification"] == "t3"
        and context["request_id"] == request
        and context["run_ids"] == run_ids
        and all(context[key] == campaign[key] for key in ("workspace", "base_sha", "brigade_commit", "seat")),
        "pair directory does not match its qualification identities",
    )
    workspace = Path(campaign["workspace"]).resolve()
    base = campaign["base_sha"]
    payload, answers = prepare_batch(workspace, base, campaign["seat"], request)
    fingerprint = run_batch.request_fingerprint(payload, base_sha=base)
    evidence.require(
        evidence.read(pair_dir / "batch.json") == payload
        and context["batch_fingerprint"] == fingerprint
        and context["answers"] == answers,
        "pair does not bind the shared batch fingerprint and pinned answers",
    )
    record = orca_qualification.collect(
        arm,
        native_t3_capacity.runs_root(workspace) / run_ids[arm],
        folder / "status.json",
        folder / "measurements.json",
        qualification="t3",
    )
    parent = evidence.parent_settings(folder, context) if arm == "t3" else None
    if closing:
        evidence.check_native_idle(workspace)
        localio.write_json_exclusive(folder / "closed.json", {**evidence.clock(), "native_live": 0})
    close_mark = evidence.read(folder / "closed.json")
    evidence.require(evidence.count(close_mark["native_live"]) == 0, "batch did not close with zero native capacity")
    offered, start, verified = (
        evidence.count(evidence.read(folder / f"{mark}.json")["monotonic_ns"])
        for mark in ("offered", "opened", "verified")
    )
    closed = evidence.count(close_mark["monotonic_ns"])
    evidence.require(offered <= start < verified <= closed, "batch has invalid or stale measurement marks")
    digest = run_batch.request_fingerprint({**payload, "request_id": "qualification-workload"}, base_sha=base)
    return {
        "pair": pair,
        "start_ns": start,
        "closed_ns": closed,
        "run_id": run_ids[arm],
        "request_id": request,
        "batch_fingerprint": fingerprint,
        "parent": parent,
        "envelope": envelope(base, campaign["brigade_commit"], digest),
        "record": record,
    }


def report_campaign(output: Path) -> dict[str, Any]:
    rows = []
    for pair, arm in schedule():
        saved = output / f"pair-{pair:02d}" / arm / "record.json"
        evidence.require(saved.is_file(), "report requires five complete pairs")
        row = _collect_row(output, pair, arm)
        evidence.require(evidence.read(saved) == row, f"pair {pair} {arm} record differs from retained evidence")
        rows.append(row)
    return summarize(rows)


def finish_t3(folder: Path) -> None:
    context = evidence.read(folder / "expected.json")
    workspace = Path(context["workspace"])
    run_id = context["run_ids"]["t3"]
    evidence.parent_settings(folder, context)
    status = native_t3_lifecycle.status(workspace=workspace, run_id=run_id)
    localio.write_json_exclusive(folder / "status.json", status)
    measurements = evidence.measure("t3", workspace, native_t3_capacity.runs_root(workspace) / run_id, folder)
    localio.write_json_exclusive(folder / "measurements.json", measurements)


QUALIFICATION_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    OverflowError,
    subprocess.CalledProcessError,
    RunJournalError,
    TaskJournalError,
    NativeChildError,
    NativeChildCustodyError,
    NativeChildCapacityError,
    inbox_lock.InboxLockTimeout,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    campaign = commands.add_parser("run", help="Run one new five-pair campaign.")
    for name in ("workspace", "output", "roster", "codex-sessions"):
        campaign.add_argument(f"--{name}", type=Path, required=True)
    campaign.add_argument("--seat", required=True)
    campaign.add_argument("--brigade-commit", required=True)
    campaign.add_argument("--parent-timeout", type=float, default=3600)
    report = commands.add_parser("report", help="Rebuild a complete report from retained batch evidence.")
    report.add_argument("output", type=Path)
    for name in ("mark", "finish-t3", "abort-t3"):
        action = commands.add_parser(name)
        action.add_argument("folder", type=Path)
        if name == "mark":
            action.add_argument(
                "name",
                choices=(
                    "opened",
                    "delegate-start",
                    "delegate-replies",
                    *(f"{kind}-{task}" for kind in ("delegate-reply", "wake") for task in WORKLOAD),
                ),
            )
        elif name == "abort-t3":
            action.add_argument("error")
    args = parser.parse_args()
    try:
        if args.command == "run":
            evidence.require(math.isfinite(args.parent_timeout) and args.parent_timeout > 0, "invalid parent timeout")
            run(args)
            print(args.output / "report.json")
        elif args.command == "report":
            localio.write_json(args.output / "report.json", report_campaign(args.output))
            print(args.output / "report.json")
        elif args.command == "mark":
            localio.write_json_exclusive(args.folder / f"{args.name}.json", evidence.clock())
        elif args.command == "finish-t3":
            finish_t3(args.folder)
        else:
            localio.write_json_exclusive(args.folder / "failure.json", {"error": args.error})
    except QUALIFICATION_ERRORS as exc:
        parser.exit(2, f"Qualification stopped: {exc}\n")


if __name__ == "__main__":
    main()
