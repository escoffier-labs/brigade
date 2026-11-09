"""`brigade memory verify status`, `doctor` and `run`.

`run --dry-run --no-model` selects and reports. It calls no model, submits
nothing, attaches nothing and writes no state: a dry run leaves the pause
counter alone. Every other run goes through `memory_verify_run`, which reuses
the selection here.
`doctor` resolves both verify seats and runs the isolation probe on each
(`memory_verify_seat`), recording passes in `probes.json`. Output holds scope
names, card ids, seat names, scores, reason codes and counts. It never holds
card text, titles, evidence, prompts or replies, and failures carry fixed
reasons only.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import memory_cmd
from . import memory_fairness as fairness
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from . import memory_proposal_io as io
from . import memory_verify_config as config
from . import memory_verify_seat as seats
from . import memory_verify_select as pick
from . import memory_verify_state as vstate
from .memory_reflect_hub import ReflectError, _check_replica_files

MAX_SCOPE_CARDS = vstate.MAX_SCOPE_CARDS
MAX_REPLICA_BYTES = 64 * 1024 * 1024
REPLICA_FILE_SLACK = 1024 * 1024
READ_STATS_PAGES = MAX_SCOPE_CARDS // hubc.READ_STATS_PAGE_MAX
# The hub allows one read_stats request per second per principal, across every scope.
PAGE_PAUSE_SECONDS = 1.05
# Injectable for tests. The pacer reads only this monotonic clock and sleeps only through SLEEP.
MONOTONIC = time.monotonic
SLEEP = time.sleep
SCORE_DIGITS = 4
# How old a stored read count snapshot may be and still score a rate-limited scope: two daily runs.
SNAPSHOT_MAX_AGE_MS = 2 * 86_400_000
SCOPE_LIMIT = f"verify per-scope bound exceeded ({MAX_SCOPE_CARDS} cards)"
HUB_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class VerifyRunError(ValueError):
    """A fixed code and reason, safe for logs. Never formatted with hub or card text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("verify", help="Check hub cards against local evidence and open reviewed corrections.")
    commands = parser.add_subparsers(dest="memory_verify_command", required=True)
    status = commands.add_parser("status", help="Show the verify job's pause state and last never-read counts.")
    run = commands.add_parser("run", help="Select cards, check them with both seats and open pinned proposals.")
    doctor = commands.add_parser("doctor", help="Probe propose_seat and confirm_seat for tool-free, recall-free calls.")
    doctor.add_argument(
        "--timeout", type=int, default=int(seats.PROBE_TIMEOUT), help="Per-probe timeout in seconds (30 to 600)."
    )
    for command in (status, run, doctor):
        command.add_argument("--target", "-t", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    run.add_argument("--dry-run", action="store_true", help="Submit, attach and write nothing, and leave the pause.")
    run.add_argument("--no-model", action="store_true", help="Call no seat. Needs --dry-run.")
    run.add_argument("--list-ids", action="store_true", help="Also print never-read card ids, never titles or text.")
    run.add_argument("--scheduled", action="store_true", help="Skip a run paused after three failures.")


def _check_overlap(target: Path) -> None:
    """Refuse a verify folder whose state would sit inside, or contain, a canonical card root."""
    care = memory_cmd._config_or_default(target)
    state = target / vstate.ROOT
    for root in care.card_roots:
        card_root = target / root.rstrip("/")
        if state.is_relative_to(card_root) or card_root.is_relative_to(state):
            raise VerifyRunError("verify_state_overlap", "verify state overlaps a canonical card root")


def status_payload(target: Path) -> dict[str, Any]:
    try:
        settings = config.load(target)
    except config.VerifyConfigError as exc:
        return {"status": "refused", "code": "verify_config", "error": str(exc), "state": vstate.summary(target)}
    return {
        "status": "ok",
        "configured": bool(settings.workspaces),
        "enabled": settings.enabled,
        "scopes": len(settings.scopes()),
        "workspaces": len(settings.workspaces),
        "cards_per_run": settings.cards_per_run,
        "state": vstate.summary(target),
    }


def _hub(target: Path) -> tuple[hubc.HubSettings, hubc.MemoryHubClient]:
    settings = hubc.load_settings(target)
    if settings.mode != "read":
        raise VerifyRunError("verify_hub_mode", 'the verify folder needs [hub] mode = "read"')
    if settings.source not in (hubc.SOURCE_ENV, hubc.SOURCE_TABLE):
        raise VerifyRunError(
            "verify_hub_config",
            "verify requires explicit hub memory settings: set [memory_hub] in fleet.toml "
            "or both BRIGADE_MEMORY_HUB_URL and BRIGADE_MEMORY_HUB_TOKEN_FILE",
        )
    return settings, hubc.client_for(settings)


def _binding(identity: str, principal: object, log_epoch: object) -> str:
    """What verify history belongs to: the hub, the principal it names for this credential, and its log epoch.

    The same parts reflect binds its schedule to. Another hub, principal or a
    restored hub with a new epoch never inherits history keyed by the same
    scope and card id.
    """
    return io.sha256(io.json_bytes([identity, principal, log_epoch]))


def _heads(
    target: Path, settings: hubc.HubSettings, client: hubc.MemoryHubClient, scopes: list[str]
) -> tuple[list[dict], str, str, str]:
    """Pull the bound scopes into verify's own replica: live heads, the binding, the principal and the log epoch.

    Every store access, the pull included, runs relocated to
    `.brigade/memory/verify/hub/`, so the shared replica under
    `.brigade/memory/hub/` that search, hooks and other hub commands use is
    never read, locked or written. A dry run writes only that replica.
    """
    with store.relocated(vstate.REPLICA_ROOT):
        return _pull_heads(target, settings, client, scopes)


def _pull_heads(
    target: Path, settings: hubc.HubSettings, client: hubc.MemoryHubClient, scopes: list[str]
) -> tuple[list[dict], str, str, str]:
    """Pull only the bound scopes, as reflect does with --scope, so an unbound scope never counts here.

    Cards of scopes no longer bound are dropped from verify's replica by the narrowed pull.
    """
    try:
        with store.locked(target):
            _check_replica_files(target, scope=None, dry_run=True)
    except ReflectError:
        raise VerifyRunError(
            "verify_replica_limit", "verify replica exceeds its bound. Snapshot was not parsed"
        ) from None
    narrowed = replace(
        settings, read_scopes=tuple(scopes), replica_max_bytes=min(settings.replica_max_bytes, MAX_REPLICA_BYTES)
    )
    report = sync.pull(target, client, narrowed)
    warnings = list(report["warnings"])
    if report.get("dropped"):
        # The narrowed pull drops unbound scopes on purpose. Any other warning still fails the run.
        dropped = f"dropped {report['dropped']} replicated card(s) in scopes this credential can no longer read"
        warnings = [warning for warning in warnings if warning != dropped]
    if report.get("bytes", 0) > narrowed.replica_max_bytes or warnings or report.get("seq") is None:
        raise VerifyRunError("verify_replica_incomplete", "verify requires a complete fresh replica")
    with store.locked(target):
        replica = store.replica(target)
        if not replica.belongs_to(client.identity, report["principal"]) or replica.meta.get("seq") != report["seq"]:
            raise VerifyRunError("verify_replica_changed", "verify replica changed during the run")
        if not set(scopes) <= set(replica.meta.get("read_scopes") or []):
            raise VerifyRunError("verify_scope_unreadable", "verify cannot read every bound scope")
        heads = list(replica.readable_heads(scopes).values())
    if any("scan_state" not in head for head in heads):
        sync.refresh_scan_metadata(target, client, narrowed)
        with store.locked(target):
            heads = list(store.replica(target).readable_heads(scopes).values())
    counts: dict[str, int] = {}
    for head in heads:
        counts[head["scope"]] = counts.get(head["scope"], 0) + 1
        if counts[head["scope"]] > MAX_SCOPE_CARDS:
            raise VerifyRunError("verify_scope_limit", SCOPE_LIMIT)
    binding = _binding(client.identity, report["principal"], report["log_epoch"])
    return heads, binding, str(report["principal"]), str(report["log_epoch"])


class Pacer:
    """One clock for every read_stats request in a run, so a new scope waits like a new page.

    The gap is measured from the end of the previous request, which is never
    earlier than the hub's admission of it.
    """

    def __init__(self) -> None:
        self.last: float | None = None

    def call(self, client: hubc.MemoryHubClient, scope: str, after: str | None) -> dict[str, Any]:
        if self.last is not None:
            remaining = PAGE_PAUSE_SECONDS - (MONOTONIC() - self.last)
            if remaining > 0:
                SLEEP(remaining)
        try:
            return hubc.read_stats(client, scope, after=after)
        finally:
            self.last = MONOTONIC()


def _counts(client: hubc.MemoryHubClient, scope: str, pacer: Pacer) -> tuple[dict[str, pick.Reads], int | None]:
    """Every read_stats page for `scope`, keyed by fairness key. Raises HubRefusal on a refusal."""
    rows: dict[str, pick.Reads] = {}
    after = None
    since = None
    for _ in range(READ_STATS_PAGES + 1):
        reply = pacer.call(client, scope, after)
        since = reply["read_counts_since_day"]
        for row in reply["rows"]:
            key = fairness.key_digest(scope, [row["card_id"]])
            rows[key] = pick.Reads(row["reads_7d"], row["reads_30d"], row["total_reads"])
        after = reply["next_after"]
        if after is None:
            return rows, since
    raise VerifyRunError("verify_read_stats_truncated", "read_stats listing exceeded the per-scope card bound")


@dataclass
class Picked:
    """What selection learned. Card ids, keys, counts and codes only, never card text."""

    heads: list[dict[str, Any]]
    binding: str
    principal: str
    log_epoch: str
    same_binding: bool
    ignored: int
    entries: dict[str, dict[str, Any]]
    selection: pick.Selection
    skipped: dict[str, int]
    sources: dict[str, str]
    never: dict[str, int | None]
    listed: list[dict[str, str]]
    since: dict[str, int | None]
    # Fresh, completely paginated counts per scope: what a non-dry run may store as a snapshot.
    complete: dict[str, dict[str, pick.Reads]] = field(default_factory=dict)


def _snapshot(state: dict[str, Any], scope: str, now: int) -> dict[str, pick.Reads] | None:
    """A scope's cached read counts, or None when there is none or it is older than SNAPSHOT_MAX_AGE_MS.

    Used only to score a rate-limited scope, never for the never-read report.
    """
    table = state["scopes"].get(scope, {})
    observed = table.get("counts_ms")
    if observed is None or not 0 <= now - observed <= SNAPSHOT_MAX_AGE_MS:
        return None
    return {key: pick.Reads(*entry["reads"]) for key, entry in table.get("cards", {}).items() if "reads" in entry}


def select_cards(
    target: Path,
    state: dict[str, Any],
    settings: config.VerifyConfig,
    hub_settings: hubc.HubSettings,
    client: hubc.MemoryHubClient,
    now: int,
    *,
    pending: dict[str, dict[str, Any]] | None = None,
) -> Picked:
    """Pull the bound scopes, page their read counts and pick this run's cards.

    `pending` overrides card history entries, so a caller that refreshed
    remembered proposals selects against their current state.
    """
    skipped: dict[str, int] = {}
    scopes = [scope for scope in settings.scopes() if scope in hub_settings.read_scopes]
    unread = len(settings.scopes()) - len(scopes)
    unbound = len([scope for scope in hub_settings.read_scopes if scope not in settings.scopes()])
    if unread:
        skipped["scope_unreadable"] = unread
    if unbound:
        skipped["scope_unbound"] = unbound
    # The replica may hold every read scope a folder can configure until the narrowed
    # pull trims it, so the card bound is what an ordinary pull allows: the per-scope
    # bound for each of up to READ_SCOPES_MAX scopes. The byte ceiling is what binds
    # memory, and _heads checks the per-scope bound on bound scopes only.
    max_cards = MAX_SCOPE_CARDS * hubc.READ_SCOPES_MAX
    try:
        with store.limits.bounded(max_cards=max_cards, max_bytes=MAX_REPLICA_BYTES + REPLICA_FILE_SLACK):
            heads, binding, principal, log_epoch = _heads(target, hub_settings, client, scopes)
    except store.limits.ReplicaLimitError as exc:
        if exc.kind == "cards":
            raise VerifyRunError("verify_scope_limit", SCOPE_LIMIT) from None
        raise
    # History from another hub, principal or log epoch is ignored for selection.
    same_binding = state.get("binding") == binding
    ignored = 0 if same_binding else sum(len(t.get("cards", {})) for t in state["scopes"].values())
    valid = []
    for head in heads:
        if hubc.valid_card_id(head.get("card_id")) and hubc.valid_scope(head.get("scope")):
            valid.append(head)
        else:
            skipped["invalid_head"] = skipped.get("invalid_head", 0) + 1
    reads: dict[str, pick.Reads] = {}
    entries: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    never: dict[str, int | None] = {}
    listed: list[dict[str, str]] = []
    per_scope_since: dict[str, int | None] = {}
    complete: dict[str, dict[str, pick.Reads]] = {}
    pacer = Pacer()
    unavailable: set[str] = set()
    for scope in scopes:
        # History loads for every bound scope under a matching binding, read stats or not,
        # so a rate-limited scope's pending proposals still count toward the job-wide ceiling.
        # Only scoring and selection skip a scope without counts.
        if same_binding:
            entries.update(state["scopes"].get(scope, {}).get("cards", {}))
        try:
            rows, since = _counts(client, scope, pacer)
        except hubc.HubRefusal as exc:
            if exc.status != 429 or exc.code != hubc.READ_STATS_RATE_LIMITED:
                raise
            skipped["read_stats_limited"] = skipped.get("read_stats_limited", 0) + 1
            # A snapshot from a complete earlier pagination under this binding, within its
            # bounded age, may score the scope. Its never-read count stays unknown.
            cached = _snapshot(state, scope, now) if same_binding else None
            if cached is None:
                skipped["read_stats_unavailable"] = skipped.get("read_stats_unavailable", 0) + 1
                sources[scope] = "unavailable"
                unavailable.add(scope)
            else:
                sources[scope] = "cached"
                reads.update(cached)
                per_scope_since[scope] = None
            continue
        sources[scope] = "fresh"
        reads.update(rows)
        complete[scope] = rows
        per_scope_since[scope] = since
    if pending is not None:
        entries.update(pending)
    selection = pick.select(
        [head for head in valid if head["scope"] not in unavailable],
        entries=entries,
        reads=reads,
        slots=settings.cards_per_run,
        now_ms=now,
        min_interval_days=settings.min_interval_days,
    )
    for scope in scopes:
        scoped = [head for head in valid if head["scope"] == scope]
        if sources.get(scope) != "fresh":
            never[scope] = None
            continue
        ids = pick.never_read(
            scoped,
            entries=entries,
            reads=reads,
            since_day=per_scope_since[scope],
            now_ms=now,
            never_read_days=settings.never_read_days,
        )
        never[scope] = None if ids is None else len(ids)
        listed.extend({"scope": scope, "card_id": card_id} for card_id in ids or ())
    for reason, count in selection.skipped.items():
        skipped[reason] = skipped.get(reason, 0) + count
    return Picked(
        heads=valid,
        binding=binding,
        principal=principal,
        log_epoch=log_epoch,
        same_binding=same_binding,
        ignored=ignored,
        entries=entries,
        selection=selection,
        skipped=skipped,
        sources=sources,
        never=never,
        listed=listed,
        since=per_scope_since,
        complete=complete,
    )


def report(picked: Picked, state: dict[str, Any], *, list_ids: bool) -> dict[str, Any]:
    """Selection fields of a run report: ids, scores, lanes, codes and counts."""
    known = [count for count in picked.never.values() if count is not None]
    payload: dict[str, Any] = {
        "cards_seen": len(picked.heads),
        "eligible": picked.selection.eligible,
        "selected": [
            {"scope": card.scope, "card_id": card.card_id, "score": round(card.score, SCORE_DIGITS), "lane": lane}
            for card, lane in picked.selection.selected
        ],
        "skipped": dict(sorted(picked.skipped.items())),
        "read_stats": picked.sources,
        "never_read": {
            "count": sum(known) if known and len(known) == len(picked.never) else None,
            "by_scope": picked.never,
        },
    }
    if not picked.same_binding and (picked.ignored or "binding" in state):
        payload["notes"] = [{"code": "verify_binding_changed", "ignored_card_entries": picked.ignored}]
    if list_ids:
        payload["never_read"]["card_ids"] = picked.listed
    return payload


def configured(target: Path) -> config.VerifyConfig:
    try:
        settings = config.load(target)
    except config.VerifyConfigError as exc:
        raise VerifyRunError("verify_config", str(exc)) from None
    if not settings.workspaces:
        raise VerifyRunError("verify_not_configured", "no [[verify.workspace]] binds a scope")
    return settings


def run_payload(*, target: Path, dry_run: bool, no_model: bool, list_ids: bool, now_ms: int | None = None) -> dict:
    """`run --dry-run --no-model`: select and report. No model, no hub write, no state write."""
    if not (dry_run and no_model):
        raise VerifyRunError("verify_no_model_requires_dry_run", "--no-model needs --dry-run")
    _check_overlap(target)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    with vstate.locked(target):
        state = vstate.load(target)
        settings = configured(target)
        hub_settings, client = _hub(target)
        picked = select_cards(target, state, settings, hub_settings, client, now)
        return {
            "status": "completed",
            "dry_run": True,
            "model": False,
            **report(picked, state, list_ids=list_ids),
            "state": vstate.summary(target),
        }


def doctor_payload(target: Path, timeout: int) -> dict[str, Any]:
    """Probe both seats. Model calls happen here only, and only with the fixed probe prompt."""
    if not 30 <= timeout <= 600:
        raise VerifyRunError("verify_seat_timeout_invalid", "--timeout must be 30 to 600 seconds")
    _check_overlap(target)
    try:
        settings = config.load(target)
    except config.VerifyConfigError as exc:
        raise VerifyRunError("verify_config", str(exc)) from None
    try:
        return seats.doctor_payload(target, settings.propose_seat, settings.confirm_seat, timeout=float(timeout))
    except seats.SeatRefused as exc:
        raise VerifyRunError(exc.code, str(exc)) from None


def _failure(code: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "code": code, "reason": reason}


def dispatch(args: argparse.Namespace) -> int:
    target = Path(args.target.absolute())
    try:
        with io.directory(target):
            pass
        if args.memory_verify_command == "status":
            payload = status_payload(target)
        elif args.memory_verify_command == "doctor":
            payload = doctor_payload(target, args.timeout)
        elif args.dry_run and args.no_model:
            payload = run_payload(target=target, dry_run=True, no_model=True, list_ids=args.list_ids)
        elif args.no_model:
            raise VerifyRunError("verify_no_model_requires_dry_run", "--no-model needs --dry-run")
        else:
            from . import memory_verify_run

            payload = memory_verify_run.run_payload(
                target=target, dry_run=args.dry_run, list_ids=args.list_ids, scheduled=args.scheduled
            )
    except VerifyRunError as exc:
        payload = _failure(exc.code, str(exc))
    except vstate.VerifyStateError as exc:
        payload = _failure("verify_state", str(exc))
    except store.limits.ReplicaLimitError:
        payload = _failure("verify_replica_limit", "verify replica bound exceeded before snapshot decoding")
    except hubc.HubRefusal as exc:
        code = exc.code if isinstance(exc.code, str) and HUB_CODE.fullmatch(exc.code) else "hub_refused"
        payload = _failure(code, "hub refused the verify request")
    except hubc.HubUnavailable:
        payload = _failure("hub_unavailable", "verify could not reach the hub")
    except hubc.HubConfigError:
        payload = _failure("verify_hub_config", "invalid hub memory configuration or replica metadata")
    except OSError:
        payload = _failure("verify_io", "verify could not safely read local state")
    except (ValueError, UnicodeError, RecursionError):
        payload = _failure("verify_invalid", "verify received invalid state or data")
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
        return _exit(payload)
    print(f"memory verify: {payload['status']}")
    if "reason" in payload or "error" in payload:
        print(f"error: {payload.get('code')}: {payload.get('reason', payload.get('error'))}")
    if args.memory_verify_command == "doctor":
        for row in payload.get("seats", []):
            suffix = f" {row['code']}" if "code" in row else ""
            print(f"seat {row['role']} {row['seat']} {row['cli']} {row['model']} {row['status']}{suffix}")
    elif args.memory_verify_command == "status":
        state = payload.get("state", {})
        print(f"paused: {str(state.get('paused')).lower()}  failures: {state.get('consecutive_failures', '-')}")
        for scope, count in state.get("never_read", {}).items():
            print(f"never_read {scope} {'unknown' if count is None else count}")
    else:
        for item in payload.get("selected", []):
            print(f"selected {item['scope']} {item['card_id']} {item['score']:.{SCORE_DIGITS}f} {item['lane']}")
        for reason, count in payload.get("skipped", {}).items():
            print(f"skipped {reason} {count}")
        for note in payload.get("notes", []):
            print(f"note {note['code']} {note['ignored_card_entries']}")
        for item in payload.get("proposals", []):
            print(f"proposal {item['scope']} {item['card_id']} {item['proposal_id']}")
        if payload["status"] == "paused":
            print("scheduled verification is paused. Run memory verify run manually to resume after a success.")
        if "never_read" in payload:
            count = payload["never_read"]["count"]
            print(f"never_read {'unknown' if count is None else count}")
            for item in payload["never_read"].get("card_ids", []):
                print(f"never_read_id {item['scope']} {item['card_id']}")
    return _exit(payload)


def _exit(payload: dict[str, Any]) -> int:
    if payload["status"] in ("ok", "completed", "paused"):
        return 0
    return 2 if payload.get("code") == "verify_no_model_requires_dry_run" else 1
