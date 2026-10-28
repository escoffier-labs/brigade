"""Opt-in node broker. One-off user systemd jobs own long provider turns."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import fleet_action_adapters as adapters, fleet_client, fleet_client_actions as client
from .fleet_action_auth import ActionError, canonical, digest, opaque, selector
from .fleet_hub_messages import MAX_SQLITE_INTEGER
from .isolated_exec import package_argv, trusted_cwd

MANIFEST_SCHEMA = "brigade.fleet_action_manifest.v1"
AUTOMATIC_RECOVERY_LIFETIME = 24 * 60 * 60
DEFAULT_EXECUTABLES = {
    "t3": "t3-fleet",
    "openclaw": "openclaw",
    "brigade": "brigade",
    "systemd-run": "systemd-run",
    "systemctl": "systemctl",
}


def read_protected(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise ActionError("manifest-not-private", 400)
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ActionError("manifest-too-large", 400)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ActionError("invalid-manifest", 400)
    return data


def load_manifest(path: Path) -> dict[str, Any]:
    data = read_protected(path)
    if data.get("schema") != MANIFEST_SCHEMA or set(data) - {"schema", "journal_dir", "targets", "executables"}:
        raise ActionError("invalid-manifest", 400)
    journal_dir = data.get("journal_dir")
    if not isinstance(journal_dir, str) or not Path(journal_dir).is_absolute() or "\x00" in journal_dir:
        raise ActionError("invalid-journal-path", 400)
    try:
        os.fsencode(journal_dir)
    except UnicodeEncodeError:
        raise ActionError("invalid-journal-path", 400) from None
    if not isinstance(data.get("targets"), list) or len(data["targets"]) > 100:
        raise ActionError("invalid-targets", 400)
    tools = data.get("executables", {})
    if not isinstance(tools, dict) or set(tools) - set(DEFAULT_EXECUTABLES):
        raise ActionError("invalid-executables", 400)
    executables = {}
    for key, default in DEFAULT_EXECUTABLES.items():
        value = tools[key] if key in tools else shutil.which(default) or default
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ActionError("invalid-executable", 400)
        try:
            os.fsencode(value)
        except UnicodeEncodeError:
            raise ActionError("invalid-executable", 400) from None
        executables[key] = value
    clean = []
    for item in data["targets"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"public", "binding"}
            or not isinstance(item["public"], dict)
            or not isinstance(item["binding"], dict)
        ):
            raise ActionError("invalid-binding", 400)
        public, binding = dict(item["public"]), item["binding"]
        opaque(public.get("target_id"))
        family = public.get("family")
        if family not in {"t3", "openclaw", "grokbot", "dot"}:
            raise ActionError("invalid-family", 400)
        if family == "t3":
            if set(binding) - {
                "host",
                "repository",
                "source_revision",
                "override_reason",
                "repo_identity",
                "runtime_mode",
            } or not {
                "host",
                "repository",
                "source_revision",
                "repo_identity",
            } <= set(binding):
                raise ActionError("invalid-binding", 400)
            if "override_reason" in binding:
                reason = binding["override_reason"]
                if not isinstance(reason, str) or len(reason) > 200 or "\x00" in reason:
                    raise ActionError("invalid-override-reason", 400)
                try:
                    reason.encode("utf-8")
                except UnicodeEncodeError:
                    raise ActionError("invalid-override-reason", 400) from None
            if not isinstance(binding.get("runtime_mode", "approval-required"), str) or binding.get(
                "runtime_mode", "approval-required"
            ) not in {
                "approval-required",
                "auto-accept-edits",
                "auto",
                "full-access",
            }:
                raise ActionError("invalid-runtime-mode", 400)
            opaque(binding["host"])
            opaque(binding["repository"])
            for key in ("host", "repository", "source_revision", "repo_identity"):
                if key not in public or public[key] != binding[key]:
                    raise ActionError("native-binding-mismatch", 400)
            if (
                not isinstance(binding["source_revision"], str)
                or re.fullmatch(r"[0-9a-f]{40}", binding["source_revision"]) is None
            ):
                raise ActionError("full-source-revision-required", 400)
        elif family == "openclaw":
            if "agent_id" not in binding or set(binding) - {"agent_id", "session_key"}:
                raise ActionError("explicit-agent-required", 400)
            if public.get("agent_id") != binding["agent_id"] or (
                ("session_key" in public) != ("session_key" in binding)
                or public.get("session_key") != binding.get("session_key")
            ):
                raise ActionError("native-binding-mismatch", 400)
        elif binding:
            raise ActionError("inbox-binding-must-be-empty", 400)
        from .fleet_action_targets import native_identity

        public["binding_digest"] = digest(canonical(native_identity(public)))
        for source in public.get("subjects", []):
            selector(source)
        clean.append({"public": public, "binding": binding})
    if len({t["public"]["target_id"] for t in clean}) != len(clean):
        raise ActionError("duplicate-target", 400)
    return {**data, "targets": clean, "executables": executables}


def _tool(manifest: dict[str, Any], family: str) -> Path:
    return Path(manifest["executables"]["brigade" if family in {"grokbot", "dot"} else family])


def _exists(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _projection(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not _exists(_tool(manifest, "systemd-run")) or not _exists(_tool(manifest, "systemctl")):
        return []
    return [t["public"] for t in manifest["targets"] if _exists(_tool(manifest, t["public"]["family"]))]


def sync_targets(path: Path) -> dict[str, Any]:
    manifest = load_manifest(path)
    return client.request({"op": "targets-sync", "targets": _projection(manifest)})


@contextmanager
def _journal_lock(root: Path, name: str) -> Iterator[int]:
    # A nonblocking owner lock keeps a telemetry tick from waiting behind a long turn.
    if os.name != "posix":
        raise ActionError("transient-executor-unavailable", 503)
    import fcntl

    directory = fleet_client._ensure_private_dir(root)
    if directory is None:
        raise ActionError("private-journal-unavailable", 503)
    fd = -1
    try:
        if os.fstat(directory).st_uid != os.getuid():
            raise ActionError("journal-owner-refused")
        fd = fleet_client._open_private(root / (name + ".lock"), os.O_RDWR | os.O_CREAT, dir_fd=directory)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ActionError("action-busy", 409) from None
        yield directory
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory)


def _read(root: Path, name: str, directory: int) -> dict[str, Any]:
    rows = fleet_client._read_spool(root / (name + ".jsonl"), dir_fd=directory, strict=True)
    if not rows:
        return {}
    if len(rows) != 1:
        raise ActionError("journal-corrupt", 409)
    return rows[0]


def _write(root: Path, name: str, record: dict[str, Any], directory: int) -> None:
    fleet_client._write_spool_atomic(root / (name + ".jsonl"), [record], dir_fd=directory)


def _report(action_id: str, generation: int, receipt: dict[str, Any]) -> dict[str, Any]:
    event_id = receipt.get("event_id") or "result_" + digest(canonical(receipt))[:32]
    return client.request(
        {"op": "report", "action_id": action_id, "generation": generation, "receipt": {**receipt, "event_id": event_id}}
    )


def _deliver_report(
    root: Path, action_id: str, generation: int, record: dict[str, Any], directory: int
) -> dict[str, Any]:
    try:
        result = _report(action_id, generation, record["receipt"])
    except ActionError as exc:
        if 400 <= exc.status < 500 and exc.status not in {408, 429}:
            rejections = record.setdefault("rejections", [])
            rejections.append({"reason": exc.reason, "status": exc.status, "receipt": record["receipt"]})
            record["rejections"] = rejections[-100:]
            record["phase"] = (
                "rejected"
                if exc.reason
                in {
                    "transition-refused",
                    "invalid-receipt",
                    "invalid-status",
                    "invalid-delivery",
                    "destination-reference-required",
                    "invalid-rejection",
                }
                else "held"
            )
            record["hold_reason"], record["hold_status"] = exc.reason, exc.status
            _write(root, action_id, record, directory)
        raise
    record["phase"] = "reported"
    if result["status"] in {"running", "needs-input"}:
        record["execution_state"] = result["status"]
    _write(root, action_id, record, directory)
    return result


def _inbox_lookup(
    action: dict[str, Any], executable: Path, root: Path, record: dict[str, Any], directory: int
) -> dict[str, Any]:
    # Read only after a durable send attempt. A missing acknowledgement never permits another send or flush.
    cursor = record.get("inbox_lookup_after", 0)
    if type(cursor) is not int or not 0 <= cursor <= MAX_SQLITE_INTEGER:
        cursor = 0
    for _ in range(10):
        payload = adapters.invoke(executable, ["fleet", "msg", "sent", "--after", str(cursor), "--json"], timeout=15)
        messages = payload.get("messages", [])
        if not isinstance(messages, list) or not messages:
            break
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("client_id") == action["action_id"]
                and type(message.get("id")) is int
            ):
                return {
                    "status": "accepted",
                    "delivery": "acknowledged" if message.get("acked_at") else "delivered",
                    "destination_ref": str(message["id"]),
                }
        next_cursor = payload.get("next_cursor")
        if type(next_cursor) is not int or not cursor < next_cursor <= MAX_SQLITE_INTEGER:
            break
        cursor = next_cursor
        # Checkpoint each observed page under the action lock, including before a later transport failure.
        record["inbox_lookup_after"] = cursor
        _write(root, action["action_id"], record, directory)
    return {"status": "unknown"}


def _recovery_expired(started_at: Any, *, now: float | None = None) -> bool:
    """Fail closed when the Hub's fixed start time cannot establish a safe age."""
    if started_at is None:
        return False
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return True
    try:
        timestamp = float(started_at)
    except (OverflowError, TypeError, ValueError):
        return True
    if not math.isfinite(timestamp) or timestamp <= 0:
        return True
    return (time.time() if now is None else now) >= timestamp + AUTOMATIC_RECOVERY_LIFETIME


