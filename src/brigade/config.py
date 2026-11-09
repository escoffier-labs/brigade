"""Read/write .brigade/config.json - the per-target source of truth."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .selection import Selection


WORKSPACE_DIRNAME = ".brigade"
LEGACY_WORKSPACE_DIRNAMES = (".solo-mise",)
CONFIG_REL_PATH = f"{WORKSPACE_DIRNAME}/config.json"
SUPPORTED_VERSIONS = (1,)
DEFAULT_CODE_GRAPH_DELTA_TIMEOUT_SECONDS = 10.0
CODE_GRAPH_DELTA_TIMEOUT_KEY = "code_graph_delta_timeout_seconds"
# Legacy key read through v0.32 when the canonical key is absent; the writer
# never emits it (docs/phase-engine-rename.md, R4).
LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY = "graphtrail_delta_timeout_seconds"
CAPTURE_BEFORE_RETRY_MODES = ("warn", "block", "off")
DEFAULT_CAPTURE_BEFORE_RETRY = "warn"
DEFAULT_VERIFY_RUNS_KEEP = 50
DEFAULT_VERIFY_ARCHIVE_ENABLED = True
DEFAULT_VERIFY_ARCHIVE_DIR = ".brigade/work/verify-archive"
DEFAULT_RUN_LOCK_WAIT_SECONDS = 0.0
# Work-loop mode (#2233). Safety rails are not a repository setting:
# their opt-out lives in a per-user record outside the repo (see work_mode).
WORK_MODES = ("engineer", "yolo")
DEFAULT_WORK_MODE = "engineer"
# Earlier name for engineer mode. Still read from configs and accepted by
# `brigade work mode set`; the writer never emits it.
LEGACY_WORK_MODE_ALIASES = {"enforced": "engineer"}


@dataclass
class Config:
    version: int
    selection: Selection
    code_graph_delta_timeout_seconds: float = DEFAULT_CODE_GRAPH_DELTA_TIMEOUT_SECONDS
    capture_before_retry: str = DEFAULT_CAPTURE_BEFORE_RETRY
    verify_runs_keep: int = DEFAULT_VERIFY_RUNS_KEEP
    verify_archive_enabled: bool = DEFAULT_VERIFY_ARCHIVE_ENABLED
    verify_archive_dir: str = DEFAULT_VERIFY_ARCHIVE_DIR
    run_lock_wait_seconds: float = DEFAULT_RUN_LOCK_WAIT_SECONDS
    # Machine-local hub or read-only mirror for session-start recall (#466).
    # Absent on repo installs until the operator sets an explicit path.
    memory_recall_target: str | None = None
    run_record: str = "metadata-only"
    work_mode: str = DEFAULT_WORK_MODE


def validate_work_mode(value: Any) -> str:
    """Return the canonical mode for ``value``; legacy names map to their current mode."""
    if isinstance(value, str):
        value = LEGACY_WORK_MODE_ALIASES.get(value, value)
    if not isinstance(value, str) or value not in WORK_MODES:
        raise ValueError(f"work.mode must be one of: {', '.join(WORK_MODES)}")
    return value


def validate_run_record(value: Any) -> str:
    if not isinstance(value, str) or value not in {"auto", "metadata-only", "off"}:
        raise ValueError("run.record must be auto, metadata-only, or off")
    return value


def validate_code_graph_delta_timeout(value: Any, *, key: str = CODE_GRAPH_DELTA_TIMEOUT_KEY) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{key} must be a positive number")
    return timeout


def validate_capture_before_retry(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"capture_before_retry must be one of: {', '.join(CAPTURE_BEFORE_RETRY_MODES)}")
    mode = value.strip().lower()
    if mode not in CAPTURE_BEFORE_RETRY_MODES:
        raise ValueError(f"capture_before_retry must be one of: {', '.join(CAPTURE_BEFORE_RETRY_MODES)}")
    return mode


def resolve_capture_before_retry(target: Path) -> str:
    cfg = load_config(target)
    if cfg is not None:
        return cfg.capture_before_retry
    return DEFAULT_CAPTURE_BEFORE_RETRY


def resolve_code_graph_delta_timeout(target: Path, cli_override: float | None = None) -> float:
    if cli_override is not None:
        try:
            return validate_code_graph_delta_timeout(cli_override)
        except ValueError:
            raise ValueError("--code-graph-timeout must be a positive number") from None
    cfg = load_config(target)
    if cfg is not None:
        return cfg.code_graph_delta_timeout_seconds
    return DEFAULT_CODE_GRAPH_DELTA_TIMEOUT_SECONDS


def legacy_config_keys(target: Path) -> list[tuple[str, str, bool]]:
    """Return ``(legacy, canonical, ignored)`` for renamed keys the target config still uses.

    ``ignored`` is true when the canonical key is also present, so ``load_config``
    never reads the legacy value. The file is the one ``load_config`` reads.
    Doctor reports them. Loading prints one stderr notice per legacy key.
    """
    path = _existing_config_path(target)
    try:
        data = json.loads(path.read_text()) if path is not None else None
    except (OSError, ValueError):
        return []
    if isinstance(data, dict) and LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY in data:
        ignored = CODE_GRAPH_DELTA_TIMEOUT_KEY in data
        return [(LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY, CODE_GRAPH_DELTA_TIMEOUT_KEY, ignored)]
    return []


def validate_verify_runs_keep(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("verify_runs_keep must be a positive integer")
    return value


def validate_verify_archive_enabled(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("verify_archive_enabled must be true or false")
    return value


def validate_verify_archive_dir(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("verify_archive_dir must be a non-empty string")
    return value.strip()


def validate_run_lock_wait_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("run_lock_wait_seconds must be a non-negative number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("run_lock_wait_seconds must be a non-negative number")
    return timeout


def validate_memory_recall_target(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("memory_recall_target must be a non-empty string when set")
    return value.strip()


def resolve_run_lock_wait_seconds(target: Path) -> float:
    cfg = load_config(target)
    if cfg is not None:
        return cfg.run_lock_wait_seconds
    return DEFAULT_RUN_LOCK_WAIT_SECONDS


def resolve_verify_runs_keep(target: Path) -> int:
    cfg = load_config(target)
    if cfg is not None:
        return cfg.verify_runs_keep
    return DEFAULT_VERIFY_RUNS_KEEP


def resolve_verify_archive(target: Path) -> tuple[bool, Path]:
    """Return (enabled, archive root) for verify-run evidence archival."""
    cfg = load_config(target)
    enabled = cfg.verify_archive_enabled if cfg is not None else DEFAULT_VERIFY_ARCHIVE_ENABLED
    raw = cfg.verify_archive_dir if cfg is not None else DEFAULT_VERIFY_ARCHIVE_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = target / path
    return enabled, path


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def write_config(target: Path, cfg: Config) -> None:
    cfg.selection.validate()
    code_graph_timeout = validate_code_graph_delta_timeout(cfg.code_graph_delta_timeout_seconds)
    path = config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": cfg.version,
        "depth": cfg.selection.depth,
        "harnesses": list(cfg.selection.harnesses),
        "owner": cfg.selection.owner,
        "includes": list(cfg.selection.includes),
    }
    run_record = validate_run_record(cfg.run_record)
    if run_record != "metadata-only":
        payload["run"] = {"record": run_record}
    if code_graph_timeout != DEFAULT_CODE_GRAPH_DELTA_TIMEOUT_SECONDS:
        payload[CODE_GRAPH_DELTA_TIMEOUT_KEY] = code_graph_timeout
    capture_before_retry = validate_capture_before_retry(cfg.capture_before_retry)
    if capture_before_retry != DEFAULT_CAPTURE_BEFORE_RETRY:
        payload["capture_before_retry"] = capture_before_retry
    verify_runs_keep = validate_verify_runs_keep(cfg.verify_runs_keep)
    if verify_runs_keep != DEFAULT_VERIFY_RUNS_KEEP:
        payload["verify_runs_keep"] = verify_runs_keep
    verify_archive_enabled = validate_verify_archive_enabled(cfg.verify_archive_enabled)
    if verify_archive_enabled != DEFAULT_VERIFY_ARCHIVE_ENABLED:
        payload["verify_archive_enabled"] = verify_archive_enabled
    verify_archive_dir = validate_verify_archive_dir(cfg.verify_archive_dir)
    if verify_archive_dir != DEFAULT_VERIFY_ARCHIVE_DIR:
        payload["verify_archive_dir"] = verify_archive_dir
    run_lock_wait_seconds = validate_run_lock_wait_seconds(cfg.run_lock_wait_seconds)
    if run_lock_wait_seconds != DEFAULT_RUN_LOCK_WAIT_SECONDS:
        payload["run_lock_wait_seconds"] = run_lock_wait_seconds
    memory_recall_target = validate_memory_recall_target(cfg.memory_recall_target)
    if memory_recall_target is not None:
        payload["memory_recall_target"] = memory_recall_target
    work: dict[str, str] = {}
    work_mode = validate_work_mode(cfg.work_mode)
    if work_mode != DEFAULT_WORK_MODE:
        work["mode"] = work_mode
    if work:
        payload["work"] = work
    # Write a sibling temp file and rename it over the config so a failed write
    # (full disk, interrupt) never leaves a truncated config behind.
    destination = Path(os.path.realpath(path))
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _existing_config_path(target: Path) -> Optional[Path]:
    """The config file ``load_config`` reads: canonical first, then legacy workspace dirs."""
    path = config_path(target)
    if path.is_file():
        return path
    for legacy in LEGACY_WORKSPACE_DIRNAMES:
        legacy_path = target / legacy / "config.json"
        if legacy_path.is_file():
            return legacy_path
    return None


def load_config(target: Path) -> Optional[Config]:
    path = _existing_config_path(target)
    if path is None:
        return None
    data = json.loads(path.read_text())
    version = data.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported config version: {version!r} (supported: {SUPPORTED_VERSIONS})")
    sel = Selection(
        depth=data.get("depth", ""),
        harnesses=list(data.get("harnesses", [])),
        owner=data.get("owner", "this-repo"),
        includes=list(data.get("includes", [])),
    )
    sel.validate()
    from brigade.engine_deprecation import notice

    if LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY in data:
        notice(LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY, CODE_GRAPH_DELTA_TIMEOUT_KEY)
    # The canonical key wins; the legacy key is read only when it is absent.
    timeout_key = (
        CODE_GRAPH_DELTA_TIMEOUT_KEY if CODE_GRAPH_DELTA_TIMEOUT_KEY in data else LEGACY_CODE_GRAPH_DELTA_TIMEOUT_KEY
    )
    timeout = validate_code_graph_delta_timeout(
        data.get(timeout_key, DEFAULT_CODE_GRAPH_DELTA_TIMEOUT_SECONDS), key=timeout_key
    )
    capture_before_retry = validate_capture_before_retry(data.get("capture_before_retry", DEFAULT_CAPTURE_BEFORE_RETRY))
    verify_runs_keep = validate_verify_runs_keep(data.get("verify_runs_keep", DEFAULT_VERIFY_RUNS_KEEP))
    verify_archive_enabled = validate_verify_archive_enabled(
        data.get("verify_archive_enabled", DEFAULT_VERIFY_ARCHIVE_ENABLED)
    )
    verify_archive_dir = validate_verify_archive_dir(data.get("verify_archive_dir", DEFAULT_VERIFY_ARCHIVE_DIR))
    run_lock_wait_seconds = validate_run_lock_wait_seconds(
        data.get("run_lock_wait_seconds", DEFAULT_RUN_LOCK_WAIT_SECONDS)
    )
    memory_recall_target = validate_memory_recall_target(data.get("memory_recall_target"))
    run = data.get("run", {})
    if not isinstance(run, dict):
        raise ValueError("run must be an object")
    run_record = validate_run_record(run.get("record", "metadata-only"))
    # An invalid optional work mode falls back to engineer instead of failing the
    # whole config: a failed load reads as "not wired" to hook target discovery,
    # which would drop enforcement. Doctor reports it. An in-repo work.rails is
    # never read here; work_mode reports it as ignored.
    work = data.get("work", {})
    if not isinstance(work, dict):
        work = {}
    try:
        work_mode = validate_work_mode(work.get("mode", DEFAULT_WORK_MODE))
    except ValueError:
        work_mode = DEFAULT_WORK_MODE
    return Config(
        version=version,
        selection=sel,
        code_graph_delta_timeout_seconds=timeout,
        capture_before_retry=capture_before_retry,
        verify_runs_keep=verify_runs_keep,
        verify_archive_enabled=verify_archive_enabled,
        verify_archive_dir=verify_archive_dir,
        run_lock_wait_seconds=run_lock_wait_seconds,
        memory_recall_target=memory_recall_target,
        run_record=run_record,
        work_mode=work_mode,
    )
