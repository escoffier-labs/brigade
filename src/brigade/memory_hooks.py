"""Bounded session-start memory recall primitive (#466 Slice 1).

Derives bounded explicit or checkout search terms, runs card search against a
machine-local hub or mirror, and formats index-level output (title, tags, path)
suitable for harness SessionStart injection. Failures stay fail-open.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_RECALL_LIMIT = 5
RECALL_MAX_LINES = 10
RECALL_TIMEOUT_SECONDS = 5
RECALL_QUERY_MAX_CHARS = 256
_GIT_TIMEOUT_SECONDS = 0.5
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


def _single_line(text: str) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in text).split())


def _normalize_query(query: str) -> str:
    normalized = _single_line(query)
    if len(normalized) <= RECALL_QUERY_MAX_CHARS:
        return normalized
    bounded = normalized[:RECALL_QUERY_MAX_CHARS]
    if normalized[RECALL_QUERY_MAX_CHARS] != " " and " " in bounded:
        bounded = bounded.rsplit(" ", 1)[0]
    return bounded.strip()


def _base_recall_query(cwd: Path, *, memory_root: Path, query: str | None) -> tuple[str, str]:
    """Choose explicit or legacy fallback terms without probing Git in the parent."""
    explicit = _normalize_query(query or "")
    if explicit:
        return explicit, "explicit"
    source = "cwd"
    try:
        if cwd.expanduser().resolve(strict=False) == memory_root.expanduser().resolve(strict=False):
            source = "workspace"
    except OSError:
        pass
    return _normalize_query(query_from_cwd(cwd, memory_root=memory_root)), source


def _query_from_repo(cwd: Path) -> str:
    """Probe only a checkout root, inside the recall worker; return basename terms."""
    try:
        checkout = cwd.expanduser().resolve(strict=False)
        marker = checkout / ".git"
        marker_mode = marker.stat().st_mode
        gitdir = None
        if stat.S_ISREG(marker_mode):
            # Nonblocking open and fstat also reject a marker swapped to a FIFO.
            fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return ""
                raw_marker = stream.read(4097)
            if len(raw_marker) > 4096:
                return ""
            marker_text = raw_marker.decode("utf-8").removesuffix("\n")
            if not marker_text.startswith("gitdir: ") or not marker_text.isprintable():
                return ""
            marker_target = marker_text.removeprefix("gitdir: ")
            if not marker_target.strip():
                return ""
            gitdir = (checkout / marker_target).resolve(strict=False)
        elif not stat.S_ISDIR(marker_mode):
            return ""
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
        )
        completed = subprocess.run(
            ["git", "-C", str(checkout), "--no-replace-objects", "rev-parse", "--show-toplevel", "--git-common-dir"],
            env=env,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            return ""
        records = completed.stdout.decode("utf-8").removesuffix("\n").split("\n")
        if len(records) != 2 or any(not record.strip() or not record.isprintable() for record in records):
            return ""
        toplevel = Path(records[0]).resolve(strict=False)
        common_dir = (checkout / records[1]).resolve(strict=False)
        if toplevel != checkout or common_dir.name != ".git":
            return ""
        if gitdir is None:
            if marker.resolve(strict=False) != common_dir:
                return ""
        elif gitdir.parent != common_dir / "worktrees":
            return ""
        return _normalize_query(" ".join(split_cwd_terms(common_dir.parent.name)))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


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
    title = _single_line(str(match.get("title") or ""))
    path = _single_line(str(match.get("path") or ""))
    raw_tags = match.get("tags")
    tags = [_single_line(str(t)) for t in raw_tags] if isinstance(raw_tags, list) else []
    return f"- {title} | tags: {_format_tags(tags)} | {path}"


def format_recall_text(payload: dict[str, Any]) -> str:
    """Render recall output: at most 5 matches and 10 lines; no bodies/summaries."""
    query = _normalize_query(str(payload.get("query") or ""))
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
    query_source: str,
    limit: int,
    status: str,
) -> dict[str, Any]:
    return {
        "target": str(target),
        "cwd": str(cwd),
        "query": query,
        "query_source": query_source,
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
    query: str | None = None,
) -> dict[str, Any]:
    """In-process recall body (runs in a killable child when timed)."""
    from .memory_cmd import search_cards_payload

    capped = _clamp_limit(limit)
    try:
        memory_root = target.expanduser().resolve(strict=False)
    except OSError:
        memory_root = target
    query, query_source = _base_recall_query(cwd, memory_root=memory_root, query=query)
    if query_source == "cwd":
        repo_query = _query_from_repo(cwd)
        if repo_query:
            query, query_source = repo_query, "repo"
    empty = _empty_recall_payload(
        target=target,
        cwd=cwd,
        query=query,
        query_source=query_source,
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
        aliases_raw = item.get("card_aliases")
        aliases = [str(alias) for alias in aliases_raw] if isinstance(aliases_raw, list) else []
        matches.append(
            {
                "path": str(item.get("path") or ""),
                "card_id": str(item.get("card_id") or ""),
                "card_aliases": aliases,
                "title": str(item.get("title") or ""),
                "tags": tags,
                "score": item.get("score"),
            }
        )
    return {
        "target": str(raw.get("target") or memory_root),
        "cwd": str(cwd),
        "query": query,
        "query_source": query_source,
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
        query=req.get("query"),
    )
    sys.stdout.write(json.dumps(payload, sort_keys=True))


def recall_cards_payload(
    *,
    target: Path,
    cwd: Path,
    limit: int = DEFAULT_RECALL_LIMIT,
    query: str | None = None,
) -> dict[str, Any]:
    """Run bounded recall against ``target`` using explicit or checkout terms.

    Git identity and card search run in a killable child bounded by
    ``RECALL_TIMEOUT_SECONDS``. Earlier target/context resolution is outside
    that deadline. Timeout and child failures return an empty fail-open payload.
    """
    capped = _clamp_limit(limit)
    try:
        memory_root = target.expanduser().resolve(strict=False)
    except OSError:
        memory_root = target
    explicit_query = _normalize_query(query or "")
    query, query_source = _base_recall_query(cwd, memory_root=memory_root, query=explicit_query)
    empty = _empty_recall_payload(
        target=target,
        cwd=cwd,
        query=query,
        query_source=query_source,
        limit=capped,
        status="error",
    )
    request = json.dumps(
        {
            "target": str(target),
            "cwd": str(cwd),
            "limit": capped,
            "query": explicit_query,
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
    query: str | None = None,
) -> str:
    """Hook-facing recall: empty string on any failure (fail open, no leak)."""
    try:
        recall_target, status = resolve_memory_recall_target(wired_target)
        if status != "active" or recall_target is None:
            return ""
        session_cwd = cwd if cwd is not None else wired_target
        if not recall_target.exists():
            return ""
        payload = recall_cards_payload(target=recall_target, cwd=session_cwd, limit=limit, query=query)
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
    query: str | None = None,
) -> int:
    """CLI entry for ``brigade memory recall``. Always exits 0 (fail open)."""
    explicit_query = _normalize_query(query or "")
    empty = _empty_recall_payload(
        target=target,
        cwd=cwd,
        query=explicit_query,
        query_source="explicit" if explicit_query else "cwd",
        limit=_clamp_limit(limit),
        status="error",
    )
    try:
        fallback_query, query_source = _base_recall_query(cwd, memory_root=target, query=explicit_query)
        empty.update(query=fallback_query, query_source=query_source)
        if not target.expanduser().resolve(strict=False).exists():
            if not explicit_query:
                empty.update(query=_normalize_query(query_from_cwd(cwd)), query_source="cwd")
            empty["status"] = "missing-target"
            if json_output:
                print(json.dumps(empty, indent=2, sort_keys=True))
            return 0
        payload = recall_cards_payload(target=target, cwd=cwd, limit=limit, query=explicit_query)
    except Exception:  # noqa: BLE001 - keep the session-start contract fail-open
        if json_output:
            print(json.dumps(empty, indent=2, sort_keys=True))
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
    parser.add_argument("--query")
    args = parser.parse_args(argv)
    return recall(target=args.target, cwd=args.cwd, limit=args.limit, json_output=args.json, query=args.query)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
