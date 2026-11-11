"""Search fusion and fallback for hub memory (S14).

In `read` and `sync` modes, `memory search` asks the hub (800 ms budget) for
matches in the configured read scopes only, and fuses its list with the local
BM25 list by reciprocal rank fusion. One entry stands for one
`(scope, card_id, content_hash)`; a local card, which has no scope, joins the
hub entry with its bytes. When the hub is unreachable, times out, or answers
3xx, 404, 408, 429 or 503, the answer comes from local cards and the replica
and says it degraded. A refusal (401, 403 or any other coded answer) never
falls back to the replica: the hub has just said no, so the answer is local
cards only and the warning says why. While a replica rebuild is in progress
there is no replica, so the fallback is local cards only. `off` mode never
reaches this module.

The query goes to the hub over the authenticated transport and nowhere else:
nothing here writes it to disk. The caller's search log keeps the #2196 rules.
"""

from __future__ import annotations

import json
import sys
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
                entry["hub_ranks"] = dict(match.get("ranks") or {})
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
    local, _ = _local_matches(target, query)
    lists: dict[str, list[dict[str, Any]]] = {"local": local}
    retrieval: dict[str, Any] = {"hub": "ok", "degraded": False}
    hub = ""
    hub_limit = min(limit, HUB_LIMIT_MAX)
    hub_match_count: int | None = None
    notes: list[str] = []
    try:
        client = hubc.client_for(settings, transport=transport)
        hub = client.identity
        body = client.call(
            "search",
            timeout=hubc.SEARCH_BUDGET_SECONDS,
            query=query,
            limit=hub_limit,
            query_source=query_source,
            scopes=list(settings.read_scopes),
        )
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
    memory_cmd._record_search_log(target.expanduser().resolve(), query=query, matches=payload["matches"])
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
