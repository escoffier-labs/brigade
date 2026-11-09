"""Hub recall, the prompt-load scan and the Stop capture for harness hooks (S24).

Everything here runs inside a killable child (``memory_recall_worker`` for
recall, ``memory_hook_capture_worker`` for the capture), so a hub that is
stopped, slow or hung costs a hook at most the child's deadline.

Recall: in hub ``read`` or ``sync`` mode the hook query goes through
``memory_hub_search.search_payload``, which asks the hub within its 800 ms
budget and fuses the answer with local cards, or answers from local cards and
the replica when the hub is down. Any other failure falls back to plain local
recall. Hub and replica hits pass the hub push path's trust rule
(``engines/hub/src/memory/search.rs``): only ``reviewed``, ``policy-promoted``
and ``imported`` versions are shown, and a hit with no trust is dropped, so
another principal's ``agent-unreviewed`` card in a shared scope never reaches a
prompt unasked. The hub's own push query source (S23) also drops private-class
cards, which local sessions may see, so the hook filters here instead.

Prompt-load scan: every recall line a hook is about to inject, local cards
included, is scanned with the hub memory bundle (S18). A blocking or warning
finding replaces the line with ``{card_id, blocked: <rule id>}``. The private
class loads into the operator's local harnesses and is replaced, with
``code: memory_content_private``, for a connector audience. A scan that raises
fails closed: no recall at all.

Capture: one ``capture`` call per batch with a hashed session reference and
the card keys pushed and opened. Ids only, never card or handoff text.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

LOCAL_AUDIENCE = "local"
CONNECTOR_AUDIENCE = "connector"
AUDIENCES = frozenset({LOCAL_AUDIENCE, CONNECTOR_AUDIENCE})
# The hub accepts these three. A session-start query from the memory root is
# still a folder-derived query, and a push query is not a person's prompt.
_HUB_QUERY_SOURCE = {"prompt": "prompt", "cwd": "cwd", "workspace": "cwd"}
# A card key is a card id or `scope/card_id`: no spaces, so never free text.
CARD_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,299}")
CAPTURE_LIST_MAX = 50  # the hub's bound on `pushed` and `opened`
CAPTURE_CALL_SECONDS = 2.0
SEARCH_WIDE_MAX = 20  # the hub's `search` limit
# The trust levels the hub's push eligibility admits (search.rs). Fail closed on anything else.
PUSH_TRUST = frozenset({"reviewed", "policy-promoted", "imported"})


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def card_key(match: dict[str, Any]) -> str:
    """`scope/card_id` for a hub or replica hit, the card id for a local card."""
    card_id = _str(match.get("card_id"))
    scope = _str(match.get("scope"))
    if match.get("source") in {"hub", "replica"} and scope:
        return f"{scope}/{card_id}"
    return card_id


def match_ids(match: dict[str, Any]) -> set[str]:
    """Every id a brief or packet could use to name this card."""
    ids = {_str(match.get("card_id")), _str(match.get("publication_id")), card_key(match)}
    aliases = match.get("card_aliases")
    if isinstance(aliases, list):
        ids.update(_str(alias) for alias in aliases)
    path = _str(match.get("path"))
    if path.endswith(".md"):
        ids.add(Path(path).stem)
    ids.discard("")
    return ids


def hub_matches(memory_root: Path, query: str, query_source: str) -> list[dict[str, Any]] | None:
    """Fused hub and local matches, or None when hub memory is off or unusable here.

    None sends the caller to plain local recall. A hub that is down is not None:
    ``search_payload`` answers from local cards and the replica, silently here.
    """
    from . import memory_hub_search

    try:
        if not memory_hub_search.enabled(memory_root):
            return None
        payload = memory_hub_search.search_payload(
            memory_root,
            query,
            # Always the hub's full page: the trust filter below must not run on a cut list.
            limit=SEARCH_WIDE_MAX,
            query_source=_HUB_QUERY_SOURCE.get(query_source, "explicit"),
        )
    except Exception:  # noqa: BLE001 - recall falls back to local cards
        return None
    matches: list[dict[str, Any]] = []
    for item in payload.get("matches") or []:
        if not isinstance(item, dict):
            continue
        source = _str(item.get("source")) or "local"
        if source != "local" and item.get("trust") not in PUSH_TRUST:
            continue
        tags_raw = item.get("tags")
        aliases_raw = item.get("card_aliases")
        match = {
            "path": _str(item.get("path")),
            "card_id": _str(item.get("card_id")),
            "card_aliases": [str(a) for a in aliases_raw] if isinstance(aliases_raw, list) else [],
            "publication_id": _str(item.get("publication_id")),
            "title": _str(item.get("title")),
            "tags": [str(t) for t in tags_raw] if isinstance(tags_raw, list) else [],
            "score": item.get("score"),
            "source": source,
            "scope": _str(item.get("scope")),
        }
        if source != "local":
            # A hub card has no file here: name it by its key and source.
            match["path"] = f"{source}:{card_key(match)}"
        matches.append(match)
    return matches


def without_ids(matches: list[dict[str, Any]], exclude: Iterable[str]) -> list[dict[str, Any]]:
    """Drop matches a brief or dispatch packet already named."""
    excluded = {value for value in exclude if isinstance(value, str) and value}
    if not excluded:
        return matches
    return [match for match in matches if not match_ids(match) & excluded]


def _placeholder_ref(match: dict[str, Any]) -> str:
    """The card key when it is a plain key that scans clean, else a neutral word."""
    from . import memory_content_scan as scanner

    key = card_key(match)
    if CARD_KEY_RE.fullmatch(key) and scanner.scan_verdict([key])[0] == scanner.CLEAN:
        return key
    return "card"


def scan_matches(matches: list[dict[str, Any]], audience: str) -> list[dict[str, Any]]:
    """Scan the exact line each match renders to. Raises when the scanner does (fail closed)."""
    from . import memory_content_scan as scanner
    from .memory_hooks import format_recall_match_line

    out: list[dict[str, Any]] = []
    for match in matches:
        line = format_recall_match_line(match)
        if audience == LOCAL_AUDIENCE:
            verdict, rules = scanner.scan_verdict([line])
            refusal = {"blocked": rules[0]} if verdict in {scanner.BLOCKED, scanner.FLAGGED} else None
        else:
            refusal = scanner.egress_refusal([line])
        if refusal is None:
            out.append(match)
            continue
        out.append({"card_id": _placeholder_ref(match), **refusal})
    return out


def _capture_worker_main() -> None:
    """Child entry: stdin request JSON, stdout ``{"status": ..., "code": ...}``. Ids only, both ways."""
    from . import memory_hub_client as hubc

    req = json.loads(sys.stdin.read())
    if not isinstance(req, dict):
        raise TypeError("capture worker stdin must be a JSON object")

    def keys(name: str) -> list[str]:
        raw = req.get(name)
        values = raw if isinstance(raw, list) else []
        return [v for v in values if isinstance(v, str) and CARD_KEY_RE.fullmatch(v)][:CAPTURE_LIST_MAX]

    result: dict[str, Any]
    try:
        settings = hubc.load_settings(Path(str(req["target"])))
        if not settings.enabled:
            result = {"status": "off"}
        else:
            hubc.client_for(settings).call(
                "capture",
                timeout=float(req.get("timeout") or CAPTURE_CALL_SECONDS),
                session_ref=str(req["session_ref"]),
                pushed=keys("pushed"),
                opened=keys("opened"),
                client_event_id=str(req["client_event_id"]),
            )
            result = {"status": "recorded"}
    except hubc.HubUnavailable as exc:
        result = {"status": "unavailable", "code": exc.reason}
    except hubc.HubRefusal as exc:
        result = {"status": "refused", "code": exc.code or f"http-{exc.status}"}
    except (hubc.HubConfigError, OSError, ValueError):
        result = {"status": "refused", "code": "hub-config-invalid"}
    sys.stdout.write(json.dumps(result, sort_keys=True))
