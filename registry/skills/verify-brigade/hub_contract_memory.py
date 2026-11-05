"""`hub-contract --profile memory`: additive v1 drive cases for `POST /memory` (S13a, S13b).

Memory routes are new under contract v1 (D21), so every 4xx carries
`{error, code}`. A hub without memory answers `404 {"error": "not found"}`,
which `--memory-absent` checks instead of the cases. The drive stays
hub-agnostic: no `brigade.*` imports.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

import hub_contract as hc

PROFILE = "memory"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DAY_MS = 24 * 3600 * 1000
# A bearer value no hub issued.
JUNK = "fake-memory-junk-credential"
# Pages the principal log check follows before it stops.
LOG_PAGES = 20
HEX64 = hc.Pred(lambda v: isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v), "sha256 hex")


def event_id(ms: int | None = None) -> str:
    """A time-ordered id: 48 bits of Unix milliseconds, 80 random bits, Crockford base32."""
    stamp = int(time.time() * 1000) if ms is None else ms
    value = (stamp << 80) | secrets.randbits(80)
    return "".join(CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def err(code: str) -> dict:
    return {"error": hc.NonEmpty(), "code": hc.Eq(code)}


def event(trust: str, state: str = "live") -> dict:
    return {
        "event": {
            "seq": int,
            "revision": int,
            "content_hash": HEX64,
            "trust": hc.Eq(trust),
            "state": hc.Eq(state),
        },
        "log_epoch": hc.NonEmpty(),
    }


STATUS = {
    "cards": int,
    "revisions": int,
    "seq": int,
    "indexed_seq": int,
    "log_epoch": hc.NonEmpty(),
    "hub_time": hc.NonEmpty(),
    "scan": {"available": hc.Eq(False), "ruleset_version": hc.NONE},
    "embedders": list,
    "default_mode": str,
}


MATCH = {
    "path": str,
    "scope": str,
    "card_id": str,
    "card_aliases": list,
    "revision": int,
    "title": str,
    "summary": str,
    "tags": list,
    "score": float,
    "content_hash": HEX64,
    "trust": str,
    "ranks": {"lexical": int},
}


def found(*, count: int | None = None) -> dict:
    return {
        "query": str,
        "matches": [MATCH],
        "match_count": int if count is None else hc.Eq(count),
        "retrieval": {"mode": hc.Eq("lexical"), "degraded": hc.Eq(False), "stale": hc.Eq(False)},
        "indexed_seq": int,
    }


def _matches(body: dict) -> list:
    items = body.get("matches")
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _revision(body: dict) -> int:
    value = hc._obj(body.get("event")).get("revision")
    return value if isinstance(value, int) else -1


def run_memory(hub: hc.Hub, *, memory_absent: bool = False) -> dict:
    d = hc.Drive(hub)
    admin = hub.admin_token

    def mem(case: str, body: Any, *, token: str | None = admin, status: int = 200, shape: Any = None) -> dict:
        return d.call(case, "POST", "/memory", status=status, shape=shape, body=body, token=token)

    if memory_absent:
        mem("memory-absent-not-found", {"action": "status"}, status=404, shape={"error": hc.Eq("not found")})
        return _result(d)

    status = mem("memory-status", {"action": "status"}, shape=STATUS)
    epoch = status.get("log_epoch", "")
    # Log checks start after the hub's sequence at the start of the run. A
    # reused --hub-url can hold more earlier events than one log page scans,
    # and a principal enrolled by this run cannot read them.
    start_seq = status.get("seq") if isinstance(status.get("seq"), int) else 0
    mem("memory-no-token", {"action": "status"}, token=None, status=401, shape={"error": hc.Eq("unauthorized")})
    mem(
        "memory-junk-token",
        {"action": "status"},
        token=JUNK,
        status=401,
        shape={"error": hc.Eq("unauthorized")},
    )
    d.call("memory-get-refused", "GET", "/memory", status=405, shape=err("memory_method_invalid"), token=admin)

    suffix = secrets.token_hex(3)
    node_id = f"memory-contract-node-{suffix}"
    node = d.call(
        "memory-enroll-node",
        "POST",
        "/nodes",
        status=200,
        body={"action": "add", "node_id": node_id},
        token=admin,
        shape={"token": hc.NonEmpty()},
    )
    node_token = str(node.get("token", ""))
    agent_name = f"memory-contract-agent-{suffix}"
    principal = d.call(
        "memory-enroll-principal",
        "POST",
        "/message-principals",
        status=201,
        body={"action": "add", "name": agent_name},
        token=admin,
        shape={"principal": {"token": hc.NonEmpty()}},
    )
    agent_token = str(hc._obj(principal.get("principal")).get("token", ""))
    d.secrets |= {node_token, agent_token}

    def put(case: str, scope: str, card: str, base: int | None, text: str, *, token: str = node_token, **expect: Any):
        body = {
            "action": "put",
            "scope": scope,
            "card_id": card,
            "base_revision": base,
            "text": text,
            "client_event_id": event_id(),
        }
        if base is not None:
            body["base_epoch"] = epoch
        return mem(case, body, token=token, **expect)

    # Card ids and repository scopes carry the run's suffix, so a second run
    # against the same --hub-url creates fresh cards instead of meeting the
    # first run's revisions.
    def card(name: str) -> str:
        return f"{name}-{suffix}"

    repo_one = f"repo:contract/one-{suffix}"
    repo_two = f"repo:contract/two-{suffix}"
    unreviewed = event("agent-unreviewed")
    # Same id in two scopes is two cards.
    fleet = put("memory-put-create", "fleet", card("build"), None, "fleet build", status=201, shape=unreviewed)
    put("memory-put-second-scope", repo_one, card("build"), None, "repo build", status=201, shape=unreviewed)
    mem(
        "memory-card-ambiguous",
        {"action": "card", "card_id": card("build")},
        token=node_token,
        status=409,
        shape={**err("memory_card_ambiguous"), "scopes": hc.Eq(["fleet", repo_one])},
    )
    mem(
        "memory-card-scoped",
        {"action": "card", "card_id": card("build"), "scope": repo_one},
        token=node_token,
        shape={
            "card": {
                "scope": hc.Eq(repo_one),
                "card_id": hc.Eq(card("build")),
                "revision": int,
                "content_hash": HEX64,
                "text": hc.Eq("repo build\n"),
                "next_offset": hc.NONE,
                "trust": hc.Eq("agent-unreviewed"),
                "seq": int,
            }
        },
    )
    mem(
        "memory-card-not-found",
        {"action": "card", "card_id": card("absent"), "scope": "fleet"},
        token=node_token,
        status=404,
        shape=err("memory_card_not_found"),
    )
    # Bytes A, B, A: the third revision has the first hash, and an edit of the first is stale.
    a1 = _revision(put("memory-bytes-a", "fleet", card("flip"), None, "A", status=201, shape=unreviewed))
    b = _revision(put("memory-bytes-b", "fleet", card("flip"), a1, "B", status=201, shape=unreviewed))
    a2 = _revision(put("memory-bytes-a-again", "fleet", card("flip"), b, "A", status=201, shape=unreviewed))
    put(
        "memory-bytes-stale",
        "fleet",
        card("flip"),
        a1,
        "C",
        status=409,
        shape={**err("memory_base_mismatch"), "current_revision": hc.Eq(a2), "current_hash": HEX64},
    )
    # Retire, revive, then a stale edit of the retirement.
    retired = mem(
        "memory-retire",
        {
            "action": "retire",
            "scope": "fleet",
            "card_id": card("flip"),
            "base_revision": a2,
            "base_epoch": epoch,
            "reason": "contract",
            "client_event_id": event_id(),
        },
        token=node_token,
        shape=event("agent-unreviewed", "retired"),
    )
    r = _revision(retired)
    revived = _revision(put("memory-revive", "fleet", card("flip"), r, "A", status=201, shape=unreviewed))
    put(
        "memory-stale-after-revival",
        "fleet",
        card("flip"),
        r,
        "late",
        status=409,
        shape={**err("memory_base_mismatch"), "current_revision": hc.Eq(revived)},
    )
    put("memory-create-over-existing", "fleet", card("flip"), None, "x", status=409, shape=err("memory_base_mismatch"))
    # A base revision read under another log_epoch, and one without its epoch.
    mem(
        "memory-epoch-changed",
        {
            "action": "put",
            "scope": "fleet",
            "card_id": card("build"),
            "base_revision": _revision(fleet),
            "base_epoch": event_id(0),
            "text": "x",
            "client_event_id": event_id(),
        },
        token=node_token,
        status=409,
        shape={**err("memory_epoch_changed"), "log_epoch": hc.Eq(epoch)},
    )
    mem(
        "memory-base-without-epoch",
        {
            "action": "put",
            "scope": "fleet",
            "card_id": card("build"),
            "base_revision": _revision(fleet),
            "text": "x",
            "client_event_id": event_id(),
        },
        token=node_token,
        status=400,
        shape=err("memory_request_invalid"),
    )
    # Trust: an admin put is unreviewed, import needs the grant, then a clean scan.
    put(
        "memory-admin-put-unreviewed", "fleet", card("admin-card"), None, "x", token=admin, status=201, shape=unreviewed
    )
    imported = {
        "action": "import",
        "scope": "fleet",
        "card_id": card("imported"),
        "base_revision": None,
        "text": "x",
    }
    mem(
        "memory-import-without-grant",
        {**imported, "client_event_id": event_id()},
        token=node_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    mem(
        "memory-import-scan-unavailable",
        {**imported, "client_event_id": event_id()},
        status=503,
        shape=err("memory_scan_unavailable"),
    )
    # Grants and scopes.
    put(
        "memory-principal-no-grant",
        repo_one,
        card("agent-card"),
        None,
        "x",
        token=agent_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    grant = {
        "action": "grant",
        "principal": f"agent:{agent_name}",
        "scope": repo_one,
        "rights": ["write", "read"],
    }
    mem(
        "memory-grant",
        {**grant, "client_event_id": event_id()},
        shape={
            "grant": {
                "principal": hc.Eq(f"agent:{agent_name}"),
                "scope": hc.Eq(repo_one),
                "rights": hc.Eq(["read", "write"]),
            }
        },
    )
    mem(
        "memory-grant-needs-admin",
        {**grant, "client_event_id": event_id()},
        token=node_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    put("memory-principal-granted", repo_one, card("agent-card"), None, "x", token=agent_token, status=201)
    put(
        "memory-principal-other-scope",
        repo_two,
        card("build"),
        None,
        "x",
        token=agent_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    put(
        "memory-foreign-private-scope",
        f"principal:agent:{agent_name}",
        "mine",
        None,
        "x",
        status=403,
        shape=err("memory_grant_missing"),
    )
    put("memory-own-private-scope", f"principal:{node_id}", "mine", None, "private", status=201)
    # Idempotency.
    body = {
        "action": "put",
        "scope": "fleet",
        "card_id": card("idempotent"),
        "base_revision": None,
        "text": "first",
        "client_event_id": event_id(),
    }
    first = mem("memory-idempotent-first", body, token=node_token, status=201, shape=unreviewed)
    retry = mem("memory-idempotent-retry", body, token=node_token, status=201, shape=unreviewed)
    d.invariant("memory-idempotent-retry", retry == first, "the stored answer", retry)
    mem(
        "memory-idempotent-conflict",
        {**body, "text": "second"},
        token=node_token,
        status=409,
        shape=err("memory_client_event_conflict"),
    )
    mem(
        "memory-client-event-malformed",
        {**body, "client_event_id": "not-an-id"},
        token=node_token,
        status=400,
        shape=err("memory_client_event_invalid"),
    )
    now = int(time.time() * 1000)
    for case, ms in (
        ("memory-client-event-future", now + 6 * 60 * 1000),
        ("memory-client-event-old", now - 30 * DAY_MS),
    ):
        mem(
            case,
            {**body, "client_event_id": event_id(ms)},
            token=node_token,
            status=409,
            shape=err("memory_client_event_expired"),
        )
    # The log: ascending, next_seq is the last seq on the page, private scopes stay private.
    page = mem(
        "memory-log",
        {"action": "log", "after_seq": start_seq, "limit": 500},
        token=node_token,
        shape={"events": [{"seq": int, "kind": str, "principal": str}], "next_seq": int, "log_epoch": hc.Eq(epoch)},
    )
    seqs = [e.get("seq") for e in page.get("events", []) if isinstance(e, dict)]
    d.invariant(
        "memory-log",
        seqs == sorted(seqs) and bool(seqs) and page.get("next_seq") == seqs[-1],
        "ascending seq with next_seq equal to the last",
        {"seqs": seqs, "next_seq": page.get("next_seq")},
    )
    # Follow next_seq to the end of the log, in case other writers on a shared
    # hub pushed this run's events past one page's scan window.
    scopes: set = set()
    after = start_seq
    for _ in range(LOG_PAGES):
        agent_page = mem("memory-log-principal", {"action": "log", "after_seq": after}, token=agent_token)
        scopes |= {e.get("scope") for e in agent_page.get("events", []) if isinstance(e, dict)}
        next_seq = agent_page.get("next_seq")
        if not isinstance(next_seq, int) or next_seq <= after:
            break
        after = next_seq
    d.invariant(
        "memory-log-principal",
        scopes == {repo_one},
        "only scopes the principal can read",
        sorted(str(s) for s in scopes),
    )
    # Handoff needs its grant; capture needs read.
    mem(
        "memory-handoff-without-grant",
        {"action": "handoff", "kind": "session", "text": "x", "client_event_id": event_id()},
        token=node_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    mem(
        "memory-handoff",
        {"action": "handoff", "kind": "session", "text": "contract handoff", "client_event_id": event_id()},
        status=201,
        shape={"handoff_id": hc.NonEmpty(), "log_epoch": hc.Eq(epoch)},
    )
    mem(
        "memory-capture",
        {
            "action": "capture",
            "session_ref": "contract-session-ref-0001",
            "pushed": [f"fleet/{card('build')}"],
            "opened": [],
            "client_event_id": event_id(),
        },
        token=node_token,
        shape={"recorded": hc.Eq(True)},
    )
    _search_cases(d, mem, put, card, suffix, node_token=node_token, agent_token=agent_token, repo_one=repo_one)
    # Bounds and actions later slices build.
    mem("memory-action-later", {"action": "propose"}, status=501, shape=err("memory_action_unavailable"))
    mem("memory-action-unknown", {"action": "nope"}, status=400, shape=err("memory_request_invalid"))
    put(
        "memory-card-too-large",
        "fleet",
        card("large"),
        None,
        "x" * (600 * 1024),
        status=413,
        shape=err("memory_card_too_large"),
    )
    # A revoked node token gets the v1 text.
    d.call(
        "memory-revoke-node",
        "POST",
        "/nodes",
        status=200,
        body={"action": "revoke", "node_id": node_id},
        token=admin,
    )
    mem(
        "memory-revoked-token",
        {"action": "status"},
        token=node_token,
        status=401,
        shape={"error": hc.Eq("unauthorized: node token revoked")},
    )
    return _result(d)


def _search_cases(
    d: hc.Drive, mem, put, card, suffix: str, *, node_token: str, agent_token: str, repo_one: str
) -> None:
    """`search` (S13b): BM25 over the caller's visible corpus, ties by path.

    Cases run in a scope only this run writes, named in `scopes`, so a reused
    hub's other cards change neither the statistics nor the order.
    """
    scope = f"repo:contract/search-{suffix}"
    word = f"lexical{suffix}"
    same = f"---\ntitle: Same\n---\n{word} shared\n"
    tie = card("tie")
    put("memory-search-put-tie", scope, tie, None, same, status=201)
    put("memory-search-put-tie-b", scope, f"{tie}-b", None, same, status=201)
    colons = put("memory-search-put-literal", scope, card("colons"), None, f"{word} path::to", status=201)
    granted = put("memory-search-put-granted", repo_one, card("granted"), None, f"{word} {word}", status=201)
    seq = max(_seq(colons), _seq(granted))
    body = {"action": "search", "query": word, "scopes": [scope], "min_seq": seq}
    page = mem("memory-search", body, token=node_token, shape=found(count=3))
    order = [(m.get("card_id"), m.get("score")) for m in _matches(page)]
    twins = [(c, score) for c, score in order if c in (tie, f"{tie}-b")]
    # cards/<tie>-b.md sorts before cards/<tie>.md, and the twins tie on score.
    d.invariant(
        "memory-search",
        [c for c, _ in twins] == [f"{tie}-b", tie]
        and twins[0][1] == twins[1][1]
        and isinstance(page.get("indexed_seq"), int)
        and page["indexed_seq"] >= seq,
        "the tied pair in path order, indexed through min_seq",
        {"order": order, "indexed_seq": page.get("indexed_seq")},
    )
    mem(
        "memory-search-limit",
        {**body, "limit": 1},
        token=node_token,
        shape={**found(count=3), "matches": hc.Pred(lambda v: isinstance(v, list) and len(v) == 1, "one match")},
    )
    literal = mem(
        "memory-search-literal",
        {**body, "query": "::"},
        token=node_token,
        shape=found(count=1),
    )
    d.invariant(
        "memory-search-literal",
        [(m.get("card_id"), m.get("score")) for m in _matches(literal)] == [(card("colons"), 0.0)],
        "a query with no word characters matches the text literally and scores 0.0",
        _matches(literal),
    )
    # The principal reads repo_one only, so it never sees the other scope's cards.
    agent = mem(
        "memory-search-principal", {"action": "search", "query": word, "min_seq": seq}, token=agent_token, shape=found()
    )
    d.invariant(
        "memory-search-principal",
        {(m.get("scope"), m.get("card_id")) for m in _matches(agent)} == {(repo_one, card("granted"))},
        "only cards in scopes the principal can read",
        _matches(agent),
    )
    mem(
        "memory-search-unreadable-scope",
        {"action": "search", "query": word, "scopes": [scope]},
        token=agent_token,
        status=403,
        shape=err("memory_grant_missing"),
    )
    tied = mem("memory-search-card", {"action": "card", "card_id": f"{tie}-b", "scope": scope}, token=node_token)
    retired = mem(
        "memory-search-retire",
        {
            "action": "retire",
            "scope": scope,
            "card_id": f"{tie}-b",
            "base_revision": hc._obj(tied.get("card")).get("revision"),
            "base_epoch": tied.get("log_epoch"),
            "client_event_id": event_id(),
        },
        token=node_token,
        shape=event("agent-unreviewed", "retired"),
    )
    after = mem(
        "memory-search-after-retire",
        {**body, "min_seq": _seq(retired)},
        token=node_token,
        shape=found(count=2),
    )
    d.invariant(
        "memory-search-after-retire",
        f"{tie}-b" not in [m.get("card_id") for m in _matches(after)],
        "a retired card leaves the results",
        _matches(after),
    )
    for case, extra in (
        ("memory-search-query-blank", {"query": " \t "}),
        ("memory-search-query-long", {"query": "q" * 257}),
        ("memory-search-limit-invalid", {"limit": 21}),
        ("memory-search-mode-invalid", {"mode": "dense"}),
        ("memory-search-field-unknown", {"extra": True}),
    ):
        mem(case, {**body, **extra}, token=node_token, status=400, shape=err("memory_query_invalid"))


def _seq(body: dict) -> int:
    value = hc._obj(body.get("event")).get("seq")
    return value if isinstance(value, int) else 0


def _result(d: hc.Drive) -> dict:
    return {
        "cases": d.cases,
        "mismatches": hc.scrub(d.mismatches, d.secrets),
        "exchanges": hc.scrub(d.exchanges, d.secrets),
    }


def add_arguments(p: Any) -> None:
    p.add_argument(
        "--memory-absent",
        action="store_true",
        help="memory profile: the hub has no memory configured, so POST /memory must answer 404.",
    )


def command(args: Any, helper: Any) -> int:
    """`control-brigade.py hub-contract --profile memory`."""
    root = helper.state_root(args)
    try:
        return _command(args, helper, root)
    except hc.ContractError as exc:
        raise helper.HelperError(str(exc)) from exc


def _command(args: Any, helper: Any, root: Path) -> int:
    if args.hub_url and args.hub_cmd:
        raise hc.ContractError("pass --hub-cmd or --hub-url, not both")
    absent = bool(getattr(args, "memory_absent", False))
    if not (args.hub_cmd or args.hub_url or absent):
        raise hc.ContractError("the in-repo Python hub has no memory; pass --hub-cmd for a Rust hub or --memory-absent")
    base = {"action": "hub-contract", "profile": PROFILE, "memory_absent": absent}
    if args.dry_run:
        return helper.emit({**base, "ok": True, "dry_run": True, "covered_routes": ["POST /memory", "GET /memory"]})
    if args.hub_url:
        token = os.environ.get(args.admin_token_env, "").strip()
        if not token:
            raise hc.ContractError(
                "--hub-url needs the admin token in the environment variable --admin-token-env names"
            )
        hub = hc.Hub(url=hc.guard_hub_url(args.hub_url), admin_token=token, mode="external")
        result = run_memory(hub, memory_absent=absent)
    else:
        startup_timeout = float(args.startup_timeout)
        if not (0 < startup_timeout < float("inf")):
            raise hc.ContractError(
                f"--startup-timeout must be a positive, finite number of seconds, not {args.startup_timeout}"
            )
        work = helper.load_sibling("hub_contract_cli").workdir(args, helper, root, dry_run=False)
        hub = hc.launch_hub(
            args.hub_cmd or hc.DEFAULT_HUB_CMD,
            brigade=helper.brigade_args(args),
            state_dir=work,
            startup_timeout=startup_timeout,
        )
        try:
            result = run_memory(hub, memory_absent=absent)
        finally:
            hc.stop_hub(hub)
    ok = not result["mismatches"]
    capture = helper.record_capture(
        root,
        "hub-contract-memory",
        {"name": "hub-contract-memory", "exchanges": result["exchanges"], "mismatches": result["mismatches"]},
    )
    return helper.emit(
        {
            **base,
            "ok": ok,
            "dry_run": False,
            "hub": {"mode": hub.mode, "url": hub.url, "argv": hub.argv, "log": str(hub.log) if hub.log else None},
            "cases_total": len(result["cases"]),
            "cases_failed": sum(1 for case in result["cases"] if not case["ok"]),
            "mismatches": result["mismatches"],
            "cases": result["cases"],
            "capture": str(capture),
        },
        0 if ok else 3,
    )
