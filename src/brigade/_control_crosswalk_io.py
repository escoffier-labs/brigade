"""Bounded transport and dependency snapshots for control assessments.

This module owns held directory handles, aggregate read budgets and private
verifier inputs. Assessment policy and public readiness schemas stay in the
control crosswalk facade. Dependencies are passed explicitly so transport never
imports that facade.
"""

from __future__ import annotations

import errno
import glob
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import approval, attestation, attestation_input, dirfd, localio, proc, run_journal


class _ReadRefusal(OSError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _AssessmentReader:
    """Hold the canonical assessment root and every descended directory.

    Reads and discovery never resolve an artifact pathname again. Bytes copied
    for delegated verifiers are private, bounded and fixed for this assessment,
    but observations across files do not constitute an atomic filesystem view.
    """

    def __init__(self, target: Path, *, byte_budget: int = 64 * 1024 * 1024):
        self.target = target
        self._byte_budget = byte_budget
        self.directories: dict[tuple[str, ...], int] = {(): dirfd.open_directory_nofollow(target, writable=False)}
        self.errors: dict[Path, str] = {}
        self.contents: dict[Path, bytes] = {}
        self.used = 0
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.copied: set[Path] = set()
        self.populations: dict[Path, list[str]] = {}
        self.approval_context: approval.ApprovalVerificationContext | None = None

    @property
    def byte_budget(self) -> int:
        return self._byte_budget

    @property
    def snapshot_target(self) -> Path:
        if self.temporary is None:
            self.temporary = tempfile.TemporaryDirectory(prefix="brigade-controls-")
        return Path(self.temporary.name)

    def close(self) -> None:
        try:
            if self.temporary is not None:
                self.temporary.cleanup()
        finally:
            for descriptor in reversed(list(self.directories.values())):
                os.close(descriptor)

    def directory(self, path: Path) -> int:
        parts = path.relative_to(self.target).parts
        parent = self.directories[()]
        for index, name in enumerate(parts):
            key = parts[: index + 1]
            if key not in self.directories:
                info = dirfd.stat_child(parent, name)
                if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) or 0) & 0x400:
                    raise _ReadRefusal("symlink_refused")
                if not stat.S_ISDIR(info.st_mode):
                    raise _ReadRefusal("discovery_unreadable")
                try:
                    self.directories[key] = dirfd.open_child_directory(parent, name, writable=False)
                except OSError as exc:
                    # An open racing a new link is still refused. No check is
                    # used as authorization for a later pathname operation.
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise _ReadRefusal("symlink_refused") from exc
                    raise
            parent = self.directories[key]
        return parent

    def info(self, path: Path) -> os.stat_result:
        if path == self.target:
            return os.fstat(self.directories[()])
        return dirfd.stat_child(self.directory(path.parent), path.name)

    def refusal(self, path: Path, exc: OSError) -> str:
        reason = (
            exc.reason
            if isinstance(exc, _ReadRefusal)
            else ("symlink_refused" if "reparse point" in str(exc) else "discovery_unreadable")
        )
        self.errors[path] = reason
        return reason

    def read(self, path: Path, limit: int = attestation_input.MAX_JSON_BYTES) -> bytes:
        if path in self.contents:
            cached = self.contents[path]
            if len(cached) > limit:
                raise _ReadRefusal("read_limit_exceeded")
            return cached
        descriptor = -1
        try:
            parent = self.directory(path.parent)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = dirfd.open_child_file(parent, path.name, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _ReadRefusal("discovery_unreadable")
            permitted = min(limit, self.byte_budget - self.used)
            if permitted < 0 or info.st_size > permitted:
                raise _ReadRefusal("read_limit_exceeded")
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, min(65536, permitted + 1 - len(raw)))
                if not chunk:
                    break
                # Charge transport even when growth or a later I/O error
                # refuses this file. Once the single aggregate overflow byte
                # is consumed, subsequent uncached reads are refused above.
                self.used += len(chunk)
                raw.extend(chunk)
                if len(raw) > permitted:
                    raise _ReadRefusal("read_limit_exceeded")
            result = bytes(raw)
            self.contents[path] = result
            return result
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                exc = _ReadRefusal("symlink_refused")
            self.refusal(path, exc)
            raise exc
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def copy(self, path: Path, *, limit: int = attestation_input.MAX_JSON_BYTES) -> Path:
        destination = self.snapshot_target / path.relative_to(self.target)
        if path in self.copied:
            return destination
        try:
            self.info(path)
        except FileNotFoundError:
            return destination  # Preserve absence of optional dependencies.
        except OSError as exc:
            self.refusal(path, exc)
            raise
        try:
            raw = self.read(path, limit)
        except FileNotFoundError as exc:
            # Disappearance after discovery is not optional-data absence.
            self.errors[path] = "discovery_unreadable"
            raise _ReadRefusal("discovery_unreadable") from exc
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("xb") as stream:
            stream.write(raw)
        self.copied.add(path)
        return destination


