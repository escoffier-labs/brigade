"""Hub-agnostic findings contract, including crash recovery on a controlled clock.

A searchable destination is modeled as a list of (report_key, reference) pairs.
Recovery searches that destination before completing generation two, rather than
sending a second report. No hub-private key or database access is needed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import hub_contract as hc
import hub_contract_lifecycle as lifecycle

COVERED_ROUTES = ("POST /findings",)


def run(hub: hc.Hub, clock: Any) -> dict:
    d = hc.Drive(hub)
    admin = hub.admin_token

    def post(case, body, *, token=admin, status=200, shape=None):
        return d.call(case, "POST", "/findings", status=status, body=body, token=token, shape=shape)

    def error(code):
        return {"error": str, "code": hc.Eq(code)}

    observe = {"action": "observe", "kind": "email", "key": "<WATCH@EXAMPLE.TEST>"}
    post("findings authentication", observe, token=None, status=401, shape=error("finding_unauthorized"))
    principal = d.call(
        "findings enroll",
        "POST",
        "/message-principals",
        token=admin,
        status=201,
        body={"action": "add", "name": "finding-watcher"},
    )["principal"]
    token = principal["token"]
    d.secrets.add(token)
    post("findings grant required", observe, token=token, status=403, shape=error("finding_forbidden"))
    post(
        "findings grant",
        {"action": "grant", "principal_id": principal["principal_id"], "enabled": True},
        shape={"principal_id": str, "findings": hc.Eq(True)},
    )
    unowned = post(
        "findings no charter",
        observe,
        token=token,
        shape={"finding_id": str, "reservation": hc.Eq(None), "owner": hc.Eq(None), "state": hc.Eq("open")},
    )
    current = d.call("findings policy", "GET", "/policy", token=admin, status=200)
    # GET /policy is an envelope around the current authority.
    policy = current.get("policy", current)
    document = policy.get("document", current.get("document", {}))
    version = policy.get("version", 0)
    document["owners"] = {
        "watcher": {"label": "Watcher", "match": {"findings": ["email"], "principals": ["finding-watcher"]}}
    }
    d.call(
        "findings charter",
        "POST",
        "/policy",
        token=admin,
        status=200,
        body={"action": "save", "document": document, "expected_version": version, "reason": "findings contract"},
    )
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(lambda _: hc.request(hub.url, "POST", "/findings", body=observe, token=token), range(2))
        )
    for i, reply in enumerate(replies):
        d.check(
            f"findings concurrent {i}",
            "POST",
            "/findings",
            reply,
            status=200,
            shape={
                "finding_id": hc.Eq(unowned["finding_id"]),
                "first_seen": (int, float),
                "owner": hc.Eq("watcher"),
                "state": hc.Eq("open"),
                "report_ref": hc.Eq(None),
                "reservation": None,
            },
        )
    held = [r.json["reservation"] for r in replies if r.json and r.json.get("reservation")]
    d.invariant("findings concurrent 0", len(held) == 1, "one reservation", len(held))
    if len(held) != 1:
        return {"cases": d.cases, "mismatches": d.mismatches}
    first = held[0]
    destination = [(first["report_key"], "issue:existing")]
    clock.set(601)
    second = post(
        "findings crash retry",
        observe,
        token=token,
        shape={
            "finding_id": hc.Eq(unowned["finding_id"]),
            "reservation": {
                "generation": hc.Eq(2),
                "report_key": hc.Eq(first["report_key"]),
                "reservation_id": str,
                "expires_at": (int, float),
            },
        },
    )["reservation"]

    def report(reservation, ref):
        return {
            "action": "reported",
            "finding_id": unowned["finding_id"],
            "reservation_id": reservation["reservation_id"],
            "generation": reservation["generation"],
            "report_ref": ref,
        }

    post(
        "findings fenced report",
        report(first, "issue:stale"),
        token=token,
        status=409,
        shape=error("finding_reservation_expired"),
    )
    found = next(ref for key, ref in destination if key == second["report_key"])
    clock.set(1202)
    post(
        "findings expired unreserved report",
        report(second, found),
        token=token,
        shape={"state": hc.Eq("reported"), "report_ref": hc.Eq("issue:existing"), "reservation": hc.Eq(None)},
    )
    post(
        "findings duplicate report",
        report(second, found),
        token=token,
        status=409,
        shape={**error("finding_already_reported"), "report_ref": hc.Eq(found)},
    )
    post(
        "findings canonical observation",
        {**observe, "key": "watch@example.test"},
        token=token,
        shape={"finding_id": hc.Eq(unowned["finding_id"]), "report_ref": hc.Eq(found), "reservation": hc.Eq(None)},
    )
    post(
        "findings resolve",
        {"action": "resolve", "finding_id": unowned["finding_id"]},
        token=token,
        shape={"state": hc.Eq("resolved")},
    )
    post("findings malformed", [], token=token, status=400, shape=error("finding_invalid"))
    post("findings revoke grant", {"action": "grant", "principal_id": principal["principal_id"], "enabled": False})
    post("findings revoked grant", observe, token=token, status=403, shape=error("finding_forbidden"))
    return {"cases": d.cases, "mismatches": hc.scrub(d.mismatches, d.secrets), "covered_routes": list(COVERED_ROUTES)}


def command(args: Any, helper: Any) -> int:
    if args.hub_url or args.hub_cmd:
        raise helper.HelperError("findings uses --clock-hub-cmd for an alternate hub with a controlled clock")
    root = helper.state_root(args)
    import hub_contract_cli as cli

    template = args.clock_hub_cmd or lifecycle.DEFAULT_CLOCK_HUB_CMD
    if args.dry_run:
        return helper.emit({"profile": "findings", "covered_routes": list(COVERED_ROUTES)}, 0)
    work = cli.workdir(args, helper, root, dry_run=False)
    hub, clock = lifecycle.launch_clocked(
        template, brigade=helper.brigade_args(args), state_dir=work, startup_timeout=args.startup_timeout
    )
    try:
        result = run(hub, clock)
    finally:
        hc.stop_hub(hub)
    ok = not result["mismatches"]
    return helper.emit({"profile": "findings", "ok": ok, "cases_total": len(result["cases"]), **result}, 0 if ok else 3)
