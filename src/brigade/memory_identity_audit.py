"""Read-only identity coverage audit for canonical memory cards.

This module intentionally reads only the Markdown authority.  It does not
call MiseLedger, edit frontmatter, write aliases, or turn legacy identity into
a validation failure.  Its deterministic proposed aliases are review inputs
for the later migration slice.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from .card_identity import valid_card_id
from .memory_cmd import _parse_frontmatter


_CARD_ROOT = Path("memory/cards")
_NAMESPACE_PATH = Path("memory/NAMESPACE")


def _namespace(workspace: Path) -> str | None:
    try:
        value = (workspace / _NAMESPACE_PATH).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def _engine_string_field(meta: dict[str, Any], *keys: str) -> str:
    """Mirror the engine's ``stringField`` coercion for card identity."""

    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return ", ".join(value)
    return ""


def _identity_warning(identity: str) -> str | None:
    if valid_card_id(identity):
        return None
    return "explicit ID does not meet the future card-<uuid> policy"


def _proposed_id(namespace: str, path: str) -> str:
    return "card-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"brigade://memory/{namespace}/{path}"))


def _card_paths(workspace: Path) -> list[Path]:
    root = workspace / _CARD_ROOT
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file() and not path.is_symlink())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _persisted_legacy_keys(workspace: Path, namespace: str | None) -> list[dict[str, str]]:
    """Report known legacy key stores without interpreting their card content."""

    base = {"workspace": workspace.name, "namespace": namespace or ""}
    keys: list[dict[str, str]] = []
    search_log = workspace / ".brigade/memory/search-log.jsonl"
    try:
        search_lines = search_log.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        search_lines = []
    for line in search_lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("card_ids"), list):
            continue
        for key in entry["card_ids"]:
            if isinstance(key, str) and key:
                keys.append({**base, "consumer": "search", "kind": "search_log_card_id", "key": key})

    care = _read_json(workspace / ".brigade/memory-care/decay/scan-latest.json")
    if care and isinstance(care.get("cards"), list):
        for card in care["cards"]:
            if isinstance(card, dict) and isinstance(card.get("card_id"), str) and card["card_id"]:
                keys.append({**base, "consumer": "care", "kind": "care_card_id", "key": card["card_id"]})

    evaluation = _read_json(workspace / "evals/memory-retrieval/queries.json")
    if evaluation and isinstance(evaluation.get("queries"), list):
        for query in evaluation["queries"]:
            if not isinstance(query, dict) or not isinstance(query.get("gold"), list):
                continue
            for key in query["gold"]:
                if isinstance(key, str) and key:
                    keys.append({**base, "consumer": "eval", "kind": "eval_gold_card_id", "key": key})
    return keys


def audit_workspaces(workspaces: list[Path]) -> dict[str, Any]:
    """Inspect memory identity coverage without modifying the supplied workspaces.

    IDs are unique within the engine's namespace.  Reused explicit IDs across
    namespaces are not archive collisions, but remain migration collisions
    because a future alias table cannot safely treat them as globally unique.
    """

    cards: list[dict[str, Any]] = []
    malformed_ids: list[dict[str, str]] = []
    legacy_keys: list[dict[str, str]] = []
    mappings: list[dict[str, str]] = []
    namespaces: list[dict[str, str | None]] = []
    by_namespace: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    explicit_across_namespaces: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    for raw_workspace in workspaces:
        workspace = raw_workspace.expanduser().resolve()
        namespace = _namespace(workspace)
        namespaces.append({"workspace": workspace.name, "namespace": namespace})
        for path in _card_paths(workspace):
            relative = path.relative_to(workspace).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                malformed_ids.append(
                    {
                        "workspace": workspace.name,
                        "namespace": namespace or "",
                        "path": relative,
                        "detail": "card unreadable",
                    }
                )
                continue
            meta, _has_frontmatter = _parse_frontmatter(text)
            explicit_id = _engine_string_field(meta, "id", "card_id")
            warning = _identity_warning(explicit_id) if explicit_id else None
            card: dict[str, str] = {
                "workspace": workspace.name,
                "namespace": namespace or "",
                "path": relative,
                "identity_source": "explicit_id" if explicit_id else "path_fallback",
                "identity": explicit_id or f"path:{relative}",
            }
            if warning:
                malformed_ids.append(
                    {"workspace": workspace.name, "namespace": namespace or "", "path": relative, "detail": warning}
                )
            cards.append(card)

            legacy_keys.append(
                {
                    "workspace": workspace.name,
                    "namespace": namespace or "",
                    "path": relative,
                    "consumer": "search_eval",
                    "kind": "filename_stem",
                    "key": path.stem,
                }
            )
            topic = meta.get("topic")
            if isinstance(topic, str) and topic.strip():
                legacy_keys.append(
                    {
                        "workspace": workspace.name,
                        "namespace": namespace or "",
                        "path": relative,
                        "consumer": "care",
                        "kind": "topic",
                        "key": topic.strip(),
                    }
                )

            if namespace:
                by_namespace[namespace][card["identity"]].append(relative)
                if explicit_id:
                    explicit_across_namespaces[explicit_id][namespace].append(relative)
                else:
                    mappings.append(
                        {
                            "namespace": namespace,
                            "old_id": f"path:{relative}",
                            "new_id": _proposed_id(namespace, relative),
                            "path": relative,
                        }
                    )

        legacy_keys.extend(_persisted_legacy_keys(workspace, namespace))

    collisions: list[dict[str, Any]] = []
    within_count = 0
    for namespace, ids in sorted(by_namespace.items()):
        for identity, paths in sorted(ids.items()):
            if len(paths) > 1:
                within_count += 1
                collisions.append(
                    {"scope": "namespace", "namespace": namespace, "id": identity, "paths": sorted(paths)}
                )

    across_count = 0
    for identity, namespace_paths in sorted(explicit_across_namespaces.items()):
        if len(namespace_paths) > 1:
            across_count += 1
            collisions.append(
                {
                    "scope": "across_namespaces",
                    "id": identity,
                    "namespaces": [
                        {"namespace": namespace, "paths": sorted(paths)}
                        for namespace, paths in sorted(namespace_paths.items())
                    ],
                }
            )

    explicit_count = sum(card["identity_source"] == "explicit_id" for card in cards)
    fallback_count = sum(card["identity_source"] == "path_fallback" for card in cards)
    blocking_reasons: list[str] = []
    if malformed_ids:
        blocking_reasons.append("malformed_ids")
    if within_count:
        blocking_reasons.append("duplicate_ids_within_namespace")
    if across_count:
        blocking_reasons.append("duplicate_ids_across_namespaces")
    if any(not item["namespace"] for item in namespaces):
        blocking_reasons.append("missing_namespace")

    summary = {
        "workspaces": len(workspaces),
        "cards": len(cards),
        "explicit_ids": explicit_count,
        "path_fallbacks": fallback_count,
        "malformed_ids": len(malformed_ids),
        "duplicate_ids_within_namespace": within_count,
        "duplicate_ids_across_namespaces": across_count,
        "legacy_keys": len(legacy_keys),
        "collisions": len(collisions),
    }
    return {
        "schema": "brigade.memory-identity-audit.v1",
        "summary": summary,
        "namespaces": namespaces,
        "cards": cards,
        "malformed_ids": malformed_ids,
        "collisions": collisions,
        "legacy_keys": legacy_keys,
        "mappings": mappings,
        "alias_readiness": {"ready": not blocking_reasons, "blocking_reasons": blocking_reasons},
    }
