"""`[verify]` and `[[verify.workspace]]` in `.brigade/memory-care.toml`.

Each workspace entry binds one hub scope to an absolute workspace root and an
explicit list of evidence archive projects. Ownership has to be unambiguous:
a root belongs to exactly one scope, no bound root sits inside another, and
every entry names its archive projects, even when the list is empty. Every
root must exist, and roots are compared by directory identity (device and
inode), so a case-only spelling, a symlink alias or a differently spelled
parent cannot make one directory look like two. Each parsed workspace keeps
that identity, and `revalidate()` checks it again before evidence is read.
An optional `commit_refs` list names the remote-tracking refs the commit
resolver (`memory_verify_resolver`) proves reachability from.
The workspace file never carries a hub URL or a credential path. Those stay in
the user-level fleet settings, as for `[hub]`.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import memory_hub_client as hubc
from . import memory_proposal_io as io

CONFIG = hubc.CONFIG_REL
DEFAULT_CARDS_PER_RUN = 12
MAX_CARDS_PER_RUN = 64
DEFAULT_MIN_INTERVAL_DAYS = 7
MAX_MIN_INTERVAL_DAYS = 365
DEFAULT_NEVER_READ_DAYS = 90
MAX_NEVER_READ_DAYS = 3650
MAX_WORKSPACES = 64
MAX_ARCHIVE_PROJECTS = 32
MAX_COMMIT_REFS = 8
SEAT = re.compile(r"[a-z][a-z0-9._-]{0,127}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
VERIFY_KEYS = frozenset(
    {
        "enabled",
        "propose_seat",
        "confirm_seat",
        "cards_per_run",
        "min_interval_days",
        "never_read_days",
        "workspace",
    }
)
WORKSPACE_KEYS = frozenset({"scope", "path", "archive_projects", "commit_refs"})
# One component of a remote name or branch: no leading dot or dash, no `..`, no `.lock` suffix.
REF_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
# Named so the refusal says where these settings belong instead of "unknown key".
HUB_KEYS = frozenset({"hub_url", "url", "node_token_file", "token_file", "token", "credential", "credential_file"})
HUB_KEY_REFUSAL = (
    "[verify] never names a hub URL or credential path: set them in [memory_hub] in ~/.brigade/fleet.toml, "
    "or in BRIGADE_MEMORY_HUB_URL and BRIGADE_MEMORY_HUB_TOKEN_FILE"
)


MISSING_ROOT = "[[verify.workspace]] path must name an existing directory"
IDENTITY_UNAVAILABLE = (
    "verify_root_identity_unavailable: a workspace root or one of its parents has no stable directory identity"
)
ROOT_CHANGED = "verify_root_changed: a bound workspace root is no longer the directory configured"


class VerifyConfigError(ValueError):
    """A fixed diagnostic. It names keys and rules, never configured values."""


Identity = tuple[int, int]


@dataclass(frozen=True)
class Workspace:
    scope: str
    path: str
    archive_projects: tuple[str, ...]
    # Resolved at parse time: normcase(realpath) for messages and ordering, and the
    # directory's (st_dev, st_ino), which revalidate() checks before any evidence read.
    resolved: str = ""
    identity: Identity = (0, 0)
    # Fully qualified remote-tracking refs the commit resolver may prove reachability from. Empty means the
    # default, the single ref `refs/remotes/origin/HEAD` names (D3 "Commit resolver contract", rule 10).
    commit_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyConfig:
    enabled: bool = False
    propose_seat: str | None = None
    confirm_seat: str | None = None
    cards_per_run: int = DEFAULT_CARDS_PER_RUN
    min_interval_days: int = DEFAULT_MIN_INTERVAL_DAYS
    never_read_days: int = DEFAULT_NEVER_READ_DAYS
    workspaces: tuple[Workspace, ...] = ()

    def scopes(self) -> tuple[str, ...]:
        """Bound scopes in configuration order. A scope with no entry is never verified."""
        return tuple(dict.fromkeys(workspace.scope for workspace in self.workspaces))


def _integer(table: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = table.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise VerifyConfigError(f"[verify] {key} must be an integer from {low} to {high}")
    return value


def _seat(table: dict[str, Any], key: str) -> str | None:
    value = table.get(key)
    if value is not None and (not isinstance(value, str) or SEAT.fullmatch(value) is None):
        raise VerifyConfigError(f"[verify] {key} must be a roster seat name")
    return value


def _root(value: object) -> str:
    """An absolute, already normalized POSIX path, without resolving anything."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value.encode()) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
        or os.path.normpath(value) != value
        or value.startswith("//")
        or value == "/"
    ):
        raise VerifyConfigError("[[verify.workspace]] path must be an absolute, normalized directory other than /")
    return value