def execute(path: Path, action_id: str, generation: int) -> dict[str, Any]:
    opaque(action_id)
    if type(generation) is not int or generation < 1:
        raise ActionError("invalid-generation", 400)
    manifest = load_manifest(path)
    root = Path(manifest["journal_dir"])
    with _journal_lock(root, action_id) as directory:
        record = _read(root, action_id, directory)
        if record.get("phase") == "held":
            raise ActionError(record["hold_reason"], record["hold_status"])
        if generation in record.get("fenced_generations", []):
            raise ActionError("fence-refused", 409)
        action = client.request({"op": "show", "action_id": action_id})
        if action["generation"] != generation:
            record["fenced_generations"] = (record.get("fenced_generations", []) + [generation])[-100:]
            _write(root, action_id, record, directory)
            raise ActionError("fence-refused", 409)
        target = action["request"]["target"]
        local = next((t for t in manifest["targets"] if t["public"]["target_id"] == target["target_id"]), None)
        if local is None or local["public"]["binding_digest"] != target["binding_digest"]:
            raise ActionError("binding-changed", 409)
        executable = _tool(manifest, target["family"])
        expired = _recovery_expired(action["started_at"])
        if record.get("receipt") and record.get("phase") == "result":
            if expired:
                record["recovery_parked"] = True
                _write(root, action_id, record, directory)
            return _deliver_report(root, action_id, generation, record, directory)
        if record.get("recovery_parked") is True or (
            action["started_at"] is not None and record.get("phase") == "parked"
        ):
            return action
        if expired:
            record["phase"] = "parked"
            record["recovery_parked"] = True
            _write(root, action_id, record, directory)
            return action
        if action["started_at"] is None and record.get("phase") in {"starting", "awaiting-reclaim"}:
            record["phase"] = "awaiting-reclaim"
            _write(root, action_id, record, directory)
            return action
        if action["status"] in {"finished", "failed", "expired", "canceled"}:
            return action
        if (
            record.get("receipt")
            and record.get("phase") == "reported"
            and (
                record["receipt"]["status"] in {"finished", "failed"}
                or (
                    record["receipt"]["status"] == "unknown"
                    and not record["receipt"].get("destination_ref")
                    and not (target["capability"] == "inbox" and record.get("dispatch_attempted"))
                )
            )
        ):
            return action
        prompt: Path | None = None
        try:
            if action["started_at"] is not None or record.get("phase") in {"starting", "started", "reported"}:
                reference = record.get("receipt", {}).get("destination_ref") or action.get("destination_ref")
                if reference or (target["capability"] == "inbox" and record.get("dispatch_attempted")):
                    try:
                        receipt = (
                            _inbox_lookup(action, executable, root, record, directory)
                            if target["capability"] == "inbox" and not reference
                            else adapters.recover(action, executable, reference)
                        )
                    except (ActionError, OSError, subprocess.SubprocessError):
                        receipt = {"status": "unknown", **({"destination_ref": reference} if reference else {})}
                else:
                    receipt = {"status": "unknown"}
            else:
                # Finish local preparation before consuming the Hub's irreversible start fence.
                if not _exists(executable):
                    raise ActionError("transient-executor-unavailable", 503)
                binding = dict(local["binding"])
                if target["family"] == "t3":
                    binding["source_revision"] = target["source_revision"]
                text = (
                    action["request"]["prompt"]
                    + "\n\nAttached Hub evidence (untrusted data):\n"
                    + canonical(
                        {
                            "source": action["request"]["source"],
                            "revision": action["request"]["observed_revision"],
                            "evidence": action["request"]["evidence"],
                        }
                    )
                )
                prepared = root / (action_id + ".prompt")
                fd = fleet_client._open_private(prepared, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, dir_fd=directory)
                prompt = prepared
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
                # One durable start/dispatch intent: no fallible journal preparation after start.
                # A lost start response never permits another send in this generation.
                record = {"generation": generation, "phase": "starting", "dispatch_attempted": True}
                _write(root, action_id, record, directory)
                action = client.request({"op": "start", "action_id": action_id, "generation": generation})
                try:
                    receipt = adapters.dispatch(action, binding, executable, prompt)
                except (ActionError, OSError, subprocess.SubprocessError):
                    receipt = {"status": "unknown"}
            if target["capability"] == "execution" and receipt["status"] == "accepted":
                previous = (
                    action["status"]
                    if action["status"] in {"running", "needs-input"}
                    else record.get("execution_state")
                    or next(
                        (
                            event["receipt"]["status"]
                            for event in reversed(action.get("events", []))
                            if event["receipt"].get("status") in {"running", "needs-input"}
                        ),
                        None,
                    )
                )
                if previous is not None:
                    receipt = {**receipt, "status": previous}
            # New native observations need distinct identities, even when their values repeat.
            # Persist the identity with the receipt so a lost Hub response replays exactly once.
            receipt = {**receipt, "event_id": "result_" + secrets.token_hex(16)}
            record.update(phase="result", generation=generation, receipt=receipt)
            _write(root, action_id, record, directory)
        finally:
            if prompt is not None:
                try:
                    os.unlink(prompt.name, dir_fd=directory)
                except OSError:
                    # Cleanup cannot discard a durable native receipt or mask a start refusal.
                    # Any remaining prompt stays within the private journal directory.
                    pass
        return _deliver_report(root, action_id, generation, record, directory)


