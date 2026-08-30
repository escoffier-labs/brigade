"""Private bounded Wazuh dedupe and classification state."""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import grokbot_findings, grokbot_ops
from .contracts import (
    CATEGORIES,
    ERROR_MESSAGES,
    FINDING_KEYS,
    MAX_RULE_ID,
    PRODUCER,
    PROPOSAL_STATES,
    WazuhError,
    is_offset_datetime,
    parse_finding_id,
    parse_fingerprint,
    parse_identifier,
    parse_opaque_id,
)

STATE_SCHEMA = "brigade.grokbot.wazuh-state.v1"
STATE_KEYS = frozenset({"alerts", "proposals", "schema", "suppressions"})
MAX_ALERTS = 2_048
MAX_SUPPRESSIONS = 256
MAX_PROPOSALS = 64
RELAY_STATUSES = frozenset({"pending", "reported"})
PUBLIC_ALERT_KEYS = frozenset(
    {
        "agent_id",
        "category",
        "count",
        "finding_id",
        "fingerprint",
        "first_seen",
        "last_seen",
        "reason",
        "rule_class",
        "rule_id",
        "severity",
    }
)
_SEMANTIC_KEYS = ("revision", "severity", "title", "body", "source_digest", "content_digest")


def _unavailable() -> None:
    raise WazuhError("unavailable", ERROR_MESSAGES["unavailable"])


def _invalid() -> None:
    raise WazuhError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _assert_secure_stat(info: os.stat_result, *, directory: bool) -> None:
    if directory:
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            _invalid()
    elif not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _invalid()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _invalid()


