"""Read configured Brigade ledgers through the public task command."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import toml_compat as tomllib
from . import worklore_client
from .fleet_client import FLEET_CONFIG_REL_PATH, brigade_home
from .worklore_brigade import BrigadeObservationError, IDENTITY_RE, normalize_task

COMMAND_TIMEOUT_SECONDS = 30
BATCH_SIZE = 500
MAX_REPORTED_SKIPPED = BATCH_SIZE
HUB_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")
HUB_CODE_BY_STATUS = {401: "hub-unauthorized", 403: "hub-unauthorized", 409: "hub-conflict"}
RECEIPT_UNSAFE = str.maketrans(
    {
        ":": "-",
        "/": "-",
        "\\": "-",
        "<": "-",
        ">": "-",
        '"': "-",
        "|": "-",
        "?": "-",
        "*": "-",
    }
)
FINGERPRINT_FIELDS = (
    "acceptance",
    "dependencies",
    "evidence_refs",
    "external_key",
    "external_state",
    # The source revision is part of what makes a batch this batch: two payloads that agree
    # on everything else but disagree on when the source last changed are different
    # observations, and must not collapse onto one idempotency key.
    "external_updated_at",
    "priority",
    "proposed_status",
    "source_policy",
    "title",
)


class BrigadeSyncError(Exception):
    """A Brigade discovery step failed with a bounded message."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Target:
    path: Path
    identity: str


class _TaskResult(list[dict[str, Any]]):
    """Normalized observations plus a bounded count of rejected tasks."""

    def __init__(self, observations: Sequence[dict[str, Any]] = (), *, skipped: int = 0) -> None:
        super().__init__(observations)
        self.skipped = min(max(skipped, 0), MAX_REPORTED_SKIPPED)


def _config_error(message: str) -> BrigadeSyncError:
    return BrigadeSyncError(message, code="field-bound")


def _adapter_failure() -> BrigadeSyncError:
    return BrigadeSyncError("adapter command failed", code="retryable-adapter-failure")


def load_brigade_config() -> dict[str, Any]:
    """Read ``[fleet.worklore.brigade]`` from the fleet config."""
    section = _read_brigade_section()
    if section is None:
        raise _config_error("Brigade Worklore config is missing targets")
    return {"targets": _parse_targets(section.get("targets"))}


def _read_brigade_section() -> dict[str, Any] | None:
    config_path = brigade_home() / FLEET_CONFIG_REL_PATH.name
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    fleet = payload.get("fleet") if isinstance(payload, dict) else None
    worklore = fleet.get("worklore") if isinstance(fleet, dict) else None
    brigade = worklore.get("brigade") if isinstance(worklore, dict) else None
    return brigade if isinstance(brigade, dict) else None


