"""Second-class repeat retrieval counts over existing local search events."""

from __future__ import annotations

from typing import Any


def _normalize_search_query(query: str) -> str:
    return " ".join(str(query or "").lower().split())


def redundancy_from_entries(entries: list[dict[str, Any]], *, window_s: int) -> dict[str, int]:
    """Compare adjacent valid events within the follow-up window, without retrieval.

    Query repeats use the log's case/whitespace normalization. Result repeats
    require the same nonempty card-id set (order does not matter). Empty results
    are misses, not useful repeated retrieval. Counts overlap and are diagnostic
    only; they do not establish usefulness or supply verification scores.
    """
    from .memory_cmd import _parse_search_log_ts

    counts = dict(compared_pairs=0, repeat_queries=0, repeat_results=0, repeat_query_results=0)
    for prior, current in zip(entries, entries[1:], strict=False):
        if not isinstance(prior, dict) or not isinstance(current, dict):
            continue
        prior_ts, current_ts = (_parse_search_log_ts(entry.get("ts")) for entry in (prior, current))
        if prior_ts is None or current_ts is None or not 0 <= (current_ts - prior_ts).total_seconds() <= window_s:
            continue
        prior_ids, current_ids = prior.get("card_ids"), current.get("card_ids")
        if not isinstance(prior_ids, list) or not isinstance(current_ids, list):
            continue
        if not all(isinstance(key, str) for key in prior_ids + current_ids):
            continue
        prior_query, current_query = (entry.get("query") for entry in (prior, current))
        if not isinstance(prior_query, str) or not isinstance(current_query, str):
            continue
        counts["compared_pairs"] += 1
        query = _normalize_search_query(prior_query)
        same_query = bool(query) and query == _normalize_search_query(current_query)
        prior_set, current_set = set(filter(None, prior_ids)), set(filter(None, current_ids))
        same_results = bool(prior_set) and prior_set == current_set
        counts["repeat_queries"] += int(same_query)
        counts["repeat_results"] += int(same_results)
        counts["repeat_query_results"] += int(same_query and same_results)
    return counts
