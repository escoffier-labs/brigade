"""`brigade memory hub import` (S20): find notes, plan against the hub's heads, send the plan.

One planning function, `build_plan`, serves the dry run and the real run, so a
dry run lists exactly the notes the real run appends. Planning reads the
scope's heads from the hub log, which needs a read grant and nothing else, and
scans each candidate locally with the hub's rules after checking that the hub
runs the same ruleset. Output names rule ids and never the text they matched.

Card ids: a note's frontmatter `id` (then `card_id`) is kept when the hub
accepts it as is. Any other note gets `<slug>-<10 hex>`, where the slug is the
lower-cased ASCII form of its root-relative path without `.md` and the hex is
the start of the SHA-256 of that NFC path, so the same path maps to the same
card on every run. Two notes with one id are a collision and neither imports.

Dedupe uses `hubc.content_hash`, the hub's own `content_hash`: SHA-256 of the
text with LF line endings and exactly one trailing newline.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_content_scan as scanner
from . import memory_hub_client as hubc

ACTIONS = (
    "import",
    "unchanged",
    "duplicate",
    "changed",
    "private",
    "visibility-unrecognized",
    "blocked",
    "flagged",
    "collision",
    "error",
)
MAX_TEXT_BYTES = 512 * 1024  # the hub's card size limit, after normalization
LOG_PAGE = 500
DAY_MS = 24 * 3600 * 1000
TOP_RULES = 5
# Consecutive "hub not serving" answers after which the real run stops.
UNAVAILABLE_LIMIT = 3
# Memory hub heads whose log events replace the previous head, as in a pull.
HEAD_KINDS = {"card.put", "card.import", "card.retire", "card.trust", "proposal.apply", "proposal.rollback"}
_STOP_CODES = {"memory_epoch_changed"}
_SLUG_LIMIT = 100


@dataclass
class Discovery:
    sources: list[tuple[Path, str]] = field(default_factory=list)  # (file, root-relative posix path)
    stats: Counter[str] = field(default_factory=Counter)


@dataclass
class Item:
    path: str
    card_id: str | None
    action: str
    rules: tuple[str, ...] = ()
    reason: str | None = None
    scan: str | None = None
    appends: bool = False
    # Not reported: what the send needs.
    text: str = field(default="", repr=False)
    content_hash: str = ""
    base: dict[str, Any] | None = None
    result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "note", "path": self.path, "card_id": self.card_id, "action": self.action}
        if self.rules:
            out["rules"] = list(self.rules)
        if self.reason:
            out["reason"] = self.reason
        if self.scan:
            out["scan"] = self.scan
        out["appends"] = self.appends
        if self.result is not None:
            out["result"] = self.result
        return out


@dataclass
class Heads:
    epoch: str
    principal: str
    live: dict[str, dict[str, Any]]  # card_id to the log event that is its live head
    retired: dict[str, dict[str, Any]]
    hashes: set[str]
    prior_imports: int


# --- discovery ------------------------------------------------------------------------------


def _excluded(rel: str, patterns: list[str]) -> bool:
    """`*` crosses `/`. A pattern without `/` also matches the file name, and `dir` or `dir/` covers what is under it."""
    name = rel.rpartition("/")[2]
    for pattern in patterns:
        base = pattern.rstrip("/")
        if fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, base) or fnmatch.fnmatchcase(rel, base + "/*"):
            return True
        if "/" not in base and fnmatch.fnmatchcase(name, base):
            return True
    return False


def _mirror_dir(directory: str) -> bool:
    from .memory_hub_mirror import MARKER

    return os.path.lexists(os.path.join(directory, MARKER))


def _relative(file: str, root: str) -> str:
    rel = os.path.relpath(file, root).replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        raise hubc.HubConfigError(f"{file} is outside the import root {root}; widen --root")
    return rel


def discover(paths: list[Path], *, root: Path | None, excludes: list[str]) -> Discovery:
    """Markdown notes under `paths`, with paths relative to `root` (default: each directory argument's own folder).

    Dot directories, folders holding the hub mirror's marker file, symlinks met
    while walking and anything matching `excludes` are skipped and counted.
    """
    found = Discovery()
    seen: set[str] = set()
    fixed = os.path.abspath(root.expanduser()) if root is not None else None

    def add(file: str, base: str) -> None:
        if file in seen:
            return
        seen.add(file)
        found.sources.append((Path(file), _relative(file, fixed or base)))

    for given in paths:
        top = os.path.abspath(given.expanduser())
        if os.path.isdir(top):
            if _mirror_dir(top):
                found.stats["mirror_dirs"] += 1
                continue
            for directory, dirnames, filenames in os.walk(top):
                keep = []
                for name in sorted(dirnames):
                    full = os.path.join(directory, name)
                    rel = _relative(full, fixed or top)
                    if name.startswith("."):
                        found.stats["dot_dirs"] += 1
                    elif os.path.islink(full):
                        found.stats["symlinks"] += 1
                    elif _mirror_dir(full):
                        found.stats["mirror_dirs"] += 1
                    elif _excluded(rel, excludes):
                        found.stats["excluded"] += 1
                    else:
                        keep.append(name)
                dirnames[:] = keep
                for name in sorted(filenames):
                    full = os.path.join(directory, name)
                    if not name.lower().endswith(".md"):
                        continue
                    if os.path.islink(full):
                        found.stats["symlinks"] += 1
                    elif _excluded(_relative(full, fixed or top), excludes):
                        found.stats["excluded"] += 1
                    else:
                        add(full, top)
        else:
            parent = os.path.dirname(top)
            if _mirror_dir(parent):
                found.stats["mirror_dirs"] += 1
            elif _excluded(_relative(top, fixed or parent), excludes):
                found.stats["excluded"] += 1
            else:
                add(top, parent)  # a missing file is reported by the planner, not skipped
    found.sources.sort(key=lambda item: item[1])
    found.stats["notes"] = len(found.sources)
    return found


# --- card ids -------------------------------------------------------------------------------


def derive_card_id(rel: str) -> str:
    """`<slug>-<10 hex>` from the root-relative path: stable, hub-valid and unique per path."""
    path = unicodedata.normalize("NFC", rel)
    stem = path[:-3] if path.lower().endswith(".md") else path
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", ascii_stem).strip("-._")[:_SLUG_LIMIT].rstrip("-._") or "note"
    return f"{slug}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:10]}"


def note_card_id(text: str, rel: str) -> str:
    """The id the note's frontmatter holds when the hub accepts it as is, else the one derived from `rel`."""
    from . import card_identity, memory_cmd

    meta, _ = memory_cmd._parse_frontmatter(text.removeprefix("﻿").replace("\r\n", "\n"))
    for key in ("id", "card_id"):
        value = meta.get(key)
        if isinstance(value, str):
            candidate = value.strip()
            if hubc.valid_card_id(candidate):
                return candidate
            minted = card_identity.valid_card_id(candidate)
            if minted and hubc.valid_card_id(minted):
                return minted
    return derive_card_id(rel)


# --- the hub's heads --------------------------------------------------------------------------


def _local_ruleset_must_match(status: dict[str, Any]) -> None:
    scan = hubc.obj(status.get("scan"))
    hub_rules = scan.get("ruleset_version")
    if not scan.get("available") or not isinstance(hub_rules, str) or not hub_rules:
        raise hubc.HubConfigError(
            "the hub's content scanner is unavailable, so it would refuse every import; try later"
        )
    local = scanner.ruleset_version()
    if hub_rules != local:
        raise hubc.HubConfigError(
            "this client and the hub run different scan rulesets, so a dry run could not predict the import "
            f"(client {local}, hub {hub_rules}); upgrade whichever is older"
        )


def read_heads(client: hubc.MemoryHubClient, scope: str) -> Heads:
    """The scope's heads from the hub log. Needs only a read grant on `scope`; writes nothing."""
    no_read = (
        f"planning an import needs a read grant on {scope} (the hub answered without one): "
        "grant read beside import, or run as a principal that holds both"
    )
    try:
        status = hubc.refuse_admin(client.call("status", scopes=[scope]))
    except hubc.HubRefusal as exc:
        if exc.status == 403 and exc.code == "memory_grant_missing":
            raise hubc.HubConfigError(no_read) from None
        raise
    if scope not in (status.get("readable_scopes") or []):
        raise hubc.HubConfigError(no_read)
    _local_ruleset_must_match(status)
    epoch, principal = str(status.get("log_epoch") or ""), str(status.get("principal") or "")
    latest: dict[str, dict[str, Any]] = {}
    prior, after = 0, 0
    while True:
        page = client.call("log", after_seq=after, limit=LOG_PAGE)
        if str(page.get("log_epoch") or "") != epoch:
            raise hubc.HubRefusal(
                409,
                {
                    "code": "memory_epoch_changed",
                    "error": "the hub's log_epoch changed while its log was read; run again",
                },
            )
        for event in page.get("events") or []:
            if (
                isinstance(event, dict)
                and event.get("scope") == scope
                and event.get("kind") in HEAD_KINDS
                and hubc.valid_card_id(event.get("card_id"))
                and isinstance(event.get("revision"), int)
            ):
                latest[event["card_id"]] = event
                prior += event["kind"] == "card.import"
        following = page.get("next_seq")
        if not isinstance(following, int) or following <= after:
            break
        after = following
    live = {cid: e for cid, e in latest.items() if e["kind"] != "card.retire"}
    retired = {cid: e for cid, e in latest.items() if e["kind"] == "card.retire"}
    hashes = {str(e.get("content_hash")) for e in live.values() if e.get("content_hash")}
    return Heads(epoch, principal, live, retired, hashes, prior)


# --- planning ---------------------------------------------------------------------------------


@dataclass
class _Note:
    rel: str
    card_id: str | None = None
    text: str = ""
    content_hash: str = ""
    item: Item | None = None  # set when the note is settled before dedupe


def _read(path: Path, rel: str) -> _Note:
    from .memory_hub_sync import VISIBILITY_PRIVATE, withheld

    note = _Note(rel)
    try:
        if not path.is_file():
            raise FileNotFoundError
        if path.stat().st_size > MAX_TEXT_BYTES + 1024:
            note.item = Item(rel, None, "error", reason="too-large")
            return note
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        failure = "not-utf8" if isinstance(exc, UnicodeDecodeError) else "unreadable"
        note.item = Item(rel, None, "error", reason=failure)
        return note
    reason = withheld(text)
    if reason is not None:
        action = "private" if reason == VISIBILITY_PRIVATE else "visibility-unrecognized"
        note.item = Item(rel, None, action, reason=reason)
        return note
    normalized = hubc.normalize_text(text)
    if not text.strip():
        note.item = Item(rel, None, "error", reason="empty")
    elif len(normalized.encode("utf-8")) > MAX_TEXT_BYTES:
        note.item = Item(rel, None, "error", reason="too-large")
    else:
        note.card_id, note.text, note.content_hash = note_card_id(text, rel), text, hubc.content_hash(text)
    return note


def build_plan(sources: list[tuple[Path, str]], heads: Heads, *, update: bool) -> list[Item]:
    """One Item per note, in path order. The same call plans the dry run and the real run."""
    notes = [_read(path, rel) for path, rel in sources]
    owners = Counter(note.card_id for note in notes if note.item is None)
    seen: set[str] = set()
    items: list[Item] = []
    for note in notes:
        if note.item is not None:
            items.append(note.item)
            continue
        card_id, digest = str(note.card_id), note.content_hash
        item = Item(note.rel, card_id, "import", content_hash=digest)
        items.append(item)
        head = heads.live.get(card_id)
        if owners[card_id] > 1:
            item.action = "collision"
        elif head is not None and head.get("content_hash") == digest:
            item.action = "unchanged"
            seen.add(digest)
        elif digest in heads.hashes or digest in seen:
            item.action = "duplicate"
        else:
            if head is not None:
                item.action = "changed"
            if head is None or update:
                _scan(item, note.text)
                if item.action in {"import", "changed"}:
                    item.text, item.appends = note.text, True
                    item.base = head or heads.retired.get(card_id)
                    seen.add(digest)
    return items


def _scan(item: Item, text: str) -> None:
    verdict, rules = scanner.scan_verdict([hubc.normalize_text(text)])
    if verdict in {scanner.BLOCKED, scanner.FLAGGED}:
        item.action, item.rules = verdict, rules
    elif verdict == scanner.PRIVATE:
        item.scan, item.rules = "private", rules
    else:
        item.scan = "clean"


def import_event_id(scope: str, item: Item, epoch: str, *, now_ms: int) -> str:
    """The same scope, card, text and base on the same UTC day always give one `client_event_id`.

    The hub replays a repeat of a committed request instead of appending, but
    only inside its 29-day acceptance window, so the time half is the start of
    the UTC day. The 80 random bits come from the request itself, and the base
    is part of it because the hub refuses one id sent with two different bases.
    """
    base = item.base.get("revision") if item.base else None
    seed = "\0".join(["brigade-memory-import.v1", scope, str(item.card_id), item.content_hash, str(base or ""), epoch])
    tail = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:10], "big")
    return hubc.event_id_at(now_ms - now_ms % DAY_MS, tail)


