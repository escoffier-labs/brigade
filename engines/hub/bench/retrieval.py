"""Bounded S17 public-fixture comparison. Two fresh runs per binary and case.

Run inside a Brigade verification script. Only disposable fixture hubs are
started. No external hub or embedding endpoint option is accepted here.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from brigade.memory_hub_client import event_id
from brigade.memory_retrieval_eval.corpus import default_fixture_root, load_cards, load_queries, resolve_gold_aliases
from brigade.memory_retrieval_eval.hub import HubOptions, hub_client, stable_digest
from brigade.memory_retrieval_eval.metrics import aggregate_scores, score_query


def measure(binary: Path, overlay: bool) -> dict:
    root = default_fixture_root()
    cards = load_cards(root)
    _, queries = load_queries(root / "queries.json")
    queries = resolve_gold_aliases(cards, queries)
    ids = {Path(c.path).stem: c.card_id for c in cards}
    with hub_client(HubOptions(binary=binary), cards) as client:
        if client is None:
            raise ValueError("comparison binary unavailable")
        seq = 0
        if overlay:
            # Public fixture plus deterministic predecessor and timer copies.
            # Both binaries receive identical cards, text and source hints.
            for card in cards:
                stem = Path(card.path).stem
                relation = f"superseded_by: {stem}\n"
                body = "---\n" + relation + card.body[4:] if card.body.startswith("---\n") else "---\n" + relation + "---\n" + card.body
                for prefix, text, origin in [("old-", body, "interactive"), ("timer-", card.body, "scheduled")]:
                    put = client.call("put", scope="fleet", card_id=prefix + stem, text=text, origin=origin, client_event_id=event_id())
                    seq = put["event"]["seq"]
        if seq:
            client.call("search", query="readiness", mode="lexical", min_seq=seq)
        load = os.getloadavg()
        start = time.monotonic()
        rows = []
        for query in queries:
            reply = client.call("search", query=query.query, mode="lexical", limit=20, scopes=["fleet"])
            assert reply["retrieval"]["mode"] == "lexical" and not reply["retrieval"]["stale"]
            ranked = [ids.get(hit["card_id"], hit["card_id"]) for hit in reply["matches"]]
            rows.append(score_query(ranked, query.gold, 5))
        elapsed = time.monotonic() - start
        return {"case": "lineage-sources" if overlay else "fixture", "load_average": load,
                "seconds": elapsed, **aggregate_scores(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for overlay in (False, True):
        for run in (1, 2):
            for label, binary in (("before", args.before), ("after", args.after)):
                results.append({"version": label, "run": run, **measure(binary.resolve(), overlay)})
    report = {"fixture_digest": stable_digest([(c.card_id, c.body) for c in load_cards(default_fixture_root())]),
              "ranking_depth": 20, "k": 5, "results": results}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    for case in ("fixture", "lineage-sources"):
        before = [r for r in results if r["case"] == case and r["version"] == "before"]
        after = [r for r in results if r["case"] == case and r["version"] == "after"]
        for key in ("hit_rate", "mrr"):
            assert min(r[key] for r in after) >= max(r[key] for r in before), (case, key)


if __name__ == "__main__":
    main()
