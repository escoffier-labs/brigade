"""Second-class repeat retrieval counts over existing local search events."""

from __future__ import annotations

from typing import Any


def _normalize_search_query(query: str) -> str:
    return " ".join(str(query or "").lower().split())


def _query_key(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Comparable query identity: normalized text, or the keyed recall digest.

    Recall events log ``query_hmac`` instead of text. The digest covers the
    normalized query under one per-host key, so equal digests mean equal
    queries. A text event and a digest event never count as the same query.
    """
    query = entry.get("query")
    if isinstance(query, str):
        return ("text", _normalize_search_query(query))
    digest = entry.get("query_hmac")
    if isinstance(digest, str):
        return ("hmac", digest)
    return None


def redundancy_from_entries(entries: list[dict[str, Any]], *, window_s: int) -> dict[str, int]:
    """Compare adjacent valid events within the follow-up window, without retrieval.

    Query repeats use the log's case/whitespace normalization, or the keyed
    ``query_hmac`` digest for recall events. Result repeats
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
        prior_key, current_key = _query_key(prior), _query_key(current)
        if prior_key is None or current_key is None:
            continue
        counts["compared_pairs"] += 1
        same_query = bool(prior_key[1]) and prior_key == current_key
        prior_set, current_set = set(filter(None, prior_ids)), set(filter(None, current_ids))
        same_results = bool(prior_set) and prior_set == current_set
        counts["repeat_queries"] += int(same_query)
        counts["repeat_results"] += int(same_results)
        counts["repeat_query_results"] += int(same_query and same_results)
    return counts