# Injectable for tests that emulate a case-insensitive filesystem. Both follow symbolic links.
_stat = os.stat
_realpath = os.path.realpath


def _identity(metadata: os.stat_result) -> Identity:
    """(st_dev, st_ino), refused when the filesystem gives no stable inode (0 on POSIX, a zero file index on Windows).

    Without one, equal identities prove nothing, so a replaced root would look unchanged.
    """
    if metadata.st_ino == 0:
        raise VerifyConfigError(IDENTITY_UNAVAILABLE)
    return metadata.st_dev, metadata.st_ino


def _directory(path: str) -> Identity:
    """The identity of an existing directory, following links. Fixed error otherwise."""
    try:
        metadata = _stat(path)
    except (OSError, ValueError):
        raise VerifyConfigError(MISSING_ROOT) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise VerifyConfigError(MISSING_ROOT)
    return _identity(metadata)


def _ancestors(resolved: str) -> set[Identity]:
    """Directory identities of every parent of a resolved root, up to and including /."""
    found: set[Identity] = set()
    for parent in PurePosixPath(resolved).parents:
        try:
            metadata = _stat(str(parent))
        except (OSError, ValueError):
            raise VerifyConfigError(MISSING_ROOT) from None
        # A parent without a stable inode could hide or fake nesting, so it is refused too.
        found.add(_identity(metadata))
    return found


def revalidate(workspace: Workspace) -> str | None:
    """None while the root is still the directory resolved at parse time, else a fixed reason.

    Evidence assembly calls this before reading anything under a root, so a
    symlink retargeted or a directory replaced after parsing reads nothing.
    """
    try:
        current = _directory(workspace.path)
    except VerifyConfigError:
        return ROOT_CHANGED
    return None if current == workspace.identity else ROOT_CHANGED


def valid_commit_ref(value: object) -> bool:
    """True for `refs/remotes/<remote>/<branch>` in conservative ASCII. Local branches and tags never qualify.

    The remote is the first component after `refs/remotes/`, so a remote name never holds a slash. A
    component cannot start with a dot or end with `.lock` or a dot, and the branch is never `HEAD`.
    """
    if not isinstance(value, str) or not value.startswith("refs/remotes/") or len(value) > 255:
        return False
    parts = value.split("/")[2:]
    return (
        len(parts) >= 2
        and parts[1:] != ["HEAD"]
        and all(
            REF_COMPONENT.fullmatch(part) is not None and ".." not in part and not part.endswith((".", ".lock"))
            for part in parts
        )
    )


