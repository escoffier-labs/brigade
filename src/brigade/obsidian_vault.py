"""One-way, transaction-backed projection of canonical memory into Obsidian."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_cmd, memory_operations
from .card_identity import card_identity
from .guard import redact_text
from .projection import kernel

PROJECTOR = "obsidian-vault"
PROJECTION_FOLDER = "Brigade Memory"
LEDGER_DIR = ".brigade/memory-vault"
LEDGER_SCHEMA = "brigade.obsidian-vault.ledger.v1"
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")


def project(*, target: Path, vault: Path, json_output: bool = False) -> int:
    """Project canonical memory to an operator-selected vault without reading it as content."""
    target = target.expanduser().resolve()
    vault = vault.expanduser().resolve()
    if not target.is_dir():
        return _error(f"--target is not a directory: {target}")
    if not vault.is_dir():
        return _error(f"--vault is not a directory: {vault}")

    payload = _project_payload(target=target, vault=vault)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "memory vault projection: "
            f"written={payload['written']} unchanged={payload['unchanged']} drift={len(payload['drift'])}"
        )
    return 0 if payload["receipt"]["terminal_state"] in {"committed", "unchanged"} else 1


def status_payload(target: Path) -> dict[str, Any] | None:
    """Return public, untrusted-data-safe status for the topology contract."""
    target = target.expanduser().resolve()
    ledgers = _ledgers(target)
    if not ledgers:
        return None
    latest_path, ledger = max(ledgers, key=lambda item: item[0].stat().st_mtime_ns)
    root_text = ledger.get("projection_root")
    root = Path(root_text) if isinstance(root_text, str) else None
    drift = _drift(root, _ledger_files(ledger)) if root is not None else ["ledger-invalid"]
    receipt_value = ledger.get("receipt")
    receipt: dict[str, Any] = receipt_value if isinstance(receipt_value, dict) else {}
    return {
        "status": "warn" if drift else "ok",
        "owner": "brigade",
        "path": "redacted:operator-vault",
        "drift_count": len(drift),
        "latest_run": _public_receipt(receipt),
        "ledger": latest_path.name,
    }


def _project_payload(*, target: Path, vault: Path) -> dict[str, Any]:
    root = vault / PROJECTION_FOLDER
    ledger_path = _ledger_path(target, vault)
    ledger = _read_ledger(ledger_path)
    prior_files = _ledger_files(ledger)
    desired = _render_projection(target, root=root, prior_files=prior_files)
    drift = _drift(root, prior_files)
    mutations: list[kernel.MutationSpec] = []
    actual_files: dict[str, str] = dict(prior_files)

    for relative, content in desired.items():
        current = root / relative
        expected = prior_files.get(relative)
        live = _digest(current)
        desired_digest = kernel.content_digest(content)
        if expected is None and live != kernel.ABSENT:
            conflict = _conflict_destination(relative, root=root, prior_files=prior_files)
            current = root / conflict
            relative = conflict
            live = _digest(current)
            expected = prior_files.get(relative)
        if expected is not None and live != expected:
            # A vault edit is untrusted data. Preserve it rather than merging or overwriting it.
            drift.append(relative)
            conflict = _conflict_destination(relative, root=root, prior_files=prior_files)
            current = root / conflict
            relative = conflict
            live = _digest(current)
            expected = prior_files.get(relative)
        if expected == desired_digest and live == desired_digest:
            actual_files[relative] = desired_digest
            continue
        if expected is None:
            expected = live
        if live != expected:
            # A collision on a conflict copy is also untrusted. Leave it and report the drift.
            drift.append(relative)
            continue
        mutations.append(
            kernel.mutation(
                destination=current,
                mutation="create" if live == kernel.ABSENT else "replace",
                expected_before=live,
                desired_after=desired_digest,
                staged_bytes=content,
                display_path=f"vault/{PROJECTION_FOLDER}/{relative}",
            )
        )
        actual_files[relative] = desired_digest

    desired_paths = set(desired)
    for relative, expected in prior_files.items():
        if relative in desired_paths or relative in drift or ".conflict-" in relative:
            continue
        current = root / relative
        if _digest(current) != expected:
            drift.append(relative)
            continue
        mutations.append(
            kernel.mutation(
                destination=current,
                mutation="remove",
                expected_before=expected,
                desired_after=kernel.ABSENT,
                display_path=f"vault/{PROJECTION_FOLDER}/{relative}",
            )
        )
        actual_files.pop(relative, None)

    drift = sorted(set(drift))
    unchanged = sum(
        1 for relative, content in desired.items() if prior_files.get(relative) == kernel.content_digest(content)
    )
    if not mutations:
        return {
            "schema": "brigade.obsidian-vault.receipt.v1",
            "written": 0,
            "unchanged": unchanged,
            "drift": drift,
            "receipt": {
                "terminal_state": "unchanged",
                "mutation_counts": {"total": 0, "create": 0, "replace": 0, "remove": 0},
            },
        }

    source_digest = _sha256(json.dumps({key: value.decode("utf-8") for key, value in desired.items()}, sort_keys=True))
    operation_id = f"obsidian-vault-{source_digest[:16]}-{uuid.uuid4().hex[:12]}"
    ledger_receipt = {
        "operation_id": operation_id,
        "terminal_state": "committed",
        "completed_at": _utc_now(),
    }
    receipt_seed = {
        "schema": LEDGER_SCHEMA,
        "projection_root": str(root),
        "files": actual_files,
        "receipt": ledger_receipt,
    }
    # The ledger shares the same transaction as every vault note, map, and canvas.
    receipt_bytes = json.dumps(receipt_seed, indent=2, sort_keys=True).encode() + b"\n"
    ledger_live = _digest(ledger_path)
    mutations.append(
        kernel.mutation(
            destination=ledger_path,
            mutation="create" if ledger_live == kernel.ABSENT else "replace",
            expected_before=ledger_live,
            desired_after=kernel.content_digest(receipt_bytes),
            staged_bytes=receipt_bytes,
            display_path=".brigade/memory-vault/ledger.json",
        )
    )
    try:
        plan = kernel.build_plan(
            operation_id=operation_id,
            projector=PROJECTOR,
            source_fingerprint=source_digest,
            mutations=mutations,
            target=target,
        )
        receipt = kernel.execute(plan, target=target).to_dict()
    except kernel.ProjectionError as exc:
        return {
            "schema": "brigade.obsidian-vault.receipt.v1",
            "written": 0,
            "unchanged": unchanged,
            "drift": drift,
            "receipt": {"terminal_state": "failed", "diagnostic": exc.diagnostic, "mutation_counts": {"total": 0}},
        }
    if receipt["terminal_state"] == "committed":
        receipt["completed_at"] = ledger_receipt["completed_at"]
    return {
        "schema": "brigade.obsidian-vault.receipt.v1",
        "written": sum(1 for item in receipt["destinations"] if item.get("mutation") != "remove"),
        "unchanged": unchanged,
        "drift": drift,
        "receipt": receipt,
    }


def _render_projection(target: Path, *, root: Path, prior_files: dict[str, str]) -> dict[str, bytes]:
    inventory = _inventory(target)
    records = [_record(target, item) for item in inventory]
    _assign_paths(records)
    _assign_link_paths(records, root=root, prior_files=prior_files)
    links = _relationships(records)
    rendered: dict[str, bytes] = {}
    for record in records:
        rendered[record["path"]] = _render_note(record, links[record["id"]])
    for category, members in _groups(records, "category").items():
        rendered[f"Maps/Categories/{_safe_name(category)}.md"] = _render_map("category", category, members)
    for harness, members in _groups(records, "source_harness").items():
        rendered[f"Maps/Harnesses/{_safe_name(harness)}.md"] = _render_map("harness", harness, members)
    rendered["Brigade Memory.canvas"] = _render_canvas(target, records, links)
    return rendered


def _inventory(target: Path) -> list[dict[str, Any]]:
    offset = 0
    items: list[dict[str, Any]] = []
    while True:
        page = memory_operations.inventory_payload(target, offset=offset, limit=memory_operations.INVENTORY_MAX_LIMIT)
        batch = page["items"]
        items.extend(item for item in batch if isinstance(item, dict))
        if not page["pagination"]["has_more"]:
            return items
        offset = int(page["pagination"]["next_offset"])


def _record(target: Path, item: dict[str, Any]) -> dict[str, Any]:
    rel = str(item["canonical_path"])
    path = target / rel
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, _ = memory_cmd._parse_frontmatter(text)
    stable_id = card_identity(frontmatter, rel).card_id if item.get("store_type") == "card" else str(item["id"])
    return {
        "id": stable_id,
        "item": item,
        "title": str(item["title"]),
        "body": _without_frontmatter(text),
        "references": _references(frontmatter),
    }


def _assign_paths(records: list[dict[str, Any]]) -> None:
    used: set[str] = set()
    folders = {"card": "Cards", "rule": "Rules", "tools": "Tools", "user": "User", "learning": "Learnings"}
    for record in sorted(records, key=lambda item: (str(item["item"]["store_type"]), item["title"], item["id"])):
        folder = folders[str(record["item"]["store_type"])]
        base = _safe_name(record["title"])
        candidate = f"{folder}/{base}.md"
        if candidate.lower() in used:
            candidate = f"{folder}/{base}-{_sha256(record['id'])[:8]}.md"
        used.add(candidate.lower())
        record["path"] = candidate


def _assign_link_paths(records: list[dict[str, Any]], *, root: Path, prior_files: dict[str, str]) -> None:
    """Link to a generated conflict copy when the canonical note's normal path drifted."""
    for record in records:
        path = str(record["path"])
        expected = prior_files.get(path)
        live = _digest(root / path)
        if (expected is None and live != kernel.ABSENT) or (expected is not None and live != expected):
            record["link_path"] = _conflict_destination(path, root=root, prior_files=prior_files)
        else:
            record["link_path"] = path