class _ReadContext(Protocol):
    """Only the held reader and canonical root are needed by transport."""

    @property
    def target(self) -> Path: ...

    @property
    def reader(self) -> _AssessmentReader: ...


class _GitRunner(Protocol):
    """A caller-bound Git transport, including its environment seam."""

    def __call__(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def _path_refusal(ctx: _ReadContext, path: Path, *, include_leaf: bool = False) -> str | None:
    """Check components using held handles; never authorize pathname I/O."""
    try:
        ctx.reader.directory(path.parent)
        if include_leaf and _is_symlink(ctx, path):
            return "symlink_refused"
    except FileNotFoundError:
        return None
    except OSError as exc:
        return ctx.reader.refusal(path, exc)
    return None


def _read_json_object(ctx: _ReadContext, path: Path) -> dict[str, Any] | None:
    """Bounded, regular-file, handle-relative JSON read."""
    try:
        value = attestation_input.strict_json_loads(ctx.reader.read(path))
        return value if isinstance(value, dict) else None
    except (attestation_input.AttestationInputError, OSError):
        return None


def _is_symlink(ctx: _ReadContext, path: Path) -> bool:
    try:
        info = ctx.reader.info(path)
        return stat.S_ISLNK(info.st_mode) or bool((getattr(info, "st_file_attributes", 0) or 0) & 0x400)
    except FileNotFoundError:
        return False
    except OSError as exc:
        # NT metadata opens refuse reparse points instead of returning link
        # metadata. Preserve that refusal just as POSIX S_IFLNK metadata does.
        return ctx.reader.refusal(path, exc) == "symlink_refused"


def _is_regular(ctx: _ReadContext, path: Path) -> bool:
    try:
        return stat.S_ISREG(ctx.reader.info(path).st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        ctx.reader.refusal(path, exc)
        return False


def _read_journal(ctx: _ReadContext, path: Path) -> run_journal.JournalReport:
    from .run_checkpoint import MAX_JOURNAL_BYTES

    try:
        snapshot = ctx.reader.copy(path, limit=MAX_JOURNAL_BYTES)
    except _ReadRefusal as exc:
        if exc.reason == "read_limit_exceeded":
            raise run_journal.RunJournalError("bound exceeded: journal byte budget") from exc
        raise
    return run_journal.read_journal_bounded(snapshot)


def _copy_public_trust(ctx: _ReadContext) -> Path:
    root = ctx.target / ".brigade" / "attestation"
    for name in (attestation.DEFAULT_ALLOWED_SIGNERS_NAME, attestation.DEFAULT_REVOKED_KEYS_NAME):
        ctx.reader.copy(root / name)
    return ctx.reader.snapshot_target


def _verify_attestation_snapshot(ctx: _ReadContext, path: Path, **kwargs: Any) -> attestation.AttestationVerifyResult:
    target = _copy_public_trust(ctx)
    envelope = ctx.reader.copy(path)
    ctx.reader.copy(path.parent / "receipt.json")
    return attestation.verify_attestation(envelope, target=target, **kwargs)


def _copy_population(
    ctx: _ReadContext,
    root: Path,
    names: tuple[str, ...] | None = None,
    *,
    max_children: int,
    read_json: Callable[[Path], dict[str, Any] | None],
) -> None:
    children, truncated, error = _bounded_children(ctx, root, max_children=max_children)
    if truncated or error is not None:
        reason = "read_limit_exceeded" if truncated else error or "discovery_unreadable"
        ctx.reader.errors[root] = reason
        raise _ReadRefusal(reason)
    for child in children:
        if names is None:
            if child.name.endswith(".json"):
                ctx.reader.copy(child)
                if read_json(child) is None:
                    ctx.reader.errors[child] = "invalid_json"
                    raise _ReadRefusal("invalid_json")
        elif stat.S_ISDIR(ctx.reader.info(child).st_mode):
            # Bind the directory before copying even absent children. A linked
            # population member is a refusal, never silently omitted.
            ctx.reader.directory(child)
            for name in names:
                dependency = child / name
                ctx.reader.copy(dependency)
                if dependency in ctx.reader.copied and name.endswith(".json") and read_json(dependency) is None:
                    ctx.reader.errors[dependency] = "invalid_json"
                    raise _ReadRefusal("invalid_json")
        elif _is_symlink(ctx, child):
            ctx.reader.errors[child] = "symlink_refused"
            raise _ReadRefusal("symlink_refused")


def _approval_live_tree(ctx: _ReadContext, key_path: Path, *, run_git: _GitRunner) -> str | None:
    """Compute the original workspace tree without staging private key bytes.

    Preserve the shared fingerprint's normalization, but explicitly exclude the
    signing key even if ignore rules change during collection. A key included
    by the original normalization is a refusal rather than a different tree
    silently compared against the approval. Git output/time bounds still apply.
    """
    relative_key = key_path.relative_to(ctx.target).as_posix()
    with tempfile.TemporaryDirectory(prefix="brigade-controls-index-") as temporary:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return run_git(*args, env=env)

        try:
            head = git("rev-parse", "HEAD")
            if head.returncode != 0 or not head.stdout.strip():
                return None
            excluded = {relative_key, ".brigade/attestation/" + attestation.DEFAULT_KEY_NAME}
            for name in sorted(excluded):
                try:
                    ctx.reader.info(ctx.target / name)
                except FileNotFoundError:
                    continue
                tracked = git("ls-files", "--error-unmatch", "--", name)
                ignored = git("check-ignore", "-q", "--", name)
                if tracked.returncode == 0 or ignored.returncode != 0:
                    ctx.reader.errors[ctx.target / name] = "discovery_unreadable"
                    raise _ReadRefusal("discovery_unreadable")
            if git("read-tree", head.stdout.strip()).returncode != 0:
                return None
            patterns = []
            for name in sorted(excluded):
                if not name[0].isascii() or not (name[0].isalnum() or name[0] == "."):
                    raise _ReadRefusal("discovery_unreadable")
                # An exact exclusion with a literal ignored prefix makes Git
                # reject add -A. Bracket-quote its first ASCII character to
                # keep the same match without treating that prefix as input.
                patterns.append(":(exclude,glob)[" + name[0] + "]" + glob.escape(name[1:]))
            if git("add", "-A", "--", ".", *patterns).returncode != 0:
                return None
            if git("reset", "-q", head.stdout.strip(), "--", *localio.TREE_FINGERPRINT_EVIDENCE_PATHS).returncode != 0:
                return None
            value = git("write-tree")
            return value.stdout.strip() if value.returncode == 0 and value.stdout.strip() else None
        except _ReadRefusal:
            raise
        except (OSError, subprocess.TimeoutExpired, _GitReadLimitExceeded):
            return None


def _verify_approval_snapshot(
    ctx: _ReadContext,
    run_dir: Path,
    *,
    max_children: int,
    read_json: Callable[[Path], dict[str, Any] | None],
    live_tree: Callable[[Path], str | None],
) -> approval.ApprovalVerification:
    from .run_checkpoint import MAX_JOURNAL_BYTES

    target = _copy_public_trust(ctx)
    ctx.reader.copy(run_dir / "run.json")
    ctx.reader.copy(run_dir / "events" / "lifecycle.jsonl", limit=MAX_JOURNAL_BYTES)
    for name in ("approvals", "requests"):
        _copy_population(ctx, run_dir / name, max_children=max_children, read_json=read_json)
    for name in ("request.json", "agent-request.json"):
        ctx.reader.copy(run_dir / name)
    _copy_population(
        ctx,
        ctx.target / ".brigade/work/verify-runs",
        ("receipt.json", "attestation.json", "changes.patch"),
        max_children=max_children,
        read_json=read_json,
    )
    # Compute the live workspace fact at its original location. Never infer
    # freshness or SoD identity from the private dependency snapshot.
    key_path = attestation.resolve_signing_key_path(ctx.target)
    public_key = Path(str(key_path) + ".pub")
    try:
        public_key.relative_to(ctx.target)
    except ValueError as exc:
        raise _ReadRefusal("discovery_unreadable") from exc
    original_tree = (
        ctx.reader.approval_context.live_tree if ctx.reader.approval_context is not None else live_tree(key_path)
    )
    public_snapshot = ctx.reader.copy(public_key)
    workspace_keyid = None
    if public_key in ctx.reader.copied:
        workspace_keyid = attestation.get_key_fingerprint(public_snapshot)
    else:
        try:
            ctx.reader.info(key_path)
        except FileNotFoundError:
            pass
        else:
            # No private key reads, including ssh-keygen's implicit fallback.
            ctx.reader.errors[public_key] = "discovery_unreadable"
            raise _ReadRefusal("discovery_unreadable")
    context = approval.ApprovalVerificationContext(
        target=ctx.target,
        live_tree=original_tree,
        live_tree_state="computed" if original_tree is not None else "unavailable",
        workspace_keyid=workspace_keyid,
        workspace_keyid_state="computed" if workspace_keyid is not None else "unavailable",
        receipts={},
    )
    ctx.reader.approval_context = context
    return approval.verify_run_approval(
        target,
        target / run_dir.relative_to(ctx.target),
        context=context,
    )


def _bounded_children(ctx: _ReadContext, root: Path, *, max_children: int) -> tuple[list[Path], bool, str | None]:
    """Capped directory children as ``(children, truncated, discovery_error)``.

    At most ``max_children + 1`` names are read, in filesystem order, so the scan stays
    bounded however large the directory is.  The kept names are sorted.  When
    the cap is exceeded the population is truncated and the inspected sample
    depends on filesystem order, so it never validates.  A missing root is an
    empty population; a symlinked or unreadable root is a discovery error, not
    an absence.
    """
    try:
        descriptor = ctx.reader.directory(root)
        if root not in ctx.reader.populations:
            ctx.reader.populations[root] = dirfd.child_names(descriptor, max_children + 1)
        names = ctx.reader.populations[root]
    except FileNotFoundError:
        return [], False, None
    except OSError as exc:
        return [], False, ctx.reader.refusal(root, exc)
    return [root / name for name in sorted(names)[:max_children]], len(names) > max_children, None


class _GitReadLimitExceeded(ValueError):
    """Git output crossed the fixed capture or transport byte budget."""


def _git(target: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Read both Git pipes under proc's fixed byte budgets and ten-second timeout."""
    argv = ["git", *args]
    bounded = proc.run(argv, cwd=target, env=env, timeout=10)
    if bounded.output_limit_exceeded or bounded.stream_limit_exceeded:
        raise _GitReadLimitExceeded
    # proc decodes UTF-8 with replacement, as the original Git calls did.
    return subprocess.CompletedProcess(argv, bounded.code, bounded.stdout, bounded.stderr)


def _read_jsonl_records(
    ctx: _ReadContext, path: Path, *, max_records: int
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Bounded strict JSONL read: (records, None) or (None, reason_code)."""
    try:
        raw = ctx.reader.read(path)
    except (attestation_input.AttestationInputError, _ReadRefusal) as exc:
        return None, "read_limit_exceeded" if "byte limit" in str(exc) or "read_limit_exceeded" in str(
            exc
        ) else "invalid_json"
    except OSError:
        return None, "invalid_json"
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(records) >= max_records:
            return None, "read_limit_exceeded"
        try:
            value = attestation_input.strict_json_loads(line)
        except attestation_input.AttestationInputError:
            return None, "invalid_jsonl_line"
        if not isinstance(value, dict):
            return None, "invalid_jsonl_line"
        records.append(value)
    return records, None


@dataclass
class _DirectoryPopulation:
    """Observed paths and ordered refusals, without readiness policy."""

    paths: list[Path] = field(default_factory=list)
    truncated: bool = False
    errors: list[tuple[Path, str]] = field(default_factory=list)


def _run_directories(ctx: _ReadContext, run_id: str | None, *, max_children: int) -> _DirectoryPopulation:
    """Run directories to inspect.

    Run-directory artifacts are bound by directory name, so a run scope
    inspects the selected (already validated bare) run directory directly and
    never loses it behind the scan cap.  A workspace scope uses the bounded scan.
    """
    root = ctx.target / ".brigade" / "runs"
    out = _DirectoryPopulation()
    refusal = _path_refusal(ctx, root)
    if refusal is not None:
        out.errors.append((root, refusal))
        return out
    try:
        mode = ctx.reader.info(root).st_mode
    except FileNotFoundError:
        return out
    except OSError as exc:
        out.errors.append((root, ctx.reader.refusal(root, exc)))
        return out
    if stat.S_ISLNK(mode):
        out.errors.append((root, "symlink_refused"))
        return out
    if not stat.S_ISDIR(mode):
        out.errors.append((root, "discovery_unreadable"))
        return out
    if run_id is not None:
        selected = root / run_id
        try:
            mode = ctx.reader.info(selected).st_mode
        except FileNotFoundError:
            return out
        except OSError as exc:
            out.errors.append((selected, ctx.reader.refusal(selected, exc)))
            return out
        out.paths = [selected] if stat.S_ISDIR(mode) or stat.S_ISLNK(mode) else []
        return out
    try:
        children, out.truncated, error = _bounded_children(ctx, root, max_children=max_children)
    except OSError:
        children, error = [], "discovery_unreadable"
    if error is not None:
        out.errors.append((root, error))
    directories = []
    for child in children:
        try:
            mode = ctx.reader.info(child).st_mode
        except OSError as exc:
            out.errors.append((child, ctx.reader.refusal(child, exc)))
            continue
        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            directories.append(child)
    out.paths = directories
    return out