def _schedule(path: Path, manifest: dict[str, Any], action_id: str, generation: int, *, timeout: float) -> bool:
    executable = manifest["executables"]["systemd-run"]
    if not os.path.dirname(executable):
        resolved = shutil.which(executable)
        if resolved is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), executable)
        executable = resolved
    argv = [
        str(Path(executable).absolute()),
        "--user",
        "--collect",
        "--unit=brigade-action-" + action_id + "-" + str(generation),
        "--property=Type=exec",
        "--property=UMask=0077",
        "--property=RuntimeMaxSec=3900",
        "--working-directory=" + str(trusted_cwd()),
        "--",
        *package_argv(
            trusted_cwd() / "__init__.py",
            "brigade",
            "fleet",
            "actions",
            "execute",
            action_id,
            "--generation",
            str(generation),
            "--manifest",
            str(path.resolve()),
            cwd_after_load=Path.cwd(),
        ),
    ]
    result = subprocess.run(
        argv,
        cwd=trusted_cwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        shell=False,
    )
    return result.returncode == 0


def _reconcile(path: Path, manifest: dict[str, Any], deadline: float, tick_directory: int) -> int:
    root = Path(manifest["journal_dir"])
    cursor = _read(root, "tick-state", tick_directory).get("after", "")
    entries = sorted(root.glob("aa_*.jsonl"))
    entries = [e for e in entries if e.name > cursor] + [e for e in entries if e.name <= cursor]
    for entry in entries[:100]:
        if time.monotonic() + 5 >= deadline:
            break
        _write(root, "tick-state", {"after": entry.name}, tick_directory)
        action_id = entry.stem
        try:
            with _journal_lock(root, action_id) as directory:
                record = _read(root, action_id, directory)
                phase = record.get("phase")
                if (
                    phase in {"closed", "obsolete", "parked", "awaiting-reclaim", "held"}
                    or record.get("recovery_parked") is True
                    or record.get("next_reconcile_at", 0) > time.time()
                ):
                    continue
                action = client.request(
                    {"op": "show", "action_id": action_id}, timeout=min(2, deadline - time.monotonic())
                )
                if action["status"] in {"finished", "failed", "expired", "canceled"}:
                    if phase == "result" and record.get("receipt"):
                        record["recovery_parked"] = True
                    else:
                        record["phase"] = "closed"
                elif action["generation"] != record.get("generation"):
                    record["phase"] = "obsolete"
                elif action["started_at"] is not None:
                    recovery_expired = _recovery_expired(action["started_at"])
                    if recovery_expired:
                        if phase == "result" and record.get("receipt"):
                            record["recovery_parked"] = True
                        else:
                            record["phase"] = "parked"
                    else:
                        receipt = record.get("receipt", {})
                        inbox = action["request"]["target"]["capability"] == "inbox"
                        if inbox and receipt.get("delivery") == "acknowledged":
                            record["phase"] = "parked"
                        elif phase in {"starting", "started", "result", "rejected"} or (
                            phase == "reported"
                            and (
                                receipt.get("status") in {"accepted", "running", "needs-input"}
                                or receipt.get("destination_ref")
                                or (inbox and record.get("dispatch_attempted") and receipt.get("status") == "unknown")
                            )
                        ):
                            record["next_reconcile_at"] = time.time() + (300 if inbox else 30)
                            _write(root, action_id, record, directory)
                            return int(
                                _schedule(
                                    path,
                                    manifest,
                                    action_id,
                                    record["generation"],
                                    timeout=min(2, deadline - time.monotonic()),
                                )
                            )
                elif action["started_at"] is None:
                    if phase == "starting":
                        record["phase"] = "awaiting-reclaim"
                    # A claim that never started is reclaimable only by the Hub's next generation.
                _write(root, action_id, record, directory)
        except (ActionError, fleet_client.FleetClientError, OSError, subprocess.SubprocessError):
            continue
    return 0


