"""Use-derived private judgments and proposal-bound lexical replay (#2376).

The hub owns search/use history and derives judgments. Neither result delivery
nor eval replay is a use signal. Outward errors and reports omit query text.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from brigade.memory_hub_client import MemoryHubClient, event_id, urllib_transport

from .adapters import bm25_adapter
from .corpus import CardDoc, QuerySpec
from .hub import HubAdapter, HubOptions, stable_digest
from .metrics import aggregate_scores, score_query

MIN_QUERIES = 1
MAX_EVENTS = 10_000
MAX_CARDS = 2_000
MAX_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 2_500
MAX_SECONDS = 60


class LiveEvalError(ValueError):
    """Fixed diagnostic or counts generated here, with no private state bytes."""


def text_hash(text: str) -> str:
    return hashlib.sha256((text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n").encode()).hexdigest()


def interactive_session(payload: dict[str, Any] | None = None) -> dict[str, str]:
    """Require affirmative session context. Never infer interactive from absence."""
    payload = payload or {}
    if (
        os.environ.get("BRIGADE_RUN_ID")
        or os.environ.get("BRIGADE_SCANNER_RUN_ID")
        or payload.get("agent_id")
        or payload.get("agent_type", "main") not in ("main", "claude")
    ):
        return {}
    kind = payload.get("session_kind") or os.environ.get("BRIGADE_SESSION_KIND")
    sid = payload.get("session_id") or os.environ.get("BRIGADE_SESSION_ID")
    if kind != "interactive" or not isinstance(sid, str) or not 1 <= len(sid) <= 128:
        return {}
    if not all(c.isascii() and (c.isalnum() or c in "-_.:") for c in sid):
        return {}
    return {"session_id": sid, "session_kind": "interactive"}


def record_use(target: Path, card_id: str, text: str, *, payload: dict[str, Any] | None = None) -> None:
    """Best effort. The hub joins only its own search in this credential/session."""
    context = interactive_session(payload)
    if not context:
        return
    try:
        from brigade.memory_hub_client import HubConfigError, HubRefusal, HubUnavailable, load_settings

        settings = load_settings(target)
        if not settings.enabled:
            return
        client = MemoryHubClient(settings.hub_url, settings.token)
        client.call(
            "retrieval_use",
            timeout=0.8,
            card_id=card_id,
            content_hash=text_hash(text),
            client_event_id=event_id(),
            **context,
        )
    except (OSError, ValueError, HubConfigError, HubRefusal, HubUnavailable):
        return


class BudgetClient(MemoryHubClient):
    """One shared aggregate budget for snapshot, replay and report recording."""

    def __init__(self, client: MemoryHubClient) -> None:
        self.client = client
        self.identity = client.identity
        self.deadline = time.monotonic() + MAX_SECONDS
        self.requests = 0
        self.bytes = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline or self.requests >= MAX_REQUESTS or self.bytes >= MAX_BYTES:
            raise LiveEvalError("live evaluation budget exceeded")

    def consume_bytes(self, size: int) -> None:
        self.bytes += size
        if self.bytes > MAX_BYTES:
            raise LiveEvalError("live evaluation budget exceeded")

    def call(self, action: str, *, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        self.check()
        self.requests += 1
        transport = self.client._transport
        counted = isinstance(transport, partial) and transport.func is urllib_transport
        if counted:
            self.client._transport = partial(
                urllib_transport, max_response_bytes=MAX_BYTES - self.bytes, response_bytes=self.consume_bytes
            )
        reply = self.client.call(
            action, timeout=min(timeout or 10, max(0.001, self.deadline - time.monotonic())), **fields
        )
        if not counted:
            self.consume_bytes(len(json.dumps(reply, ensure_ascii=False).encode()))
        if self.bytes > MAX_BYTES or time.monotonic() >= self.deadline:
            raise LiveEvalError("live evaluation budget exceeded")
        return reply


def derive_queries(client: MemoryHubClient, scope: str) -> tuple[list[QuerySpec], dict[str, Any]]:
    from brigade.memory_hub_client import HubRefusal

    try:
        judged = client.call("retrieval_judgments", scope=scope)
    except HubRefusal:
        raise LiveEvalError("hub judged history unavailable or insufficient") from None
    queries = [QuerySpec(str(i), q, tuple(gold), "observed") for i, (q, gold) in enumerate(judged["queries"])]
    if len(queries) < MIN_QUERIES:
        raise LiveEvalError(f"insufficient judged queries: {len(queries)} available, {MIN_QUERIES} required")
    return queries, judged


def _card(card_id: str, text: str) -> CardDoc:
    from brigade.memory_cmd import card_fields_from_text

    path = f"memory/cards/{card_id}.md"
    fields = card_fields_from_text(text, path, stem=card_id)
    return CardDoc(card_id, path, fields["title"], tuple(fields["tags"]), fields["summary"], text)


def snapshot(client: MemoryHubClient, scope: str) -> tuple[list[CardDoc], str]:
    status = client.call("status")
    epoch, high = status["log_epoch"], status["seq"]
    after = 0
    events = 0
    heads: dict[str, dict[str, Any]] = {}
    while after < high:
        page = client.call("log", after_seq=after, limit=500)
        if page.get("log_epoch") != epoch or page["next_seq"] <= after:
            raise LiveEvalError("live scope snapshot changed or incomplete")
        events += len(page["events"])
        if events > MAX_EVENTS:
            raise LiveEvalError("live evaluation budget exceeded")
        for event in page["events"]:
            if event["seq"] <= high and event.get("scope") == scope and event.get("revision") is not None:
                heads[event["card_id"]] = event
                if len(heads) > MAX_CARDS:
                    raise LiveEvalError("live evaluation budget exceeded")
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
    queries = [replace(q, gold=tuple(g for g in q.gold if g in before)) for q in queries]
    queries = [q for q in queries if q.gold]
    if len(queries) < MIN_QUERIES:
        raise LiveEvalError(f"insufficient judged queries: {len(queries)} available, {MIN_QUERIES} required")
    local = bm25_adapter(cards)
    candidate = bm25_adapter(sorted(after.values(), key=lambda c: c.path))
    scores: dict[str, list[dict[str, Any]]] = {"before": [], "candidate": []}
    parity = True
    vetoes = 0
    for query in queries:
        budget = getattr(baseline, "client", None)
        if isinstance(budget, BudgetClient):
            budget.check()
        ranked = baseline(query.query, 20)
        parity &= [i for i, _ in ranked] == [i for i, _ in local(query.query, 20)]
        scores["before"].append(score_query([i for i, _ in ranked], query.gold, 5))
        candidate_ids = [i for i, _ in candidate(query.query, 20)]
        before_ids = [i for i, _ in ranked]
        vetoes += sum(
            (candidate_ids.index(gold) if gold in candidate_ids else float("inf"))
            > (before_ids.index(gold) if gold in before_ids else float("inf"))
            for gold in query.gold
        )
        scores["candidate"].append(score_query(candidate_ids, query.gold, 5))
    blocks: dict[str, Any] = {
        name: {"available": True, "overall": aggregate_scores(rows)} for name, rows in scores.items()
    }
    failures = []
    if not parity:
        failures.append("live lexical replay differs from snapshot")
    if vetoes:
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
        "gate": {"name": "Live-R0", "passed": not failures, "failures": failures, "authorizes": False},
        "live": {
            "proposal_id": proposal["proposal_id"],
            "targets": safe_targets,
            "replay_matches": parity,
            "veto_count": vetoes,
            "binding": binding,
        },
        "adapters": blocks,
    }


def run_live_eval(target: Path, options: HubOptions, proposal_id: str, *, record: bool = False) -> dict[str, Any]:
    if not options.url or not options.token_file or options.binary or options.memory_config:
        raise LiveEvalError("live evaluation requires an explicit external hub and credential")
    client = BudgetClient(
        MemoryHubClient(
            options.url,
            options.token_file.read_text().strip(),
            transport=partial(urllib_transport, max_response_bytes=MAX_BYTES),
        )
    )
    cards, epoch = snapshot(client, options.scope)
    queries, judged = derive_queries(client, options.scope)
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
    report["query_set_digest"] = judged["query_set_digest"]
    report["live"]["history_seq"] = judged["history_seq"]
    client.check()
    report["digest"] = stable_digest(report)
    if record:
        from .hub import record_report

        record_report(client, report)
    return report
