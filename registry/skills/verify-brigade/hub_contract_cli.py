"""`control-brigade.py hub-contract`: the parser and the command.

The core cases (`hub_contract.py`, with the refusal sweep) run on one hub on
real time. The lifecycle cases (`hub_contract_lifecycle.py`) run on a second
hub that reads the drive's clock, launched from `--clock-hub-cmd`, or from the
in-repo default when neither `--hub-cmd` nor `--hub-url` names another hub.
Every refusal's text and the lifecycle tombstones are then compared with
`hub-contract-golden.json` (`hub_contract_golden.py`).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

import hub_contract as hc
import hub_contract_golden as golden
import hub_contract_lifecycle as lifecycle

SKIP_LIFECYCLE = (
    "the hub under test came from --hub-cmd or --hub-url; pass --clock-hub-cmd with a launch template "
    "whose hub reads {clock_file} to run the lifecycle cases"
)


def add_parser(sub: Any, helper: Any, add_dry_run: Callable[[Any, str], None]) -> Any:
    p = sub.add_parser("hub-contract", help="Drive a Fleet Hub over HTTP against the recorded wire contract.")
    p.add_argument("--target", default=None, help="Temp target to hold the hub state (default: a new one).")
    p.add_argument(
        "--hub-cmd",
        default=None,
        help="Hub launch template with {port}, plus optional {brigade}, {host}, {state_dir}, {db}, {token_file} "
        "(default: the in-repo Python hub).",
    )
    p.add_argument("--hub-url", default=None, help="Drive an already running hub instead (loopback origin only).")
    p.add_argument(
        "--clock-hub-cmd",
        default=None,
        help="Launch template for the lifecycle cases' hub; needs {port} and {clock_file}, and may use {python} "
        "and {launcher} (default: the in-repo Python hub on hub_clock.py, unless --hub-cmd or --hub-url is set).",
    )
    p.add_argument(
        "--admin-token-env",
        default="BRIGADE_HUB_CONTRACT_ADMIN_TOKEN",
        help="Env var holding the admin token for --hub-url.",
    )
    p.add_argument(
        "--operator-recipient",
        default=None,
        help="For --hub-url: the agent:<name> the hub has in BRIGADE_FLEET_OPERATOR_RECIPIENT. "
        "The drive enrolls it, so use a fresh name per run.",
    )
    p.add_argument("--golden", default=None, help="Golden file (default: the checked-in hub-contract-golden.json).")
    p.add_argument(
        "--write-golden", action="store_true", help="Record this run's refusals and tombstones as the golden."
    )
    p.add_argument("--startup-timeout", type=float, default=30.0, help="Seconds to wait for /health.")
    add_dry_run(p, "Report the hub commands and covered routes without starting anything.")
    p.set_defaults(func=lambda args: command(args, helper))
    return p


def workdir(args: Any, helper: Any, root: Path, *, dry_run: bool) -> Path:
    """The directory a launched hub keeps its database, token file, and log in.

    With `--target` it is a fresh directory under `<target>/hub-contract/`.
    Without one the helper makes a new directory under `<state-root>/targets`
    and records it first, so `cleanup` reclaims it even when the hub fails.
    """
    if args.target:
        target = helper.guard_target(Path(args.target), root)
        if not target.is_dir():
            raise helper.HelperError(f"target {target} is not an existing directory; create one with new-target first")
        parent = target / "hub-contract"
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise helper.HelperError(f"refusing to write through {parent}: it is a symlink or not a directory")
        if dry_run:
            return parent / "<stamp>"
        parent.mkdir(mode=0o700, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{helper.now_stamp()}-", dir=parent))
    if dry_run:
        return root / "targets" / "<stamp>-hub-contract"
    work = Path(tempfile.mkdtemp(prefix=f"{helper.now_stamp()}-hub-contract-", dir=root / "targets"))
    helper.record_target(root, work)
    return work


def _plan(args: Any, helper: Any) -> tuple[bool, str | None, str | None]:
    """(external, the lifecycle template or None, why the lifecycle is skipped or None)."""
    if args.hub_url and args.hub_cmd:
        raise helper.HelperError("pass --hub-cmd or --hub-url, not both")
    external = args.hub_url is not None
    if args.write_golden and (args.hub_cmd or args.hub_url or args.clock_hub_cmd):
        raise helper.HelperError(
            "--write-golden records the in-repo Python hub alone; drop --hub-cmd, --hub-url and --clock-hub-cmd"
        )
    if args.clock_hub_cmd:
        return external, args.clock_hub_cmd, None
    if args.hub_cmd or external:
        return external, None, SKIP_LIFECYCLE
    return external, lifecycle.DEFAULT_CLOCK_HUB_CMD, None


def _dry_run(
    args: Any, helper: Any, root: Path, external: bool, url: str | None, clocked: str | None, skip: str | None
) -> int:
    brigade = helper.brigade_args(args)
    work = None if external and not clocked else workdir(args, helper, root, dry_run=True)
    would_launch = None
    template = args.hub_cmd or hc.DEFAULT_HUB_CMD
    try:
        if not external:
            values = {
                "host": hc.LOOPBACK,
                "port": "<port>",
                "state_dir": str(work),
                "db": f"{work}/hub.db",
                "token_file": f"{work}/admin-token",
            }
            would_launch = hc.expand_command(template, brigade=brigade, values=values)
        clock_launch = (
            lifecycle.would_launch(clocked, brigade=brigade, state_dir=f"{work}/lifecycle") if clocked else None
        )
    except hc.ContractError as exc:
        raise helper.HelperError(str(exc)) from exc
    return helper.emit(
        {
            "action": "hub-contract",
            "ok": True,
            "dry_run": True,
            "contract_version": hc.CONTRACT_VERSION,
            "mode": "external" if external else "launched",
            "hub_url": url,
            "would_launch": would_launch,
            "would_create": str(work) if work else None,
            "lifecycle": {"would_launch": clock_launch, "skipped": skip},
            "covered_routes": list(hc.COVERED_ROUTES),
            "covered_message_actions": list(hc.COVERED_MESSAGE_ACTIONS),
        }
    )


def _fail_cases(cases: list[dict], problems: list[dict]) -> None:
    failed = {problem["case"] for problem in problems}
    for case in cases:
        if case["case"] in failed:
            case["ok"] = False


def command(args: Any, helper: Any) -> int:
    """Drive a Fleet Hub over HTTP and compare it to the recorded wire contract and goldens."""
    root = helper.state_root(args)
    external, clocked, skip = _plan(args, helper)
    try:
        url = hc.guard_hub_url(args.hub_url) if external else None
    except hc.ContractError as exc:
        raise helper.HelperError(str(exc)) from exc
    if args.dry_run:
        return _dry_run(args, helper, root, external, url, clocked, skip)
    startup_timeout = float(args.startup_timeout)
    if not (0 < startup_timeout < float("inf")):
        raise helper.HelperError(
            f"--startup-timeout must be a positive, finite number of seconds, not {args.startup_timeout}"
        )
    admin_token = os.environ.get(args.admin_token_env, "").strip() if external else None
    if args.operator_recipient and not external:
        raise helper.HelperError("--operator-recipient applies to --hub-url; a launched hub gets a fresh operator")
    brigade = helper.brigade_args(args)
    work = None if external and not clocked else workdir(args, helper, root, dry_run=False)
    try:
        hub, result = hc.drive(
            brigade=brigade,
            state_dir=None if external else work,
            hub_cmd=args.hub_cmd or hc.DEFAULT_HUB_CMD,
            hub_url=args.hub_url,
            admin_token=admin_token,
            startup_timeout=startup_timeout,
            operator=args.operator_recipient,
        )
        clocked_run = (
            lifecycle.run(clocked, brigade=brigade, state_dir=work / "lifecycle", startup_timeout=startup_timeout)
            if clocked and work is not None
            else None
        )
    except hc.ContractError as exc:
        raise helper.HelperError(str(exc)) from exc
    cases = list(result["cases"])
    mismatches = list(result["mismatches"])
    exchanges = list(result["exchanges"])
    observed = {"core": golden.refusals(result, result.get("tag"))}
    ran = {"core": {case["case"] for case in result["cases"]}}
    tombstones = None
    if clocked_run is not None:
        lc = clocked_run.result
        cases += lc["cases"]
        mismatches += lc["mismatches"]
        exchanges += lc["exchanges"]
        observed["lifecycle"] = golden.refusals(lc, None)
        ran["lifecycle"] = {case["case"] for case in lc["cases"]}
        tombstones = clocked_run.tombstones
    golden_path = Path(args.golden) if args.golden else golden.GOLDEN_PATH
    written = False
    if args.write_golden and not mismatches:
        golden.write(golden_path, observed, tombstones or {})
        written = True
    problems = golden.compare(
        golden.load(golden_path), observed, ran, tombstones, tombstone_cases=lifecycle.TOMBSTONE_CASES
    )
    _fail_cases(cases, problems)
    mismatches += problems
    capture = helper.record_capture(
        root,
        "hub-contract",
        {
            "name": "hub-contract",
            "contract_version": hc.CONTRACT_VERSION,
            "hub_url": hub.url,
            "argv": hub.argv,
            "lifecycle_argv": clocked_run.hub.argv if clocked_run else None,
            "exchanges": exchanges,
            "mismatches": mismatches,
            "hub_log_tail": hc.log_tail(hub.log) if hub.log else None,
            "lifecycle_hub_log_tail": hc.log_tail(clocked_run.hub.log) if clocked_run and clocked_run.hub.log else None,
        },
    )
    ok = not mismatches
    return helper.emit(
        {
            "action": "hub-contract",
            "ok": ok,
            "dry_run": False,
            "contract_version": hc.CONTRACT_VERSION,
            "hub": {
                "mode": hub.mode,
                "url": hub.url,
                "argv": hub.argv,
                "state_dir": str(hub.state_dir) if hub.state_dir else None,
                "log": str(hub.log) if hub.log else None,
            },
            "lifecycle": {
                "ran": clocked_run is not None,
                "skipped": skip,
                "argv": clocked_run.hub.argv if clocked_run else None,
                "state_dir": str(clocked_run.hub.state_dir) if clocked_run else None,
                "log": str(clocked_run.hub.log) if clocked_run and clocked_run.hub.log else None,
                "cases_total": len(clocked_run.result["cases"]) if clocked_run else 0,
            },
            "golden": {
                "path": str(golden_path),
                "written": written,
                "refusals_checked": sum(len(seen) for seen in observed.values()),
                "tombstones_checked": len(tombstones) if tombstones is not None else 0,
            },
            "cases_total": len(cases),
            "cases_failed": sum(1 for case in cases if not case["ok"]),
            "covered_routes": result["covered_routes"],
            "covered_message_actions": result["covered_message_actions"],
            "mismatches": mismatches,
            "cases": cases,
            "capture": str(capture),
        },
        0 if ok else 3,
    )