def tick(path: Path, *, budget_seconds: float = 15, max_actions: int = 1) -> dict[str, Any]:
    if not 0 < budget_seconds <= 15 or max_actions != 1:
        raise ActionError("invalid-tick-budget", 400)
    deadline = time.monotonic() + budget_seconds
    manifest = load_manifest(path)
    root = Path(manifest["journal_dir"])
    try:
        with _journal_lock(root, "tick") as tick_directory:
            projection = _projection(manifest)
            ready = False
            if projection:
                try:
                    result = subprocess.run(
                        [str(_tool(manifest, "systemctl")), "--user", "show-environment"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=min(2, max(0.01, deadline - time.monotonic())),
                        check=False,
                        shell=False,
                    )
                    ready = result.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    pass
            client.request(
                {"op": "targets-sync", "targets": projection if ready else []},
                timeout=min(2.5, max(0.01, deadline - time.monotonic())),
            )
            if not ready:
                return {"available": False, "scheduled": 0}
            reconciled = _reconcile(path, manifest, deadline, tick_directory)
            if time.monotonic() + 3 >= deadline:
                return {"available": True, "scheduled": 0}
            action = client.request({"op": "claim"}, timeout=min(2.5, deadline - time.monotonic())).get("action")
            if action is None:
                return {"available": True, "scheduled": reconciled, "new_actions": 0, "reconciled": reconciled}
            action_id, generation = action["action_id"], action["generation"]
            with _journal_lock(root, opaque(action_id)) as directory:
                _write(root, action_id, {"generation": generation, "phase": "scheduled"}, directory)
                scheduled = _schedule(
                    path, manifest, action_id, generation, timeout=min(2, max(0.01, deadline - time.monotonic()))
                )
            return {
                "available": True,
                "scheduled": int(scheduled) + reconciled,
                "new_actions": int(scheduled),
                "reconciled": reconciled,
            }
    except ActionError as exc:
        if exc.reason == "action-busy":
            return {"available": True, "scheduled": 0}
        raise
