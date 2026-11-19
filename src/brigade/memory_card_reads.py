"""Cumulative read days, independent of the bounded search log."""

from __future__ import annotations

from contextlib import ExitStack
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
CLEARED_REL = ".brigade/memory/read-recorder-cleared.json"
# Enough for a recorded cause, never the whole file.
DISABLED_CAUSE_BYTES = 256
# Last resort when storage cannot accept a marker or invalidate the ledger.
_FAILED_ROOTS: set[Path] = set()


@dataclass(frozen=True)
class DisabledSentinel:
    present: bool
    cause: str | None


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


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _marker_count(bound, name: str) -> int:
    """Parsed coalesced count, or zero when the marker cannot be read."""
    try:
        raw = bound.read_bytes(*Path(FAILURES_REL).parts, name, max_bytes=4096)
        payload = json.loads(raw) if raw is not None else None
    except (OSError, ValueError, TypeError):
        return 0
    count = payload.get("count") if isinstance(payload, dict) else None
    return count if type(count) is int and count > 0 else 0


def _failures(target: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """Failure markers keyed by path, with the identity and count seen at read time."""
    with bound_run_dir(target) as bound:
        try:
            parent = bound.dir_fd(*Path(FAILURES_REL).parts)
        except FileNotFoundError:
            return {}
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            raise ValueError("read recorder failure limit exceeded")
        markers = {}
        for name in names:
            try:
                info = dirfd.stat_child(parent, name)
            except FileNotFoundError:
                continue
            markers[f"{FAILURES_REL}/{name}"] = (*_fingerprint(info), _marker_count(bound, name))
        return markers


def _unlink_if_unchanged(target: Path, rel: str, expected: tuple[int, int, int, int, int]) -> None:
    """Consume a marker only while it is still the file that was read.

    A failing process writes markers without the ledger lock, so it can replace
    today's marker after this pass listed it. Keeping the newer entry leaves a
    failure this pass never observed instead of deleting it.
    """
    parts = Path(rel).parts
    with bound_run_dir(target) as bound:
        try:
            parent = bound.dir_fd(*parts[:-1])
            info = dirfd.stat_child(parent, parts[-1])
        except FileNotFoundError:
            return
        if (*_fingerprint(info), _marker_count(bound, parts[-1])) != expected:
            return
        try:
            dirfd.unlink_child(parent, parts[-1])
        except FileNotFoundError:
            return
        dirfd.fsync_directory(parent)


def _disabled(target: Path) -> bool:
    # An unreadable or malformed sentinel is still a stop signal.
    with bound_run_dir(target) as bound:
        try:
            bound.stat((), DISABLED_REL)
        except FileNotFoundError:
            return False
        return True


def _record_failure(target: Path, *, today: date | None = None) -> None:
    """Use storage independent of both the ledger and its intent writer.

    Repeated failures inside one day share that day's marker and only raise its
    count, so a persistent failure cannot fill the bounded marker scan. The day
    is the same interval the lifecycle uses for heartbeats and pending intents.
    """
    _FAILED_ROOTS.add(target.resolve())
    today = today or _day()
    name = f"{today.isoformat()}.json"
    try:
        with bound_run_dir(target) as bound:
            # Counting is best effort: concurrent writers can lose one increment,
            # and any count of 1 or more keeps the interval unhealthy.
            count = _marker_count(bound, name) + 1
            bound.write_text_atomic(
                tuple(Path(FAILURES_REL).parts),
                name,
                json.dumps({"day": today.isoformat(), "count": count}, sort_keys=True) + "\n",
            )
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


def _read_descriptor(descriptor: int) -> bytes:
    data = bytearray()
    while len(data) <= DISABLED_CAUSE_BYTES:
        chunk = os.read(descriptor, DISABLED_CAUSE_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data[:DISABLED_CAUSE_BYTES])


def _open_sentinel(bound) -> int:
    # O_BINARY keeps a Windows handle from translating the recorded cause bytes.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    return bound.open_file((), DISABLED_REL, flags)


def _sentinel_problem(info: os.stat_result) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_nlink != 1:
        return "multiple hard links"
    return None


def disabled_sentinel(target: Path) -> DisabledSentinel:
    """The root sentinel and its recorded cause, read without following a link."""
    try:
        with bound_run_dir(target) as bound:
            try:
                entry = bound.stat((), DISABLED_REL)
            except FileNotFoundError:
                return DisabledSentinel(False, None)
            problem = _sentinel_problem(entry)
            if problem is not None:
                return DisabledSentinel(True, problem)
            descriptor = _open_sentinel(bound)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                    return DisabledSentinel(True, "changed")
                problem = _sentinel_problem(opened)
                if problem is not None:
                    return DisabledSentinel(True, problem)
                data = _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return DisabledSentinel(False, None)
    except OSError:
        # A sentinel that cannot be verified without following a link is still a stop signal.
        return DisabledSentinel(True, "unreadable")
    cause = data.decode("utf-8", "replace").strip()
    return DisabledSentinel(True, cause or "empty")


def disabled_message(target: Path) -> str | None:
    """Operator line for the sentinel, encoded for a terminal. ``None`` when absent."""
    from .memory_care_instructions import field

    state = disabled_sentinel(target)
    if not state.present:
        return None
    cause = state.cause if state.cause is not None else "unreadable"
    return (
        f"stopped={DISABLED_REL} {field('cause', cause)} "
        f'clear="brigade memory care clear-read-recorder --confirm --target {target.expanduser().resolve()}"'
    )


def _stored_since(target: Path) -> date | None:
    try:
        return date.fromisoformat(_read_json(target, READS_REL)["healthy_since"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


@dataclass(frozen=True)
class ClearedRecord:
    day: date | None  # the recorded clear, when the record can be trusted
    trusted: bool  # absent records are trusted; present but unreadable ones are not


def _cleared_record(target: Path) -> ClearedRecord:
    """The last recorded sentinel clear.

    Absent means no clear ever happened. A record that is present but torn,
    unreadable, oversized or not a regular file cannot be trusted to bound an
    outage, so the caller must fail closed instead of trusting the ledger.
    """
    try:
        payload = _read_json(target, CLEARED_REL)
    except (OSError, ValueError):
        return ClearedRecord(None, False)
    if payload is None:
        return ClearedRecord(None, True)
    if not isinstance(payload, dict):
        return ClearedRecord(None, False)
    try:
        day = date.fromisoformat(payload["day"])
    except (KeyError, TypeError, ValueError):
        return ClearedRecord(None, False)
    return ClearedRecord(day, True) if day <= _day() else ClearedRecord(None, False)


def _reset_coverage(target: Path, *, today: date) -> bool:
    """Start a fresh window at ``today`` so a cleared outage never counts as covered."""
    ledger = load(target)
    days = {key: day for key, day in ledger.last_read.items() if day <= today}
    value = {
        "tracking_since": min(ledger.tracking_since, today).isoformat(),
        "healthy_since": today.isoformat(),
        "heartbeat": today.isoformat(),
        "coverage_days": [today.isoformat()],
        "last_read": {key: day.isoformat() for key, day in days.items()},
    }
    mac = authenticate(target, READS_REL, value)
    if mac is None:
        return False
    _write_json(target, READS_REL, {**value, "mac": mac})
    return True


def clear_disabled(target: Path, *, today: date | None = None) -> dict[str, object]:
    """Clear a plain root sentinel after starting a fresh coverage window.

    The window reset runs under the ledger lock and happens before the unlink:
    when it cannot be written the sentinel stays, so a cleared outage can never
    be treated as covered. The sentinel is verified and removed through one
    bound directory handle, never a path that could resolve to a link target.
    """
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path

    target = target.expanduser().resolve()
    today = today or _day()
    try:
        with bound_run_dir(target) as bound:
            try:
                entry = bound.stat((), DISABLED_REL)
            except FileNotFoundError:
                return {"cleared": False, "reason": "missing"}
            problem = _sentinel_problem(entry)
            if problem is not None:
                return {"cleared": False, "reason": problem}
            descriptor = _open_sentinel(bound)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                    return {"cleared": False, "reason": "changed"}
                problem = _sentinel_problem(opened)
                if problem is not None:
                    return {"cleared": False, "reason": problem}
                cause = _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
            if receipt_signing.workspace_key(target) is not None:
                try:
                    with held_file_lock(safe_path(target, READS_REL).with_suffix(".lock"), deadline_seconds=5):
                        if not _reset_coverage(target, today=today):
                            return {"cleared": False, "reason": "coverage reset failed"}
                except (OSError, ValueError):
                    return {"cleared": False, "reason": "coverage reset failed"}
            parent = bound.dir_fd()
            try:
                current = dirfd.stat_child(parent, DISABLED_REL)
            except FileNotFoundError:
                return {"cleared": False, "reason": "missing"}
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or _sentinel_problem(current):
                return {"cleared": False, "reason": "changed"}
            dirfd.unlink_child(parent, DISABLED_REL)
            dirfd.fsync_directory(parent)
    except FileNotFoundError:
        return {"cleared": False, "reason": "missing"}
    except OSError:
        return {"cleared": False, "reason": "unreadable"}
    recorded = True
    try:
        _write_json(
            target,
            CLEARED_REL,
            {
                "sentinel": DISABLED_REL,
                "day": today.isoformat(),
                "cause_sha256": hashlib.sha256(cause).hexdigest(),
            },
        )
    except (OSError, ValueError):
        recorded = False
    return {"cleared": True, "recorded": recorded}


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
        # A recorded clear is a floor: coverage before it was never observed,
        # even when a restored ledger still claims an earlier window. A record
        # that cannot be read is never treated as absent.
        cleared = _cleared_record(target)
        if not cleared.trusted:
            return ReadLedger(_day(), {}, recorder_error=True)
        if cleared.day is not None and cleared.day > since:
            since = cleared.day
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
    """Record use and its intent under the same lock as lifecycle moves."""
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path

    if receipt_signing.workspace_key(target) is None:
        return _record_locked(target, keys, today=today)
    try:
        with held_file_lock(safe_path(target, READS_REL).with_suffix(".lock"), deadline_seconds=5):
            return _record_locked(target, keys, today=today)
    except (OSError, ValueError):
        _record_failure(target, today=today)
        return False


def _record_locked(target: Path, keys: Iterable[str], *, today: date | None = None) -> bool:
    """Persist intent before recording. Failed or interrupted writes block coverage.

    A subsequent successful heartbeat starts a new window after recorded errors.
    Expired pending intents reset the window before they are removed.
    """
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
            _record_failure(target, today=today)
            return False
        if _disabled(target):
            return False
        _write_json(target, intent, {"state": "pending", "day": today.isoformat()})
        ledger = load(target)
        cleared = _cleared_record(target)
        stored = _stored_since(target)
        clamped = not cleared.trusted or (cleared.day is not None and (stored is None or stored < cleared.day))
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
            or clamped
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
        for error in errors:
            _unlink(target, error)
        for marker, fingerprint in failures.items():
            _unlink_if_unchanged(target, marker, fingerprint)
        _unlink(target, intent)
        _FAILED_ROOTS.discard(target.resolve())
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        # This is independent of the ledger lock and remains visible to lifecycle.
        _record_failure(target, today=today)
        try:
            _write_json(target, intent, {"state": "error", "day": today.isoformat()})
        except (OSError, ValueError):
            pass
        return False


def read_content(target: Path, rel: str, reader: Callable[[], str]) -> str:
    """Record and re-read under the ledger lock shared with archive moves.

    A failed lock or recorder still permits retrieval, but leaves a failure
    veto. The hub anchor in #2620 is required before local coverage authorizes
    archiving in production.
    """
    from .inbox_lock import held_file_lock
    from .memory_care_journal import safe_path
    from .memory_cmd import card_identity_from_text

    if receipt_signing.workspace_key(target) is None:
        text = reader()
        _record_locked(target, [rel, card_identity_from_text(text, rel)])
        return text
    with ExitStack() as stack:
        try:
            stack.enter_context(held_file_lock(safe_path(target, READS_REL).with_suffix(".lock"), deadline_seconds=5))
        except (OSError, ValueError):
            _record_failure(target)
            return reader()
        text = reader()
        _record_locked(target, [rel, card_identity_from_text(text.lstrip("\ufeff").replace("\r\n", "\n"), rel)])
        return reader()


def record_content(target: Path, text: str, rel: str) -> None:
    """Record already-delivered remote or restored content, without authorizing moves."""
    read_content(target, rel, lambda: text)


def last_read(ledger: ReadLedger, keys: Iterable[str], *, today: date | None = None) -> date | None:
    today = today or _day()
    return max(
        (ledger.last_read[k] for k in keys if k in ledger.last_read and ledger.last_read[k] <= today), default=None
    )


HUB_PAGE_GAP_SECONDS = 1.1


def hub_last_reads(
    target: Path, *, unused_after_days: int = 120, sleep: Callable[[float], None] = time.sleep
) -> dict[str, date] | None:
    """Last read day per hub card, or ``None`` without verifiable hub coverage.

    ``None`` is not "nobody read anything": callers must not retire on use grounds without it.
    The hub allows one read-stat request per second per principal, so pages are paced.
    """
    try:
        settings = hubc.load_settings(target)
        if not settings.enabled or not settings.scope:
            return None
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
