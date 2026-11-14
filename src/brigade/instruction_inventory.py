"""Read-only inventory of the always-loaded instruction files for one target.

`discover` finds every candidate path, resolves each to its real file, groups
paths that reach the same file, and gives each file one limit: the smallest of
its loader caps and Brigade's own target budget. It writes nothing, starts no
subprocess, and measures sizes in UTF-8 bytes.

Nothing a configuration file says removes a file from the inventory or turns its
protection off without a trace. An excluded file stays as a row marked
`excluded`, with the source of the exclusion. An override that names a path
outside the target and the user's home, or an absolute path that is not already
a discovered instruction file, is rejected: the row is forced to class `report`
and the rejection is listed. Every row carries the class and limit that were
requested beside the effective ones, and where each came from. Consumers must
skip rows marked `excluded` when they raise alerts or tend, and still count
them in loader totals, because the harness loads them either way.
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import budgets
from . import harness_profiles as hp
from .bounded_read import read_regular_file
from .instruction_loaders import (
    LoaderCap,
    codex_project_cap,
    openclaw_loaders,
    user_md_caps,
)

REPORT_MARKER = "<!-- brigade:instructions report -->"
APPEND_NAMES = (
    "USER.md",
    "TOOLS.md",
    ".learnings/LEARNINGS.md",
    ".learnings/ERRORS.md",
    ".learnings/FEATURE_REQUESTS.md",
)
CODEX_OVERRIDE_NAME = "AGENTS.override.md"
CLASSES = ("append", "policy", "report")
INSPECTION_CEILING = 1024 * 1024  # context_preview.RESOURCE_MAX_BYTES
_MARKER_LINES = 5
_CONFIG_REL = ".brigade/memory-care.toml"
CONFIG_MAX_BYTES = 1024 * 1024
EXTERNAL_MARKER = "<external>"
EXCLUSION_SOURCE = "config:instructions.exclude"
OVERRIDE_SOURCE = "config:instructions.files"
# Per-name root files a loader reads: Brigade's table, OpenClaw's bootstrap list and Codex's override.
ROOT_NAMES = (*dict.fromkeys((*budgets.BOOTSTRAP_BUDGETS, *hp.OPENCLAW_BOOTSTRAP_FILES, CODEX_OVERRIDE_NAME)),)


@dataclass(frozen=True)
class FileOverride:
    file_class: str | None = None  # "append" | "policy" | "report"
    limit_bytes: int | None = None


@dataclass(frozen=True)
class InstructionsConfig:
    enabled: bool = True
    act_at_percent: int = budgets.ACT_AT_PERCENT
    trim_to_percent: int = budgets.TRIM_TO_PERCENT
    default_limit_bytes: int = budgets.DEFAULT_LIMIT_BYTES
    exclude: tuple[str, ...] = ()
    files: tuple[tuple[str, FileOverride], ...] = ()


@dataclass(frozen=True)
class InstructionFile:
    key: str  # "user-md", "codex-agents-md"
    display: str  # display path of the real file
    real_path: Path
    paths: tuple[str, ...]  # display form of every path that resolves here, sorted
    level: str  # "workspace" | "user" | "config"
    loaders: tuple[str, ...]  # sorted
    caps: tuple[LoaderCap, ...]
    file_class: str  # "append" | "policy" | "report"
    target_budget: int
    target_source: str  # "target:config" | "target:table" | "target:default"
    limit: int
    limit_source: str
    limit_reason: str | None
    act_at: int
    trim_to: int
    size: int  # bytes, 0 when missing
    exists: bool
    skip_reason: str | None  # "not-utf8" | "over-1-mib" | "not-regular" | "unreadable" | None
    # Audit fields. `file_class` and `limit` are the effective values.
    excluded: bool = False  # named by [instructions].exclude: never tended, still counted in totals
    exclusion_source: str | None = None
    requested_class: str | None = None  # what [instructions.files] asked for, even when not applied
    requested_limit_bytes: int | None = None
    requested_source: str | None = None  # where the request came from
    class_source: str = "default"  # config | marker | repository-depth | memory-index | name-table
    # | default | excluded | rejected-override
    override_reason: str | None = None  # why the override on this row was rejected, else None


@dataclass(frozen=True)
class OverrideNote:
    """A `[instructions.files]` entry that was rejected or ignored, and why."""

    name: str  # display form of the configured path
    reason: str  # "outside-target-and-home" | "absolute-path-not-discovered" | "matches-no-file"
    action: str  # "forced-report" | "ignored"


@dataclass(frozen=True)
class LoaderTotal:
    loader: str
    total: int  # bytes across the files that list this loader
    cap: int | None
    label: str | None


@dataclass(frozen=True)
class Inventory:
    target: Path
    files: tuple[InstructionFile, ...]  # sorted by display
    totals: tuple[LoaderTotal, ...]  # sorted by loader
    repository_depth: bool
    override_notes: tuple[OverrideNote, ...] = ()


# --- configuration ---------------------------------------------------------


def _percent(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError(f"invalid instructions.{key}")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"invalid {name}")
    return value


def load_instructions_config(target: Path) -> InstructionsConfig:
    """Read the `[instructions]` table of `.brigade/memory-care.toml`.

    A missing file gives the defaults. Unknown keys are ignored. Every other
    problem raises `ValueError`: a wrong value names its key, and a file that is
    unreadable, not a regular file, over 1 MiB, not UTF-8 or not TOML says so
    without echoing its content. Callers treat the `ValueError` as an invalid
    table (the plan's decision 10): they use `InstructionsConfig()` and report
    `config-invalid`, so a handoff is never lost. `discover` does not call this.
    """
    read = read_regular_file(target / _CONFIG_REL, max_bytes=CONFIG_MAX_BYTES, require_stable=True)
    if read.reason == "missing":
        return InstructionsConfig()
    problem = {
        "too-large": "is over 1 MiB",
        "not-regular": "is not a regular file",
    }.get(read.reason or "", "could not be read")
    if read.data is None:
        raise ValueError(f"invalid instructions: {_CONFIG_REL} {problem}")
    try:
        # utf-8-sig: a leading byte-order mark is dropped, as in memory_cmd.
        parsed = tomllib.loads(read.data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise ValueError(f"invalid instructions: {_CONFIG_REL} is not UTF-8") from None
    except (tomllib.TOMLDecodeError, RecursionError):
        raise ValueError(f"invalid instructions: {_CONFIG_REL} is not valid TOML") from None
    table = parsed.get("instructions", {})
    if not isinstance(table, dict):
        raise ValueError("invalid instructions")
    defaults = InstructionsConfig()
    enabled = table.get("enabled", defaults.enabled)
    if type(enabled) is not bool:
        raise ValueError("invalid instructions.enabled")
    act_at = _percent(table, "act_at_percent", defaults.act_at_percent)
    trim_to = _percent(table, "trim_to_percent", defaults.trim_to_percent)
    if trim_to >= act_at:
        raise ValueError("invalid instructions.trim_to_percent")
    default_limit = _positive(
        table.get("default_limit_bytes", defaults.default_limit_bytes), "instructions.default_limit_bytes"
    )
    exclude = table.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        raise ValueError("invalid instructions.exclude")
    raw_files = table.get("files", {})
    if not isinstance(raw_files, dict):
        raise ValueError("invalid instructions.files")
    files: list[tuple[str, FileOverride]] = []
    for name, entry in raw_files.items():
        if not isinstance(entry, dict):
            raise ValueError(f"invalid instructions.files.{name}")
        file_class = entry.get("class")
        if file_class is not None and file_class not in CLASSES:
            raise ValueError(f"invalid instructions.files.{name}.class")
        limit = entry.get("limit_bytes")
        if limit is not None:
            limit = _positive(limit, f"instructions.files.{name}.limit_bytes")
        files.append((name, FileOverride(file_class, limit)))
    return InstructionsConfig(enabled, act_at, trim_to, default_limit, tuple(exclude), tuple(files))


# --- naming ----------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def display_path(path: Path, *, target: Path, home: Path) -> str:
    """Relative POSIX path inside the target, `~/...` under home, else a fixed marker and the file name.

    A path outside both (a symlink target, say) never shows its directories. Code that
    needs the real path reads `InstructionFile.real_path`.
    """
    for base in (target, _resolved(target)):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            pass
    for base in (home, _resolved(home)):
        try:
            return "~/" + path.relative_to(base).as_posix()
        except ValueError:
            pass
    return f"{EXTERNAL_MARKER}/{path.name}"


def file_key(workspace_relative: str | None, harness: str | None, real_path: Path) -> str:
    if workspace_relative:
        return _slug(workspace_relative)
    if harness:
        return f"{harness}-{_slug(real_path.name)}"
    digest = hashlib.sha256(str(real_path).encode("utf-8")).hexdigest()[:8]
    return f"external-{_slug(real_path.name)}-{digest}"


# --- discovery -------------------------------------------------------------


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _expand(value: str, home: Path, target: Path) -> Path:
    if value == "~" or value.startswith("~/"):
        return home / value[2:]
    path = Path(value)
    return path if path.is_absolute() else target / path


@dataclass
class _Group:
    reals: set[Path]
    exists: bool = True
    root_names: list[str] | None = None
    harnesses: set[str] | None = None
    configured: bool = False
    candidates: list[Path] | None = None
    exclusion: str | None = None
    rejected: str | None = None

    def __post_init__(self) -> None:
        self.root_names = []
        self.harnesses = set()
        self.candidates = []

    @property
    def real(self) -> Path:
        return min(self.reals, key=str)


def _repository_depth(target: Path) -> bool:
    from . import config as brigade_config

    try:
        loaded = brigade_config.load_config(target)
    except (ValueError, OSError):
        return False
    return loaded is not None and loaded.selection.depth == "repo"


def _index_paths(target: Path) -> set[Path]:
    from . import memory_cmd

    names = memory_cmd.MemoryCareConfig().index_paths
    # Check the file with the bounded reader first: the memory-care loader reads it
    # without a ceiling. An unreadable, oversized or non-regular file means defaults.
    if read_regular_file(target / _CONFIG_REL, max_bytes=CONFIG_MAX_BYTES).reason in (None, "missing"):
        try:
            names = memory_cmd._config_or_default(target).index_paths
        except (ValueError, OSError):
            pass
    return {_resolved(target / name) for name in names}


def _identity(real: Path) -> tuple[int, int] | None:
    """Device and inode, so two hard links to one file group together."""
    try:
        info = os.stat(real)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino) if info.st_ino else None


def _read_state(real: Path, *, missing: bool) -> tuple[int, str | None, list[str]]:
    """Size in bytes, a skip reason, and the first lines when the file can be read as text.

    One bounded, nonblocking open: the size and the bytes come from the same descriptor.
    """
    if missing:
        return 0, None, []
    read = read_regular_file(real, max_bytes=INSPECTION_CEILING)
    if read.data is None:
        skip = {"too-large": "over-1-mib", "not-regular": "not-regular"}.get(read.reason or "", "unreadable")
        return read.size, skip, []
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError:
        return read.size, "not-utf8", []
    return read.size, None, text.splitlines()[:_MARKER_LINES]


def discover(
    target: Path,
    config: InstructionsConfig,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_missing: bool = False,
) -> Inventory:
    """Find every always-loaded instruction file and give each one limit.

    It never raises for a file it cannot read: that file stays a row with a
    `skip_reason`. It does not read the `[instructions]` table (callers pass
    `config`, from `load_instructions_config`). It does read `.brigade/memory-care.toml`
    for the memory index paths, and uses the defaults when that file is unreadable.
    """
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    repository_depth = _repository_depth(target)
    index = _index_paths(target)
    overrides = {_resolved(_expand(name, home, target)): override for name, override in config.files}
    excluded = [_expand(name, home, target) for name in config.exclude]
    excluded_real = {_resolved(path) for path in excluded}
    excluded_plain = {Path(os.path.abspath(path)) for path in excluded}
    notes: list[OverrideNote] = []

    groups: dict[object, _Group] = {}
    standard: set[object] = set()

    def add(
        path: Path,
        *,
        root_name: str | None = None,
        harness: str | None = None,
        configured: bool = False,
        rejected: str | None = None,
    ) -> None:
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            if root_name is None or not include_missing or os.path.lexists(path):
                return
            real = path  # an absent per-name file, listed on request
            exists = False
        else:
            if not real.is_file():
                return
            exists = True
        if root_name is None:
            # A configured path that is a known per-name file at the root is that root file.
            relative = _inside(path, target)
            if relative in ROOT_NAMES:
                root_name = relative
        key: object = (_identity(real) if exists else None) or real
        group = groups.setdefault(key, _Group({real}, exists))
        group.reals.add(real)
        assert group.candidates is not None and group.root_names is not None and group.harnesses is not None
        group.candidates.append(path)
        if root_name is not None:
            group.root_names.append(root_name)
        if harness is not None:
            group.harnesses.add(harness)
        group.configured = group.configured or configured
        group.rejected = group.rejected or rejected
        if real in excluded_real or Path(os.path.abspath(path)) in excluded_plain:
            group.exclusion = EXCLUSION_SOURCE

    def add_configured(name: str) -> None:
        path = _expand(name, home, target)
        shown = display_path(path, target=target, home=home)
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            notes.append(OverrideNote(shown, "matches-no-file", "ignored"))
            return
        if not real.is_file():
            notes.append(OverrideNote(shown, "matches-no-file", "ignored"))
            return
        if (_identity(real) or real) in standard:
            add(path, configured=True)  # already a discovered instruction file
            return
        if not name.startswith("~") and Path(name).is_absolute():
            reason = "absolute-path-not-discovered"
        elif _inside(real, target) is None and _under_home(real, home) is None:
            reason = "outside-target-and-home"
        else:
            add(path, configured=True)
            return
        add(path, configured=True, rejected=reason)
        notes.append(OverrideNote(shown, reason, "forced-report"))

    for name in ROOT_NAMES:
        add(target / name, root_name=name)
    # A missing override is not a row: only an existing one changes what Codex loads.
    for profile in hp.resolve_user_profiles(harness="all", home=home, workspace=target):
        if profile.instruction_mode == "managed-file":
            continue
        add(profile.instruction_path, harness=profile.harness)
    standard.update(groups)  # what discovery finds without any configuration
    for name, _override in config.files:
        add_configured(name)

    openclaw = openclaw_loaders(target, home=home, environ=environ)
    codex_cap = codex_project_cap(home=home, environ=environ)
    override_present = (target / CODEX_OVERRIDE_NAME).is_file()
    codex_name = CODEX_OVERRIDE_NAME if override_present else "AGENTS.md"

    files = [
        _build(
            group,
            target=target,
            home=home,
            config=config,
            repository_depth=repository_depth,
            index=index,
            overrides=overrides,
            openclaw_per_file=openclaw.per_file,
            openclaw_user_caps=user_md_caps(openclaw),
            codex_cap=codex_cap,
            codex_name=codex_name,
        )
        for group in groups.values()
        if not (not group.exists and group.root_names == [CODEX_OVERRIDE_NAME])
    ]
    files.sort(key=lambda item: item.display)
    totals = tuple(
        LoaderTotal(
            entry.loader,
            sum(item.size for item in files if entry.loader in item.loaders),
            entry.cap,
            entry.label if entry.cap is not None else None,
        )
        for entry in sorted(openclaw.totals, key=lambda item: item.loader)
    )
    return Inventory(
        target, tuple(files), totals, repository_depth, tuple(sorted(set(notes), key=lambda n: (n.name, n.reason)))
    )


def _build(
    group: _Group,
    *,
    target: Path,
    home: Path,
    config: InstructionsConfig,
    repository_depth: bool,
    index: set[Path],
    overrides: dict[Path, FileOverride],
    openclaw_per_file: tuple[LoaderCap, ...],
    openclaw_user_caps: tuple[LoaderCap, ...],
    codex_cap: LoaderCap,
    codex_name: str,
) -> InstructionFile:
    assert group.candidates is not None and group.root_names is not None and group.harnesses is not None
    real = group.real
    root_names = sorted(set(group.root_names))
    has_root = bool(root_names)
    level = "workspace" if has_root else "config" if group.configured else "user"

    loaders: set[str] = {f"{harness}:user" for harness in group.harnesses}
    caps: list[LoaderCap] = []
    if any(name in hp.OPENCLAW_BOOTSTRAP_FILES for name in root_names):
        loaders.update(cap.loader for cap in openclaw_per_file)
        caps.extend(openclaw_per_file)
    if "USER.md" in root_names:
        # OpenClaw's fixed cap is on the workspace's own USER.md, not on any file named USER.md.
        caps.extend(openclaw_user_caps)
    if codex_name in root_names:
        loaders.add(codex_cap.loader)
        caps.append(codex_cap)

    requested = next((found for found in map(overrides.get, group.reals) if found is not None), None) or next(
        (found for path in group.candidates if (found := overrides.get(_resolved(path))) is not None), None
    )
    # A rejected override is recorded but applies nothing.
    override = None if group.rejected else requested
    table_budget = next(
        (budgets.BOOTSTRAP_BUDGETS[name] for name in root_names if name in budgets.BOOTSTRAP_BUDGETS), None
    )
    if override is not None and override.limit_bytes is not None:
        target_budget, target_source = override.limit_bytes, "target:config"
    elif table_budget is not None:
        target_budget, target_source = table_budget, "target:table"
    else:
        target_budget, target_source = config.default_limit_bytes, "target:default"

    usable = [(cap.label, cap.cap) for cap in caps if cap.cap is not None]
    limit, limit_source = budgets.limit_for(target_budget, target_source, usable)
    limit_reason = next((cap.reason for cap in caps if cap.cap is None), None)

    size, skip_reason, head = _read_state(real, missing=not group.exists)
    relative = _relative_names(group.candidates, target)
    if group.rejected:
        file_class, class_source = "report", "rejected-override"
    elif group.exclusion:
        file_class, class_source = "report", "excluded"
    elif override is not None and override.file_class is not None:
        file_class, class_source = override.file_class, "config"
    elif any(REPORT_MARKER in line for line in head):
        file_class, class_source = "report", "marker"
    elif repository_depth:
        file_class, class_source = "report", "repository-depth"
    elif any(item in index for item in group.reals) or any(_resolved(path) in index for path in group.candidates):
        file_class, class_source = "report", "memory-index"
    elif any(name in APPEND_NAMES for name in relative):
        file_class, class_source = "append", "name-table"
    else:
        file_class, class_source = "policy", "default"

    workspace_relative = root_names[0] if has_root else None
    if workspace_relative is None and group.configured:
        workspace_relative = _inside(real, target)
    harness = min(group.harnesses) if group.harnesses else None
    return InstructionFile(
        key=file_key(workspace_relative, harness, real),
        display=display_path(real, target=target, home=home),
        real_path=real,
        paths=tuple(sorted({display_path(path, target=target, home=home) for path in group.candidates})),
        level=level,
        loaders=tuple(sorted(loaders)),
        caps=tuple(caps),
        file_class=file_class,
        target_budget=target_budget,
        target_source=target_source,
        limit=limit,
        limit_source=limit_source,
        limit_reason=limit_reason,
        act_at=budgets.threshold(limit, config.act_at_percent),
        trim_to=budgets.threshold(limit, config.trim_to_percent),
        size=size,
        exists=group.exists,
        skip_reason=skip_reason,
        excluded=group.exclusion is not None,
        exclusion_source=group.exclusion,
        requested_class=requested.file_class if requested is not None else None,
        requested_limit_bytes=requested.limit_bytes if requested is not None else None,
        requested_source=OVERRIDE_SOURCE if requested is not None else None,
        class_source=class_source,
        override_reason=group.rejected,
    )


def _inside(path: Path, target: Path) -> str | None:
    for base in (target, _resolved(target)):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            pass
    return None


def _under_home(path: Path, home: Path) -> str | None:
    for base in (home, _resolved(home)):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            pass
    return None


def _relative_names(paths: list[Path], target: Path) -> list[str]:
    return [name for path in paths if (name := _inside(path, target)) is not None]


def find(inventory: Inventory, document: str) -> InstructionFile | None:
    """The row one of whose paths equals `document`."""
    for item in inventory.files:
        if document in item.paths:
            return item
    return None
