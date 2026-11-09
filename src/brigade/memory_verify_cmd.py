"""`brigade memory verify status` and `memory verify run --dry-run --no-model`.

This slice selects and reports. It calls no model, submits nothing, attaches
nothing and writes no state: a dry run leaves the pause counter alone. Output
holds scope names, card ids, scores, reason codes and counts. It never holds
card text, titles or evidence, and failures carry fixed reasons only.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import memory_cmd
from . import memory_fairness as fairness
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from . import memory_proposal_io as io
from . import memory_verify_config as config
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
HUB_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")


class VerifyRunError(ValueError):
    """A fixed code and reason, safe for logs. Never formatted with hub or card text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("verify", help="Select hub cards for evidence-checked verification (dry run only).")
    commands = parser.add_subparsers(dest="memory_verify_command", required=True)
    status = commands.add_parser("status", help="Show the verify job's pause state and last never-read counts.")
    run = commands.add_parser("run", help="Score and select verification candidates without a model.")
    for command in (status, run):
        command.add_argument("--target", "-t", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    run.add_argument("--dry-run", action="store_true", help="Submit, attach and write nothing. Required for now.")
    run.add_argument("--no-model", action="store_true", help="Call no seat. Required for now.")
    run.add_argument("--list-ids", action="store_true", help="Also print never-read card ids, never titles or text.")


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


def _heads(target: Path, settings: hubc.HubSettings, client: hubc.MemoryHubClient, scopes: list[str]) -> list[dict]:
    """Pull the replica, as reflect does, and return the live heads of the bound scopes."""
    try:
        with store.locked(target):
            _check_replica_files(target, scope=None, dry_run=True)
    except ReflectError:
        raise VerifyRunError(
            "verify_replica_limit", "verify replica exceeds its bound. Snapshot was not parsed"
        ) from None
    settings = replace(settings, replica_max_bytes=min(settings.replica_max_bytes, MAX_REPLICA_BYTES))
    report = sync.pull(target, client, settings)
    if report.get("bytes", 0) > settings.replica_max_bytes or report["warnings"] or report.get("seq") is None:
        raise VerifyRunError("verify_replica_incomplete", "verify requires a complete fresh replica")
    with store.locked(target):
        replica = store.replica(target)
        if not replica.belongs_to(client.identity, report["principal"]) or replica.meta.get("seq") != report["seq"]:
            raise VerifyRunError("verify_replica_changed", "verify replica changed during the run")
        heads = list(replica.readable_heads(scopes).values())
    if any("scan_state" not in head for head in heads):
        sync.refresh_scan_metadata(target, client, settings)
        with store.locked(target):
            heads = list(store.replica(target).readable_heads(scopes).values())
    counts: dict[str, int] = {}
    for head in heads:
        counts[head["scope"]] = counts.get(head["scope"], 0) + 1
        if counts[head["scope"]] > MAX_SCOPE_CARDS:
            raise VerifyRunError("verify_scope_limit", f"verify per-scope bound exceeded ({MAX_SCOPE_CARDS} cards)")
    return heads


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


def _cached(state: dict[str, Any], scope: str) -> tuple[dict[str, pick.Reads], int | None]:
    """Counts cached in the state file. Anything malformed makes the whole scope's cache unavailable."""
    unavailable: tuple[dict[str, pick.Reads], int | None] = ({}, None)
    scopes = state.get("scopes") if isinstance(state, dict) else None
    table = scopes.get(scope, {}) if isinstance(scopes, dict) else None
    cards = table.get("cards", {}) if isinstance(table, dict) else None
    if not isinstance(cards, dict):
        return unavailable
    since = table.get("since_day") if isinstance(table, dict) else None
    if since is not None and (type(since) is not int or since < 0):
        return unavailable
    rows: dict[str, pick.Reads] = {}
    for key, entry in cards.items():
        if not isinstance(entry, dict) or "reads" not in entry:
            continue
        reads = entry["reads"]
        if not isinstance(reads, list) or len(reads) != 3 or not all(type(v) is int and v >= 0 for v in reads):
            return unavailable
        rows[key] = pick.Reads(*reads)
    return rows, since


def run_payload(*, target: Path, dry_run: bool, no_model: bool, list_ids: bool, now_ms: int | None = None) -> dict:
    if not (dry_run and no_model):
        raise VerifyRunError(
            "verify_live_unavailable", "memory verify run needs --dry-run --no-model until the verification pass lands"
        )
    _check_overlap(target)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    with vstate.locked(target):
        state = vstate.load(target)
        try:
            settings = config.load(target)
        except config.VerifyConfigError as exc:
            raise VerifyRunError("verify_config", str(exc)) from None
        if not settings.workspaces:
            raise VerifyRunError("verify_not_configured", "no [[verify.workspace]] binds a scope")
        hub_settings, client = _hub(target)
        skipped: dict[str, int] = {}
        scopes = [scope for scope in settings.scopes() if scope in hub_settings.read_scopes]
        unread = len(settings.scopes()) - len(scopes)
        unbound = len([scope for scope in hub_settings.read_scopes if scope not in settings.scopes()])
        if unread:
            skipped["scope_unreadable"] = unread
        if unbound:
            skipped["scope_unbound"] = unbound
        with store.limits.bounded(max_cards=MAX_SCOPE_CARDS, max_bytes=MAX_REPLICA_BYTES + REPLICA_FILE_SLACK):
            heads = _heads(target, hub_settings, client, scopes)
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
        pacer = Pacer()
        for scope in scopes:
            try:
                rows, since = _counts(client, scope, pacer)
                sources[scope] = "fresh"
            except hubc.HubRefusal as exc:
                if exc.status != 429 or exc.code != hubc.READ_STATS_RATE_LIMITED:
                    raise
                rows, since = _cached(state, scope)
                sources[scope] = "cached"
                skipped["read_stats_limited"] = skipped.get("read_stats_limited", 0) + 1
            reads.update(rows)
            per_scope_since[scope] = since
            entries.update(state["scopes"].get(scope, {}).get("cards", {}))
        selection = pick.select(
            valid,
            entries=entries,
            reads=reads,
            slots=settings.cards_per_run,
            now_ms=now,
            min_interval_days=settings.min_interval_days,
        )
        for scope in scopes:
            scoped = [head for head in valid if head["scope"] == scope]
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
        known = [count for count in never.values() if count is not None]
        payload: dict[str, Any] = {
            "status": "completed",
            "dry_run": True,
            "model": False,
            "cards_seen": len(valid),
            "eligible": selection.eligible,
            "selected": [
                {"scope": card.scope, "card_id": card.card_id, "score": round(card.score, SCORE_DIGITS), "lane": lane}
                for card, lane in selection.selected
            ],
            "skipped": dict(sorted(skipped.items())),
            "read_stats": sources,
            "never_read": {
                "count": sum(known) if known and len(known) == len(never) else None,
                "by_scope": never,
            },
            "state": vstate.summary(target),
        }
        if list_ids:
            payload["never_read"]["card_ids"] = listed
        return payload


def _failure(code: str, reason: str) -> dict[str, Any]:
    return {"status": "failed", "code": code, "reason": reason}


def dispatch(args: argparse.Namespace) -> int:
    target = Path(args.target.absolute())
    try:
        with io.directory(target):
            pass
        if args.memory_verify_command == "status":
            payload = status_payload(target)
        else:
            payload = run_payload(target=target, dry_run=args.dry_run, no_model=args.no_model, list_ids=args.list_ids)
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
    except (ValueError, UnicodeError, RecursionError, TypeError, KeyError):
        payload = _failure("verify_invalid", "verify received invalid state or data")
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))
        return _exit(payload)
    print(f"memory verify: {payload['status']}")
    if "reason" in payload or "error" in payload:
        print(f"error: {payload.get('code')}: {payload.get('reason', payload.get('error'))}")
    if args.memory_verify_command == "status":
        state = payload.get("state", {})
        print(f"paused: {str(state.get('paused')).lower()}  failures: {state.get('consecutive_failures', '-')}")
        for scope, count in state.get("never_read", {}).items():
            print(f"never_read {scope} {'unknown' if count is None else count}")
    else:
        for item in payload.get("selected", []):
            print(f"selected {item['scope']} {item['card_id']} {item['score']:.{SCORE_DIGITS}f} {item['lane']}")
        for reason, count in payload.get("skipped", {}).items():
            print(f"skipped {reason} {count}")
        if "never_read" in payload:
            count = payload["never_read"]["count"]
            print(f"never_read {'unknown' if count is None else count}")
            for item in payload["never_read"].get("card_ids", []):
                print(f"never_read_id {item['scope']} {item['card_id']}")
    return _exit(payload)


def _exit(payload: dict[str, Any]) -> int:
    if payload["status"] in ("ok", "completed"):
        return 0
    return 2 if payload.get("code") == "verify_live_unavailable" else 1
