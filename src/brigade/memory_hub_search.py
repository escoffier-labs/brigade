"""Search fusion and fallback for hub memory (S14).

In `read` and `sync` modes, `memory search` asks the hub (800 ms budget) for
matches in the configured read scopes only, and fuses its list with the local
BM25 list by reciprocal rank fusion. One entry stands for one
`(scope, card_id, content_hash)`; a local card, which has no scope, joins the
hub entry with its bytes. When the hub is unreachable, times out, or answers
3xx, 404, 408, 429 or 503, the answer comes from local cards and the replica
and says it degraded. A refusal (401, 403 or any other coded answer) never
falls back to the replica: the hub has just said no, so the answer is local
cards only and the warning says why. One exception is a missing grant on one
of several configured read scopes: the hub refuses the whole search and names
no scope, so the client searches each scope once inside the search budget,
keeps the readable ones and reports the refused scopes. It remembers a
refusal briefly, so later searches leave that scope out up front and make one
request, and it never asks for a scope the principal did not configure. While
a replica rebuild is in progress there is no replica, so the fallback is local
cards only. `off` mode never reaches this module.

The query goes to the hub over the authenticated transport and nowhere else:
nothing here writes it to disk. The caller's search log keeps the #2196 rules.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import memory_bm25
from . import memory_hub_client as hubc
from . import memory_hub_store as store

RRF_K = 60
WIDE = 200
HUB_LIMIT_MAX = 20  # the hub's `search` limit
_SOURCE_ORDER = {"hub": 0, "local": 1, "replica": 2}


def enabled(target: Path) -> bool:
    """Whether `memory search` should route here.

    A config that cannot be read or parsed stays on the local path, which
    reports it exactly as before. A readable config with a bad `[hub]` table
    routes here, to be reported.
    """
    import tomllib

    target = target.expanduser().resolve()
    try:
        raw = hubc.read_config_text(target)
        if raw is None:
            return False
        tomllib.loads(raw)
    except (OSError, ValueError):
        return False
    try:
        return hubc.hub_mode(target) != "off"
    except hubc.HubConfigError:
        return True


def _replica_matches(
    target: Path, query: str, hub: str, settings: hubc.HubSettings
) -> tuple[list[dict[str, Any]], bool]:
    """Matches from replica heads in the read scopes, and whether the replica is incomplete."""
    from . import memory_cmd

    replica = store.replica(target)
    if replica.watermark(hub) is None or not replica.meta.get("principal"):
        return [], False
    if not hubc.replica_principal_allowed(replica.meta.get("principal")):
        return [], False
    heads = list(replica.readable_heads(settings.read_scopes).values())
    fields = []
    for head in heads:
        text = str(head.get("text", ""))
        meta, _ = memory_cmd._parse_frontmatter(text)
        tags = meta.get("tags")
        fields.append(
            {
                "rel": f"cards/{head['card_id']}.md",
                "card_id": head["card_id"],
                "card_aliases": [],
                "title": str(meta.get("title") or head["card_id"]),
                "tags": [str(t) for t in tags] if isinstance(tags, list) else ([str(tags)] if tags else []),
                "summary": str(meta.get("description") or meta.get("summary") or ""),
                "body": text,
            }
        )
    ranked = memory_bm25.rank([memory_bm25.card_text(item) for item in fields], query)
    matches = []
    for index, score in ranked:
        match = memory_cmd.card_search_match(fields[index], score)
        head = heads[index]
        match.update(
            scope=head["scope"],
            revision=head.get("revision"),
            content_hash=head.get("content_hash"),
            trust=head.get("trust"),
        )
        matches.append(match)
    matches.sort(key=lambda item: (-item["score"], item["path"]))
    return matches[:WIDE], bool(replica.missing)


def _local_matches(target: Path, query: str) -> tuple[list[dict[str, Any]], int]:
    from . import memory_cmd, memory_hub_sync

    payload = memory_cmd.search_cards_payload(target, query, limit=WIDE)
    matches = payload["matches"]
    for match in matches:
        try:
            text = (target / match["path"]).read_text(encoding="utf-8")
            match["content_hash"] = hubc.content_hash(text)
            legacy_id = match["card_id"]
            match["publication_id"] = memory_hub_sync.publication_card_id(text, match["path"], legacy_id)
            if match["publication_id"] != legacy_id:
                match["card_aliases"] = list(dict.fromkeys([legacy_id, *match.get("card_aliases", [])]))
        except (OSError, UnicodeDecodeError):
            match["content_hash"] = None
    return matches, int(payload["match_count"])


def fuse(lists: dict[str, list[dict[str, Any]]], limit: int, *, home_scope: str | None = None) -> list[dict[str, Any]]:
    """RRF over the named lists, one entry per `(scope, card_id, content_hash)`.

    Hub and replica matches carry a scope; the same bytes in two scopes stay
    two entries. A local match has none and joins an entry with its card id
    and bytes, preferring `home_scope` (where this workspace publishes).
    A local publication_id is a fusion key. Its readable card_id stays local
    unless it joins a scoped result, whose hub card_id opens that version.
    """
    entries: list[dict[str, Any]] = []
    by_content: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def find(match: dict[str, Any], content: tuple[str, str]) -> dict[str, Any] | None:
        candidates = by_content.get(content, [])
        scope = match.get("scope")
        if scope:
            exact = [e for e in candidates if e.get("scope") == scope]
            pool = exact or [e for e in candidates if not e.get("scope")]
        else:
            pool = [e for e in candidates if e.get("scope") == home_scope] or candidates
        return pool[0] if pool else None

    for source in sorted(lists, key=_SOURCE_ORDER.__getitem__):
        for rank, match in enumerate(lists[source], start=1):
            fusion_id = match.get("publication_id") if source == "local" else None
            content = (
                str(fusion_id or match.get("card_id")),
                str(match.get("content_hash") or f"{source}:{match.get('path')}"),
            )
            entry = find(match, content)
            if entry is None:
                entry = {**match, "source": source, "ranks": {}, "rrf": 0.0, "best": (rank, source)}
                entries.append(entry)
                by_content.setdefault(content, []).append(entry)
            aliases = list(
                dict.fromkeys(
                    alias
                    for values in (entry.get("card_aliases"), match.get("card_aliases"))
                    if isinstance(values, list)
                    for alias in values
                    if isinstance(alias, str)
                )
            )
            if source == "hub":
                # The latest contributing hub match decides, so a later match without ranks clears older ones.
                if isinstance(match.get("ranks"), dict):
                    entry["hub_ranks"] = dict(match["ranks"])
                else:
                    entry.pop("hub_ranks", None)
            entry["ranks"][source] = rank
            entry["rrf"] += 1.0 / (RRF_K + rank)
            scope = entry.get("scope") or match.get("scope")
            scoped_id = match.get("card_id") if match.get("scope") else entry.get("card_id")
            if (rank, _SOURCE_ORDER[source]) < (entry["best"][0], _SOURCE_ORDER[entry["best"][1]]):
                entry["best"] = (rank, source)
                entry.update({k: v for k, v in match.items() if k != "ranks"}, source=source)
            entry["card_aliases"] = aliases
            if scope:
                entry["scope"] = scope
                entry["card_id"] = scoped_id
    ordered = sorted(entries, key=lambda m: (-m["rrf"], _SOURCE_ORDER[m["source"]], str(m.get("path"))))
    out = []
    for entry in ordered[:limit]:
        entry.pop("best", None)
        entry["score"] = round(entry.pop("rrf"), 6)
        out.append(entry)
    return out


def _now() -> float:
    """Wall clock for the refused-scope memory. A test can replace it."""
    return time.time()


def _monotonic() -> float:
    """Monotonic clock for the search deadline. A test can replace it."""
    return time.monotonic()


@dataclass(frozen=True)
class _ScopeSearch:
    """What a hub search over the configured scopes answered, and what it skipped."""

    body: dict[str, Any] | None
    refused: tuple[str, ...] = ()
    unprobed: tuple[str, ...] = ()
    remembered: tuple[str, ...] = ()


def _hub_search(
    client: hubc.MemoryHubClient,
    *,
    query: str,
    limit: int,
    query_source: str,
    scopes: tuple[str, ...],
    deadline: float,
) -> dict[str, Any]:
    """One `search` call inside the whole search's deadline."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise hubc.HubUnavailable("timeout")
    from .memory_retrieval_eval.live import interactive_session

    return client.call(
        "search",
        timeout=remaining,
        query=query,
        limit=limit,
        query_source=query_source,
        scopes=list(scopes),
        **interactive_session(),
    )


