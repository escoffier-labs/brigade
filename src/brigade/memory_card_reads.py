"""Cumulative read days, independent of the bounded search log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import sys
import json
import time
import secrets
import os
import stat
import hashlib
import hmac
from pathlib import Path
from typing import Callable, Iterable

from . import memory_hub_client as hubc
from . import dirfd, receipt_signing
from .run_dirfd import bound_run_dir, MAX_READ_BYTES

READS_REL = ".brigade/memory/card-reads.json"


@dataclass(frozen=True)
class ReadLedger:
    tracking_since: date
    last_read: dict[str, date]
    healthy_since: date | None = None
    heartbeat: date | None = None
    recorder_error: bool = False


COVERAGE_REL = ".brigade/memory/read-coverage"
FAILURES_REL = ".brigade/read-recorder-failures"
DISABLED_REL = ".brigade-read-recorder-disabled"
# Last resort when storage cannot accept a marker or invalidate the ledger.
_FAILED_ROOTS: set[Path] = set()


def _coverage_policy(target: Path) -> dict:
    from .memory_lifecycle import policy

    return policy(target)


def _mac(target: Path, context: str, value: dict, key: bytes) -> str:
    message = json.dumps(
        ["brigade.memory-use.v1", str(target.expanduser().resolve()), context, value],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def authenticate(target: Path, context: str, value: dict) -> str | None:
    key = receipt_signing.workspace_key(target)
    return _mac(target, context, value, key) if key is not None else None


def authenticated(target: Path, context: str, value: dict, mac: object) -> bool:
    if not isinstance(mac, str) or len(mac) != 64 or any(c not in "0123456789abcdef" for c in mac):
        return False
    expected = authenticate(target, context, value)
    return expected is not None and hmac.compare_digest(mac, expected)


def _day() -> date:
    return datetime.now(timezone.utc).date()


def _read_json(target: Path, rel: str):
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        try:
            fd = bound.open_file(parts[:-1], parts[-1], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("read coverage must be a regular single-link file")
            raw = handle.read(MAX_READ_BYTES + 1)
            if len(raw) > MAX_READ_BYTES:
                raise ValueError("read coverage byte limit exceeded")
            return json.loads(raw)


def _write_json(target: Path, rel: str, value: dict) -> None:
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        bound.write_text_atomic(parts[:-1], parts[-1], json.dumps(value, sort_keys=True) + "\n")


def _unlink(target: Path, rel: str) -> None:
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        parent = bound.dir_fd(*parts[:-1])
        dirfd.unlink_child(parent, parts[-1])
        dirfd.fsync_directory(parent)


def _pending(target: Path) -> list[str]:
    with bound_run_dir(target) as bound:
        try:
            parent = bound.dir_fd(*Path(COVERAGE_REL).parts)
        except FileNotFoundError:
            return []
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            raise ValueError("read coverage intent limit exceeded")
        return [f"{COVERAGE_REL}/{name}" for name in names if name.endswith(".json")]


def _failures(target: Path) -> list[str]:
    with bound_run_dir(target) as bound:
        try:
            parent = bound.dir_fd(*Path(FAILURES_REL).parts)
        except FileNotFoundError:
            return []
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            raise ValueError("read recorder failure limit exceeded")
        return [f"{FAILURES_REL}/{name}" for name in names]


def _disabled(target: Path) -> bool:
    # An unreadable or malformed sentinel is still a stop signal.
    with bound_run_dir(target) as bound:
        try:
            bound.stat((), DISABLED_REL)
        except FileNotFoundError:
            return False
        return True


def _record_failure(target: Path) -> None:
    """Use storage independent of both the ledger and its intent writer."""
    _FAILED_ROOTS.add(target.resolve())
    try:
        with bound_run_dir(target) as bound:
            bound.write_text_atomic(tuple(Path(FAILURES_REL).parts), secrets.token_hex(16), "recorder-error\n")
        return
    except (OSError, ValueError):
        pass
    try:
        # If Brigade's state directory is unavailable, permanently disable
        # transitions at the workspace root until an operator reviews the gap.
        with bound_run_dir(target) as bound:
            bound.write_text_atomic((), DISABLED_REL, "recorder-error\n")
        return
    except (OSError, ValueError):
        pass
    try:
        # Removing stale authority is safer than reusing it after recovery.
        _unlink(target, READS_REL)
    except (OSError, ValueError):
        pass


def load(target: Path) -> ReadLedger:
    try:
        p = _read_json(target, READS_REL)
        mac = p.pop("mac", None)
        if not authenticated(target, READS_REL, p, mac):
            raise ValueError("unauthenticated read coverage")
        since = date.fromisoformat(p["healthy_since"])
        heartbeat = date.fromisoformat(p["heartbeat"])
        # Authenticate observed heartbeat days, including bounded offline intervals.
        allowance = _coverage_policy(target)["heartbeat_gap_allowance_days"]
        history = [date.fromisoformat(day) for day in p["coverage_days"]]
        if (
            not history
            or history[0] != since
            or history[-1] != heartbeat
            or any(not 1 <= (b - a).days <= allowance + 1 for a, b in zip(history, history[1:], strict=False))
            or date.fromisoformat(p["tracking_since"]) > since
        ):
            raise ValueError("incomplete read coverage history")
        return ReadLedger(
            date.fromisoformat(p["tracking_since"]),
            {k: date.fromisoformat(v) for k, v in p["last_read"].items()},
            since,
            heartbeat,
            bool(_pending(target)) or bool(_failures(target)) or _disabled(target) or target.resolve() in _FAILED_ROOTS,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return ReadLedger(_day(), {}, recorder_error=True)


def covered(ledger: ReadLedger, *, today: date, days: int) -> bool:
    return bool(
        not ledger.recorder_error
        and ledger.heartbeat == today
        and ledger.healthy_since is not None
        and 0 <= (today - ledger.healthy_since).days
        and (today - ledger.healthy_since).days >= days
    )


def record(target: Path, keys: Iterable[str], *, today: date | None = None) -> bool:
    """Persist intent before recording. Failed or interrupted writes block coverage.

    A subsequent successful heartbeat starts a new window after recorded errors.
    Expired pending intents reset the window before they are removed.
    """
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path

    today = today or _day()
    keys = tuple(keys)
    intent = f"{COVERAGE_REL}/{secrets.token_hex(16)}.json"
    try:
        # No authority means transitions are already disabled. Do not accumulate
        # an intent for every content open in an unconfigured workspace.
        if receipt_signing.workspace_key(target) is None:
            # Only a confined lookup proving absence permits a silent return.
            # Unsafe parents, dangling links and inaccessible keys are failures.
            with bound_run_dir(target) as bound:
                try:
                    bound.stat((".brigade",), receipt_signing.KEY_NAME)
                except FileNotFoundError:
                    return False
            _record_failure(target)
            return False
        if _disabled(target):
            return False
        _write_json(target, intent, {"state": "pending", "day": today.isoformat()})
        path = safe_path(target, READS_REL)
        with held_file_lock(path.with_suffix(".lock"), deadline_seconds=5):
            ledger = load(target)
            limits = _coverage_policy(target)
            failures = _failures(target)
            errors = []
            for pending in _pending(target):
                if pending == intent:
                    continue
                malformed = False
                try:
                    payload = _read_json(target, pending)
                    if not isinstance(payload, dict) or payload.get("state") not in {"error", "pending"}:
                        raise ValueError("invalid intent state")
                    day = date.fromisoformat(payload["day"])
                    if day > today:
                        raise ValueError("future intent day")
                except (ValueError, TypeError, KeyError):
                    malformed = True
                if malformed:
                    # Keep only a body-free reason. Do not copy attacker-controlled
                    # payloads or filenames into the quarantine receipt.
                    quarantine = hashlib.sha256(pending.encode()).hexdigest()
                    _write_json(
                        target,
                        f".brigade/memory/read-coverage-quarantine/{quarantine}.json",
                        {"reason": "malformed-intent", "day": today.isoformat()},
                    )
                    errors.append(pending)
                elif payload["state"] == "error" or (today - day).days >= limits["pending_intent_max_age_days"]:
                    errors.append(pending)
            since = ledger.healthy_since
            if (
                errors
                or failures
                or target.resolve() in _FAILED_ROOTS
                or since is None
                or ledger.heartbeat is None
                or not 0 <= (today - ledger.heartbeat).days <= limits["heartbeat_gap_allowance_days"] + 1
            ):
                since = today
            days = {k: v for k, v in ledger.last_read.items() if v <= today}
            for key in keys:
                days[key] = max(today, days.get(key, today))
            history = [] if since == today else _read_json(target, READS_REL)["coverage_days"]
            history = sorted(set([*history, today.isoformat()]))
            value = {
                "tracking_since": min(ledger.tracking_since, today).isoformat(),
                "healthy_since": since.isoformat(),
                "heartbeat": today.isoformat(),
                "coverage_days": history,
                "last_read": {k: v.isoformat() for k, v in days.items()},
            }
            mac = authenticate(target, READS_REL, value)
            if mac is None:
                raise ValueError("workspace receipt signing key unavailable")
            _write_json(
                target,
                READS_REL,
                {**value, "mac": mac},
            )
            for error in [*errors, *failures]:
                _unlink(target, error)
            _unlink(target, intent)
        _FAILED_ROOTS.discard(target.resolve())
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        # This is independent of the ledger lock and remains visible to lifecycle.
        _record_failure(target)
        try:
            _write_json(target, intent, {"state": "error", "day": today.isoformat()})
        except (OSError, ValueError):
            pass
        return False


def record_content(target: Path, text: str, rel: str) -> None:
    """Record an explicit content open before returning its content to the caller."""
    from .memory_cmd import card_identity_from_text

    try:
        record(target, [rel, card_identity_from_text(text.lstrip("\ufeff").replace("\r\n", "\n"), rel)])
    except (OSError, ValueError):
        _record_failure(target)


def last_read(ledger: ReadLedger, keys: Iterable[str], *, today: date | None = None) -> date | None:
    today = today or _day()
    return max(
        (ledger.last_read[k] for k in keys if k in ledger.last_read and ledger.last_read[k] <= today), default=None
    )


HUB_PAGE_GAP_SECONDS = 1.1


def hub_last_reads(
    target: Path, *, unused_after_days: int = 120, sleep: Callable[[float], None] = time.sleep
) -> dict[str, date] | None:
    """Last read day per hub card, or ``None`` when a configured hub could not be read in full.

    ``None`` is not "nobody read anything": callers must not retire on use grounds without it.
    The hub allows one read-stat request per second per principal, so pages are paced.
    """
    try:
        settings = hubc.load_settings(target)
        if not settings.enabled or not settings.scope:
            return {}
        client = hubc.client_for(settings)
        after = None
        result = {}
        cursors: set[str] = set()
        while True:
            if after is not None:
                sleep(HUB_PAGE_GAP_SECONDS)
            for attempt in range(3):
                try:
                    body = hubc.read_stats(client, settings.scope, limit=500, after=after)
                    break
                except hubc.HubRefusal as exc:
                    if getattr(exc, "status", None) != 429 or attempt == 2:
                        raise
                    sleep(HUB_PAGE_GAP_SECONDS)
            since = body["read_counts_since_day"]
            if (
                type(since) is not int
                or since < 0
                or (_day() - (date(1970, 1, 1) + timedelta(days=since))).days < unused_after_days
            ):
                raise ValueError("hub read tracking too young")
            for row in body["rows"]:
                try:
                    # Legacy totals combine searches with opens and cannot establish use.
                    if "last_card_read_day" not in row:
                        raise ValueError("hub read stats do not distinguish explicit card opens")
                    day = row["last_card_read_day"]
                    if type(day) is not int or day < 0 or not isinstance(row["card_id"], str):
                        raise ValueError("invalid read day")
                    if day:
                        observed = date(1970, 1, 1) + timedelta(days=day)
                        if observed <= _day():
                            result[row["card_id"]] = observed
                except (TypeError, ValueError, OverflowError, KeyError):
                    raise ValueError("malformed hub read-stat row") from None
            following = body.get("next_after")
            if following is None:
                return result
            if not isinstance(following, str) or not following or following in cursors:
                raise ValueError("repeated or malformed hub read-stat cursor")
            cursors.add(following)
            after = following
    except (
        hubc.HubConfigError,
        hubc.HubUnavailable,
        hubc.HubRefusal,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
    ) as exc:
        print(f"memory care: hub read stats unavailable, retiring nothing on use grounds: {exc}", file=sys.stderr)
        return None