def _commit_refs(item: dict[str, Any]) -> tuple[str, ...]:
    if "commit_refs" not in item:
        return ()
    refs = item["commit_refs"]
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= MAX_COMMIT_REFS
        or not all(valid_commit_ref(ref) for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise VerifyConfigError(
            f"[[verify.workspace]] commit_refs must list 1 to {MAX_COMMIT_REFS} distinct fully qualified "
            "remote-tracking refs (refs/remotes/<remote>/<branch>)"
        )
    return tuple(refs)


def _workspaces(raw: object) -> tuple[Workspace, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_WORKSPACES:
        raise VerifyConfigError(f"[[verify.workspace]] must be an array of at most {MAX_WORKSPACES} tables")
    entries: list[Workspace] = []
    for item in raw:
        if not isinstance(item, dict):
            raise VerifyConfigError("[[verify.workspace]] entries must be tables")
        if set(item) & HUB_KEYS:
            raise VerifyConfigError(HUB_KEY_REFUSAL)
        unknown = sorted(set(item) - WORKSPACE_KEYS)
        if unknown:
            raise VerifyConfigError(f"[[verify.workspace]] has unknown keys: {', '.join(unknown)}")
        scope = item.get("scope")
        if not hubc.valid_scope(scope):
            raise VerifyConfigError(
                "[[verify.workspace]] scope must be fleet, repo:<identity>, collection:<name> or principal:<id>"
            )
        path = _root(item.get("path"))
        if "archive_projects" not in item:
            raise VerifyConfigError("[[verify.workspace]] archive_projects must be listed explicitly, even when empty")
        projects = item["archive_projects"]
        if (
            not isinstance(projects, list)
            or len(projects) > MAX_ARCHIVE_PROJECTS
            or not all(isinstance(name, str) and PROJECT.fullmatch(name) for name in projects)
            or len(set(projects)) != len(projects)
        ):
            raise VerifyConfigError(
                f"[[verify.workspace]] archive_projects must be a list of at most {MAX_ARCHIVE_PROJECTS} "
                "distinct archive project names"
            )
        assert isinstance(scope, str)
        commit_refs = _commit_refs(item)
        identity = _directory(path)
        resolved = os.path.normcase(_realpath(path))
        entries.append(
            Workspace(
                scope=scope,
                path=path,
                archive_projects=tuple(projects),
                resolved=resolved,
                identity=identity,
                commit_refs=commit_refs,
            )
        )
    # Ownership compares directory identity, never spelling: a case-only variant on a
    # case-insensitive filesystem, a symlink alias and a differently spelled parent
    # all name the same directory and are caught here.
    parents = {entry.identity: _ancestors(entry.resolved) for entry in entries}
    for index, entry in enumerate(entries):
        for other in entries[index + 1 :]:
            if entry.identity == other.identity:
                if entry.scope != other.scope:
                    raise VerifyConfigError("[[verify.workspace]] a workspace root is bound to two scopes")
                raise VerifyConfigError("[[verify.workspace]] a workspace root is listed twice")
            if entry.identity in parents[other.identity] or other.identity in parents[entry.identity]:
                raise VerifyConfigError("[[verify.workspace]] a workspace root is nested inside another root")
    return tuple(sorted(entries, key=lambda entry: (entry.resolved, entry.scope)))


def parse(data: dict[str, Any]) -> VerifyConfig:
    """Validate the `[verify]` table of an already decoded memory-care config."""
    table = data.get("verify")
    if table is None:
        return VerifyConfig()
    if not isinstance(table, dict):
        raise VerifyConfigError("[verify] must be a table")
    if set(table) & HUB_KEYS:
        raise VerifyConfigError(HUB_KEY_REFUSAL)
    unknown = sorted(set(table) - VERIFY_KEYS)
    if unknown:
        raise VerifyConfigError(f"[verify] has unknown keys: {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        raise VerifyConfigError("[verify] enabled must be true or false")
    propose, confirm = _seat(table, "propose_seat"), _seat(table, "confirm_seat")
    if propose is not None and propose == confirm:
        raise VerifyConfigError("[verify] propose_seat and confirm_seat must name different seats")
    return VerifyConfig(
        enabled=enabled,
        propose_seat=propose,
        confirm_seat=confirm,
        cards_per_run=_integer(table, "cards_per_run", DEFAULT_CARDS_PER_RUN, 1, MAX_CARDS_PER_RUN),
        min_interval_days=_integer(table, "min_interval_days", DEFAULT_MIN_INTERVAL_DAYS, 1, MAX_MIN_INTERVAL_DAYS),
        never_read_days=_integer(table, "never_read_days", DEFAULT_NEVER_READ_DAYS, 1, MAX_NEVER_READ_DAYS),
        workspaces=_workspaces(table.get("workspace")),
    )


def load(target: Any) -> VerifyConfig:
    """Read `[verify]` from the target's memory-care config. Absent file or table means not configured."""
    try:
        raw = io.read_bytes(target / CONFIG, absent=True)
    except (OSError, ValueError):
        raise VerifyConfigError("memory-care config could not be read safely") from None
    if raw is None:
        return VerifyConfig()
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise VerifyConfigError("invalid memory-care config") from None
    return parse(data)
