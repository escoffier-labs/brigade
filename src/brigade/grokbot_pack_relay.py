"""Relay first-party steward ledger findings through generic owner delivery."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import grokbot_findings, grokbot_findings_relay, grokbot_ops, grokbot_packs
from .grokbot_backup.contracts import BackupError
from .grokbot_backup.ledger import BackupLedger
from .grokbot_findings import DEFAULT_LIMIT, FINDINGS_SCHEMA, FindingsError, MAX_ENTRIES
from .grokbot_fleet.contracts import FleetError
from .grokbot_fleet.ledger import FleetLedger
from .grokbot_wazuh.contracts import WazuhError
from .grokbot_wazuh.store import WazuhStore

PRODUCER_ORDER = ("backup-steward", "fleet-steward", "wazuh-triage")
LIVE_PRODUCERS = {
    "backup-steward": "backup",
    "fleet-steward": "fleet",
    "wazuh-triage": "wazuh",
}
PUBLIC_KEYS = (
    "apply",
    "eligible",
    "known",
    "created",
    "skipped",
    "pending",
    "reported",
    "limit",
    "relays",
)
RELAY_CONFIG_SCHEMA = "brigade.grokbot.pack-relay.v1"
RELAY_CONFIG_KEYS = frozenset({"schema", "owner_workspace"})
RELAY_CONFIG_DIR = Path(".brigade") / "grokbot"
RELAY_SERVICE_UNIT = "brigade-grokbot-findings-relay.service"
RELAY_TIMER_UNIT = "brigade-grokbot-findings-relay.timer"


class PackRelayError(ValueError):
    """A rejected first-party finding relay request."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def collect_manifests(target: Path) -> list[dict[str, Any]]:
    """Build in-memory generic manifests in producer order. Never writes files."""
    return [
        {"schema": FINDINGS_SCHEMA, "entries": _collect_entries(Path(target), pack_id)} for pack_id in PRODUCER_ORDER
    ]