# --- the run ----------------------------------------------------------------------------------


def _send(client: hubc.MemoryHubClient, scope: str, heads: Heads, items: list[Item], *, now_ms: int) -> str | None:
    """Send each appending item. Returns why the run stopped early, or None when it reached the end."""
    unavailable = 0
    for item in (item for item in items if item.appends):
        body: dict[str, Any] = {
            "scope": scope,
            "card_id": item.card_id,
            "base_revision": item.base.get("revision") if item.base else None,
            "text": item.text,
            "client_event_id": import_event_id(scope, item, heads.epoch, now_ms=now_ms),
        }
        if item.base:
            body["base_epoch"] = heads.epoch
        try:
            reply = client.call("import", **body)
        except hubc.HubRefusal as exc:
            item.result = {"status": "refused", "code": exc.code or "refused", "http": exc.status}
            unavailable = 0
            if exc.denies_access:
                return (
                    f"the hub refused the import ({exc.status} {exc.code or 'unauthorized'}): "
                    f"this credential needs the import grant on {scope}"
                )
            if exc.code in _STOP_CODES:
                return f"the hub refused the import ({exc.code}): its log was restored since planning; run again"
        except hubc.HubUnavailable as exc:
            item.result = {"status": "unavailable", "reason": exc.reason}
            unavailable += 1
            if unavailable >= UNAVAILABLE_LIMIT:
                return f"the hub stopped serving imports ({exc.reason}); run again to continue"
        else:
            unavailable = 0
            item.result = {"status": "imported", "revision": hubc.obj(reply.get("event")).get("revision")}
    return None


