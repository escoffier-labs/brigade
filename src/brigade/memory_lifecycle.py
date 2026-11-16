"""Policy-driven, reversible retirement of unused agent-authored cards."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from collections.abc import Iterator

import string
import html
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
import re
import posixpath
import os
import tomllib
from urllib.parse import unquote, urlsplit
from typing import Any

from . import memory_archive, memory_card_reads, memory_cmd, memory_review, receipt_signing
from . import memory_care_journal as journal
from .card_identity import card_identity
from .memory_log_retractions import retraction


# Workspace files can only veto. Enable archive moves only after the hub anchor
# in escoffier-labs/brigade#2620 can detect ledger rollback and erased failures.
AUTOMATIC_ARCHIVE_ENABLED = False
ARCHIVE_DISABLED_REASON = "automatic archive awaits hub anchor (#2620)"


class InstalledSchedulesUnreadable(ValueError):
    """Schedule references cannot be checked during this pass."""


# Match the shipped defaults. Workspace edits cannot authorize earlier edges.
MIN_STALE_AFTER_DAYS = 30
MIN_ARCHIVE_AFTER_DAYS = 90


def policy(target: Path) -> dict[str, Any]:
    defaults = Path(__file__).parent / "templates/memory/lifecycle-policy.json"
    values = json.loads(defaults.read_text(encoding="utf-8"))
    path = target / ".brigade/memory-care.toml"
    table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("lifecycle", {}) if path.exists() else {}
    if not isinstance(table, dict) or set(table) - set(values):
        raise ValueError("invalid lifecycle policy")
    for key, default in values.items():
        value = table.get(key, default)
        if type(value) is not type(default) or (type(value) is int and value < 1):
            raise ValueError(f"invalid lifecycle.{key}")
        values[key] = value
    values["stale_after_days"] = max(values["stale_after_days"], MIN_STALE_AFTER_DAYS)
    values["archive_after_days"] = max(values["archive_after_days"], MIN_ARCHIVE_AFTER_DAYS)
    if values["archive_after_days"] <= values["stale_after_days"]:
        raise ValueError("lifecycle archive window must exceed stale window")
    return values


def _mentioned(
    text: str,
    rel: str,
    card_id: str,
    aliases: tuple[str, ...] = (),
    *,
    referring: str = "",
    resolved_targets: set[str] | None = None,
) -> bool:
    # Match complete identities and paths, never a filename's incidental stem.
    names = (rel, card_id, *aliases)
    for name in names:
        if not name:
            continue
        # Absolute runbook paths protect any component-boundary suffix.
        if "/" in name and re.search(r"(?<![\w./-])/(?:[^\r\n/]+/)*" + re.escape(name) + r"(?![\w./-])", text):
            return True
        path_shaped = "/" in name or name.endswith(".md")
        endings = r"(?![\w./-])" if path_shaped else r"(?![\w/-]|\.[\w./-])"
        candidates = (name, "./" + name) if path_shaped else (name,)
        if any(re.search(r"(?<![\w./-])" + re.escape(value) + endings, text) for value in candidates):
            return True
    targets = _resolved_targets(text, referring) if resolved_targets is None else resolved_targets
    return bool(targets.intersection(names)) or any(
        "/" in name and any(path.startswith("windows:") and path.endswith("/" + name.casefold()) for path in targets)
        for name in names
        if name
    )


def _resolved_targets(text: str, referring: str) -> set[str]:
    """Parse each source's local link destinations once per lifecycle pass."""
    targets: set[str] = set()
    # Extract native absolute paths once, before URL parsing treats a drive as
    # a scheme. Component suffixes preserve workspace-independent references.
    for match in re.finditer(r'"((?:[A-Za-z]:[\\/]|\\\\)[^"\r\n]+)"', text):
        normalized = re.sub(r"\\+", "/", re.split(r"[?#]", match[1], maxsplit=1)[0])
        targets.add("windows:" + normalized.casefold())
    for match in re.finditer(r"(?<![\w./\\-])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"'()\[\]{},;]+", text):
        normalized = re.sub(r"\\+", "/", re.split(r"[?#]", match.group(), maxsplit=1)[0])
        targets.add("windows:" + normalized.casefold())
    if referring and not referring.startswith("scheduler:"):
        destinations = re.findall(r"\]\(\s*(?:<([^>]+)>|((?:\\.|[^\s)])+))", text)
        destinations.extend(
            re.findall(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(?:\r?\n[ \t]*)?(?:<([^>\n]+)>|([^\s]+))", text, flags=re.MULTILINE)
        )
        destinations.extend(
            ("", name + ("" if name.endswith(".md") else ".md"))
            for name in re.findall(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]", text)
        )
        for angle, bare in destinations:
            try:
                destination = re.sub(r"\\([" + re.escape(string.punctuation) + r"])", r"\1", angle or bare)
                link = urlsplit(html.unescape(destination))
            except ValueError:
                continue
            if link.scheme or link.netloc or not link.path:
                continue
            destination = unquote(link.path)
            resolved = posixpath.normpath(
                destination.lstrip("/")
                if destination.startswith("/")
                else posixpath.join(posixpath.dirname(referring), destination)
            )
            targets.add(resolved)
    return targets


def _references(target: Path, cards: dict[str, str]) -> dict[str, str]:
    config = memory_cmd._config_or_default(target)
    refs = {rel: text for rel, text in cards.items() if rel not in config.index_paths}
    roots = (
        ".brigade/care",
        ".brigade/runbooks",
        ".brigade/memory-care/runbooks",
        ".brigade/schedules",
        ".brigade/jobs",
        ".brigade/work",
        ".brigade/workflows",
        ".brigade/hooks",
        ".claude",
        ".codex",
        ".github/workflows",
        "runbooks",
        "docs/runbooks",
        "memory/runbooks",
    )
    # Prune before descending, rather than filtering after an unbounded rglob.
    skip = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "vendor",
        "build",
        "dist",
        "__pycache__",
        ".tox",
        ".nox",
        ".next",
        ".cache",
        "coverage",
    }

    from .instruction_inventory import ROOT_NAMES
    import subprocess

    target = target.resolve()
    ignored = set()
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            ignored = {os.fsdecode(name).rstrip("/") for name in result.stdout.split(b"\0") if name}
    except (OSError, subprocess.TimeoutExpired):
        # Unknown ignore rules only expand the scan. They never hide references.
        pass

    def walk(root: Path, *, explicit: bool = True):
        if not root.exists():
            return
        if root != target:
            try:
                journal.safe_path(target, root.relative_to(target).as_posix())
            except (journal.JournalConflict, OSError):
                return
        if root.is_symlink():
            return
        excluded = skip - {"build", "dist", "target", "coverage", ".next", ".cache"} if explicit else skip

        def unreadable(error):
            raise error

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
            base = Path(directory)
            dirs[:] = [
                name
                for name in dirs
                if name not in excluded
                and not (base / name).is_symlink()
                and (explicit or (base / name).relative_to(target).as_posix() not in ignored)
            ]
            for name in files:
                yield base / name

    paths = [p for root in roots for p in walk(target / root)]
    paths.extend(p for p in walk(target, explicit=False) if p.name in {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md"})
    paths.extend(
        target / name
        for name in (*ROOT_NAMES, ".brigade/work.json", ".brigade/hooks.json")
        if (target / name).is_file()
    )
    paths.extend(
        p
        for p in walk(target / "memory")
        if p.is_file()
        and (
            p.name in {"AGENTS.md", "AGENTS.override.md", "CLAUDE.md"}
            or any(
                Path(part).stem.lower() in {"pin", "pins", "pinned"} for part in p.relative_to(target / "memory").parts
            )
        )
    )
    outputs = {"runs", "verify-runs", "receipts", "logs", "sessions", "claude-hooks", "memory-handoffs"}
    for path in paths:
        rel = path.relative_to(target).as_posix()
        if (
            rel in config.index_paths
            or memory_archive.is_archived_path(rel)
            or outputs.intersection(Path(rel).parts)
            or path.suffix in {".log", ".jsonl"}
        ):
            continue
        try:
            refs[rel] = journal.safe_path(target, rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, journal.JournalConflict):
            continue
    # Installed schedules live outside the target. These are reads only.
    from . import care_cmd
    import shutil
    import sys

    if sys.platform != "win32" and shutil.which("crontab"):
        text, error = care_cmd._read_crontab()
        if error:
            raise InstalledSchedulesUnreadable("installed care schedules are unreadable")
        refs["scheduler:crontab"] = text
    if sys.platform == "win32":
        import subprocess

        try:
            raw = subprocess.run(
                ["schtasks", "/Query", "/FO", "CSV", "/V"], capture_output=True, text=True, check=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstalledSchedulesUnreadable("installed care schedules are unreadable") from exc
        refs["scheduler:schtasks"] = raw.stdout
    directories = [care_cmd._systemd_user_dir(None), care_cmd._launchd_dir(None)]
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.suffix not in {".service", ".timer", ".plist"} or not path.is_file() or path.is_symlink():
                continue
            try:
                refs[f"scheduler:{path.name}"] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return refs


def _signals(target: Path, cards: dict[str, str], *, today: date) -> dict[str, date]:
    """Admit only receipt-backed card citations under the promotion contract."""
    from . import memory_evidence_log as evidence

    result: dict[str, date] = {}
    for rel in ("memory/outcome/records.jsonl", evidence.LOG_PATH):
        path = journal.safe_path(target, rel)
        if not path.exists():
            continue
        from .memory_log_retractions import active

        rows = [
            (json.loads(line), line.encode())
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if line.strip()
        ]
        for row in active(rows):
            if not isinstance(row, dict) or retraction(row):
                continue
            if rel == evidence.LOG_PATH:
                if not evidence._valid(row) or row["kind"] not in {"outcome", "negative-outcome"}:
                    continue
                ref, stamp = row["evidence_id"], row["ts"]
            else:
                if row.get("source") != "verify" or row.get("artifact_kind") != "card":
                    continue
                ref, stamp = row.get("evidence_ref"), row.get("ts")
                if not isinstance(ref, str):
                    continue
                if Path(ref).is_absolute():
                    try:
                        ref = Path(ref).relative_to(target).as_posix()
                    except ValueError:
                        continue
            observed = evidence._timestamp(stamp)
            if observed is None or observed.date() > today:
                continue
            if not evidence.verify_outcome_ref(ref):
                continue
            try:
                receipt = json.loads(evidence.io.read_bytes(target / ref) or b"")
            except (OSError, ValueError):
                continue
            if not isinstance(receipt, dict):
                continue
            receipt_stamp = receipt.get("completed_at") or receipt.get("started_at")
            receipt_time = evidence._timestamp(receipt_stamp)
            if receipt_time is None or receipt_time.date() > today:
                continue
            if rel == evidence.LOG_PATH and stamp != receipt_stamp:
                continue
            # Outcome capture time is not the time of the cited verification.
            stamp, observed = receipt_stamp, receipt_time
            for card_rel, text in cards.items():
                meta, _ = memory_cmd._parse_frontmatter(text.lstrip("\ufeff").replace("\r\n", "\n"))
                identity = card_identity(meta, card_rel)
                keys = (identity.card_id, *identity.aliases)
                if row.get("card_id", row.get("artifact_id")) not in keys or (
                    "card_path" in row and row["card_path"] != card_rel
                ):
                    continue
                if evidence._validate_outcome(
                    target,
                    card_path=card_rel,
                    card_text=text,
                    ref=ref,
                    timestamp=stamp,
                    negative=row.get("kind") == "negative-outcome" or row.get("signal_value") == -1,
                    observed=receipt,
                ):
                    result[card_rel] = max(observed.date(), result.get(card_rel, observed.date()))
    return result


def _keys(text: str, rel: str) -> tuple[str, ...]:
    from .memory_hub_sync import publication_identity

    normalized = text.lstrip("\ufeff").replace("\r\n", "\n")
    fm, _ = memory_cmd._parse_frontmatter(normalized)
    identity = card_identity(fm, rel)
    return (identity.card_id, rel, *identity.aliases, publication_identity(normalized, rel)[0])


def _last_use(
    target: Path,
    rel: str,
    text: str,
    ledger: memory_card_reads.ReadLedger,
    signals: dict[str, date],
    opens: dict,
    hub_reads: dict,
    *,
    today: date,
    keys: tuple[str, ...] | None = None,
) -> date:
    fm, _ = memory_cmd._parse_frontmatter(text.lstrip("\ufeff").replace("\r\n", "\n"))
    # A new write or restore can be ahead of the policy clock. Keep that
    # filesystem activity fresh instead of discarding it with untrusted dates.
    modified = datetime.fromtimestamp((target / rel).stat().st_mtime, timezone.utc).date()
    days = [ledger.tracking_since]
    if modified <= datetime.now(timezone.utc).date():
        days.append(min(modified, today))
    if ledger.healthy_since:
        days.append(ledger.healthy_since)
    for key in ("created", "created_at", "updated", "updated_at", "last_reviewed"):
        if day := memory_cmd._parse_date(fm.get(key)):
            days.append(day)
    for key in _keys(text, rel) if keys is None else keys:
        days.extend(mapping[key] for mapping in (signals, hub_reads, ledger.last_read) if key in mapping)
        days.extend(opens.get(key, (set(), set()))[1])
    return max((day for day in days if day <= today), default=today)


@contextmanager
def _card_read_lock(target: Path) -> Iterator[bool]:
    from .inbox_lock import held_file_lock, InboxLockTimeout

    with ExitStack() as stack:
        try:
            stack.enter_context(
                held_file_lock(
                    journal.safe_path(target, memory_card_reads.READS_REL).with_suffix(".lock"), deadline_seconds=5
                )
            )
        except InboxLockTimeout:
            yield False
        else:
            yield True


@journal.serialized
def run(target: Path, *, today: date, dry_run: bool = False) -> dict[str, Any]:
    rules = policy(target)
    payload: dict[str, Any] = {"enabled": rules["enabled"], "transitions": [], "skipped": []}
    if not rules["enabled"]:
        return payload
    if receipt_signing.workspace_key(target) is None:
        return {**payload, "blocked": "workspace key unavailable, lifecycle and expiry disabled"}
    config = memory_cmd._config_or_default(target)
    cards = {}
    for path in memory_cmd._iter_cards(target, config):
        rel = path.relative_to(target).as_posix()
        try:
            cards[rel] = journal.safe_path(target, rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
    signals = _signals(target, cards, today=today)
    if cards and not dry_run:
        # Start an observation window even when no reader has ever opened a card.
        if not memory_card_reads.record(target, [], today=today):
            return {**payload, "blocked": "local read recorder error"}
    ledger = memory_card_reads.load(target)
    opens = memory_review._opens(target)
    hub_reads = memory_card_reads.hub_last_reads(target, unused_after_days=rules["archive_after_days"])
    if hub_reads is None:
        payload["blocked"] = "hub read coverage unavailable"
    if not memory_card_reads.covered(ledger, today=today, days=rules["stale_after_days"]):
        return {**payload, "blocked": "local read coverage incomplete"}
    keys = {rel: _keys(text, rel) for rel, text in cards.items()}
    # Match every potential card source before taking the ledger lock. Its
    # freshness is checked under the lock, so a newly read source still vetoes.
    try:
        refs = _references(target, {})
    except InstalledSchedulesUnreadable as exc:
        return {**payload, "blocked": str(exc)}
    external_sources = set(refs)
    refs = {**{rel: text for rel, text in cards.items() if rel not in config.index_paths}, **refs}
    targets = {other: _resolved_targets(body, other) for other, body in refs.items()}
    referenced = {}
    for rel, text in cards.items():
        fm, _ = memory_cmd._parse_frontmatter(text.lstrip("\ufeff").replace("\r\n", "\n"))
        referenced[rel] = {
            other
            for other, body in refs.items()
            if other != rel
            and _mentioned(
                body,
                rel,
                keys[rel][0] if fm.get("id") or fm.get("card_id") else "",
                tuple(k for k in keys[rel] if k != Path(rel).stem),
                referring=other,
                resolved_targets=targets[other],
            )
        }
    digest = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()
    archived_cards: set[str] = set()
    for rel, text in sorted(cards.items()):
        with _card_read_lock(target) as acquired:
            if not acquired:
                payload["skipped"].append({"path": rel, "reason": "local read ledger busy"})
                continue
            ledger = memory_card_reads.load(target)
            if not memory_card_reads.covered(ledger, today=today, days=rules["stale_after_days"]):
                return {**payload, "blocked": "local read coverage incomplete"}
            last_use = _last_use(
                target, rel, text, ledger, signals, opens, hub_reads or {}, today=today, keys=keys[rel]
            )
            fm, _ = memory_cmd._parse_frontmatter(text.lstrip("\ufeff").replace("\r\n", "\n"))
            identity = card_identity(fm, rel).card_id
            reason = None
            if (
                fm.get("trust") != "agent-unreviewed"
                or str(fm.get("author_type", "")).lower() == "human"
                or str(fm.get("author", "")).lower() == "human"
            ):
                reason = "human or unknown authorship"
            elif (
                fm.get("pinned")
                or memory_archive.frontmatter_refusal(text)
                or memory_cmd._is_read_only(config, rel)
                or rel in config.index_paths
            ):
                reason = "pinned or protected"
            elif any(
                other in external_sources
                or (
                    today
                    - _last_use(
                        target,
                        other,
                        cards[other],
                        ledger,
                        signals,
                        opens,
                        hub_reads or {},
                        today=today,
                        keys=keys[other],
                    )
                ).days
                < rules["stale_after_days"]
                for other in referenced[rel]
                if other not in archived_cards
            ):
                reason = "referenced"
            if reason:
                payload["skipped"].append({"path": rel, "reason": reason})
                continue
            days = [last_use]
            age = (today - last_use).days
            if not memory_card_reads.covered(
                ledger,
                today=today,
                days=rules["archive_after_days"] if age >= rules["archive_after_days"] else rules["stale_after_days"],
            ):
                payload["skipped"].append({"path": rel, "reason": "local read coverage incomplete"})
                continue
            state_rel = f".brigade/memory/lifecycle/{hashlib.sha256(rel.encode()).hexdigest()}.json"
            before = journal.current(target, state_rel)
            saved_state = json.loads(before) if before else {"state": "active"}
            if (
                not isinstance(saved_state, dict)
                or not isinstance(saved_state.get("state"), str)
                or saved_state["state"] not in {"active", "stale", "archived"}
            ):
                raise ValueError("invalid lifecycle state")
            previous = saved_state["state"]
            desired = (
                "archived"
                if age >= rules["archive_after_days"]
                else "stale"
                if age >= rules["stale_after_days"]
                else "active"
            )
            # Record both lifecycle edges for a first run after the archive window.
            states = ["stale", "archived"] if previous == "active" and desired == "archived" else [desired]
            for state in states:
                if state == previous or len(payload["transitions"]) >= rules["max_transitions_per_run"]:
                    continue
                if state == "archived" and (not AUTOMATIC_ARCHIVE_ENABLED or hub_reads is None):
                    reason = (
                        ARCHIVE_DISABLED_REASON if not AUTOMATIC_ARCHIVE_ENABLED else "hub read coverage unavailable"
                    )
                    payload["skipped"].append({"path": rel, "reason": reason})
                    break
                # Failure markers can arrive without the lock and only veto.
                # Successful readers hold this lock until their final content read.
                if not memory_card_reads.covered(
                    memory_card_reads.load(target),
                    today=today,
                    days=rules["archive_after_days"] if state == "archived" else rules["stale_after_days"],
                ):
                    payload["skipped"].append({"path": rel, "reason": "local read coverage incomplete"})
                    break
                metadata = {
                    "actor": "policy",
                    "policy_digest": digest,
                    "from_state": previous,
                    "to_state": state,
                    "last_use": max(days).isoformat(),
                    "card_path": rel,
                }
                after = (json.dumps({"state": state, "path": rel}, sort_keys=True) + "\n").encode()
                change = journal.Change(state_rel, before, after)
                entry = {**metadata, "decision_id": None}
                if not dry_run:
                    if state == "archived":
                        try:
                            decision = memory_archive.archive_card(
                                target,
                                rel,
                                reason="unused-card",
                                config=config,
                                policy_metadata=metadata,
                                state_change=change,
                                expected_before=text.encode("utf-8"),
                            )
                        except memory_archive.ArchiveRecoveryUnverified as exc:
                            payload["error"] = True
                            payload.setdefault("recovery_unverified", []).append(exc.decision_id)
                            payload["skipped"].append({"path": rel, "reason": str(exc), "decision_id": exc.decision_id})
                            break
                        except memory_archive.ArchiveRefused as exc:
                            # A refusal leaves the card and any existing archive in place.
                            # Skip only this card so the rest of the pass still runs.
                            payload["skipped"].append({"path": rel, "reason": str(exc)})
                            break
                    else:
                        decision = journal.commit_phased(
                            target,
                            kind="lifecycle",
                            reason="unused-card",
                            card_id=identity,
                            changes=[change],
                            prepared_paths=[state_rel],
                            hub={"status": "skipped"},
                            metadata=metadata,
                        )
                    if state == "archived":
                        archived_cards.add(rel)
                    entry["decision_id"] = decision.decision_id
                payload["transitions"].append(entry)
                previous, before = state, after
    return payload
