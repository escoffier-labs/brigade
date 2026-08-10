"""Bounded session-start memory recall primitive (#466 Slice 1).

Derives cwd-based search terms, runs the deterministic card search against a
machine-local hub or mirror, and formats index-level output (title, tags, path)
suitable for harness SessionStart injection. Failures stay fail-open.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_RECALL_LIMIT = 5
RECALL_MAX_LINES = 10
RECALL_TIMEOUT_SECONDS = 5
GENERIC_WORKSPACE_QUERY = "workspace"
_TERM_SPLIT = re.compile(r"[-_]+")
_RECALL_WORKER_COMMAND = "from brigade.memory_hooks import _recall_worker_main; _recall_worker_main()"


def split_cwd_terms(basename: str) -> list[str]:
    """Split a directory basename on hyphens and underscores into query terms."""
    return [part.lower() for part in _TERM_SPLIT.split(basename.strip()) if part]


def query_from_cwd(cwd: Path, *, memory_root: Path | None = None) -> str:
    """Build the recall query from a session cwd.

    When the session starts at the configured memory root, use the generic
    workspace fallback instead of the hub directory's basename.
    """
    try:
        cwd_resolved = cwd.expanduser().resolve(strict=False)
    except OSError:
        return GENERIC_WORKSPACE_QUERY
    if memory_root is not None:
        try:
            root_resolved = memory_root.expanduser().resolve(strict=False)
        except OSError:
            root_resolved = None
        if root_resolved is not None and cwd_resolved == root_resolved:
            return GENERIC_WORKSPACE_QUERY
    terms = split_cwd_terms(cwd_resolved.name)
    return " ".join(terms) if terms else GENERIC_WORKSPACE_QUERY


def resolve_memory_recall_target(wired_target: Path) -> tuple[Path | None, str]:
    """Resolve the machine-local recall hub/mirror for a wired Brigade target.

    Returns ``(path, status)`` where status is ``active`` or ``unconfigured``.
    Workspace-depth installs may default to the current target when the config
    key is absent. Repo-depth installs stay unconfigured until an explicit hub
    or mirror path is set.
    """
    from .config import load_config, validate_memory_recall_target

    try:
        target = wired_target.expanduser().resolve(strict=False)
    except OSError:
        return None, "unconfigured"
    try:
        cfg = load_config(target)
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "unconfigured"
    if cfg is None:
        return None, "unconfigured"
    configured = validate_memory_recall_target(cfg.memory_recall_target)
    if configured:
        try:
            return Path(configured).expanduser().resolve(strict=False), "active"
        except OSError:
            return None, "unconfigured"
    if cfg.selection.depth == "workspace":
        return target, "active"
    return None, "unconfigured"


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, DEFAULT_RECALL_LIMIT)


def _format_tags(tags: list[str]) -> str:
    return ", ".join(tags) if tags else "-"


def format_recall_match_line(match: dict[str, Any]) -> str:
    title = str(match.get("title") or "")
    path = str(match.get("path") or "")
    raw_tags = match.get("tags")
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    return f"- {title} | tags: {_format_tags(tags)} | {path}"


def format_recall_text(payload: dict[str, Any]) -> str:
    """Render recall output: at most 5 matches and 10 lines; no bodies/summaries."""
    query = str(payload.get("query") or "")
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return ""
    lines = [f"memory recall: {query}"]
    for match in matches[:DEFAULT_RECALL_LIMIT]:
        if not isinstance(match, dict):
            continue
        lines.append(format_recall_match_line(match))
        if len(lines) >= RECALL_MAX_LINES:
            break
    return "\n".join(lines[:RECALL_MAX_LINES])


def _empty_recall_payload(
    *,
    target: Path,
    cwd: Path,
    query: str,
    limit: int,
    status: str,
) -> dict[str, Any]:
    return {
        "target": str(target),
        "cwd": str(cwd),
        "query": query,
        "match_count": 0,
        "matches": [],
        "limit": limit,
        "status": status,
    }


def _recall_cards_payload_impl(
    *,
    target: Path,
    cwd: Path,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> dict[str, Any]:
    """In-process recall body (runs in a killable child when timed)."""
    from .memory_cmd import search_cards_payload

    capped = _clamp_limit(limit)
    try:
        memory_root = target.expanduser().resolve(strict=False)
    except OSError:
        memory_root = target
    query = query_from_cwd(cwd, memory_root=memory_root)
    empty = _empty_recall_payload(
        target=target,
        cwd=cwd,
        query=query,
        limit=capped,
        status="ok",
    )
    try:
        raw = search_cards_payload(memory_root, query, limit=capped)
    except (OSError, ValueError):
        empty["status"] = "error"
        return empty
    matches: list[dict[str, Any]] = []
    for item in raw.get("matches") or []:
        if not isinstance(item, dict):
            continue
        tags_raw = item.get("tags")
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        matches.append(
            {
                "path": str(item.get("path") or ""),
                "title": str(item.get("title") or ""),
                "tags": tags,
                "score": item.get("score"),
            }
        )
    return {
        "target": str(raw.get("target") or memory_root),
        "cwd": str(cwd),
        "query": query,
        "match_count": len(matches),
        "matches": matches,
        "limit": capped,
        "status": "ok",
    }


def _recall_worker_main() -> None:
    """Child entry for timed recall; stdin request JSON, stdout payload JSON."""
    req = json.loads(sys.stdin.read())
    if not isinstance(req, dict):
        raise TypeError("recall worker stdin must be a JSON object")
    payload = _recall_cards_payload_impl(
        target=Path(str(req["target"])),
        cwd=Path(str(req["cwd"])),
        limit=int(req["limit"]),
    )
    sys.stdout.write(json.dumps(payload, sort_keys=True))


def recall_cards_payload(
    *,
    target: Path,
    cwd: Path,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> dict[str, Any]:
    """Run bounded recall against ``target`` using terms derived from ``cwd``.

    Card search runs in a killable child process so a stalled scan cannot block
    SessionStart past ``RECALL_TIMEOUT_SECONDS``. Timeout and child failures
    return an empty fail-open payload.
    """
    capped = _clamp_limit(limit)
    try:
        memory_root = target.expanduser().resolve(strict=False)
    except OSError:
        memory_root = target
    query = query_from_cwd(cwd, memory_root=memory_root)
    empty = _empty_recall_payload(
        target=target,
        cwd=cwd,
        query=query,
        limit=capped,
        status="error",
    )
    request = json.dumps(
        {
            "target": str(target),
            "cwd": str(cwd),
            "limit": capped,
        },
        sort_keys=True,
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _RECALL_WORKER_COMMAND],
            input=request,
            capture_output=True,
            text=True,
            timeout=RECALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        empty["status"] = "timeout"
        return empty
    except OSError:
        return empty
    if completed.returncode != 0:
        return empty
    stdout = completed.stdout.strip()
    if not stdout:
        return empty
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return empty
    if not isinstance(parsed, dict):
        return empty
    return parsed


def recall_text_for_hook(
    *,
    wired_target: Path,
    cwd: Path | None = None,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> str:
    """Hook-facing recall: empty string on any failure (fail open, no leak)."""
    try:
        recall_target, status = resolve_memory_recall_target(wired_target)
        if status != "active" or recall_target is None:
            return ""
        session_cwd = cwd if cwd is not None else wired_target
        if not recall_target.exists():
            return ""
        payload = recall_cards_payload(target=recall_target, cwd=session_cwd, limit=limit)
        if payload.get("status") != "ok":
            return ""
        return format_recall_text(payload)
    except Exception:  # noqa: BLE001 - session-start recall must never block the harness
        return ""


def recall(
    *,
    target: Path,
    cwd: Path,
    limit: int = DEFAULT_RECALL_LIMIT,
    json_output: bool = False,
) -> int:
    """CLI entry for ``brigade memory recall``. Always exits 0 (fail open)."""
    try:
        if not target.expanduser().resolve(strict=False).exists():
            payload = {
                "target": str(target),
                "cwd": str(cwd),
                "query": query_from_cwd(cwd, memory_root=None),
                "match_count": 0,
                "matches": [],
                "limit": _clamp_limit(limit),
                "status": "missing-target",
            }
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        payload = recall_cards_payload(target=target, cwd=cwd, limit=limit)
    except Exception:  # noqa: BLE001 - keep the session-start contract fail-open
        if json_output:
            print(
                json.dumps(
                    {
                        "target": str(target),
                        "cwd": str(cwd),
                        "query": "",
                        "match_count": 0,
                        "matches": [],
                        "limit": _clamp_limit(limit),
                        "status": "error",
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return 0
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    text = format_recall_text(payload)
    if text:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Minimal argv entry used by subprocess fixtures."""
    import argparse

    parser = argparse.ArgumentParser(prog="brigade memory recall")
    parser.add_argument("--target", "-t", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_RECALL_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return recall(target=args.target, cwd=args.cwd, limit=args.limit, json_output=args.json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
