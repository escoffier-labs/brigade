"""Read-only promotion review queue, ranked by observed card opens.

A session earns 100 points and a distinct open day earns 10. Hub totals
are capped at 9 because their session kind is unknown. Recency contributes
at most one point. Legacy last-read dates are displayed but earn no credit:
the ledger also records automatic recall, which is not an interactive open.
Only dated interactive opens in hook ledgers earn session and day credit.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import memory_hub_store


def note_open(ledger: dict[str, Any], card_id: str, payload: dict[str, Any]) -> None:
    """Extend the existing session ledger, excluding known automated contexts.

    Hook subprocesses have no terminal, so isatty cannot identify the parent.
    Ordinary main-session hooks count unless dispatch or agent metadata marks
    them automated. Unknown explicit session kinds are excluded as well.
    """
    if (
        os.environ.get("BRIGADE_RUN_ID")
        or os.environ.get("BRIGADE_SCANNER_RUN_ID")
        or payload.get("agent_id")
        or payload.get("agent_type", "main") not in ("main", "claude")
        or payload.get("session_kind", "interactive") != "interactive"
    ):
        return
    table = ledger.setdefault("interactive_open_days", {})
    if card_id not in table and len(table) >= 200:
        return
    table[card_id] = sorted(set([*table.get(card_id, []), date.today().isoformat()]))[-366:]


def _opens(target: Path) -> dict[str, tuple[set[str], set[date]]]:
    from .claude_hooks import hub_recall

    result: dict[str, tuple[set[str], set[date]]] = {}
    root = target / ".brigade/work/claude-hooks/recall"
    for path in sorted(root.glob("*.json")):
        if path.is_symlink():
            continue
        ledger = hub_recall.load(path)
        for key, days in ledger["interactive_open_days"].items():
            valid = {date.fromisoformat(day) for day in days if date.fromisoformat(day) <= date.today()}
            if key not in ledger["opened"] or not valid:
                continue
            sessions, opened_days = result.setdefault(key, (set(), set()))
            sessions.add(path.stem)
            opened_days.update(valid)
    return result


def _hub_counts(target: Path, replica: memory_hub_store.Replica) -> dict[str, Any]:
    from . import memory_verify_state
    from .memory_verify_cmd import _binding, _snapshot

    try:
        state = memory_verify_state.load(target)
    except memory_verify_state.VerifyStateError:
        return {}
    meta = replica.meta
    if not all(meta.get(key) for key in ("hub", "principal", "log_epoch")):
        return {}
    if state.get("binding") != _binding(meta["hub"], meta["principal"], meta["log_epoch"]):
        return {}
    return {
        scope: snapshot
        for scope in state["scopes"]
        if (snapshot := _snapshot(state, scope, int(time.time() * 1000))) is not None
    }


def review_payload(target: Path) -> dict[str, Any]:
    from . import memory_card_reads, memory_cmd, memory_fairness, memory_hub_client, memory_hub_store

    target = target.expanduser().resolve()
    opens = _opens(target)
    reads = memory_card_reads.load(target)
    rows: list[dict[str, Any]] = []
    for card in memory_cmd.search_card_fields(target):
        meta, _ = memory_cmd._parse_frontmatter(card["body"])
        if meta.get("trust") != "agent-unreviewed":
            continue
        scope = meta.get("scope")
        rows.append(
            {
                "card_id": card["card_id"],
                "path": card["rel"],
                "scope": scope if memory_hub_client.valid_scope(scope) else None,
            }
        )
    try:
        replica = memory_hub_store.replica(target)
        # This is an offline snapshot listing, not a fresh hub authorization check.
        heads = sorted(
            replica.readable_heads(replica.meta.get("read_scopes") or []).values(),
            key=lambda h: (h["scope"], h["card_id"]),
        )
        counts = _hub_counts(target, replica)
    except (OSError, ValueError):
        # StoreError and ReplicaLimitError inherit OSError. Local review remains usable.
        heads = []
        counts = {}
    for head in heads:
        if head.get("trust") == "agent-unreviewed" and head.get("state") != "retired":
            rows.append({"card_id": head["card_id"], "scope": head["scope"], "path": None})
    # A local card with an explicit scope and its cached head are one review
    # item. Keep local path order and avoid counting the same card twice.
    local_keys = {(row["scope"], row["card_id"]) for row in rows if row["path"] is not None}
    rows = [row for row in rows if row["path"] is not None or (row["scope"], row["card_id"]) not in local_keys]
    identities = Counter(row["card_id"] for row in rows)
    for row in rows:
        card_id = row["card_id"]
        # Hook keys do not carry scope. Credit them only to local cards or a
        # unique cached hub identity, never to two scopes with the same id.
        unique = identities[card_id] == 1
        sessions, days = opens.get(card_id, (set(), set())) if unique else (set(), set())
        last = max(days, default=None)
        legacy = memory_card_reads.last_read(reads, [card_id, row["path"] or ""])
        scope_counts = counts.get(row["scope"], {})
        cached = scope_counts.get(memory_fairness.key_digest(row["scope"], [card_id])) if row["scope"] else None
        total = cached.total_reads if cached else None
        recency = round(1 / (1 + (date.today() - last).days), 6) if last else 0
        row["use"] = {
            "score": 100 * len(sessions) + 10 * len(days) + min(total or 0, 9) + recency,
            "interactive_sessions": len(sessions),
            "distinct_days": len(days),
            "hub_total_reads": total,
            "last_interactive_read": last.isoformat() if last else None,
            "last_recorded_read": legacy.isoformat() if legacy else None,
            "recency_points": recency,
        }
    # Python's stable sort preserves the path/scope order for ties and zero use.
    rows.sort(key=lambda row: -row["use"]["score"])
    return {"schema": "brigade.memory-review.v1", "items": rows, "total": len(rows), "offline": True}


def review(*, target: Path, json_output: bool = False) -> int:
    payload = review_payload(target)
    if json_output:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Promotion review: {payload['total']} unreviewed card(s), local snapshots only")
        for row in payload["items"]:
            use = row["use"]
            print(
                f"  {row['path'] or str(row['scope']) + '/' + row['card_id']} "
                f"score={use['score']:g} sessions={use['interactive_sessions']} days={use['distinct_days']} "
                f"hub_reads={use['hub_total_reads']} last_open={use['last_interactive_read']} "
                f"last_recorded={use['last_recorded_read']} recency={use['recency_points']:g}"
            )
    return 0