def preview_relay(
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Count steward findings without writing drafts, markers, or manifests."""
    return _relay(Path(target), Path(owner), apply=False, limit=limit, now=None)


def apply_relay(
    target: Path,
    owner: Path,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Relay a bounded steward batch through generic findings and Fleet Hub."""
    return _relay(Path(target), Path(owner), apply=True, limit=limit, now=now)


def _relay(
    target: Path,
    owner: Path,
    *,
    apply: bool,
    limit: int,
    now: datetime | None,
) -> dict[str, Any]:
    try:
        bound = grokbot_findings._validate_limit(limit)
        grokbot_findings._validate_owner(owner)
    except FindingsError as exc:
        raise PackRelayError(exc.reason) from exc
    remaining = bound
    totals = {
        "apply": apply,
        "eligible": 0,
        "known": 0,
        "created": 0,
        "skipped": 0,
        "pending": 0,
        "reported": 0,
        "limit": bound,
        "relays": [],
    }
    relays: list[str] = []
    for manifest in collect_manifests(target):
        entries = list(manifest["entries"])
        for offset in range(0, len(entries), MAX_ENTRIES):
            chunk = entries[offset : offset + MAX_ENTRIES]
            try:
                if not apply or remaining <= 0:
                    preview = grokbot_findings_relay.relay_preview(chunk, target, owner, limit=max(remaining, 1))
                    totals["eligible"] += preview["eligible"]
                    totals["known"] += preview["known"]
                    if apply:
                        continue
                    relays.extend(preview["relays"][:remaining])
                    remaining = max(remaining - preview["eligible"], 0)
                    continue
                result = grokbot_findings_relay.relay_apply(chunk, target, owner, limit=remaining, now=now)
            except FindingsError as exc:
                raise PackRelayError(exc.reason) from exc
            totals["eligible"] += result["eligible"]
            totals["known"] += result["known"]
            totals["created"] += result["created"]
            totals["skipped"] += result["skipped"]
            totals["pending"] = result["pending"]
            totals["reported"] += result["reported"]
            relays.extend(result["relays"])
            remaining -= result["created"]
    totals["relays"] = _unique(relays)
    if apply:
        _confirm_wazuh(target)
    return {key: totals[key] for key in PUBLIC_KEYS}


def _collect_entries(target: Path, pack_id: str) -> list[dict[str, str]]:
    config = _optional_instance(target, pack_id)
    if config is None:
        return []
    ledger_path = config["ledger_path"]
    try:
        if pack_id == "backup-steward":
            return _backup_entries(str(ledger_path))
        if pack_id == "fleet-steward":
            return _fleet_entries(str(ledger_path))
        return _wazuh_entries(str(ledger_path))
    except FindingsError as exc:
        raise PackRelayError(exc.reason) from exc
    except (BackupError, FleetError, WazuhError) as exc:
        raise PackRelayError("unsafe-path") from exc


def _optional_instance(target: Path, pack_id: str) -> dict[str, Any] | None:
    path = grokbot_packs.instance_config_path(target, pack_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackRelayError("unsafe-config") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PackRelayError("unsafe-config")
    try:
        return grokbot_packs._load_instance_config(target, pack_id)
    except grokbot_packs.PackError as exc:
        raise PackRelayError(exc.reason) from exc


def _backup_entries(ledger_path: str) -> list[dict[str, str]]:
    ledger = BackupLedger(ledger_path)
    entries = []
    for index, record in enumerate(ledger.finding_records()):
        finding = record["finding"]
        revision = record["revision"]
        entries.append(
            grokbot_findings.adapt_live_finding(
                {
                    "producer": LIVE_PRODUCERS["backup-steward"],
                    "finding_id": finding["finding_id"],
                    "revision": revision,
                    "observed_at": finding["observed_at"],
                    "severity": finding["severity_class"],
                    "title": finding["summary"],
                    "body": finding["summary"],
                    "source_ref": f"backup:{finding['finding_id']}",
                    "source_digest": revision,
                    "trust": "untrusted",
                    "delivery": "review-only",
                },
                index=index,
            )
        )
    return entries


def _fleet_entries(ledger_path: str) -> list[dict[str, str]]:
    ledger = FleetLedger(ledger_path)
    entries = []
    for index, finding in enumerate(ledger.findings()):
        revision = _fleet_revision(finding)
        entries.append(
            grokbot_findings.adapt_live_finding(
                {
                    "producer": LIVE_PRODUCERS["fleet-steward"],
                    "finding_id": finding["finding_id"],
                    "revision": revision,
                    "observed_at": finding.get("observed_at") or "",
                    "severity": "unknown",
                    "title": finding["reason"],
                    "body": finding["reason"],
                    "source_ref": f"fleet:{finding['finding_id']}",
                    "source_digest": revision,
                    "trust": "untrusted",
                    "delivery": "review-only",
                },
                index=index,
            )
        )
    return entries


def _wazuh_entries(ledger_path: str) -> list[dict[str, str]]:
    store = WazuhStore(ledger_path)
    return [dict(entry) for entry in store.pending_relay_entries()]


def _confirm_wazuh(target: Path) -> None:
    config = _optional_instance(target, "wazuh-triage")
    if config is None:
        return
    try:
        reported = [
            record["relay_id"]
            for record in grokbot_findings_relay._read_outbox_records(target)
            if record.get("status") == "reported"
        ]
        WazuhStore(str(config["ledger_path"])).confirm_reported_relays(reported)
    except (FindingsError, WazuhError) as exc:
        reason = exc.reason if isinstance(exc, FindingsError) else "unsafe-path"
        raise PackRelayError(reason) from exc


def _fleet_revision(finding: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(finding), separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def relay_config_path(target: Path) -> Path:
    return Path(target) / RELAY_CONFIG_DIR / "relay.json"


def preview_relay_setup(target: Path, owner_workspace: Path) -> dict[str, Any]:
    return _setup_relay(Path(target), Path(owner_workspace), apply=False)


def apply_relay_setup(target: Path, owner_workspace: Path) -> dict[str, Any]:
    return _setup_relay(Path(target), Path(owner_workspace), apply=True)


def load_relay_config(target: Path) -> dict[str, str]:
    path = relay_config_path(target)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PackRelayError("missing-config") from exc
    except OSError as exc:
        raise PackRelayError("unsafe-config") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PackRelayError("unsafe-config")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PackRelayError("unsafe-config")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PackRelayError("unsafe-config")
    try:
        payload = json.loads(grokbot_ops._read_regular_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackRelayError("unsafe-config") from exc
    if not isinstance(payload, dict) or set(payload) != RELAY_CONFIG_KEYS:
        raise PackRelayError("unsafe-config")
    if payload.get("schema") != RELAY_CONFIG_SCHEMA:
        raise PackRelayError("unsafe-config")
    owner = payload.get("owner_workspace")
    if not isinstance(owner, str) or not owner:
        raise PackRelayError("invalid-owner")
    validated = _validate_owner_workspace(Path(owner))
    return {"schema": RELAY_CONFIG_SCHEMA, "owner_workspace": str(validated)}


def preview_configured_relay(target: Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    config = load_relay_config(target)
    return preview_relay(target, Path(config["owner_workspace"]), limit=limit)


def apply_configured_relay(target: Path, limit: int = DEFAULT_LIMIT, now: datetime | None = None) -> dict[str, Any]:
    config = load_relay_config(target)
    return apply_relay(target, Path(config["owner_workspace"]), limit=limit, now=now)


def relay_doctor(target: Path) -> list[dict[str, str]]:
    try:
        load_relay_config(target)
    except PackRelayError as exc:
        status = "fail" if exc.reason != "missing-config" else "fail"
        return [{"check": "config", "status": status}, {"check": "owner", "status": "fail"}]
    return [{"check": "config", "status": "ok"}, {"check": "owner", "status": "ok"}]


def render_relay_units(target: Path, *, python: str | None = None) -> dict[str, str]:
    units, secrets = _render_relay_units(target, python=python)
    return {name: _redact_preview_paths(text, secrets) for name, text in units.items()}


def _render_relay_units(target: Path, *, python: str | None = None) -> tuple[dict[str, str], list[str]]:
    config = load_relay_config(target)
    owner = Path(config["owner_workspace"]).resolve()
    root = Path(target).resolve()
    executable = python or sys.executable
    args = [
        executable,
        "-m",
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "pack",
        "relay",
        "--target",
        str(root),
        "--apply",
        "--limit",
        "50",
    ]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    writable = [
        str((root / grokbot_ops.QUEUE_STATE_DIR).resolve()),
        str(owner),
    ]
    secrets = [str(root), str(owner), executable, str(Path(executable).parent)]
    wazuh = _optional_instance(root, "wazuh-triage")
    if wazuh is not None:
        ledger = Path(wazuh["ledger_path"]).resolve()
        writable.append(str(ledger.parent))
        secrets.extend((str(ledger), str(ledger.parent)))
    read_write = " ".join(grokbot_ops._systemd_quote(path) for path in _unique(writable))
    service = (
        "# Generated by brigade run cloud grokbot pack install-relay-service.\n"
        f"# Unit: {RELAY_SERVICE_UNIT}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot first-party findings relay\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
        "UMask=0077\n"
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n"
        f"ReadWritePaths={read_write}\n"
        "TimeoutStartSec=120\n"
    )
    timer = (
        "# Generated by brigade run cloud grokbot pack install-relay-service.\n"
        f"# Unit: {RELAY_TIMER_UNIT}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot first-party findings relay timer\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=5min\n"
        "RandomizedDelaySec=30s\n"
        "AccuracySec=30s\n"
        "Persistent=false\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {RELAY_SERVICE_UNIT: service, RELAY_TIMER_UNIT: timer}, _unique(secrets)


def _redact_preview_paths(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def write_relay_units(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> list[Path]:
    units, _secrets = _render_relay_units(target, python=python)
    planned: list[tuple[Path, str, str | None, tuple[str, int] | None]] = []
    for name, rendered in units.items():
        path = Path(out_dir) / name
        existing, snapshot = _preflight_unit_destination(path, rendered, force=force)
        planned.append((path, rendered, existing, snapshot))
    written: list[Path] = []
    try:
        for path, rendered, existing, _snapshot in planned:
            if existing == rendered:
                written.append(path)
                continue
            grokbot_ops._write_text_nofollow_atomic(
                path,
                rendered,
                mode=0o644,
                replace_symlink=force,
                replace=force or existing is not None,
            )
            written.append(path)
    except OSError as exc:
        _rollback_unit_destinations([(path, snapshot) for path, _rendered, _existing, snapshot in planned])
        raise PackRelayError("unsafe-path") from exc
    return written


def _preflight_unit_destination(path: Path, rendered: str, *, force: bool) -> tuple[str | None, tuple[str, int] | None]:
    try:
        existing = grokbot_ops._read_regular_text(path)
        snapshot = (existing, stat.S_IMODE(path.lstat().st_mode))
    except FileNotFoundError:
        existing = None
        snapshot = None
    except OSError as exc:
        if not force or not grokbot_ops._path_is_symlink(path):
            raise PackRelayError("unsafe-path") from exc
        existing = None
        snapshot = None
    if existing is not None and existing != rendered and not force:
        raise PackRelayError("unsafe-path")
    return existing, snapshot


def _rollback_unit_destinations(items: list[tuple[Path, tuple[str, int] | None]]) -> None:
    for path, snapshot in items:
        if snapshot is None:
            if grokbot_ops._path_is_symlink(path) or path.is_symlink():
                raise PackRelayError("unsafe-path")
            try:
                grokbot_ops.remove_regular_file(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PackRelayError("unsafe-path") from exc
            continue
        text, mode = snapshot
        try:
            current = grokbot_ops._read_regular_text(path)
            current_mode = stat.S_IMODE(path.lstat().st_mode)
        except FileNotFoundError:
            current = None
            current_mode = None
        except OSError as exc:
            raise PackRelayError("unsafe-path") from exc
        if current == text and current_mode == mode:
            continue
        try:
            grokbot_ops._write_text_nofollow_atomic(path, text, mode=mode)
        except OSError as exc:
            raise PackRelayError("unsafe-path") from exc


def _setup_relay(target: Path, owner_workspace: Path, *, apply: bool) -> dict[str, Any]:
    _validate_owner_workspace(owner_workspace)
    result = {
        "action": "relay-setup",
        "apply": apply,
        "writes": [str(RELAY_CONFIG_DIR / "relay.json")],
    }
    if not apply:
        return result
    payload = {
        "schema": RELAY_CONFIG_SCHEMA,
        "owner_workspace": str(_validate_owner_workspace(owner_workspace.resolve())),
    }
    path = relay_config_path(target)
    try:
        grokbot_ops._write_text_nofollow_atomic(
            path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )
    except OSError as exc:
        raise PackRelayError("unsafe-config") from exc
    return result


def _validate_owner_workspace(owner: Path) -> Path:
    path = Path(owner)
    if not path.is_absolute():
        raise PackRelayError("invalid-owner")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PackRelayError("invalid-owner") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PackRelayError("invalid-owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise PackRelayError("invalid-owner")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PackRelayError("invalid-owner")
    try:
        grokbot_findings._validate_owner(path)
    except FindingsError as exc:
        raise PackRelayError(exc.reason) from exc
    return path.resolve()