def read_secure_text(path: Path | str, *, expected_mode: int = 0o600) -> str:
    """Read one current-UID-owned regular file through a no-follow descriptor."""
    target = Path(path)
    parent = -1
    descriptor = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(target, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if os.name == "posix":
            descriptor = os.open(target.name, flags, dir_fd=parent)
        else:
            from ..work_cmd import nt_dirfd

            descriptor = nt_dirfd.open_file(parent, target.name, os.O_RDONLY)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != expected_mode:
            _invalid()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            _invalid()
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
            return handle.read()
    except WazuhError:
        raise
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise WazuhError("protocol_error", ERROR_MESSAGES["protocol_error"]) from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if parent != -1:
            os.close(parent)


class WazuhStore:
    """Owner-only JSON state with atomic mode-0600 replacement."""

    def __init__(self, state_path: str):
        if not isinstance(state_path, str) or not state_path:
            _invalid()
        self._path = Path(state_path)
        self._lock = threading.Lock()

    def ready(self) -> None:
        with self._lock:
            self._ensure_state_dir()
            self._load()

    def upsert_alert(
        self,
        record: Mapping[str, Any],
        decision: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        return self.upsert_alerts(((record, decision),), now=now)[0]

    def upsert_alerts(
        self,
        items: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        with self._lock:
            state = self._load()
            incoming = [parse_fingerprint(record["fingerprint"]) for record, _decision in items]
            new_count = len({fingerprint for fingerprint in incoming if fingerprint not in state["alerts"]})
            if len(state["alerts"]) + new_count > MAX_ALERTS:
                _unavailable()
            results: list[dict[str, Any]] = []
            for record, decision in items:
                results.append(self._apply_alert(state, record, decision, now=now))
            self._persist(state)
            return results

    def preflight_capacity(self, records: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            state = self._load()
            incoming = {parse_fingerprint(record["fingerprint"]) for record in records}
            new_count = len(incoming - set(state["alerts"]))
            if len(state["alerts"]) + new_count > MAX_ALERTS:
                _unavailable()

    def mark_relay_status(self, fingerprints: Sequence[str], status: str) -> None:
        if status not in RELAY_STATUSES:
            _invalid()
        with self._lock:
            state = self._load()
            changed = False
            for fingerprint in fingerprints:
                key = parse_fingerprint(fingerprint)
                item = state["alerts"].get(key)
                if item is None or item.get("relay_status") == status:
                    continue
                item["relay_status"] = status
                changed = True
            if changed:
                self._persist(state)

    def current_count(self) -> int:
        with self._lock:
            return len(self._load()["alerts"])

    def get_alert(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._load()["alerts"].get(parse_fingerprint(fingerprint))
            return dict(item) if item is not None else None

    def public_alert(self, fingerprint: str) -> dict[str, Any] | None:
        stored = self.get_alert(fingerprint)
        if stored is None:
            return None
        return {key: stored[key] for key in sorted(PUBLIC_ALERT_KEYS) if key in stored}

    def alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._load()["alerts"].values()]

    def public_alerts(self) -> list[dict[str, Any]]:
        return [{key: item[key] for key in sorted(PUBLIC_ALERT_KEYS) if key in item} for item in self.alerts()]

    def suppressions(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._load()["suppressions"].values()]

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        key = parse_finding_id(finding_id)
        for item in self.alerts():
            if item["finding_id"] == key:
                return item
        return None

    def put_proposal(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            proposal_id = parse_opaque_id(proposal["proposal_id"])
            recorded = {
                "proposal_id": proposal_id,
                "finding_id": parse_finding_id(proposal["finding_id"]),
                "fingerprint": parse_fingerprint(proposal["fingerprint"]),
                "state": _proposal_state(proposal["state"]),
                "created_at": _require_time(proposal["created_at"]),
                "expires_at": _require_time(proposal["expires_at"]),
            }
            if len(state["proposals"]) >= MAX_PROPOSALS and proposal_id not in state["proposals"]:
                _unavailable()
            state["proposals"][proposal_id] = recorded
            self._persist(state)
            return dict(recorded)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._load()["proposals"].get(parse_opaque_id(proposal_id))
            return dict(item) if item is not None else None

    def expire_proposal_if_needed(self, proposal_id: str, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            state = self._load()
            item = state["proposals"].get(parse_opaque_id(proposal_id))
            if item is None:
                return None
            recorded = dict(item)
            if recorded["state"] == "proposed" and _is_expired(recorded["expires_at"], now):
                recorded["state"] = "expired"
                state["proposals"][recorded["proposal_id"]] = recorded
                self._persist(state)
            return recorded

    def counts(self) -> dict[str, int]:
        alerts = self.public_alerts()
        counts = {"current": len(alerts), "suppressed": 0, "watched": 0, "escalated": 0}
        for item in alerts:
            category = item.get("category")
            if category == "suppress":
                counts["suppressed"] += 1
            elif category == "escalate":
                counts["escalated"] += 1
            else:
                counts["watched"] += 1
        return counts

    def last_seen(self) -> str | None:
        stamps = [item["last_seen"] for item in self.public_alerts() if item.get("last_seen")]
        return max(stamps) if stamps else None

    def finding_entry(self, stored: Mapping[str, Any]) -> dict[str, str]:
        return {key: str(stored[key]) for key in sorted(FINDING_KEYS)}

    def pending_relay_entries(self) -> list[dict[str, str]]:
        with self._lock:
            pending = [
                self.finding_entry(item)
                for item in self._load()["alerts"].values()
                if item.get("relay_status") == "pending"
            ]
        return sorted(pending, key=lambda item: (item["finding_id"], item["revision"]))

    def confirm_reported_relays(self, relay_ids: Sequence[str]) -> None:
        wanted = {parse_fingerprint(value) for value in relay_ids}
        if not wanted:
            return
        with self._lock:
            state = self._load()
            changed = False
            for item in state["alerts"].values():
                if item.get("relay_status") != "pending":
                    continue
                relay_id = grokbot_findings.identity_digest(item["producer"], item["finding_id"], item["revision"])
                if relay_id in wanted:
                    item["relay_status"] = "reported"
                    changed = True
            if changed:
                self._persist(state)

    def _apply_alert(
        self,
        state: dict[str, Any],
        record: Mapping[str, Any],
        decision: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        fingerprint = parse_fingerprint(record["fingerprint"])
        existing = state["alerts"].get(fingerprint)
        stamp = _iso(now)
        observed = str(record["observed_at"] or stamp)
        if existing is None:
            stored = _stored_alert(record, decision, first_seen=observed, last_seen=observed, count=1)
            stored["relay_status"] = "pending"
            created = True
            needs_relay = True
        else:
            stored = dict(existing)
            stored["count"] = int(existing["count"]) + 1
            stored["last_seen"] = observed
            stored["category"] = decision["category"]
            stored["reason"] = decision["reason"]
            semantic_changed = any(stored.get(key) != record.get(key) for key in _SEMANTIC_KEYS)
            if semantic_changed:
                stored["revision"] = parse_identifier(record["revision"])
                stored["severity"] = str(record["severity"])
                stored["title"] = str(record["title"])
                stored["body"] = str(record["body"])
                stored["source_digest"] = str(record["source_digest"])
                stored["content_digest"] = str(record["content_digest"])
                stored["observed_at"] = str(record["observed_at"])
                stored["relay_status"] = "pending"
            created = False
            needs_relay = semantic_changed or stored.get("relay_status") != "reported"
        state["alerts"][fingerprint] = stored
        if decision["category"] == "suppress" and decision.get("expires_at"):
            state["suppressions"][fingerprint] = {
                "reason": str(decision["reason"]),
                "fingerprint": fingerprint,
                "scope": str(record.get("rule_class") or "unknown"),
                "created_at": existing["first_seen"] if existing is not None else observed,
                "expires_at": str(decision["expires_at"]),
            }
        return {
            "created": created,
            "fingerprint": fingerprint,
            "count": stored["count"],
            "needs_relay": needs_relay,
        }

    def _ensure_state_dir(self) -> None:
        parent = -1
        try:
            parent = grokbot_ops._open_parent_nofollow(self._path, create=True)
            info = os.fstat(parent)
            if not stat.S_ISDIR(info.st_mode):
                _invalid()
            if hasattr(os, "fchmod"):
                os.fchmod(parent, 0o700)
                info = os.fstat(parent)
            _assert_secure_stat(info, directory=True)
        except WazuhError:
            raise
        except OSError as exc:
            raise WazuhError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        finally:
            if parent != -1:
                os.close(parent)

    def _empty(self) -> dict[str, Any]:
        return {"schema": STATE_SCHEMA, "alerts": {}, "suppressions": {}, "proposals": {}}

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(read_secure_text(self._path))
        except FileNotFoundError:
            return self._empty()
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WazuhError("protocol_error", ERROR_MESSAGES["protocol_error"]) from exc
        if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA or set(payload) != STATE_KEYS:
            _invalid()
        alerts = payload.get("alerts")
        suppressions = payload.get("suppressions")
        proposals = payload.get("proposals")
        if not isinstance(alerts, dict) or not isinstance(suppressions, dict) or not isinstance(proposals, dict):
            _invalid()
        if len(alerts) > MAX_ALERTS or len(suppressions) > MAX_SUPPRESSIONS or len(proposals) > MAX_PROPOSALS:
            _invalid()
        return {
            "schema": STATE_SCHEMA,
            "alerts": {key: dict(value) for key, value in alerts.items()},
            "suppressions": {key: dict(value) for key, value in suppressions.items()},
            "proposals": {key: dict(value) for key, value in proposals.items()},
        }

    def _persist(self, state: Mapping[str, Any]) -> None:
        if len(state["alerts"]) > MAX_ALERTS or len(state["suppressions"]) > MAX_SUPPRESSIONS:
            _unavailable()
        if len(state["proposals"]) > MAX_PROPOSALS:
            _unavailable()
        self._ensure_state_dir()
        body = json.dumps(
            {
                "schema": STATE_SCHEMA,
                "alerts": state["alerts"],
                "suppressions": state["suppressions"],
                "proposals": state["proposals"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            grokbot_ops._write_text_nofollow_atomic(self._path, body, mode=0o600)
        except OSError as exc:
            raise WazuhError("unavailable", ERROR_MESSAGES["unavailable"]) from exc


def _stored_alert(
    record: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    first_seen: str,
    last_seen: str,
    count: int,
) -> dict[str, Any]:
    if decision.get("category") not in CATEGORIES:
        _invalid()
    stored = {
        "producer": PRODUCER,
        "finding_id": parse_finding_id(record["finding_id"]),
        "revision": parse_identifier(record["revision"]),
        "observed_at": str(record["observed_at"]),
        "severity": str(record["severity"]),
        "title": str(record["title"]),
        "body": str(record["body"]),
        "source_ref": str(record["source_ref"]),
        "source_digest": str(record["source_digest"]),
        "content_digest": str(record["content_digest"]),
        "fingerprint": parse_fingerprint(record["fingerprint"]),
        "rule_class": parse_identifier(record["rule_class"]),
        "agent_id": parse_identifier(record["agent_id"]),
        "rule_id": parse_identifier(record["rule_id"], maximum=MAX_RULE_ID),
        "category": decision["category"],
        "reason": str(decision["reason"]),
        "first_seen": first_seen,
        "last_seen": last_seen,
        "count": count,
        "relay_status": "pending",
    }
    return stored


def _proposal_state(value: object) -> str:
    if value not in PROPOSAL_STATES:
        _invalid()
    return str(value)


def _require_time(value: object) -> str:
    if not is_offset_datetime(value):
        _invalid()
    return str(value)


def _is_expired(value: str, now: datetime) -> bool:
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return parsed <= stamp


def _iso(now: datetime) -> str:
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")