def _parse_targets(raw: object) -> list[Target]:
    if not isinstance(raw, list) or not raw:
        raise _config_error("targets must be a non-empty list")
    targets: list[Target] = []
    seen: set[tuple[Path, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise _config_error("targets items must be tables")
        path = _require_target_path(item.get("path"))
        identity = _require_identity(item.get("identity"))
        key = (path, identity)
        if key in seen:
            raise _config_error("targets path and identity are duplicated")
        seen.add(key)
        targets.append(Target(path=path, identity=identity))
    return targets


def _require_identity(value: object) -> str:
    if not isinstance(value, str) or not IDENTITY_RE.fullmatch(value):
        raise _config_error("identity must match the configured identity pattern")
    return value


def _require_target_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _config_error("path must be a non-empty string")
    resolved = Path(value).expanduser().resolve()
    if not resolved.is_dir():
        raise _config_error("path must exist and be a directory")
    return resolved


def _target_path(target: Path | Target) -> Path:
    return target.path if isinstance(target, Target) else Path(target)


def _public_edge_is_valid(edge: object) -> bool:
    if not isinstance(edge, Mapping):
        return False
    source = edge.get("source")
    dest = edge.get("target")
    edge_type = edge.get("type")
    return (
        isinstance(source, str)
        and bool(source.strip())
        and isinstance(dest, str)
        and bool(dest.strip())
        and isinstance(edge_type, str)
        and bool(edge_type.strip())
    )


def read_public_tasks(target: Path | Target) -> tuple[list[Any], list[Any]]:
    """Return ``(tasks, edges)`` from ``brigade work tasks --all --json``."""
    display = _target_path(target).expanduser().resolve()
    if not display.is_dir():
        raise _config_error("path must exist and be a directory")
    argv = ["brigade", "work", "tasks", "--target", str(display), "--all", "--json"]
    try:
        result = subprocess.run(  # noqa: S603 - argv is constructed, never shell-interpolated
            argv,
            timeout=COMMAND_TIMEOUT_SECONDS,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _adapter_failure() from None
    if result.returncode != 0:
        raise _adapter_failure()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise _adapter_failure() from None
    if not isinstance(payload, Mapping):
        raise _adapter_failure()
    tasks = payload.get("tasks")
    edges = payload.get("edges")
    if not isinstance(tasks, list) or not isinstance(edges, list):
        raise _adapter_failure()
    if not all(isinstance(task, Mapping) for task in tasks):
        raise _adapter_failure()
    if not all(_public_edge_is_valid(edge) for edge in edges):
        raise _adapter_failure()
    return tasks, edges


def observations_for_target(
    tasks: Mapping[str, Any] | list[object],
    *,
    edges: list[Mapping[str, Any]] | list[object] | None = None,
    identity: str,
) -> _TaskResult:
    """Normalize public Brigade tasks, skipping and counting the unsafe ones.

    A task the adapter cannot bound never reaches the hub and never aborts the
    other tasks in the same target; only its bounded count is reported.
    """
    items: list[object]
    if isinstance(tasks, Mapping):
        items = [tasks]
    elif isinstance(tasks, list):
        items = tasks
    else:
        raise _adapter_failure()
    observations = _TaskResult()
    for item in items:
        if not isinstance(item, Mapping):
            raise _adapter_failure()
        try:
            observations.append(normalize_task(item, identity=identity, edges=edges))
        except BrigadeObservationError:
            observations.skipped = min(observations.skipped + 1, MAX_REPORTED_SKIPPED)
    return observations


def _hub_error_code(exc: Exception) -> str:
    """Classify a hub refusal so a permanent 4xx is not retried as transient."""
    match = HUB_STATUS_RE.search(str(exc))
    if match is None:
        return "hub-unavailable"
    status = int(match.group(1))
    if not 400 <= status < 500:
        return "hub-unavailable"
    return HUB_CODE_BY_STATUS.get(status, "hub-rejected")


def _hub_error(code: str) -> BrigadeSyncError:
    return BrigadeSyncError(code, code=code)


def _receipt_write_failed() -> BrigadeSyncError:
    return BrigadeSyncError("receipt-write-failed", code="receipt-write-failed")


def _identity_conflict() -> BrigadeSyncError:
    return BrigadeSyncError("identity-conflict", code="identity-conflict")


def _safe_identity(identity: str) -> str:
    return identity.translate(RECEIPT_UNSAFE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _observation_fingerprint(observation: Mapping[str, Any]) -> str:
    payload = {field: observation.get(field) for field in FINGERPRINT_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _batch_idempotency_key(identity: str, observations: Sequence[Mapping[str, Any]]) -> str:
    fingerprints = sorted(_observation_fingerprint(item) for item in observations)
    digest = hashlib.sha256(
        json.dumps(fingerprints, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]
    return f"brigade:{identity}:{digest}"


def _chunks(observations: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if not observations:
        return [[]]
    return [observations[index : index + size] for index in range(0, len(observations), size)]


def _ensure_private_receipt_dir() -> Path:
    directory = brigade_home() / "worklore" / "brigade"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        directory.chmod(0o700)
        directory.parent.chmod(0o700)
    except OSError:
        pass
    return directory


def _write_receipt(
    identity: str,
    *,
    adapter_id: str,
    idempotency_key: str,
    observation_count: int,
    last_error_code: str | None,
    refused: int = 0,
    skipped: int = 0,
) -> None:
    directory = _ensure_private_receipt_dir()
    path = directory / f"{_safe_identity(identity)}.json"
    payload = {
        "adapter_id": adapter_id,
        "idempotency_key": idempotency_key,
        "last_error_code": last_error_code,
        "observation_count": observation_count,
        "refused": _as_count(refused),
        "skipped": min(max(skipped, 0), MAX_REPORTED_SKIPPED),
        "updated_at": _utc_now(),
    }
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_receipt_best_effort(
    identity: str,
    *,
    adapter_id: str,
    idempotency_key: str,
    observation_count: int,
    last_error_code: str | None,
    refused: int = 0,
    skipped: int = 0,
) -> None:
    try:
        _write_receipt(
            identity,
            adapter_id=adapter_id,
            idempotency_key=idempotency_key,
            observation_count=observation_count,
            last_error_code=last_error_code,
            refused=refused,
            skipped=skipped,
        )
    except OSError:
        return


def _require_targets(targets: object) -> list[Target]:
    if not isinstance(targets, list) or not targets:
        raise _config_error("targets must be a non-empty list")
    if not all(isinstance(item, Target) for item in targets):
        raise _config_error("targets must be a non-empty list")
    return targets


def _discover_groups(targets: list[Target]) -> tuple[list[tuple[str, list[dict[str, Any]], int]], int]:
    order: list[str] = []
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    skipped_by_identity: dict[str, int] = {}
    skipped = 0
    for target in targets:
        identity = target.identity
        if identity not in groups:
            order.append(identity)
            groups[identity] = {}
            skipped_by_identity[identity] = 0
        tasks, edges = read_public_tasks(target)
        observations = observations_for_target(tasks, edges=edges, identity=identity)
        target_skipped = min(max(int(getattr(observations, "skipped", 0)), 0), MAX_REPORTED_SKIPPED)
        skipped_by_identity[identity] = min(
            skipped_by_identity[identity] + target_skipped,
            MAX_REPORTED_SKIPPED,
        )
        skipped = min(skipped + target_skipped, MAX_REPORTED_SKIPPED)
        for observation in observations:
            key = str(observation["external_key"])
            existing = groups[identity].get(key)
            if existing is None:
                groups[identity][key] = observation
                continue
            if _observation_fingerprint(existing) != _observation_fingerprint(observation):
                raise _identity_conflict()
    return [
        (
            identity,
            sorted(groups[identity].values(), key=lambda item: str(item["external_key"])),
            skipped_by_identity[identity],
        )
        for identity in order
    ], skipped


def _add_hub_counts(totals: dict[str, int], response: object) -> None:
    if not isinstance(response, Mapping):
        return
    totals["created"] += _as_count(response.get("created"))
    totals["updated"] += _as_count(response.get("updated"))
    totals["unchanged"] += _as_count(response.get("unchanged"))
    totals["refused"] += _as_count(response.get("refused"))


def sync_brigade(targets: object) -> dict[str, Any]:
    """Read configured Brigade targets and post idempotent Worklore import batches."""
    parsed = _require_targets(targets)
    groups, skipped = _discover_groups(parsed)
    posted = 0
    observation_count = 0
    totals = {"created": 0, "updated": 0, "unchanged": 0, "refused": 0}
    for identity, observations, identity_skipped in groups:
        adapter_id = f"brigade:{identity}"
        identity_refused = 0
        for chunk in _chunks(observations, BATCH_SIZE):
            idempotency_key = _batch_idempotency_key(identity, chunk)
            envelope = {
                "adapter_id": adapter_id,
                "source_type": "brigade",
                "idempotency_key": idempotency_key,
                "observations": chunk,
            }
            try:
                response = worklore_client.import_batch(envelope)
            except worklore_client.FleetClientError as exc:
                code = _hub_error_code(exc)
                _write_receipt_best_effort(
                    identity,
                    adapter_id=adapter_id,
                    idempotency_key=idempotency_key,
                    observation_count=len(chunk),
                    last_error_code=code,
                    refused=identity_refused,
                    skipped=identity_skipped,
                )
                raise _hub_error(code) from None
            identity_refused += _as_count(response.get("refused")) if isinstance(response, Mapping) else 0
            _add_hub_counts(totals, response)
            try:
                _write_receipt(
                    identity,
                    adapter_id=adapter_id,
                    idempotency_key=idempotency_key,
                    observation_count=len(chunk),
                    last_error_code="hub-partial" if identity_refused else None,
                    refused=identity_refused,
                    skipped=identity_skipped,
                )
            except OSError:
                raise _receipt_write_failed() from None
            posted += 1
            observation_count += len(chunk)
    return {
        "targets": len(parsed),
        "identities": len(groups),
        "posted": posted,
        "observation_count": observation_count,
        "skipped": skipped,
        "created": totals["created"],
        "refused": totals["refused"],
        "updated": totals["updated"],
        "unchanged": totals["unchanged"],
    }