def _missing_grant(exc: hubc.HubRefusal) -> bool:
    return exc.status == 403 and exc.code == "memory_grant_missing"


def _merge_scope_replies(replies: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """One search reply from per-scope replies, ordered as one hub search would order them.

    Each reply is already in the hub's order, and its scores come from its own
    query, so scores are never compared across scopes. Hits take turns by rank,
    every hub-demoted hit after all the others, and the list stops at `limit`,
    the way a single search's answer would.
    """
    ranked: list[tuple[bool, int, int, dict[str, Any]]] = []
    for position, reply in enumerate(replies):
        for rank, match in enumerate(reply.get("matches") or []):
            if isinstance(match, dict):
                ranked.append((bool(match.get("source_demoted")), rank, position, match))
    ranked.sort(key=lambda row: row[:3])
    merged: dict[str, Any] = {"matches": [row[3] for row in ranked[:limit]]}
    counts = [reply.get("match_count") for reply in replies]
    if all(isinstance(count, int) and not isinstance(count, bool) for count in counts):
        merged["match_count"] = sum(counts)
    mode = None
    stale = False
    for reply in replies:
        retrieval = reply.get("retrieval")
        if not isinstance(retrieval, dict):
            continue
        if mode is None and isinstance(retrieval.get("mode"), str):
            mode = retrieval["mode"]
        stale = stale or retrieval.get("stale") is True
    merged["retrieval"] = {"mode": mode, "stale": stale}
    return merged


def _remember_refused(target: Path, scopes: tuple[str, ...], *, now: float) -> None:
    """Remember a refusal for a later search. A cache write never fails the search."""
    try:
        store.remember_refused_scopes(target, scopes, now=now)
    except OSError:
        pass


def _forget_refused(target: Path, scopes: tuple[str, ...], *, now: float) -> None:
    try:
        store.forget_refused_scopes(target, scopes, now=now)
    except OSError:
        pass


def _search_readable_scopes(
    target: Path,
    client: hubc.MemoryHubClient,
    *,
    query: str,
    limit: int,
    query_source: str,
    scopes: tuple[str, ...],
    deadline: float,
) -> _ScopeSearch:
    """Search the configured scopes inside one budget, keeping what the hub allows.

    The hub answers 403 `memory_grant_missing` to the whole search when any
    requested scope is unreadable, and the error names no scope. Search each
    scope once instead, inside the same deadline as the whole search. A
    refusal is remembered for a short TTL, so later searches leave that scope
    out up front and make one request; after the TTL the scope is sent again,
    so a re-issued grant comes back. A scope the budget did not reach is
    reported, never guessed at. Nothing here widens a grant: only configured
    scopes are sent, and a scope that answered the refusal is dropped.
    """
    now = _now()
    remembered = {name for name in store.refused_scopes(target, now=now) if name in scopes}
    refused = [name for name in scopes if name in remembered]
    send = tuple(name for name in scopes if name not in remembered)
    if not send:
        return _ScopeSearch(None, tuple(refused), remembered=tuple(refused))
    try:
        body = _hub_search(client, query=query, limit=limit, query_source=query_source, scopes=send, deadline=deadline)
    except hubc.HubRefusal as exc:
        if not _missing_grant(exc):
            raise
        if len(send) == 1:
            _remember_refused(target, send, now=now)
            return _ScopeSearch(None, tuple([*refused, *send]), remembered=tuple(refused))
    else:
        _forget_refused(target, send, now=now)
        return _ScopeSearch(body, tuple(refused), remembered=tuple(refused))
    replies: list[dict[str, Any]] = []
    unprobed: list[str] = []
    for index, scope in enumerate(send):
        if deadline - _monotonic() <= 0:
            unprobed = list(send[index:])
            break
        try:
            replies.append(
                _hub_search(
                    client, query=query, limit=limit, query_source=query_source, scopes=(scope,), deadline=deadline
                )
            )
        except hubc.HubRefusal as exc:
            if not _missing_grant(exc):
                raise
            _remember_refused(target, (scope,), now=now)
            refused.append(scope)
        except hubc.HubUnavailable as exc:
            if exc.reason != "timeout":
                raise
            unprobed = list(send[index:])
            break
    if not replies:
        return _ScopeSearch(None, tuple(refused), tuple(unprobed), remembered=tuple(refused[: len(remembered)]))
    return _ScopeSearch(
        _merge_scope_replies(replies, limit),
        tuple(refused),
        tuple(unprobed),
        remembered=tuple(refused[: len(remembered)]),
    )


def search_payload(
    target: Path,
    query: str,
    *,
    limit: int = 20,
    query_source: str = "explicit",
    transport: hubc.Transport | None = None,
    settings: hubc.HubSettings | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    settings = settings or hubc.load_settings(target)
    # One budget for the whole search: local cards, the hub calls and any probes.
    deadline = _monotonic() + hubc.SEARCH_BUDGET_SECONDS
    local, _ = _local_matches(target, query)
    lists: dict[str, list[dict[str, Any]]] = {"local": local}
    retrieval: dict[str, Any] = {"hub": "ok", "degraded": False}
    hub = ""
    hub_limit = min(limit, HUB_LIMIT_MAX)
    hub_match_count: int | None = None
    notes: list[str] = []
    refused: list[str] = []
    unprobed: list[str] = []
    remembered: list[str] = []
    try:
        client = hubc.client_for(settings, transport=transport)
        hub = client.identity
        found = _search_readable_scopes(
            target,
            client,
            query=query,
            limit=hub_limit,
            query_source=query_source,
            scopes=tuple(settings.read_scopes),
            deadline=deadline,
        )
        refused, unprobed, remembered = list(found.refused), list(found.unprobed), list(found.remembered)
        body = found.body
        if body is None:
            # Every configured scope is refused, now or by the remembered refusals.
            retrieval.update(hub="refused", degraded=True, reason="memory_grant_missing", status=403)
            retrieval["fallback"] = sorted(lists)
        else:
            lists["hub"] = [
                {**m, "path": m.get("path") or f"cards/{m.get('card_id')}.md"}
                for m in body.get("matches") or []
                if isinstance(m, dict) and m.get("scope") in settings.read_scopes
            ]
            hub_retrieval = hubc.obj(body.get("retrieval"))
            retrieval["effective"] = hub_retrieval.get("mode")
            retrieval["stale"] = hub_retrieval.get("stale")
            if isinstance(body.get("match_count"), int):
                hub_match_count = int(body["match_count"])
            if limit > HUB_LIMIT_MAX:
                retrieval["hub_limit"] = HUB_LIMIT_MAX
                notes.append(
                    f"the hub returns at most {HUB_LIMIT_MAX} matches per search; --limit {limit} was capped there"
                )
    except hubc.HubUnavailable as exc:
        # Not now, rather than no: the replica may stand in.
        retrieval.update(hub="unavailable", degraded=True, reason=exc.reason)
        if hub:
            replica, incomplete = _replica_matches(target, query, hub, settings)
            if replica:
                lists["replica"] = replica
            if incomplete:
                retrieval["replica_incomplete"] = True
            if not replica and store.replica_snapshot_missing(target):
                retrieval["replica_unusable"] = "snapshot-missing"
                notes.append("the replica's heads snapshot is missing, so it was not used; run memory hub pull")
        retrieval["fallback"] = sorted(lists)
    except hubc.HubRefusal as exc:
        # The hub refused this credential or request: answer from local cards only.
        retrieval.update(hub="refused", degraded=True, reason=exc.code or f"http-{exc.status}", status=exc.status)
        retrieval["fallback"] = sorted(lists)
    except hubc.HubConfigError:
        retrieval.update(hub="unavailable", degraded=True, reason="hub-config-invalid")
        retrieval["fallback"] = sorted(lists)
    if refused:
        retrieval["skipped_scopes"] = refused
    if unprobed:
        retrieval["unprobed_scopes"] = unprobed
    matches = fuse(lists, limit, home_scope=settings.scope)
    payload: dict[str, Any] = {
        "target": str(target),
        "query": query,
        "mode": settings.mode,
        "match_count": len(fuse(lists, 10**9, home_scope=settings.scope)),
        "matches": matches,
        "retrieval": retrieval,
    }
    if hub_match_count is not None:
        payload["hub_match_count"] = hub_match_count
    if retrieval["degraded"]:
        verb = "refused" if retrieval["hub"] == "refused" else "unavailable"
        status = f"{retrieval['status']} " if retrieval["hub"] == "refused" else ""
        warning = (
            f"hub search {verb} ({status}{retrieval['reason']}); answered from {' and '.join(retrieval['fallback'])}"
        )
        if retrieval.get("replica_incomplete"):
            warning += "; the replica is incomplete (it reached its size cap)"
        notes.insert(0, warning)
    if retrieval.get("skipped_scopes"):
        notes.append("the hub refused a read grant on " + ", ".join(retrieval["skipped_scopes"]))
    if retrieval.get("unprobed_scopes"):
        notes.append("the search budget ended before " + ", ".join(retrieval["unprobed_scopes"]) + " could be searched")
    if remembered:
        notes.append(
            "those refusals are remembered for "
            f"{int(store.REFUSED_SCOPES_TTL_SECONDS // 60)} minutes, then those scopes are tried again"
        )
    if notes:
        payload["warning"] = "; ".join(notes)
    return payload


def search(*, target: Path, query: str, limit: int = 20, json_output: bool = False) -> int:
    """`brigade memory search` in `read` or `sync` mode."""
    from . import memory_cmd

    try:
        payload = search_payload(target, query, limit=limit)
    except (hubc.HubConfigError, ValueError) as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: hub memory state under .brigade/memory/hub is unusable: {exc}", file=sys.stderr)
        return 2
    memory_cmd._record_search_log(
        target.expanduser().resolve(), query=query, matches=payload["matches"], private_query=True
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    hub_count = f", hub reports {payload['hub_match_count']}" if "hub_match_count" in payload else ""
    print(f"memory search: {payload['query']} ({payload['match_count']} match(es){hub_count})")
    if "warning" in payload:
        print(f"warning: {payload['warning']}")
    for match in payload["matches"]:
        print(f"- {match['path']}  [{match['source']}]  {match['title']}")
    return 0
