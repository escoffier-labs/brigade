"""Search fusion and fallback for hub memory (S14).

In `read` and `sync` modes, `memory search` asks the hub (800 ms budget) and
fuses its list with the local BM25 list by reciprocal rank fusion, keyed by
`(card_id, content_hash)`. When the hub is unreachable, times out, or answers
404 or 503, the answer comes from local cards and the replica and says it
degraded. While a replica rebuild is in progress there is no replica, so the
fallback is local cards only. `off` mode never reaches this module.

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
HUB_LIMIT = 20
_SOURCE_ORDER = {"hub": 0, "local": 1, "replica": 2}


def enabled(target: Path) -> bool:
    """Whether `memory search` should route here. Config errors route here too, to be reported."""
    try:
        return hubc.hub_mode(target.expanduser().resolve()) != "off"
    except hubc.HubConfigError:
        return True


def _replica_matches(target: Path, query: str, hub: str) -> list[dict[str, Any]]:
    from . import memory_cmd

    replica = store.replica(target)
    if replica.watermark(hub) is None:
        return []
    heads = list(replica.heads.values())
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
    return matches[:WIDE]


def _local_matches(target: Path, query: str) -> tuple[list[dict[str, Any]], int]:
    from . import memory_cmd

    payload = memory_cmd.search_cards_payload(target, query, limit=WIDE)
    matches = payload["matches"]
    for match in matches:
        try:
            match["content_hash"] = hubc.content_hash((target / match["path"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            match["content_hash"] = None
    return matches, int(payload["match_count"])


def fuse(lists: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """RRF over the named lists, one entry per `(card_id, content_hash)`."""
    fused: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sorted(lists, key=_SOURCE_ORDER.__getitem__):
        for rank, match in enumerate(lists[source], start=1):
            key = (str(match.get("card_id")), str(match.get("content_hash") or f"{source}:{match.get('path')}"))
            entry = fused.get(key)
            if entry is None:
                entry = fused[key] = {**match, "source": source, "ranks": {}, "rrf": 0.0, "best": (rank, source)}
            entry["ranks"][source] = rank
            entry["rrf"] += 1.0 / (RRF_K + rank)
            if (rank, _SOURCE_ORDER[source]) < (entry["best"][0], _SOURCE_ORDER[entry["best"][1]]):
                entry["best"] = (rank, source)
                entry.update({k: v for k, v in match.items() if k != "ranks"}, source=source)
    ordered = sorted(fused.values(), key=lambda m: (-m["rrf"], _SOURCE_ORDER[m["source"]], str(m.get("path"))))
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
    try:
        client = hubc.client_for(settings, transport=transport)
        hub = client.identity
        body = client.call(
            "search",
            timeout=hubc.SEARCH_BUDGET_SECONDS,
            query=query,
            limit=HUB_LIMIT,
            query_source=query_source,
        )
        lists["hub"] = [
            {**m, "path": m.get("path") or f"cards/{m.get('card_id')}.md"}
            for m in body.get("matches") or []
            if isinstance(m, dict)
        ]
        hub_retrieval = hubc.obj(body.get("retrieval"))
        retrieval["effective"] = hub_retrieval.get("mode")
        retrieval["stale"] = hub_retrieval.get("stale")
    except (hubc.HubUnavailable, hubc.HubRefusal, hubc.HubConfigError) as exc:
        reason = (
            exc.reason
            if isinstance(exc, hubc.HubUnavailable)
            else (exc.code or f"http-{exc.status}")
            if isinstance(exc, hubc.HubRefusal)
            else "hub-config-invalid"
        )
        retrieval.update(hub="unavailable", degraded=True, reason=reason)
        if hub:
            replica = _replica_matches(target, query, hub)
            if replica:
                lists["replica"] = replica
        retrieval["fallback"] = sorted(lists)
    matches = fuse(lists, limit)
    payload: dict[str, Any] = {
        "target": str(target),
        "query": query,
        "mode": settings.mode,
        "match_count": len(fuse(lists, 10**9)),
        "matches": matches,
        "retrieval": retrieval,
    }
    if retrieval["degraded"]:
        payload["warning"] = (
            f"hub search unavailable ({retrieval['reason']}); answered from {' and '.join(retrieval['fallback'])}"
        )
    return payload


def search(*, target: Path, query: str, limit: int = 20, json_output: bool = False) -> int:
    """`brigade memory search` in `read` or `sync` mode."""
    from . import memory_cmd

    try:
        payload = search_payload(target, query, limit=limit)
    except (hubc.HubConfigError, ValueError) as exc:
        print(f"error: invalid memory-care config: {exc}", file=sys.stderr)
        return 2
    memory_cmd._record_search_log(target.expanduser().resolve(), query=query, matches=payload["matches"])
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"memory search: {payload['query']} ({payload['match_count']} match(es))")
    if "warning" in payload:
        print(f"warning: {payload['warning']}")
    for match in payload["matches"]:
        print(f"- {match['path']}  [{match['source']}]  {match['title']}")
    return 0
