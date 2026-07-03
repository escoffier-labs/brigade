# ruff: noqa: F401
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import actionqueue, config as brigade_config, reportstore, toml_compat as tomllib, work_cmd
from ..budgets import HANDOFF_BACKLOG_STALE_DAYS
from ..install import apply_gitignore
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit
from ..selection import Selection, WRITER_INBOXES

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/repos.toml"
REPORT_STALE_HOURS = 24
# A fleet repo with pending handoffs whose oldest is older than this is flagged
# as an un-ingested backlog (handoffs written but the ingester never reaches it).
# Canonical value lives in budgets.py so doctor/ingest/repos all agree.
BACKLOG_STALE_DAYS = HANDOFF_BACKLOG_STALE_DAYS
HEALTH_COMMAND_RECEIPT_STALE_HOURS = 24
ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}
DISPATCH_STALE_HOURS = 24
RELEASE_TRAIN_STALE_HOURS = 168
RELEASE_EVIDENCE_STEPS = {"verification", "release-doctor", "candidate-compare", "tag", "push", "release", "other"}
RELEASE_EVIDENCE_STATUSES = {"completed", "skipped", "blocked", "deferred"}
REQUIRED_RELEASE_EVIDENCE_STEPS = ("verification", "release-doctor", "candidate-compare", "tag", "push", "release")
RELEASE_WAIVER_SCOPES = {"blocked-repo", "unresolved-action", "missing-evidence", "blocked-evidence"}
RELEASE_WAIVER_STALE_HOURS = 168
RELEASE_WAIVER_REASON_MIN_LENGTH = 16
RELEASE_WAIVER_GENERIC_REASONS = {"ok", "reviewed", "waived", "temporary", "later", "n/a", "na", "accepted"}
RELEASE_BUNDLE_FILES = (
    "FLEET_RELEASE_EVIDENCE.json",
    "FLEET_RELEASE_TRAIN.md",
    "MANUAL_PUBLISH_PLAN.md",
    "CLOSEOUT.json",
    "RELEASE_TRAIN_REPORT.json",
    "RELEASE_TRAIN_REPORT.md",
    "RELEASE_TRAIN_MATRIX.json",
    "RELEASE_TRAIN_MATRIX.md",
    "RELEASE_TRAIN_MANIFEST.json",
)
UNWIRED_REARM_REASON = "unwired; run quickstart with explicit harnesses"


@dataclass(frozen=True)
class RepoEntry:
    repo_id: str
    label: str
    path: Path
    enabled: bool = True
    expect_brigade: bool = False
    expect_publish_guard: bool = False
    health_commands: tuple[SweepCommand, ...] = ()


@dataclass(frozen=True)
class SweepCommand:
    label: str
    argv: list[str]
    timeout: int = 120


@dataclass(frozen=True)
class DiscoveryRoot:
    root_id: str
    label: str
    path: Path
    enabled: bool = True
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    max_depth: int = 2


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


__all__ = tuple(name for name in globals() if not name.startswith("__"))
