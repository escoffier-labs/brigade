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


def measure(binary: Path, overlay: bool, padding: int = 0) -> dict:
    root = default_fixture_root()
    cards = load_cards(root)
    _, queries = load_queries(root / "queries.json")
    queries = resolve_gold_aliases(cards, queries)
    ids = {Path(c.path).stem: c.card_id for c in cards}
    with hub_client(HubOptions(binary=binary), cards) as client:
        if client is None:
            raise ValueError("comparison binary unavailable")
        assert client.call("status")["scan"]["available"], "scan gate unavailable in comparison binary"
        seq = 0
        if overlay:
            # Public fixture plus deterministic predecessor and timer copies.
            # Both binaries receive identical cards, text and source hints.
            for card in cards:
                stem = Path(card.path).stem
                if client.call("card", scope="fleet", card_id=stem)["card"]["trust"] == "flagged":
                    continue
                relation = f"superseded_by: {stem}\n"
                body = "---\n" + relation + card.body[4:] if card.body.startswith("---\n") else "---\n" + relation + "---\n" + card.body
                for prefix, text, origin in [("old-", body, "interactive"), ("timer-", card.body, "scheduled")]:
                    put = client.call("import" if prefix == "old-" else "put", scope="fleet", card_id=prefix + stem, text=text, origin=origin, client_event_id=event_id())
                    seq = put["event"]["seq"]
        for n in range(padding):
            added = client.call("put", scope="fleet", card_id=f"padding-{n:05d}", text="paddingzzzz", origin="interactive", client_event_id=event_id())
            seq = added["event"]["seq"]
        if seq:
            client.call("search", query="readiness", mode="lexical", min_seq=seq)
        live_count = client.call("status")["cards"]
        load = os.getloadavg()
        start = time.monotonic()
        rows = []
        visited = []
        for query in queries:
            reply = client.call("search", query=query.query, mode="lexical", limit=20, scopes=["fleet"])
            assert reply["retrieval"]["mode"] == "lexical" and not reply["retrieval"]["stale"]
            ranked = [ids.get(hit["card_id"], hit["card_id"]) for hit in reply["matches"]]
            if "lineage_visited" in reply:
                visited.append(reply["lineage_visited"])
            rows.append(score_query(ranked, query.gold, 5))
        elapsed = time.monotonic() - start
        empty = client.call("search",query="nohitzzzz",mode="lexical",limit=20,scopes=["fleet"])
        assert not empty["matches"]
        if visited:
            assert max(visited) <= 200
            assert empty["lineage_visited"] == 0
        return {"case": f"fixture-{padding}-padding" if padding else "lineage-sources" if overlay else "fixture", "load_average": load,
                "live_count":live_count,"max_lineage_visited":max(visited) if visited else None,"empty_lineage_visited":empty.get("lineage_visited"),
                "seconds": elapsed, **aggregate_scores(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for overlay, padding in ((False, 0), (True, 0), (False, 5000)):
        for run in (1, 2):
            for label, binary in (("before", args.before), ("after", args.after)):
                results.append({"version": label, "run": run, **measure(binary.resolve(), overlay, padding)})
    report = {"fixture_digest": stable_digest([(c.card_id, c.body) for c in load_cards(default_fixture_root())]),
              "ranking_depth": 20, "k": 5, "results": results}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    for case in ("fixture", "lineage-sources", "fixture-5000-padding"):
        before = [r for r in results if r["case"] == case and r["version"] == "before"]
        after = [r for r in results if r["case"] == case and r["version"] == "after"]
        for key in ("hit_rate", "mrr"):
            assert min(r[key] for r in after) >= max(r[key] for r in before), (case, key)


if __name__ == "__main__":
    main()
