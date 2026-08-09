"""Read/write .brigade/config.json - the per-target source of truth."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .selection import Selection


WORKSPACE_DIRNAME = ".brigade"
LEGACY_WORKSPACE_DIRNAMES = (".solo-mise",)
CONFIG_REL_PATH = f"{WORKSPACE_DIRNAME}/config.json"
SUPPORTED_VERSIONS = (1,)
DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS = 10.0
CAPTURE_BEFORE_RETRY_MODES = ("warn", "block", "off")
DEFAULT_CAPTURE_BEFORE_RETRY = "warn"
DEFAULT_VERIFY_RUNS_KEEP = 50
DEFAULT_VERIFY_ARCHIVE_ENABLED = True
DEFAULT_VERIFY_ARCHIVE_DIR = ".brigade/work/verify-archive"
DEFAULT_RUN_LOCK_WAIT_SECONDS = 0.0


@dataclass
class Config:
    version: int
    selection: Selection
    graphtrail_delta_timeout_seconds: float = DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS
    capture_before_retry: str = DEFAULT_CAPTURE_BEFORE_RETRY
    verify_runs_keep: int = DEFAULT_VERIFY_RUNS_KEEP
    verify_archive_enabled: bool = DEFAULT_VERIFY_ARCHIVE_ENABLED
    verify_archive_dir: str = DEFAULT_VERIFY_ARCHIVE_DIR
    run_lock_wait_seconds: float = DEFAULT_RUN_LOCK_WAIT_SECONDS


def validate_graphtrail_delta_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("graphtrail_delta_timeout_seconds must be a positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("graphtrail_delta_timeout_seconds must be a positive number")
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


def resolve_graphtrail_delta_timeout(target: Path, cli_override: float | None = None) -> float:
    if cli_override is not None:
        try:
            return validate_graphtrail_delta_timeout(cli_override)
        except ValueError:
            raise ValueError("--graphtrail-timeout must be a positive number") from None
    cfg = load_config(target)
    if cfg is not None:
        return cfg.graphtrail_delta_timeout_seconds
    return DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS


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
    graphtrail_timeout = validate_graphtrail_delta_timeout(cfg.graphtrail_delta_timeout_seconds)
    path = config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": cfg.version,
        "depth": cfg.selection.depth,
        "harnesses": list(cfg.selection.harnesses),
        "owner": cfg.selection.owner,
        "includes": list(cfg.selection.includes),
    }
    if graphtrail_timeout != DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS:
        payload["graphtrail_delta_timeout_seconds"] = graphtrail_timeout
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
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_config(target: Path) -> Optional[Config]:
    path = config_path(target)
    if not path.is_file():
        for legacy in LEGACY_WORKSPACE_DIRNAMES:
            legacy_path = target / legacy / "config.json"
            if legacy_path.is_file():
                path = legacy_path
                break
        else:
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
    timeout_raw = data.get("graphtrail_delta_timeout_seconds", DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS)
    timeout = validate_graphtrail_delta_timeout(timeout_raw)
    capture_before_retry = validate_capture_before_retry(data.get("capture_before_retry", DEFAULT_CAPTURE_BEFORE_RETRY))
    verify_runs_keep = validate_verify_runs_keep(data.get("verify_runs_keep", DEFAULT_VERIFY_RUNS_KEEP))
    verify_archive_enabled = validate_verify_archive_enabled(
        data.get("verify_archive_enabled", DEFAULT_VERIFY_ARCHIVE_ENABLED)
    )
    verify_archive_dir = validate_verify_archive_dir(data.get("verify_archive_dir", DEFAULT_VERIFY_ARCHIVE_DIR))
    run_lock_wait_seconds = validate_run_lock_wait_seconds(
        data.get("run_lock_wait_seconds", DEFAULT_RUN_LOCK_WAIT_SECONDS)
    )
    return Config(
        version=version,
        selection=sel,
        graphtrail_delta_timeout_seconds=timeout,
        capture_before_retry=capture_before_retry,
        verify_runs_keep=verify_runs_keep,
        verify_archive_enabled=verify_archive_enabled,
        verify_archive_dir=verify_archive_dir,
        run_lock_wait_seconds=run_lock_wait_seconds,
    )