def summarize(items: list[Item], found: Discovery) -> dict[str, Any]:
    counts = Counter(item.action for item in items)
    rules = Counter(rule for item in items if item.action in {"blocked", "flagged"} for rule in item.rules)
    return {
        "type": "summary",
        "counts": {action: counts[action] for action in ACTIONS if counts[action]},
        "appends": sum(item.appends for item in items),
        "private_class_imports": sum(item.appends and item.scan == "private" for item in items),
        "top_rules": [[rule, count] for rule, count in rules.most_common(TOP_RULES)],
        "discovery": {name: count for name, count in sorted(found.stats.items()) if count},
    }


def run(
    client: hubc.MemoryHubClient,
    *,
    scope: str,
    paths: list[Path],
    root: Path | None,
    excludes: list[str],
    dry_run: bool,
    update: bool,
    yes: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Plan, and unless `dry_run` send. Raises HubConfigError, HubRefusal or HubUnavailable before anything is sent."""
    found = discover(paths, root=root, excludes=excludes)
    heads = read_heads(client, scope)
    items = build_plan(found.sources, heads, update=update)
    report: dict[str, Any] = {
        "scope": scope,
        "dry_run": dry_run,
        "update": update,
        "first_import": heads.prior_imports == 0,
        "items": items,
        "summary": summarize(items, found),
        "ok": True,
        "stopped": None,
        "needs_confirmation": False,
    }
    appends = report["summary"]["appends"]
    if dry_run or not appends:
        return report
    if report["first_import"] and not yes:
        report.update(ok=False, needs_confirmation=True)
        return report
    report["stopped"] = _send(client, scope, heads, items, now_ms=now_ms or int(time.time() * 1000))
    for item in items:
        if item.appends and item.result is None:
            item.result = {"status": "not-sent"}
    report["ok"] = report["stopped"] is None and all(
        item.result and item.result["status"] == "imported" for item in items if item.appends
    )
    return report


# --- output -----------------------------------------------------------------------------------


def lines(report: dict[str, Any]) -> list[str]:
    summary = report["summary"]
    out = [
        f"memory hub import into {report['scope']}" + (" (dry run, nothing written)" if report["dry_run"] else "") + ":"
    ]
    for item in report["items"]:
        detail = ",".join(item.rules) if item.action in {"blocked", "flagged"} else (item.reason or "")
        if item.action == "changed":
            detail = "will update" if report["update"] else "kept, pass --update to overwrite"
        entry = f"- {item.path}  {item.card_id or '-'}  {item.action}" + (f"  {detail}" if detail else "")
        if item.result:
            entry += "  -> " + " ".join(f"{k}={v}" for k, v in item.result.items())
        out.append(entry)
    out.append("summary:")
    out += [f"  {action:<24}{count}" for action, count in summary["counts"].items()]
    out.append(f"  {'appends' if not report['dry_run'] else 'would append':<24}{summary['appends']}")
    if summary["private_class_imports"]:
        out.append(f"  of which with private-class content: {summary['private_class_imports']}")
    if summary["top_rules"]:
        out.append("top rules: " + ", ".join(f"{rule} x{count}" for rule, count in summary["top_rules"]))
    if summary["discovery"]:
        out.append("discovery: " + ", ".join(f"{name} {count}" for name, count in summary["discovery"].items()))
    if report["needs_confirmation"]:
        out.append(
            f"First import into {report['scope']} would append {summary['appends']} card(s). Run again with --yes."
        )
    if report["stopped"]:
        out.append(f"stopped: {report['stopped']}")
    return out


def json_lines(report: dict[str, Any]) -> list[dict[str, Any]]:
    head = {
        key: report[key]
        for key in ("scope", "dry_run", "update", "first_import", "needs_confirmation", "stopped", "ok")
    }
    return [*(item.as_dict() for item in report["items"]), {**report["summary"], **head}]
