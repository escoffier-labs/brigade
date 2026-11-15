"""Use-derived private judgments and proposal-bound lexical replay (#2376).

Only explicit searches enter this store. A later interactive open with identical
content judges the latest matching search. Neither result delivery nor eval
replay is a use signal. All outward errors and reports omit query text.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from brigade import memory_proposal_io as io
from brigade.inbox_lock import held_file_lock
from brigade.memory_hub_client import MemoryHubClient, event_id
from brigade.memory_hub_store import write_private

from .adapters import bm25_adapter
from .corpus import CardDoc, QuerySpec, repo_root
from .hub import HubAdapter, HubOptions, stable_digest
from .metrics import aggregate_scores, score_query

MIN_QUERIES = 20
MAX_HISTORY = 500
USE_WINDOW_SECONDS = 600
MAX_AGE_SECONDS = 30 * 86400
METRICS = ("hit_rate", "mrr", "precision_at_k", "recall_at_k")


class LiveEvalError(ValueError):
    """Fixed diagnostic or counts generated here, with no private state bytes."""


def text_hash(text: str) -> str:
    return hashlib.sha256((text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n").encode()).hexdigest()


def state_path(target: Path) -> Path:
    root = Path.home() / ".brigade" / "private-retrieval-eval"
    resolved = root.resolve()
    if any(resolved.is_relative_to(p.resolve()) for p in (target, repo_root())) or any(
        (p / ".git").exists() for p in (resolved, *resolved.parents)
    ):
        raise LiveEvalError("private retrieval state must stay outside repositories")
    return root / stable_digest(str(target.resolve())) / "history.json"


def derived_path(target: Path, hub: str, epoch: str, scope: str) -> Path:
    return state_path(target).parent / (stable_digest([hub, epoch, scope]) + ".json")


def _read(path: Path) -> list[dict[str, Any]]:
    raw = io.text(path, limit=io.MAX_STATE_BYTES, absent=True, private=True)
    rows = json.loads(raw) if raw is not None else []
    if not isinstance(rows, list) or len(rows) > MAX_HISTORY or any(not isinstance(r, dict) for r in rows):
        raise LiveEvalError("invalid private history")
    return rows


def _change(target: Path, update: Callable[[list[dict[str, Any]]], None]) -> None:
    # Logging must never break retrieval or echo sensitive state on failure.
    try:
        path = state_path(target)
        with io.directory(path.parent, create=True, private=True):
            pass
        lock = path.with_suffix(".lock")
        io.protect_file(lock)
        with held_file_lock(lock, deadline_seconds=1):
            rows = _read(path)
            update(rows)
            write_private(path, json.dumps(rows[-MAX_HISTORY:], separators=(",", ":")))
    except (OSError, ValueError, TypeError, KeyError):
        return


def record_search(
    target: Path, query: str, matches: list[dict[str, Any]], *, hub: str, epoch: str, now: float | None = None
) -> None:
    if not query.strip() or len(query.encode()) > 4096 or not epoch:
        return
    results = [
        {k: m[k] for k in ("scope", "card_id", "content_hash")}
        for m in matches
        if all(isinstance(m.get(k), str) and m[k] for k in ("scope", "card_id", "content_hash"))
    ]
    if results:
        _change(
            target,
            lambda rows: rows.append(
                {
                    "ts": time.time() if now is None else now,
                    "hub": hub,
                    "epoch": epoch,
                    "query": query,
                    "results": results,
                    "gold": [],
                }
            ),
        )


def record_use(target: Path, card_id: str, text: str, *, now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    digest = text_hash(text)

    def update(rows: list[dict[str, Any]]) -> None:
        eligible = [
            (r, m)
            for r in rows
            if 0 <= stamp - r["ts"] <= USE_WINDOW_SECONDS
            for m in r["results"]
            if m["card_id"] == card_id and m["content_hash"] == digest
        ]
        # File opens have no hub scope. Ambiguous identities earn no label.
        if len({(r["hub"], r["epoch"], m["scope"]) for r, m in eligible}) != 1:
            return
        row, match = max(eligible, key=lambda pair: pair[0]["ts"])
        if match not in row["gold"]:
            row["gold"].append(match)

    _change(target, update)


def derive_queries(target: Path, *, hub: str, epoch: str, scope: str, now: float | None = None) -> list[QuerySpec]:
    stamp = time.time() if now is None else now
    try:
        rows = _read(state_path(target))
        grouped: dict[str, set[str]] = {}
        for row in rows:
            if row["hub"] != hub or row["epoch"] != epoch or not 0 <= stamp - row["ts"] <= MAX_AGE_SECONDS:
                continue
            query = " ".join(row["query"].lower().split())
            gold = {m["card_id"] for m in row["gold"] if m["scope"] == scope}
            if gold:
                grouped.setdefault(query, set()).update(gold)
        queries = [
            QuerySpec(str(i), q, tuple(sorted(gold)), "observed") for i, (q, gold) in enumerate(sorted(grouped.items()))
        ]
        if len(queries) < MIN_QUERIES:
            raise LiveEvalError(f"insufficient judged queries: {len(queries)} available, {MIN_QUERIES} required")
        write_private(
            derived_path(target, hub, epoch, scope),
            json.dumps({"queries": [{"query": q.query, "gold": q.gold} for q in queries]}, separators=(",", ":")),
        )
        return queries
    except (OSError, TypeError, KeyError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("insufficient judged queries:"):
            raise
        raise LiveEvalError("private retrieval history unavailable") from None


def _card(card_id: str, text: str) -> CardDoc:
    from brigade.memory_cmd import card_fields_from_text

    path = f"memory/cards/{card_id}.md"
    fields = card_fields_from_text(text, path, stem=card_id)
    return CardDoc(card_id, path, fields["title"], tuple(fields["tags"]), fields["summary"], text)


def snapshot(client: MemoryHubClient, scope: str) -> tuple[list[CardDoc], str]:
    status = client.call("status")
    epoch, high = status["log_epoch"], status["seq"]
    after = 0
    heads: dict[str, dict[str, Any]] = {}
    while after < high:
        page = client.call("log", after_seq=after, limit=500)
        if page.get("log_epoch") != epoch or page["next_seq"] <= after:
            raise LiveEvalError("live scope snapshot changed or incomplete")
        for event in page["events"]:
            if event["seq"] <= high and event.get("scope") == scope and event.get("revision") is not None:
                heads[event["card_id"]] = event
        after = page["next_seq"]
    from brigade.memory_hub_sync import _card_text, _EpochMoved

    cards = []
    for card_id, head in sorted(heads.items()):
        try:
            card = _card_text(client, scope, card_id, head["revision"], epoch)
        except _EpochMoved:
            raise LiveEvalError("live scope snapshot epoch changed") from None
        if card is None:
            raise LiveEvalError("live scope snapshot card unavailable")
        if card.get("state") != "retired":
            if text_hash(card["text"]) != card["content_hash"]:
                raise LiveEvalError("live scope snapshot content mismatch")
            cards.append(_card(card_id, card["text"]))
    return cards, epoch


def measure_candidate(
    cards: Sequence[CardDoc],
    queries: Sequence[QuerySpec],
    proposal: dict[str, Any],
    baseline: Callable[[str, int], list[tuple[str, float]]],
    binding: dict[str, Any],
) -> dict[str, Any]:
    if len(queries) < MIN_QUERIES:
        raise LiveEvalError(f"insufficient judged queries: {len(queries)} available, {MIN_QUERIES} required")
    before = {c.card_id: c for c in cards}
    after = dict(before)
    targets = proposal["targets"]
    if not targets:
        raise LiveEvalError("live candidate targets unavailable")
    for target in targets:
        card_id = target["card_id"]
        if card_id not in before or text_hash(before[card_id].body) != target["before_hash"]:
            raise LiveEvalError("live candidate target changed")
        if text_hash(target["after_text"]) != target["after_hash"]:
            raise LiveEvalError("live candidate content mismatch")
        if target["after_state"] == "retired":
            after.pop(card_id)
        elif target["after_state"] == "live":
            after[card_id] = _card(card_id, target["after_text"])
        else:
            raise LiveEvalError("live candidate state unavailable")
    if any(g not in before for q in queries for g in q.gold):
        raise LiveEvalError("judged card no longer live")
    local = bm25_adapter(cards)
    candidate = bm25_adapter(sorted(after.values(), key=lambda c: c.path))
    scores: dict[str, list[dict[str, Any]]] = {"before": [], "candidate": []}
    parity = True
    for query in queries:
        ranked = baseline(query.query, 20)
        parity &= [i for i, _ in ranked] == [i for i, _ in local(query.query, 20)]
        scores["before"].append(score_query([i for i, _ in ranked], query.gold, 5))
        scores["candidate"].append(score_query([i for i, _ in candidate(query.query, 20)], query.gold, 5))
    blocks: dict[str, Any] = {
        name: {"available": True, "overall": aggregate_scores(rows)} for name, rows in scores.items()
    }
    failures = []
    if not parity:
        failures.append("live lexical replay differs from snapshot")
    if any(blocks["candidate"]["overall"][m] < blocks["before"]["overall"][m] for m in METRICS):
        failures.append("candidate retrieval regression")
    safe_targets = [
        {k: t[k] for k in ("scope", "card_id", "before_hash", "after_hash", "after_state")} for t in targets
    ]
    return {
        "kind": "memory-retrieval-eval",
        "evaluation_id": event_id(),
        "k": 5,
        "query_set_digest": stable_digest([(q.query, q.gold) for q in queries]),
        "corpus": {"card_count": len(cards), "query_count": len(queries)},
        "gate": {"name": "Live-R0", "passed": not failures, "failures": failures},
        "live": {
            "proposal_id": proposal["proposal_id"],
            "targets": safe_targets,
            "replay_matches": parity,
            "binding": binding,
        },
        "adapters": blocks,
    }


def run_live_eval(target: Path, options: HubOptions, proposal_id: str, *, record: bool = False) -> dict[str, Any]:
    if not options.url or not options.token_file or options.binary or options.memory_config:
        raise LiveEvalError("live evaluation requires an explicit external hub and credential")
    client = MemoryHubClient(options.url, options.token_file.read_text().strip())
    cards, epoch = snapshot(client, options.scope)
    queries = derive_queries(target, hub=client.identity, epoch=epoch, scope=options.scope)
    proposal = client.call("proposal_show", proposal_id=proposal_id)
    targets = proposal.get("retrieval_eval_targets")
    if not targets or any(t["scope"] != options.scope for t in targets):
        raise LiveEvalError("live candidate requires review-readable targets in the evaluated scope")
    search = HubAdapter(client, "lexical", replace(options, raw=True), cards)
    search(queries[0].query, 20)
    binding = dict(search.binding or {})
    status = client.call("status")
    if status.get("default_mode") != "lexical":
        raise LiveEvalError("live hybrid candidate replay unavailable")
    report = measure_candidate(cards, queries, {"proposal_id": proposal_id, "targets": targets}, search, binding)
    search(queries[0].query, 20)  # Fence the complete run, including the last query.
    report["digest"] = stable_digest(report)
    if record:
        from .hub import record_report

        record_report(client, report)
    return report