def _relationships(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_source = {str(record["item"]["canonical_path"]): record for record in records}
    result: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        related: dict[str, dict[str, Any]] = {}
        item = record["item"]
        tags = {str(tag).lower() for tag in item.get("tags") or []}
        category = str(item.get("category") or "")
        for other in records:
            if other is record:
                continue
            other_item = other["item"]
            if category and category == str(other_item.get("category") or ""):
                related[other["id"]] = other
                continue
            if tags & {str(tag).lower() for tag in other_item.get("tags") or []}:
                related[other["id"]] = other
        for reference in record["references"]:
            referenced = by_source.get(reference)
            if referenced is not None and referenced is not record:
                related[referenced["id"]] = referenced
        result[record["id"]] = sorted(related.values(), key=lambda item: (item["title"], item["id"]))
    return result


def _render_note(record: dict[str, Any], related: list[dict[str, Any]]) -> bytes:
    item = record["item"]
    fresh_until = item.get("fresh_until")
    care_state = str(item.get("freshness") or "missing")
    tags = [str(tag) for tag in item.get("tags") or []]
    tags.extend([f"care/{care_state}", f"harness/{item.get('source_harness') or 'unknown'}"])
    if fresh_until:
        tags.append(f"freshness/{fresh_until}")
    tags = list(dict.fromkeys(_redacted_scalar(tag) for tag in tags if _redacted_scalar(tag)))
    frontmatter = [
        "---",
        "generated: true",
        f"canonical_id: {_yaml(record['id'])}",
        f"canonical_path: {_yaml(item['canonical_path'])}",
        f"title: {_yaml(record['title'])}",
        f"store_type: {_yaml(item['store_type'])}",
        f"category: {_yaml(item.get('category') or 'uncategorized')}",
        f"care_state: {_yaml(care_state)}",
        f"freshness: {_yaml(care_state)}",
        f"freshness_date: {_yaml(fresh_until) if fresh_until else 'null'}",
        f"review_state: {_yaml(item.get('review_state') or 'missing')}",
        f"source_harness: {_yaml(item.get('source_harness') or 'unknown')}",
        "tags:",
        *[f"  - {_yaml(tag)}" for tag in tags],
        "---",
        "",
        f"# {_redacted_scalar(record['title'])}",
        "",
        _redact(record["body"]),
    ]
    if related:
        frontmatter.extend(["", "## Related", "", *[f"- {_wikilink(other)}" for other in related]])
    return ("\n".join(frontmatter).rstrip() + "\n").encode()


def _render_map(kind: str, name: str, members: list[dict[str, Any]]) -> bytes:
    lines = [
        "---",
        "generated: true",
        f"map_kind: {_yaml(kind)}",
        f"map_value: {_yaml(name)}",
        "---",
        "",
        f"# {kind.title()}: {_redacted_scalar(name)}",
        "",
        *[f"- {_wikilink(member)}" for member in members],
        "",
    ]
    return "\n".join(lines).encode()


def _render_canvas(target: Path, records: list[dict[str, Any]], links: dict[str, list[dict[str, Any]]]) -> bytes:
    topology = memory_operations.topology_payload(target)
    records_sorted = sorted(records, key=lambda item: item["id"])
    nodes = [
        {
            "id": f"card-{_sha256(record['id'])[:16]}",
            "type": "text",
            "text": _redacted_scalar(record["title"]),
            "x": index * 260,
            "y": 0,
            "width": 220,
            "height": 80,
        }
        for index, record in enumerate(records_sorted)
    ]
    node_ids = {record["id"]: node["id"] for record, node in zip(records_sorted, nodes, strict=True)}
    edges = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        for other in links[record["id"]]:
            pair = tuple(sorted((record["id"], other["id"])))
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(
                {
                    "id": f"edge-{_sha256(':'.join(pair))[:16]}",
                    "fromNode": node_ids[pair[0]],
                    "toNode": node_ids[pair[1]],
                }
            )
    # The canvas is intentionally structural. Volatile receipts and this projection's
    # own derived node are omitted so a no-change rerun has no canvas mutation.
    topology_nodes = [
        node
        for node in topology["nodes"]
        if isinstance(node, dict) and str(node.get("id") or "") != "derived:obsidian_vault"
    ]
    topology_ids: dict[str, str] = {}
    for index, node in enumerate(sorted(topology_nodes, key=lambda item: str(item["id"]))):
        original_id = str(node["id"])
        canvas_id = f"topology:{original_id}"
        topology_ids[original_id] = canvas_id
        nodes.append(
            {
                "id": canvas_id,
                "type": "text",
                "text": _redacted_scalar(node.get("label") or original_id),
                "x": index * 260,
                "y": 180,
                "width": 220,
                "height": 80,
            }
        )
    for edge in sorted(topology["edges"], key=lambda item: str(item.get("id") or "")):
        if not isinstance(edge, dict):
            continue
        frm = topology_ids.get(str(edge.get("from") or ""))
        to = topology_ids.get(str(edge.get("to") or ""))
        if frm is not None and to is not None:
            edges.append({"id": f"topology:{edge['id']}", "fromNode": frm, "toNode": to})
    return (
        json.dumps({"brigade_generated": True, "nodes": nodes, "edges": edges}, indent=2, sort_keys=True) + "\n"
    ).encode()


def _groups(records: list[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        value = str(record["item"].get(field) or "uncategorized")
        groups.setdefault(value, []).append(record)
    return {key: sorted(value, key=lambda item: (item["title"], item["id"])) for key, value in sorted(groups.items())}


def _wikilink(record: dict[str, Any]) -> str:
    path = str(record.get("link_path") or record["path"])
    target = path.removesuffix(".md")
    return f"[[{_redacted_scalar(target)}|{_redacted_scalar(record['title'])}]]"


def _references(frontmatter: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("references", "refs", "related", "links"):
        value = frontmatter.get(key)
        values.extend(value if isinstance(value, list) else [value])
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip().replace("\\", "/")
        if candidate.endswith(".md") and not candidate.startswith(("/", "~", "..")) and candidate not in found:
            found.append(candidate)
    return found


def _without_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return text


def _ledgers(target: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = target / LEDGER_DIR
    if not root.is_dir():
        return []
    return [(path, payload) for path in root.glob("*.json") if (payload := _read_ledger(path))]


def _ledger_path(target: Path, vault: Path) -> Path:
    return target / LEDGER_DIR / f"{_sha256(str(vault))[:24]}.json"


def _read_ledger(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) and payload.get("schema") == LEDGER_SCHEMA else {}


def _ledger_files(ledger: dict[str, Any]) -> dict[str, str]:
    files = ledger.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        str(path): str(digest) for path, digest in files.items() if isinstance(path, str) and isinstance(digest, str)
    }


def _drift(root: Path | None, expected: dict[str, str]) -> list[str]:
    if root is None:
        return ["ledger-invalid"]
    drift = [relative for relative, digest in expected.items() if _digest(root / relative) != digest]
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and path.relative_to(root).as_posix() not in expected:
                drift.append(f"untracked:{path.relative_to(root).as_posix()}")
    return sorted(set(drift))


def _digest(path: Path) -> str:
    try:
        return kernel.content_digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else kernel.ABSENT
    except OSError:
        return kernel.ABSENT


def _conflict_destination(relative: str, *, root: Path, prior_files: dict[str, str]) -> str:
    """Allocate a generated conflict path without treating vault contents as trusted."""
    attempt = 1
    while True:
        candidate = _conflict_path(relative, _sha256(relative), attempt=attempt)
        expected = prior_files.get(candidate)
        live = _digest(root / candidate)
        if (expected is None and live == kernel.ABSENT) or (expected is not None and live == expected):
            return candidate
        attempt += 1


def _conflict_path(relative: str, digest: str, *, attempt: int = 1) -> str:
    path = Path(relative)
    suffix = digest[:8] if attempt == 1 else f"{digest[:8]}-{attempt}"
    return (path.parent / f"{path.stem}.conflict-{suffix}{path.suffix}").as_posix()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", _redacted_scalar(value)).strip(" .")
    return cleaned or "untitled"


def _redacted_scalar(value: object) -> str:
    return _redact(str(value)).replace("\r", " ").replace("\n", " ").strip()


def _redact(value: str) -> str:
    """Apply the content guard and the bounded bearer-token rule used by projections."""
    return _BEARER_RE.sub(r"\1 [redacted]", redact_text(value))


def _yaml(value: object) -> str:
    text = _redacted_scalar(value)
    return text if re.fullmatch(r"[A-Za-z0-9_./-]+", text) else json.dumps(text)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_receipt(receipt: dict[str, Any]) -> dict[str, Any] | None:
    if not receipt:
        return None
    return {
        "run_id": receipt.get("operation_id"),
        "status": receipt.get("terminal_state"),
        "completed_at": receipt.get("completed_at"),
    }


def _error(message: str) -> int:
    import sys

    print(f"error: {message}", file=sys.stderr)
    return 2
