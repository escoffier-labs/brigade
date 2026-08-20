"""Scanner run, doctor, and health operations."""

from __future__ import annotations
import contextlib
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import dogfood_cmd
from ..install import apply_gitignore
from . import constants, helpers, ledger as ledger_mod, config as config_mod

from . import sweeps as sweeps_mod


class _ScannerRunProof:
    """Non-serialized verifier capability for one completed built-in scanner run."""

    def __init__(self, *, scanner: dict[str, Any], run_id: str) -> None:
        self.scanner = dict(scanner)
        self.run_id = run_id


_SCANNER_RUN_PROOFS: dict[int, _ScannerRunProof] = {}


class _ScannerRunDirectoryAuthority:
    """Retained descriptor authority for files belonging to one scanner run."""

    def __init__(self, *, root: int, directory: int, run_id: str) -> None:
        self.root = root
        self.directory = directory
        self.run_id = run_id


_SCANNER_RUN_DIRECTORY_AUTHORITIES: dict[int, _ScannerRunDirectoryAuthority] = {}
_SCANNER_RUN_PUBLICATION_SNAPSHOTS: dict[int, dict[str, Any]] = {}

_SCANNER_CHILD_ENV_ALLOWLIST = (
    # Command resolution and locale.
    "PATH",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LANGUAGE",
    "TZ",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    # Scratch space. Without these a child falls back to a system temp dir it
    # may not be allowed to write.
    "TMPDIR",
    "TEMP",
    "TMP",
    # Identity that git and gh read directly instead of through HOME.
    "USER",
    "LOGNAME",
    # Credentials the shipped scanners genuinely need. `handoff-ingest` shells
    # out to `gh`, and git-over-ssh remotes need the agent socket.
    "SSH_AUTH_SOCK",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
    # Network egress.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

# Sandboxed in the child so `component_paths.data_root()` can never resolve to
# the verifier authority store. Nothing on the allowlist above may reach it.
_SCANNER_CHILD_ENV_SANDBOXED = (
    "HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "LOCALAPPDATA",
    "APPDATA",
)


def _parent_gh_config_dir() -> str | None:
    """Resolve the operator's existing `gh` config dir, which the sandboxed HOME hides."""
    configured = os.environ.get("GH_CONFIG_DIR")
    if configured:
        return configured
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        candidate = Path(config_home) / "gh"
    else:
        home = os.environ.get("HOME")
        if not home:
            return None
        candidate = Path(home) / ".config" / "gh"
    try:
        if not candidate.is_dir():
            return None
    except OSError:
        return None
    return str(candidate)


def _scanner_child_environment(sandbox: Path) -> dict[str, str]:
    """Build the child env: an explicit allowlist plus HOME/XDG paths inside ``sandbox``.

    The sandbox is what keeps `component_paths.data_root()` in the child away
    from the verifier authority store. Everything a shipped scanner needs that
    would otherwise be lost with HOME is passed explicitly: `gh` credentials
    (`GH_TOKEN`/`GITHUB_TOKEN`, or `GH_CONFIG_DIR` pointed at the operator's
    real `gh` config, which holds no Brigade authority), `SSH_AUTH_SOCK`,
    `USER`/`LOGNAME`, `TMPDIR`, and proxy settings.
    """
    env = {key: value for key in _SCANNER_CHILD_ENV_ALLOWLIST if (value := os.environ.get(key))}
    data_home = sandbox / ".local" / "share"
    config_home = sandbox / ".config"
    cache_home = sandbox / ".cache"
    state_home = sandbox / ".local" / "state"
    for directory in (data_home, config_home, cache_home, state_home):
        directory.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(sandbox)
    env["XDG_DATA_HOME"] = str(data_home)
    env["XDG_CONFIG_HOME"] = str(config_home)
    env["XDG_CACHE_HOME"] = str(cache_home)
    env["XDG_STATE_HOME"] = str(state_home)
    if "GH_CONFIG_DIR" not in env:
        gh_config = _parent_gh_config_dir()
        if gh_config is not None:
            env["GH_CONFIG_DIR"] = gh_config
    return env


def _seed_child_authority_store(target: Path, env: dict[str, str]) -> None:
    """Give a Brigade child the parent's directory bindings inside its own sandbox.

    The copy is re-signed with an ephemeral sandbox key. Nothing the child
    signs there is authority for the parent. ``BRIGADE_AUTHORITY_KEY_FILE``
    stays off the child env allowlist.
    """
    try:
        _path, payload = ledger_mod._read_external_directory_authority(target)
    except OSError:
        return
    if payload is None:
        return
    try:
        from .. import authority_key

        ephemeral = authority_key.generate_key(env=env)
        child_path = ledger_mod._directory_authority_store_path(target, env=env)
        ledger_mod._write_external_directory_authority(child_path, payload, env=env, key_material=ephemeral)
    except OSError:
        return


@contextlib.contextmanager
def _scanner_child_environment_sandbox(target: Path | None = None) -> Iterator[dict[str, str]]:
    """Yield a child env whose sandbox directory is removed when the child exits."""
    sandbox = Path(tempfile.mkdtemp(prefix="brigade-scanner-child-"))
    try:
        env = _scanner_child_environment(sandbox)
        if target is not None:
            _seed_child_authority_store(target, env)
        yield env
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _prebind_child_visible_directories(target: Path) -> None:
    """Create and bind workspace directories before a child could create them first.

    Scanner children run with a sandboxed HOME, so a directory they create is
    bound only in their throwaway store and the verifier would later refuse it.
    The parent creates and binds those directories up front instead, which keeps
    #871's rule that an unbound pre-existing directory is never adopted.
    """
    try:
        descriptor = ledger_mod._open_import_proof_directory(target, create=True)
    except OSError:
        return
    os.close(descriptor)


_SCANNER_RUNS_COMPONENTS = (".brigade", "scanners", "runs")


def _scanner_runs_root_operator_message(target: Path, reason: str) -> str:
    """Return a fail-closed operator message for an unsafe unbound runs root."""
    path = helpers._scanner_runs_root(target)
    return (
        f"scanner runs directory {path} exists but is unbound and cannot be "
        f"migrated safely: {reason}. Move or remove that directory, then run "
        f"`brigade work scanners doctor --target {target}`."
    )


def _scanner_runs_directory_is_bound(target: Path, extra: tuple[str, ...] = ()) -> bool:
    """Return whether the runs root (or a child) has an external authority record."""
    _path, payload = ledger_mod._read_external_directory_authority(target)
    directories = payload.get("directories") if isinstance(payload, dict) else None
    if not isinstance(directories, dict):
        return False
    return ledger_mod._directory_authority_scope(_SCANNER_RUNS_COMPONENTS + extra) in directories


def _scanner_path_red_flag(metadata: os.stat_result) -> str | None:
    """Return an adoption red-flag reason, or None when the inode is operator-owned.

    POSIX ownership and mode bits are not a containment signal on Windows.
    Reparse points are already refused by the dirfd walk (``nt_dirfd``).
    """
    if not stat.S_ISDIR(metadata.st_mode):
        return "not a directory"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    if metadata.st_uid != geteuid():
        return "owned by a foreign uid"
    if metadata.st_mode & 0o002:
        return "world-writable"
    getegid = getattr(os, "getegid", None)
    getgid = getattr(os, "getgid", None)
    if (
        metadata.st_mode & 0o020
        and getegid is not None
        and getgid is not None
        and metadata.st_gid not in {getegid(), getgid()}
    ):
        return "group-writable by a foreign gid"
    return None


def _scanner_runs_dirfd_is_symlink_error(exc: OSError) -> bool:
    """Return whether a dirfd walk failed because a component is not a real directory."""
    if getattr(exc, "errno", None) in {errno.ELOOP, errno.ENOTDIR}:
        return True
    detail = str(exc).strip().lower()
    return any(token in detail for token in ("reparse", "symlink", "not a directory", "not a single contained name"))


def _bind_released_unbound_scanner_runs_root(target: Path) -> str:
    """Bind a released pre-authority runs root without adopting child trees.

    Returns ``missing`` when the directory is absent, ``bound`` when a record
    already exists, or ``repaired`` after writing a fresh root-only binding.
    Child run directories and receipts stay untrusted. An existing tree that
    is foreign-owned, world-writable, or reached through a symlink fails
    closed. This is not the #1036 legacy adoption fallback.

    Descriptor-relative walks use the ledger dirfd abstraction so Windows
    binds through ``nt_dirfd`` the same way import-inbox does. When no
    platform dirfd exists and the tree is absent, return ``missing`` so
    first-create paths such as ``operator quickstart`` can proceed. An
    existing unbound tree is never adopted without a descriptor walk.
    """
    if _scanner_runs_directory_is_bound(target):
        return "bound"
    if not ledger_mod._dirfd_available():
        if not helpers._scanner_runs_root(target).exists():
            return "missing"
        raise OSError("descriptor-relative directory authority operations are unavailable")
    descriptor = ledger_mod._open_directory_nofollow(target.expanduser().resolve())
    opened = [descriptor]
    try:
        for component in _SCANNER_RUNS_COMPONENTS:
            try:
                child = ledger_mod._dirfd_open_dir(opened[-1], component)
            except FileNotFoundError:
                return "missing"
            except OSError as exc:
                if _scanner_runs_dirfd_is_symlink_error(exc):
                    raise OSError(
                        _scanner_runs_root_operator_message(target, "path contains a symlink or is not a directory")
                    ) from exc
                raise
            opened.append(child)
            flag = _scanner_path_red_flag(os.fstat(child))
            if flag is not None:
                raise OSError(_scanner_runs_root_operator_message(target, flag))
        ledger_mod._record_external_directory_authority(
            target,
            _SCANNER_RUNS_COMPONENTS,
            opened[-1],
            workspace=ledger_mod._directory_identity(opened[0]),
        )
        return "repaired"
    finally:
        for handle in reversed(opened):
            os.close(handle)


def _scanner_runs_directory_failure_message(target: Path, exc: OSError) -> str:
    """Render a traceback-free operator error for a runs-dir open failure."""
    detail = str(exc).strip() or exc.__class__.__name__
    if "unbound and cannot be migrated safely" in detail:
        return detail
    return f"scanner runs directory is unavailable: {detail}. Run `brigade work scanners doctor --target {target}`."


def _open_scanner_runs_directory(target: Path, *, create: bool) -> int:
    """Open the verifier-owned scanner-runs root.

    A pre-existing unbound tree is never adopted as scheduling authority
    (#1036). There is no OSError fallback into legacy child adoption.
    Create binds only a verifier-owned, uncompromised released root so a
    pre-0.27 workspace can keep sweeping.
    """
    if create:
        _bind_released_unbound_scanner_runs_root(target)
    return ledger_mod._open_verifier_owned_directory(
        target,
        components=_SCANNER_RUNS_COMPONENTS,
        anchor_name=".runs.authority.json",
        create=create,
    )


def _validate_scanner_run_directory(authority: _ScannerRunDirectoryAuthority) -> None:
    if not ledger_mod._dirfd_available():
        raise OSError("descriptor-relative scanner run validation is unavailable")
    opened = os.fstat(authority.directory)
    named_fd = ledger_mod._dirfd_open_dir(authority.root, authority.run_id)
    try:
        named = os.fstat(named_fd)
    finally:
        os.close(named_fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (
            opened.st_dev,
            opened.st_ino,
        )
        != (named.st_dev, named.st_ino)
    ):
        raise OSError("scanner run directory no longer matches its held descriptor")


def _open_scanner_run_directory(target: Path, run_id: str) -> _ScannerRunDirectoryAuthority:
    root = _open_scanner_runs_directory(target, create=True)
    directory = -1
    try:
        ledger_mod._dirfd_mkdir(root, run_id)
        directory = ledger_mod._dirfd_open_dir(root, run_id)
        authority = _ScannerRunDirectoryAuthority(root=root, directory=directory, run_id=run_id)
        _validate_scanner_run_directory(authority)
        ledger_mod._record_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs", run_id),
            directory=directory,
        )
        return authority
    except BaseException:
        if directory != -1:
            os.close(directory)
        os.close(root)
        raise


def _validate_scanner_run_file(parent: int, name: str, descriptor: int) -> None:
    if not ledger_mod._dirfd_available():
        raise OSError("descriptor-relative scanner run file validation is unavailable")
    opened = os.fstat(descriptor)
    named = ledger_mod._dirfd_stat(parent, name)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink)
        != (named.st_dev, named.st_ino, named.st_mode, named.st_nlink)
    ):
        raise OSError("scanner run file no longer matches its held descriptor")


def _list_scanner_run_ids(root: int, target: Path) -> list[str]:
    """List run directory names through a held fd when the platform supports it."""
    if os.listdir in getattr(os, "supports_fd", set()):
        names = os.listdir(root)
    else:
        names = [entry.name for entry in helpers._scanner_runs_root(target).iterdir()]
    return [name for name in names if isinstance(name, str)]


def _scanner_run_file_open_flags(*, write: bool) -> int:
    """Return no-follow file flags; Windows dirfd helpers ignore unknown POSIX bits."""
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if write:
        return os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags
    return os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | flags


def _write_scanner_run_file(
    authority: _ScannerRunDirectoryAuthority,
    name: str,
    data: bytes,
    *,
    after_publish: Callable[[int], None] | None = None,
) -> None:
    """Failure-atomically publish one scanner artifact below a retained run descriptor."""
    _validate_scanner_run_directory(authority)
    existing = -1
    descriptor = -1
    previous_raw = b""
    previous_exists = False
    temporary_name = f".{name}.{uuid4().hex}.tmp"
    try:
        try:
            existing = ledger_mod._dirfd_open_file(
                authority.directory,
                name,
                _scanner_run_file_open_flags(write=False),
            )
        except FileNotFoundError:
            pass
        else:
            _validate_scanner_run_file(authority.directory, name, existing)
            chunks: list[bytes] = []
            while chunk := os.read(existing, 1024 * 1024):
                chunks.append(chunk)
            previous_raw = b"".join(chunks)
            previous_exists = True
        descriptor = ledger_mod._dirfd_open_file(
            authority.directory,
            temporary_name,
            _scanner_run_file_open_flags(write=True),
            0o600,
        )
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_scanner_run_file(authority.directory, temporary_name, descriptor)
        _validate_scanner_run_directory(authority)
        ledger_mod._dirfd_replace(authority.directory, temporary_name, name)
        _validate_scanner_run_file(authority.directory, name, descriptor)
        ledger_mod._dirfd_fsync(authority.directory)
        if after_publish is not None:
            after_publish(descriptor)
    except BaseException:
        try:
            ledger_mod._dirfd_unlink(authority.directory, temporary_name)
        except FileNotFoundError:
            pass
        _restore_scanner_run_file_snapshot(authority, name, previous_raw, previous_exists)
        raise
    finally:
        if existing != -1:
            os.close(existing)
        if descriptor != -1:
            os.close(descriptor)


def _restore_scanner_run_file_snapshot(
    authority: _ScannerRunDirectoryAuthority, name: str, data: bytes, exists: bool
) -> None:
    """Restore a producer artifact through the same retained run authority."""
    if not exists:
        try:
            ledger_mod._dirfd_unlink(authority.directory, name)
        except FileNotFoundError:
            pass
        ledger_mod._dirfd_fsync(authority.directory)
        return
    for _attempt in range(3):
        temporary_name = f".{name}.{uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = ledger_mod._dirfd_open_file(
                authority.directory,
                temporary_name,
                _scanner_run_file_open_flags(write=True),
                0o600,
            )
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_scanner_run_file(authority.directory, temporary_name, descriptor)
            ledger_mod._dirfd_replace(authority.directory, temporary_name, name)
            temporary_name = ""
            _validate_scanner_run_file(authority.directory, name, descriptor)
            ledger_mod._dirfd_fsync(authority.directory)
            return
        except OSError:
            pass
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_name:
                try:
                    ledger_mod._dirfd_unlink(authority.directory, temporary_name)
                except FileNotFoundError:
                    pass
    raise OSError("scanner run artifact rollback could not restore its retained snapshot")


def _apply_scanner_publication_snapshot(snapshot: dict[str, Any], *, owned: Sequence[str] = ()) -> None:
    """Restore inbox, proofs, and file bindings captured before a scanner publication."""
    target = snapshot["target"]
    if not isinstance(target, Path):
        raise OSError("scanner publication snapshot is missing its target")
    proof_items = snapshot.get("proof_items")
    owned_scopes = list(owned)
    if isinstance(proof_items, list) and proof_items:
        ledger_mod._remove_persisted_import_proofs(target, proof_items)
        owned_scopes.extend(ledger_mod._persisted_import_proof_scopes(proof_items))
    inbox = snapshot.get("inbox")
    inbox_exists = bool(snapshot.get("inbox_exists"))
    if isinstance(inbox, bytes):
        _restore_scanner_inbox_bytes(target, inbox, inbox_exists)
    files = snapshot.get("files")
    if not isinstance(files, dict) and files is not None:
        raise OSError("scanner publication snapshot file bindings are malformed")
    ledger_mod._restore_external_file_authorities(target, files, owned=owned_scopes)


def _write_scanner_run_receipt(run: dict[str, Any]) -> None:
    authority = _SCANNER_RUN_DIRECTORY_AUTHORITIES.get(id(run))
    if authority is None:
        raise OSError("scanner run directory authority is unavailable")
    data = (json.dumps(run, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target_value = run.get("target")
    target = Path(target_value) if isinstance(target_value, str) and target_value else None
    bind = target is not None
    prior_files = ledger_mod._snapshot_external_file_authorities(target) if bind else None

    def after_publish(descriptor: int) -> None:
        if not bind or target is None:
            return
        ledger_mod._record_verifier_owned_file(
            target,
            components=(".brigade", "scanners", "runs", authority.run_id, "receipt.json"),
            descriptor=descriptor,
            data=data,
        )

    receipt_scope = ledger_mod._directory_authority_scope(
        (".brigade", "scanners", "runs", authority.run_id, "receipt.json")
    )
    try:
        _write_scanner_run_file(authority, "receipt.json", data, after_publish=after_publish)
    except BaseException:
        if bind and target is not None:
            snapshot = _SCANNER_RUN_PUBLICATION_SNAPSHOTS.pop(id(run), None)
            try:
                if snapshot is not None:
                    _apply_scanner_publication_snapshot(snapshot, owned=[receipt_scope])
                else:
                    ledger_mod._restore_external_file_authorities(target, prior_files, owned=[receipt_scope])
            except OSError as exc:
                raise OSError("scanner receipt binding rollback could not restore its retained snapshot") from exc
        raise
    else:
        _SCANNER_RUN_PUBLICATION_SNAPSHOTS.pop(id(run), None)


def _release_scanner_run_directory_authority(run: dict[str, Any]) -> None:
    _SCANNER_RUN_PUBLICATION_SNAPSHOTS.pop(id(run), None)
    authority = _SCANNER_RUN_DIRECTORY_AUTHORITIES.pop(id(run), None)
    if authority is not None:
        os.close(authority.directory)
        os.close(authority.root)


def _register_scanner_run_proof(scanner: dict[str, Any], run: dict[str, Any]) -> None:
    """Keep authority for a completed built-in scan outside its serialized row bytes."""
    run_id = run.get("run_id")
    if run.get("status") == "completed" and run.get("exit_code") == 0 and isinstance(run_id, str) and run_id:
        _SCANNER_RUN_PROOFS[id(run)] = _ScannerRunProof(scanner=scanner, run_id=run_id)


def _scanner_run_proof(scanner: dict[str, Any], run: dict[str, Any]) -> _ScannerRunProof | None:
    """Return a capability only for this exact verifier-owned scanner run object."""
    proof = _SCANNER_RUN_PROOFS.get(id(run))
    if proof is None or proof.run_id != run.get("run_id") or proof.scanner != scanner:
        return None
    return proof


def _record_scanner_import_proof(scanner: dict[str, Any], run: dict[str, Any], item: dict[str, Any]) -> None:
    """Persist the verifier's content binding outside the imported row itself."""
    proof_data = run.setdefault(
        "self_import_proofs",
        {
            "scanner_id": scanner.get("id"),
            "source": scanner.get("source"),
            "scanner": dict(scanner),
            "imports": [],
        },
    )
    if isinstance(proof_data, dict) and isinstance(proof_data.get("imports"), list):
        proof_data["imports"].append(
            {
                "id": item.get("id"),
                "content_hash": ledger_mod._locally_stamped_import_content_hash(item),
            }
        )


def _read_scanner_receipt_at(target: Path, root: int, run_id: str, *, path: Path) -> dict[str, Any] | None:
    """Read one receipt through the anchored runs descriptor."""
    run = -1
    receipt = -1
    try:
        run = ledger_mod._dirfd_open_dir(root, run_id)
        opened_run = os.fstat(run)
        named_run_fd = ledger_mod._dirfd_open_dir(root, run_id)
        try:
            named_run = os.fstat(named_run_fd)
        finally:
            os.close(named_run_fd)
        if not stat.S_ISDIR(opened_run.st_mode) or (opened_run.st_dev, opened_run.st_ino) != (
            named_run.st_dev,
            named_run.st_ino,
        ):
            return None
        ledger_mod._validate_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
        receipt = ledger_mod._dirfd_open_file(run, "receipt.json", _scanner_run_file_open_flags(write=False))
        before = os.fstat(receipt)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return None
        chunks: list[bytes] = []
        while chunk := os.read(receipt, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(receipt)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_nlink) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
        ):
            return None
        _validate_scanner_run_directory(_ScannerRunDirectoryAuthority(root=root, directory=run, run_id=run_id))
        ledger_mod._validate_verifier_owned_directory(
            target,
            components=(".brigade", "scanners", "runs", run_id),
            directory=run,
        )
        payload = b"".join(chunks)
        named_receipt = ledger_mod._dirfd_stat(run, "receipt.json")
        if (
            not stat.S_ISREG(named_receipt.st_mode)
            or named_receipt.st_nlink != 1
            or (named_receipt.st_dev, named_receipt.st_ino, named_receipt.st_mode, named_receipt.st_nlink)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink)
        ):
            return None
        _validate_scanner_run_file(run, "receipt.json", receipt)
        ledger_mod._validate_verifier_owned_file(
            target,
            components=(".brigade", "scanners", "runs", run_id, "receipt.json"),
            descriptor=receipt,
            data=payload,
        )
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            return None
        data.setdefault("path", str(path))
        return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        if receipt != -1:
            os.close(receipt)
        if run != -1:
            os.close(run)


def _scanner_read_receipt(path: Path) -> dict[str, Any] | None:
    """Compatibility reader that still requires the runs-root authority anchor."""
    run_path = path.parent if path.name == "receipt.json" else path
    try:
        target = run_path.parents[3]
    except IndexError:
        return None
    try:
        root = _open_scanner_runs_directory(target, create=False)
    except OSError:
        return None
    try:
        return _read_scanner_receipt_at(target, root, run_path.name, path=run_path)
    finally:
        os.close(root)


def _scanner_receipt_collection(target: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        root = _open_scanner_runs_directory(target, create=False)
    except OSError:
        return [], []
    try:
        valid: list[dict[str, Any]] = []
        malformed: list[str] = []
        for run_id in _list_scanner_run_ids(root, target):
            if not isinstance(run_id, str):
                continue
            if not _scanner_runs_directory_is_bound(target, (run_id,)):
                continue
            path = helpers._scanner_runs_root(target) / run_id
            receipt = _read_scanner_receipt_at(target, root, run_id, path=path)
            if receipt is None:
                malformed.append(run_id)
            else:
                valid.append(receipt)
        return valid, malformed
    finally:
        os.close(root)


def _scanner_receipts(target: Path) -> list[dict[str, Any]]:
    valid, _malformed = _scanner_receipt_collection(target)
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _scanner_read_sweep(path: Path) -> dict[str, Any] | None:
    report = path / "sweep.json" if path.is_dir() else path
    if not report.is_file():
        return None
    try:
        data = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(report.parent))
    return data


def _scanner_sweeps(target: Path) -> list[dict[str, Any]]:
    root = helpers._scanner_sweeps_root(target)
    if not root.is_dir():
        return []
    sweeps = [_scanner_read_sweep(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in sweeps if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("sweep_id") or ""), reverse=True)
    return valid


def _scanner_latest_sweep(target: Path) -> dict[str, Any] | None:
    sweeps = _scanner_sweeps(target)
    return sweeps[0] if sweeps else None


def _scanner_latest_success(target: Path, scanner_id: str) -> dict[str, Any] | None:
    for receipt in _scanner_receipts(target):
        if (
            receipt.get("scanner_id") == scanner_id
            and receipt.get("status") == "completed"
            and receipt.get("exit_code") == 0
        ):
            return receipt
    return None


def _scanner_stale_hours(cadence: str) -> float:
    """Return the freshness WARN threshold for a scanner cadence."""
    value = (cadence or "").strip()
    if value.startswith("weekly@"):
        return float(constants.SCANNER_WEEKLY_STALE_HOURS)
    return float(constants.SCANNER_OUTPUT_STALE_HOURS)


def _scanner_is_due(target: Path, scanner: dict[str, Any], *, now: datetime | None = None) -> bool:
    now = now or helpers._now()
    scanner_id = str(scanner.get("id") or "")
    latest = _scanner_latest_success(target, scanner_id)
    if latest is None:
        return True
    started = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
    if started is None:
        return True
    cadence = str(scanner.get("cadence") or "")
    if cadence.startswith("hourly@"):
        return (now - started).total_seconds() >= 3600
    if cadence.startswith("daily@"):
        return now.date() > started.date()
    if cadence.startswith("weekly@"):
        return (now - started).total_seconds() >= 7 * 24 * 3600
    return False


def _scanner_due_items(target: Path, scanners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [scanner for scanner in scanners if scanner.get("enabled", True) and _scanner_is_due(target, scanner)]


def _scanner_running_receipts(target: Path) -> list[dict[str, Any]]:
    return [receipt for receipt in _scanner_receipts(target) if receipt.get("status") == "running"]


def _scanner_output_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "size": stat.st_size if path.is_file() else None,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _scanner_run_summary(text: str, limit: int = 1200) -> str:
    rendered = text.strip()
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _scanner_run_receipt_path(run: dict[str, Any]) -> str | None:
    path = run.get("path")
    if isinstance(path, str) and path.strip():
        return str(Path(path) / "receipt.json")
    return None


def _scanner_import_fingerprint(record: dict[str, Any], *, scanner: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    scanner_source = str(scanner.get("source") or "scanner").strip() or "scanner"
    source_key = ledger_mod._safe_self_import_source_item_key(
        metadata.get("source_item_key"), importer_source=scanner_source
    )
    source_fingerprint = ledger_mod._safe_stable_source_fingerprint(metadata.get("source_fingerprint"))
    if (
        source_key is not None
        and source_fingerprint is not None
        and not ledger_mod._has_canonical_untrusted_import_identity(record)
    ):
        return source_fingerprint
    return ledger_mod._untrusted_import_canonical_hash(record)


def _scanner_import_provenance(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    record: dict[str, Any],
) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    output_after = run.get("output_after") if isinstance(run.get("output_after"), dict) else None
    provenance = {
        "scanner_id": scanner.get("id"),
        "scanner_source": scanner.get("source"),
        "scanner_run_id": run.get("run_id"),
        "scanner_receipt_path": _scanner_run_receipt_path(run),
        "scanner_output_path_snapshot": output_after,
        "source_fingerprint": _scanner_import_fingerprint(record, scanner=scanner),
    }
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is not None:
        provenance["scanner_import_path"] = str(import_path)
    return {key: value for key, value in {**metadata, **provenance}.items() if value is not None}


def _scanner_enrich_import_records(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        scanner_source = str(scanner.get("source") or "scanner").strip() or "scanner"
        item = ledger_mod._sanitize_untrusted_import_record(record, importer_source=scanner_source)
        item["metadata"] = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=item)
        enriched.append(item)
    return enriched


def _trusted_builtin_scanner(scanner: dict[str, Any]) -> bool:
    """Return whether verifier-owned scanner configuration authorizes privileged fields."""
    normalized = _normalized_scanner_configuration(scanner)
    if normalized is None:
        return False
    for trusted in constants.SCANNER_DEFAULTS:
        if normalized == _normalized_scanner_configuration(trusted):
            return True
    return False


def _normalized_scanner_configuration(scanner: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize every scanner field that can affect privileged self-import behavior."""
    required = ("id", "source", "command", "cadence", "enabled", "timeout", "output_path", "conflict_window")
    optional = ("cwd", "import_path", "import_format")
    if set(scanner) - {*required, *optional} or any(field not in scanner for field in required):
        return None
    normalized: dict[str, Any] = {}
    for field in ("id", "source", "command", "cadence", "output_path", "conflict_window"):
        value = scanner.get(field)
        if not isinstance(value, str):
            return None
        normalized[field] = value.strip()
    if not isinstance(scanner.get("enabled"), bool):
        return None
    normalized["enabled"] = scanner["enabled"]
    timeout = scanner.get("timeout")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return None
    normalized["timeout"] = float(timeout)
    for field in optional:
        if field not in scanner:
            continue
        value = scanner[field]
        if not isinstance(value, str):
            return None
        normalized[field] = value.strip()
    return normalized


def _raw_jsonl_rows(raw: bytes) -> list[bytes]:
    return raw.splitlines(keepends=True)


def _raw_row_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _scanner_inbox_path(target: Path) -> Path:
    """Return the inbox only when its parent remains contained by the target."""
    target_root = target.expanduser().resolve()
    inbox_path = helpers._imports_path(target_root)
    resolved_parent = inbox_path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(target_root)
    except ValueError as exc:
        raise OSError("scanner inbox parent escapes target") from exc
    return resolved_parent / inbox_path.name


def _scanner_inbox_identity(metadata: os.stat_result) -> tuple[int, int] | None:
    """Return a meaningful device/inode identity when the platform supplies one."""
    try:
        device, inode = metadata.st_dev, metadata.st_ino
    except AttributeError:
        return None
    if not isinstance(device, int) or not isinstance(inode, int) or device <= 0 or inode <= 0:
        return None
    return device, inode


def _scanner_inbox_has_single_link(metadata: os.stat_result) -> bool:
    """Return whether metadata proves the inbox has exactly one directory entry."""
    try:
        link_count = metadata.st_nlink
    except AttributeError:
        return False
    return type(link_count) is int and link_count == 1


def _scanner_inbox_open_primitives_available() -> bool:
    """Return whether this platform can keep every inbox path component no-follow."""
    return (
        os.name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
    )


def _open_scanner_inbox_parent(target: Path, *, create: bool) -> tuple[int, str, tuple[tuple[int, int], ...]]:
    """Open the inbox parent by descriptors, rejecting every symlinked component."""
    if not _scanner_inbox_open_primitives_available():
        raise OSError("descriptor-relative scanner inbox operations are unavailable")
    target_root = target.expanduser().resolve()
    inbox_path = helpers._imports_path(target_root)
    try:
        relative = inbox_path.relative_to(target_root)
    except ValueError as exc:
        raise OSError("scanner inbox escapes target") from exc
    if len(relative.parts) < 2:
        raise OSError("scanner inbox path is incomplete")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(target_root, directory_flags)
    identities: list[tuple[int, int]] = []
    try:
        opened = os.fstat(descriptor)
        identity = _scanner_inbox_identity(opened)
        if not stat.S_ISDIR(opened.st_mode) or identity is None:
            raise OSError("scanner inbox root identity unavailable")
        identities.append(identity)
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            opened = os.fstat(descriptor)
            identity = _scanner_inbox_identity(opened)
            if not stat.S_ISDIR(opened.st_mode) or identity is None:
                raise OSError("scanner inbox parent is not an identified directory")
            identities.append(identity)
        return descriptor, relative.parts[-1], tuple(identities)
    except BaseException:
        os.close(descriptor)
        raise


def _scanner_inbox_parent_is_current(target: Path, identities: tuple[tuple[int, int], ...]) -> bool:
    """Reject a namespace swap even though held descriptors would remain safe."""
    try:
        descriptor, _name, current = _open_scanner_inbox_parent(target, create=False)
    except OSError:
        return False
    try:
        return current == identities
    finally:
        os.close(descriptor)


def _validate_scanner_inbox_descriptor(descriptor: int) -> None:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not _scanner_inbox_has_single_link(opened)
        or _scanner_inbox_identity(opened) is None
    ):
        raise OSError("scanner inbox is not an identified single-link regular file")


def _open_scanner_inbox(target: Path, flags: int, *, create: bool = False) -> int:
    """Open the inbox through held parent descriptors with no-follow authority."""
    parent, name, identities = _open_scanner_inbox_parent(target, create=create)
    safe_flags = flags | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        try:
            descriptor = os.open(name, safe_flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            descriptor = os.open(name, safe_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        try:
            _validate_scanner_inbox_descriptor(descriptor)
            if not _scanner_inbox_parent_is_current(target, identities):
                raise OSError("scanner inbox parent changed while opening")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def _validate_scanner_inbox_at(parent: int, name: str, *, missing_ok: bool) -> None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        _validate_scanner_inbox_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _validate_scanner_inbox_name_matches_descriptor(parent: int, name: str, descriptor: int) -> None:
    """Require a no-follow name lookup to still identify the held descriptor."""
    _validate_scanner_inbox_descriptor(descriptor)
    expected_identity = _scanner_inbox_identity(os.fstat(descriptor))
    assert expected_identity is not None
    candidate = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
    try:
        try:
            _validate_scanner_inbox_descriptor(candidate)
        except OSError as exc:
            raise OSError("temporary scanner inbox changed before replacement") from exc
        if _scanner_inbox_identity(os.fstat(candidate)) != expected_identity:
            raise OSError("temporary scanner inbox changed before replacement")
    finally:
        os.close(candidate)


def _fsync_scanner_inbox_parent(parent: int) -> None:
    """Durably record an inbox replacement when the platform supports directory fsync."""
    try:
        os.fsync(parent)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise


def _scanner_inbox_temp_name() -> str:
    return f".inbox.{uuid4().hex}.tmp"


def _write_scanner_inbox_temp(parent: int, data: bytes) -> tuple[str, int]:
    """Write and retain one validated temporary inbox object."""
    name = _scanner_inbox_temp_name()
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent,
    )
    try:
        with os.fdopen(os.dup(descriptor), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_scanner_inbox_descriptor(descriptor)
        return name, descriptor
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        raise


def _scanner_inbox_descriptor_bytes(descriptor: int) -> bytes:
    """Read the retained regular inbox object for a rollback snapshot."""
    _validate_scanner_inbox_descriptor(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _restore_scanner_inbox_from_snapshot(parent: int, name: str, data: bytes) -> None:
    """Restore a failed publication, retrying if a rollback temp is substituted."""
    for _attempt in range(3):
        temporary_name, temporary = _write_scanner_inbox_temp(parent, data)
        try:
            os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
            temporary_name = ""
            try:
                _validate_scanner_inbox_name_matches_descriptor(parent, name, temporary)
            except OSError:
                continue
            return
        finally:
            os.close(temporary)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent)
                except FileNotFoundError:
                    pass
    _remove_scanner_inbox_at(parent, name)
    _fsync_scanner_inbox_parent(parent)
    raise OSError("scanner inbox rollback could not restore its retained snapshot")


def _remove_scanner_inbox_at(parent: int, name: str) -> None:
    """Restore a previously missing inbox after a failed first publication."""
    try:
        os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    _validate_scanner_inbox_at(parent, name, missing_ok=True)


def _write_scanner_inbox_bytes(target: Path, data: bytes) -> None:
    """Atomically publish complete inbox bytes through a held no-follow parent."""
    parent, name, identities = _open_scanner_inbox_parent(target, create=True)
    temporary_name = _scanner_inbox_temp_name()
    temporary = -1
    previous = -1
    previous_bytes = b""
    rollback_required = False
    try:
        try:
            previous = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            pass
        else:
            _validate_scanner_inbox_descriptor(previous)
            previous_bytes = _scanner_inbox_descriptor_bytes(previous)
        temporary_name, temporary = _write_scanner_inbox_temp(parent, data)
        if not _scanner_inbox_parent_is_current(target, identities):
            raise OSError("scanner inbox parent changed before replacement")
        _validate_scanner_inbox_name_matches_descriptor(parent, temporary_name, temporary)
        rollback_required = True
        os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary_name = ""
        _validate_scanner_inbox_name_matches_descriptor(parent, name, temporary)
        if not _scanner_inbox_parent_is_current(target, identities):
            raise OSError("scanner inbox parent changed during replacement")
        _fsync_scanner_inbox_parent(parent)
        rollback_required = False
    except BaseException:
        if rollback_required:
            if previous != -1:
                _restore_scanner_inbox_from_snapshot(parent, name, previous_bytes)
            else:
                _remove_scanner_inbox_at(parent, name)
            _fsync_scanner_inbox_parent(parent)
        raise
    finally:
        if previous != -1:
            os.close(previous)
        if temporary != -1:
            os.close(temporary)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _scanner_inbox_bytes(target: Path) -> bytes:
    """Read a regular inbox through a held no-follow parent descriptor."""
    try:
        descriptor = _open_scanner_inbox(target, os.O_RDONLY)
    except FileNotFoundError:
        return b""
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _append_scanner_inbox_bytes(target: Path, data: bytes) -> None:
    """Append scanner records by atomically publishing complete inbox bytes."""
    existing = _scanner_inbox_bytes(target)
    separator = b"\n" if existing and not existing.endswith((b"\n", b"\r")) else b""
    _write_scanner_inbox_bytes(target, existing + separator + data)


def _restore_scanner_inbox_bytes(target: Path, data: bytes, exists: bool) -> None:
    """Restore the scanner inbox transaction to its exact pre-publication state."""
    if exists:
        _write_scanner_inbox_bytes(target, data)
        return
    parent, name, identities = _open_scanner_inbox_parent(target, create=True)
    try:
        if not _scanner_inbox_parent_is_current(target, identities):
            raise OSError("scanner inbox parent changed during restoration")
        _remove_scanner_inbox_at(parent, name)
        _fsync_scanner_inbox_parent(parent)
    finally:
        os.close(parent)


def _restore_scanner_inbox_snapshot_direct(target: Path, data: bytes, exists: bool) -> None:
    """Recover a scanner transaction without re-entering its publication wrapper."""
    parent, name, identities = _open_scanner_inbox_parent(target, create=True)
    try:
        if not _scanner_inbox_parent_is_current(target, identities):
            raise OSError("scanner inbox parent changed during restoration")
        if exists:
            _restore_scanner_inbox_from_snapshot(parent, name, data)
        else:
            _remove_scanner_inbox_at(parent, name)
        _fsync_scanner_inbox_parent(parent)
    finally:
        os.close(parent)


def _scanner_inbox_imports(target: Path) -> list[dict[str, Any]]:
    return [item for raw in _raw_jsonl_rows(_scanner_inbox_bytes(target)) if (item := _raw_row_object(raw)) is not None]


def _scanner_import_counts(target: Path) -> dict[str, Any]:
    try:
        records = _scanner_inbox_imports(target)
    except OSError:
        records = []
    imports = [
        item
        for item in records
        if item.get("status", "pending") == "pending" and isinstance(item.get("text"), str) and item["text"].strip()
    ]
    return ledger_mod._import_counts(imports)


def _reconcile_known_row(before: dict[str, Any], after: dict[str, Any], *, trusted_scanner: bool) -> bool:
    """Allow only built-in lifecycle mutation of an existing local inbox row."""
    if not trusted_scanner or before.get("id") != after.get("id"):
        return False
    lifecycle_keys = {"status", "updated_at", "dismissed_at", "dismiss_reason"}
    return {key: value for key, value in before.items() if key not in lifecycle_keys} == {
        key: value for key, value in after.items() if key not in lifecycle_keys
    }


def _scanner_stamp_new_imports(
    *,
    target: Path,
    scanner: dict[str, Any],
    run: dict[str, Any],
    before_ids: set[str],
    before_imports: list[dict[str, Any]],
    before_raw: bytes | None = None,
) -> list[str]:
    try:
        imports_raw = _scanner_inbox_bytes(target)
    except OSError:
        run["self_import"] = {
            "created": 0,
            "rejected": 1,
            "rejection_reasons": {"provenance_stamp_failed": 1},
        }
        return []
    if before_raw is None:
        before_raw = b""
    stamped_ids: list[str] = []
    staged_proof_items: list[dict[str, Any]] = []
    rejected = 0
    scanner_source = str(scanner.get("source") or "scanner").strip() or "scanner"
    trusted_scanner = _trusted_builtin_scanner(scanner)
    run_proof = _scanner_run_proof(scanner, run)
    existing_identities = {
        (identity, ledger_mod._import_fingerprint(item), content_identity)
        for item in before_imports
        if (
            isinstance(item, dict)
            and (
                ledger_mod._has_locally_stamped_import_proof(item, target=target)
                or ledger_mod._has_persisted_import_proof(item, target=target)
            )
            and (identity := ledger_mod._import_source_identity(item)) is not None
            and (content_identity := ledger_mod._import_content_identity(item)) is not None
        )
    }
    before_rows = _raw_jsonl_rows(before_raw)
    after_rows = _raw_jsonl_rows(imports_raw)
    remaining_before = Counter(before_rows)
    before_by_id: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, raw in enumerate(before_rows):
        item = _raw_row_object(raw)
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            before_by_id.setdefault(item["id"], []).append((index, item))
    replacements: dict[int, bytes] = {}
    new_rows: list[bytes] = []
    for raw in after_rows:
        if remaining_before[raw]:
            remaining_before[raw] -= 1
            continue
        item = _raw_row_object(raw)
        if item is not None and isinstance(item.get("id"), str):
            candidates = before_by_id.get(item["id"], [])
            matching_index = next(
                (
                    index
                    for index, before_item in candidates
                    if index not in replacements
                    and _reconcile_known_row(before_item, item, trusted_scanner=trusted_scanner)
                ),
                None,
            )
            if candidates:
                if matching_index is not None:
                    replacements[matching_index] = raw
                else:
                    rejected += 1
                continue
        new_rows.append(raw)

    final_rows: list[bytes] = []
    for index, raw in enumerate(before_rows):
        final_rows.append(replacements.get(index, raw))
    for raw in new_rows:
        item = _raw_row_object(raw)
        if item is None:
            rejected += 1
            continue
        producer_proof = ledger_mod._has_persisted_import_proof(item, target=target)
        item_proof = (
            ledger_mod._make_locally_stamped_import_proof(item, importer_source=scanner_source)
            if producer_proof and run_proof is not None
            else None
        )
        locally_stamped = ledger_mod._is_locally_stamped_self_import(
            item, importer_source=scanner_source, proof=item_proof
        )
        trusted_local = locally_stamped and trusted_scanner and run_proof is not None
        raw_record, errors = ledger_mod._validate_import_record(
            item,
            label="self-import",
            allow_non_task_fields=trusted_local,
        )
        if errors or raw_record is None:
            rejected += 1
            continue
        record_proof = (
            ledger_mod._make_locally_stamped_import_proof(raw_record, importer_source=scanner_source)
            if trusted_local
            else None
        )
        sanitized = ledger_mod._sanitize_self_import_record(
            raw_record,
            importer_source=scanner_source,
            target=target,
            trusted_producer=trusted_local,
            proof=record_proof,
        )
        identity = ledger_mod._import_source_identity(sanitized)
        content_identity = ledger_mod._import_content_identity(sanitized)
        if (identity, ledger_mod._import_fingerprint(sanitized), content_identity) in existing_identities:
            rejected += 1
            continue
        metadata = _scanner_import_provenance(target=target, scanner=scanner, run=run, record=sanitized)
        try:
            rebuilt = ledger_mod._make_import(
                str(sanitized["text"]),
                kind=str(sanitized["kind"]),
                source=scanner_source,
                metadata=metadata,
                task_type=sanitized.get("type") if isinstance(sanitized.get("type"), str) else None,
                priority=sanitized.get("priority") if isinstance(sanitized.get("priority"), str) else None,
                acceptance=sanitized.get("acceptance") if isinstance(sanitized.get("acceptance"), list) else None,
                template=sanitized.get("template") if isinstance(sanitized.get("template"), str) else None,
                provenance_source=scanner_source,
            )
        except ledger_mod._ImportProvenanceError:
            rejected += 1
            continue
        if final_rows and not final_rows[-1].endswith((b"\n", b"\r")):
            final_rows.append(b"\n")
        final_rows.append(json.dumps(rebuilt, sort_keys=True).encode("utf-8") + b"\n")
        rebuilt_identity = ledger_mod._import_source_identity(rebuilt)
        rebuilt_content_identity = ledger_mod._import_content_identity(rebuilt)
        if rebuilt_identity is not None and rebuilt_content_identity is not None:
            existing_identities.add(
                (rebuilt_identity, ledger_mod._import_fingerprint(rebuilt), rebuilt_content_identity)
            )
        stamped_ids.append(str(rebuilt["id"]))
        staged_proof_items.append(rebuilt)
    rendered = b"".join(final_rows)
    if rendered != imports_raw:
        before_files = ledger_mod._snapshot_external_file_authorities(target)
        inbox_exists = True
        try:
            existing_inbox = _open_scanner_inbox(target, os.O_RDONLY)
        except OSError:
            inbox_exists = False
        else:
            os.close(existing_inbox)
        try:
            _write_scanner_inbox_bytes(target, rendered)
        except OSError:
            run["self_import"] = {
                "created": 0,
                "rejected": rejected + len(stamped_ids) + 1,
                "rejection_reasons": {"provenance_stamp_failed": rejected + len(stamped_ids) + 1},
            }
            return []
        try:
            ledger_mod._write_persisted_import_proofs(target, staged_proof_items, operation_id=uuid4().hex)
        except BaseException:
            ledger_mod._remove_persisted_import_proofs(target, staged_proof_items)
            try:
                _write_scanner_inbox_bytes(target, imports_raw)
            except BaseException:
                _restore_scanner_inbox_snapshot_direct(target, imports_raw, exists=True)
                raise
            try:
                ledger_mod._restore_external_file_authorities(
                    target,
                    before_files,
                    owned=ledger_mod._persisted_import_proof_scopes(staged_proof_items),
                )
            except OSError as exc:
                raise OSError("scanner import binding rollback could not restore its retained snapshot") from exc
            run["self_import"] = {
                "created": 0,
                "rejected": rejected + len(stamped_ids) + 1,
                "rejection_reasons": {"provenance_stamp_failed": rejected + len(stamped_ids) + 1},
            }
            return []
        _SCANNER_RUN_PUBLICATION_SNAPSHOTS[id(run)] = {
            "target": target,
            "inbox": imports_raw,
            "inbox_exists": inbox_exists,
            "proof_items": list(staged_proof_items),
            "files": before_files,
        }
        if run_proof is not None:
            for rebuilt in staged_proof_items:
                _record_scanner_import_proof(scanner, run, rebuilt)
    if stamped_ids or rejected:
        run["self_import"] = {
            "created": len(stamped_ids),
            "rejected": rejected,
            "rejection_reasons": {"provenance_stamp_failed": rejected} if rejected else {},
        }
    return stamped_ids


def _scanner_validate_import_output(
    target: Path,
    scanner: dict[str, Any],
) -> tuple[Path | None, list[dict[str, Any]], list[str]]:
    import_path = config_mod._scanner_import_path(target, scanner)
    if import_path is None:
        # No import_path means a self-importing scanner: its command appends to
        # the inbox directly (e.g. `brigade work import memory-refresh`,
        # `brigade handoff sync-issues`), so there is no JSONL file for the sweep
        # to ingest. Skip it silently rather than failing the whole sweep.
        return None, [], []
    if scanner.get("import_format", "jsonl") != "jsonl":
        return import_path, [], [f"{scanner.get('id')}: import_format must be jsonl"]
    try:
        records, errors = _scanner_read_import_jsonl(target, scanner)
    except (OSError, UnicodeDecodeError):
        return import_path, [], [f"{scanner.get('id')}: import file not found: {import_path}"]
    return import_path, records, [f"{scanner.get('id')}: {error}" for error in errors]


def _scanner_import_read_primitives_available() -> bool:
    """Return whether scanner imports can be opened without pathname races."""
    return (
        os.name == "posix"
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NONBLOCK", 0))
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
    )


def _scanner_import_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink


def _validate_scanner_import_leaf(parent: int, name: str, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or _scanner_import_identity(opened) != _scanner_import_identity(named)
    ):
        raise OSError("scanner import file no longer matches its held descriptor")


def _validate_scanner_import_directories(anchor: int, directories: list[tuple[int, str]]) -> None:
    parent = anchor
    for descriptor, name in directories:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or _scanner_import_identity(opened) != _scanner_import_identity(named)
        ):
            raise OSError("scanner import parent no longer matches its held descriptor")
        parent = descriptor


def _scanner_import_relative_path(scanner: dict[str, Any]) -> Path:
    value = scanner.get("import_path")
    if not isinstance(value, str) or not value.strip():
        raise OSError("scanner import path is missing")
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise OSError("scanner import path must be relative and must not contain '..'")
    return path


def _open_scanner_import_directory(parent: int, name: str, flags: int) -> int:
    intended = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(intended.st_mode):
        raise OSError("scanner import directory is not a directory")
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _scanner_import_identity(intended) != _scanner_import_identity(opened)
            or _scanner_import_identity(intended) != _scanner_import_identity(named)
        ):
            raise OSError("scanner import directory no longer matches its held descriptor")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _scanner_read_import_jsonl(target: Path, scanner: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Read scanner JSONL through descriptors rooted at the exact workspace target."""
    if not _scanner_import_read_primitives_available():
        raise OSError("descriptor-relative scanner import operations are unavailable")
    relative = _scanner_import_relative_path(scanner)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    absolute_target = Path(os.path.abspath(target.expanduser()))
    target_components = absolute_target.parts[1:]
    if not target_components:
        raise OSError("scanner import target must have a bindable parent")
    anchor = -1
    descriptor = -1
    directories: list[tuple[int, str]] = []
    try:
        anchor = os.open(absolute_target.anchor, directory_flags)
        parent = anchor
        for component in target_components:
            child = _open_scanner_import_directory(parent, component, directory_flags)
            directories.append((child, component))
            _validate_scanner_import_directories(anchor, directories)
            parent = child
        for component in relative.parts[:-1]:
            child = _open_scanner_import_directory(parent, component, directory_flags)
            directories.append((child, component))
            _validate_scanner_import_directories(anchor, directories)
            parent = child
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=parent)
        _validate_scanner_import_leaf(parent, relative.parts[-1], descriptor)
        _validate_scanner_import_directories(anchor, directories)
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _validate_scanner_import_leaf(parent, relative.parts[-1], descriptor)
        _validate_scanner_import_directories(anchor, directories)
        if _scanner_import_identity(before) != _scanner_import_identity(after):
            raise OSError("scanner import file changed while reading")
        return ledger_mod._parse_import_jsonl(b"".join(chunks).decode())
    finally:
        if descriptor != -1:
            os.close(descriptor)
        for child, _name in reversed(directories):
            os.close(child)
        if anchor != -1:
            os.close(anchor)


def _scanner_run_one(
    target: Path,
    scanner: dict[str, Any],
    *,
    force: bool = False,
    isolated: bool = False,
) -> dict[str, Any]:
    scanner_id = str(scanner.get("id") or "scanner")
    command = str(scanner.get("command") or "")
    argv, blocker = config_mod._scanner_argv(command)
    output_path = config_mod._scanner_output_path(target, scanner)
    import_path = config_mod._scanner_import_path(target, scanner)
    cwd = config_mod._scanner_cwd(target, scanner)
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{helpers._slug(scanner_id)}-{uuid4().hex[:6]}"
    run_dir = helpers._scanner_runs_root(target) / run_id
    try:
        authority = _open_scanner_run_directory(target, run_id)
    except OSError as exc:
        completed = helpers._now()
        error = _scanner_runs_directory_failure_message(target, exc)
        return {
            "run_id": run_id,
            "scanner_id": scanner_id,
            "source": scanner.get("source"),
            "status": "failed",
            "path": str(run_dir),
            "target": str(target),
            "cwd": str(cwd),
            "command": command,
            "argv": argv or [],
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "duration_seconds": (completed - started).total_seconds(),
            "timeout": scanner.get("timeout"),
            "exit_code": None,
            "timed_out": False,
            "error": error,
            "stdout_summary": "",
            "stderr_summary": error,
            "forced": force,
            "runs_directory_error": True,
        }
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "scanner_id": scanner_id,
        "source": scanner.get("source"),
        "status": "running",
        "path": str(run_dir),
        "target": str(target),
        "cwd": str(cwd),
        "command": command,
        "argv": argv or [],
        "started_at": started.isoformat(),
        "timeout": scanner.get("timeout"),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "output_path": str(output_path) if output_path is not None else None,
        "output_before": _scanner_output_snapshot(output_path),
        "import_path": str(import_path) if import_path is not None else None,
        "import_format": scanner.get("import_format", "jsonl") if import_path is not None else None,
        "forced": force,
    }
    _SCANNER_RUN_DIRECTORY_AUTHORITIES[id(receipt)] = authority
    try:
        _write_scanner_run_receipt(receipt)
        if blocker is not None:
            completed = helpers._now()
            receipt.update(
                {
                    "status": "failed",
                    "completed_at": completed.isoformat(),
                    "duration_seconds": (completed - started).total_seconds(),
                    "exit_code": None,
                    "timed_out": False,
                    "error": blocker,
                    "stdout_summary": "",
                    "stderr_summary": blocker,
                    "output_after": _scanner_output_snapshot(output_path),
                }
            )
            _write_scanner_run_file(authority, "stdout.log", b"")
            _write_scanner_run_file(authority, "stderr.log", (blocker + "\n").encode("utf-8"))
            _write_scanner_run_receipt(receipt)
            return receipt
        if not cwd.is_dir():
            completed = helpers._now()
            error = f"scanner cwd does not exist: {cwd}"
            receipt.update(
                {
                    "status": "failed",
                    "completed_at": completed.isoformat(),
                    "duration_seconds": (completed - started).total_seconds(),
                    "exit_code": None,
                    "timed_out": False,
                    "error": error,
                    "stdout_summary": "",
                    "stderr_summary": error,
                    "output_after": _scanner_output_snapshot(output_path),
                }
            )
            _write_scanner_run_file(authority, "stdout.log", b"")
            _write_scanner_run_file(authority, "stderr.log", (error + "\n").encode("utf-8"))
            _write_scanner_run_receipt(receipt)
            return receipt
        _prebind_child_visible_directories(target)
        try:
            with _scanner_child_environment_sandbox(target) as child_env:
                run_kwargs: dict[str, Any] = {
                    "cwd": cwd,
                    "text": True,
                    "capture_output": True,
                    "timeout": float(scanner.get("timeout") or 300),
                    "shell": False,
                    "env": child_env,
                }
                if isolated:
                    from .. import scanner_isolation

                    status = scanner_isolation.probe_isolation()
                    if not status.available:
                        raise OSError(f"isolated scanners unavailable: {status.reason}")
                    sandbox = Path(child_env["HOME"])
                    covers = scanner_isolation.prepare_isolation_covers(sandbox)
                    argv = scanner_isolation.isolated_argv(argv, covers)
                completed_process = subprocess.run(argv, **run_kwargs)
            stdout = completed_process.stdout or ""
            stderr = completed_process.stderr or ""
            _write_scanner_run_file(authority, "stdout.log", stdout.encode("utf-8"))
            _write_scanner_run_file(authority, "stderr.log", stderr.encode("utf-8"))
            completed = helpers._now()
            receipt.update(
                {
                    "status": "completed" if completed_process.returncode == 0 else "failed",
                    "completed_at": completed.isoformat(),
                    "duration_seconds": (completed - started).total_seconds(),
                    "exit_code": completed_process.returncode,
                    "timed_out": False,
                    "stdout_summary": _scanner_run_summary(stdout),
                    "stderr_summary": _scanner_run_summary(stderr),
                    "output_after": _scanner_output_snapshot(output_path),
                }
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            _write_scanner_run_file(authority, "stdout.log", stdout.encode("utf-8"))
            _write_scanner_run_file(authority, "stderr.log", stderr.encode("utf-8"))
            completed = helpers._now()
            receipt.update(
                {
                    "status": "failed",
                    "completed_at": completed.isoformat(),
                    "duration_seconds": (completed - started).total_seconds(),
                    "exit_code": None,
                    "timed_out": True,
                    "error": f"scanner timed out after {scanner.get('timeout')} seconds",
                    "stdout_summary": _scanner_run_summary(stdout),
                    "stderr_summary": _scanner_run_summary(stderr),
                    "output_after": _scanner_output_snapshot(output_path),
                }
            )
        _write_scanner_run_receipt(receipt)
        return receipt
    except BaseException:
        # The payload finally only releases runs that reached `runs.append`.
        # Release here so a failure after install cannot retain the descriptors.
        _release_scanner_run_directory_authority(receipt)
        raise


def _scanner_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    scanners, errors = config_mod._load_scanner_config(target)
    enabled = [scanner for scanner in scanners if scanner.get("enabled", True)]
    planned: list[dict[str, Any]] = []
    for scanner in enabled:
        start = config_mod._scanner_start_minute(str(scanner.get("cadence", "")))
        if start is None:
            continue
        duration = config_mod._scanner_duration_minutes(scanner)
        planned.append(
            {
                "id": scanner.get("id"),
                "source": scanner.get("source"),
                "command": scanner.get("command"),
                "cadence": scanner.get("cadence"),
                "start_minute": start,
                "start": config_mod._format_clock_minutes(start),
                "duration_minutes": duration,
                "end": config_mod._format_clock_minutes(start + duration),
                "conflict_window": scanner.get("conflict_window"),
                "output_path": scanner.get("output_path"),
                "import_path": scanner.get("import_path"),
                "import_format": scanner.get("import_format", "jsonl") if scanner.get("import_path") else None,
            }
        )
    planned.sort(key=lambda item: int(item.get("start_minute", 0)))

    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(planned):
        left_start = int(left["start_minute"])
        left_end = left_start + int(left["duration_minutes"])
        left_window = config_mod._scanner_window_minutes(str(left.get("conflict_window") or ""))
        for right in planned[index + 1 :]:
            right_start = int(right["start_minute"])
            right_end = right_start + int(right["duration_minutes"])
            right_window = config_mod._scanner_window_minutes(str(right.get("conflict_window") or ""))
            if left_start < right_end and right_start < left_end:
                conflicts.append({"type": "run_overlap", "scanners": [left["id"], right["id"]]})
            if left_window and right_window and left_window[0] < right_window[1] and right_window[0] < left_window[1]:
                conflicts.append({"type": "window_overlap", "scanners": [left["id"], right["id"]]})
            if abs(right_start - left_start) < 15:
                conflicts.append({"type": "clustered_runs", "scanners": [left["id"], right["id"]]})

    suggestions: list[dict[str, Any]] = []
    next_start: int | None = None
    for item in planned:
        current = int(item["start_minute"])
        suggested = current if next_start is None else max(current, next_start)
        cadence_value = str(item.get("cadence", ""))
        if cadence_value.startswith("daily@"):
            suggested_cadence = f"daily@{config_mod._format_clock_minutes(suggested)}"
        elif cadence_value.startswith("weekly@"):
            suggested_cadence = f"weekly@{config_mod._format_clock_minutes(suggested)}"
        else:
            suggested_cadence = f"hourly@{suggested % 60:02d}"
        suggestions.append(
            {
                "id": item["id"],
                "current": item["cadence"],
                "suggested_start": config_mod._format_clock_minutes(suggested),
                "suggested_cadence": suggested_cadence,
            }
        )
        next_start = suggested + 15

    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
        "planned": planned,
        "conflicts": conflicts,
        "suggestions": suggestions,
    }


def _required_scanner_ids(target: Path) -> tuple[str, ...]:
    """Required local-producer scanner ids for this target.

    chat-memory-sweep only matters when the repo actually has an enabled chat
    surface. A code repo with every surface disabled (or no chat-surfaces.toml
    at all) has nothing to sweep, so it should not be nagged to enable it.
    """
    from .. import chat_cmd

    surfaces = chat_cmd.health(target).get("surfaces")
    surfaces = surfaces if isinstance(surfaces, list) else []
    chat_active = any(isinstance(surface, dict) and surface.get("enabled") for surface in surfaces)
    return tuple(
        scanner_id for scanner_id in constants.SCANNER_REQUIRED_IDS if scanner_id != "chat-memory-sweep" or chat_active
    )


def _scanner_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = _scanner_plan_payload(target)
    scanners = plan["scanners"] if isinstance(plan.get("scanners"), list) else []
    checks: list[dict[str, Any]] = []
    if not helpers._scanner_config_path(target).is_file():
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_config",
                "detail": f"missing, run `brigade work scanners init --target {target}`",
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_config", "detail": plan["config_path"]})
    else:
        checks.append({"status": constants.FAIL, "name": "scanner_config", "detail": "; ".join(plan.get("errors", []))})

    by_id = {scanner.get("id"): scanner for scanner in scanners if isinstance(scanner, dict)}
    required_ids = _required_scanner_ids(target)
    missing_required = [scanner_id for scanner_id in required_ids if scanner_id not in by_id]
    disabled_required = [
        scanner_id
        for scanner_id in required_ids
        if isinstance(by_id.get(scanner_id), dict) and not by_id[scanner_id].get("enabled", True)
    ]
    if missing_required or disabled_required:
        detail_parts = []
        if missing_required:
            detail_parts.append(f"missing={','.join(missing_required)}")
        if disabled_required:
            detail_parts.append(f"disabled={','.join(disabled_required)}")
        checks.append({"status": constants.WARN, "name": "scanner_required", "detail": "; ".join(detail_parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_required", "detail": "required local producers are enabled"}
        )

    bad_commands = []
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        _, blocker = config_mod._scanner_argv(str(scanner.get("command") or ""))
        if blocker is not None:
            bad_commands.append(str(scanner.get("id")))
    if bad_commands:
        checks.append({"status": constants.WARN, "name": "scanner_commands", "detail": ", ".join(bad_commands)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_commands", "detail": "enabled scanner commands are resolvable"}
        )

    stale_outputs: list[str] = []
    missing_outputs: list[str] = []
    now = helpers._now() if scanners else None
    for scanner in scanners:
        if not scanner.get("enabled", True):
            continue
        output = scanner.get("output_path")
        if not isinstance(output, str) or not output.strip():
            continue
        path = Path(output).expanduser()
        path = path if path.is_absolute() else target / path
        if not path.exists():
            missing_outputs.append(str(scanner.get("id")))
            continue
        if now is None:
            continue
        age_hours = (now.timestamp() - path.stat().st_mtime) / 3600
        if age_hours > _scanner_stale_hours(str(scanner.get("cadence") or "")):
            stale_outputs.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if missing_outputs or stale_outputs:
        parts = []
        if missing_outputs:
            parts.append(f"missing={','.join(missing_outputs)}")
        if stale_outputs:
            parts.append(f"stale={','.join(stale_outputs)}")
        checks.append({"status": constants.WARN, "name": "scanner_outputs", "detail": "; ".join(parts)})
    else:
        checks.append(
            {"status": constants.OK, "name": "scanner_outputs", "detail": "enabled scanner outputs exist and are fresh"}
        )

    conflicts = plan.get("conflicts") if isinstance(plan.get("conflicts"), list) else []
    if conflicts:
        rendered = ", ".join(
            f"{item.get('type')}:{'/'.join(str(v) for v in item.get('scanners', []))}" for item in conflicts[:5]
        )
        checks.append({"status": constants.WARN, "name": "scanner_schedule", "detail": rendered})
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_schedule", "detail": "no scanner schedule conflicts"})

    try:
        runs_root_state = _bind_released_unbound_scanner_runs_root(target)
    except OSError as exc:
        checks.append({"status": constants.FAIL, "name": "scanner_runs_root", "detail": str(exc)})
    else:
        if runs_root_state == "repaired":
            checks.append(
                {
                    "status": constants.OK,
                    "name": "scanner_runs_root",
                    "detail": "bound released pre-authority runs root",
                }
            )
        elif runs_root_state == "bound":
            checks.append(
                {
                    "status": constants.OK,
                    "name": "scanner_runs_root",
                    "detail": "bound to this workspace",
                }
            )

    receipts, malformed_receipts = _scanner_receipt_collection(target)
    receipts.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    if malformed_receipts:
        checks.append(
            {"status": constants.FAIL, "name": "scanner_run_receipts", "detail": ", ".join(malformed_receipts[:5])}
        )

    running = [receipt for receipt in receipts if receipt.get("status") == "running"]
    if running:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_running",
                "detail": ", ".join(str(item.get("run_id")) for item in running[:5]),
            }
        )

    recent_failed = [receipt for receipt in receipts if receipt.get("status") == "failed" or receipt.get("timed_out")][
        :5
    ]
    if recent_failed:
        rendered = ", ".join(f"{item.get('scanner_id')}:{item.get('run_id')}" for item in recent_failed)
        checks.append({"status": constants.WARN, "name": "scanner_runs_failed", "detail": rendered})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_runs_failed", "detail": "none"})

    missing_logs = []
    for receipt in receipts[:20]:
        for key in ("stdout_path", "stderr_path"):
            value = receipt.get(key)
            if isinstance(value, str) and value and not Path(value).is_file():
                missing_logs.append(f"{receipt.get('run_id')}:{key}")
    if missing_logs:
        checks.append({"status": constants.WARN, "name": "scanner_run_logs", "detail": ", ".join(missing_logs[:5])})
    elif receipts:
        checks.append({"status": constants.OK, "name": "scanner_run_logs", "detail": "receipt logs exist"})

    stale_successes: list[str] = []
    if scanners:
        now = helpers._now()
        for scanner in scanners:
            if not scanner.get("enabled", True):
                continue
            latest_success = _scanner_latest_success(target, str(scanner.get("id") or ""))
            if latest_success is None:
                continue
            completed = helpers._parse_iso_datetime(
                latest_success.get("completed_at") or latest_success.get("started_at")
            )
            if completed is None:
                stale_successes.append(str(scanner.get("id")))
                continue
            age_hours = (now - completed).total_seconds() / 3600
            if age_hours > _scanner_stale_hours(str(scanner.get("cadence") or "")):
                stale_successes.append(f"{scanner.get('id')}={age_hours:.1f}h")
    if stale_successes:
        checks.append(
            {"status": constants.WARN, "name": "scanner_runs_stale", "detail": ", ".join(stale_successes[:5])}
        )
    elif receipts and plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_stale", "detail": "none"})

    due = _scanner_due_items(target, scanners)
    if due:
        checks.append(
            {
                "status": constants.WARN,
                "name": "scanner_runs_due",
                "detail": ", ".join(str(item.get("id")) for item in due[:5]),
            }
        )
    elif plan.get("valid"):
        checks.append({"status": constants.OK, "name": "scanner_runs_due", "detail": "none"})

    from .. import scanner_isolation

    isolation_status, _isolation_name, isolation_detail = scanner_isolation.doctor_check()
    mapped = {
        "OK": constants.OK,
        "WARN": constants.WARN,
        "FAIL": constants.FAIL,
        "MANUAL": constants.WARN,
    }.get(isolation_status, constants.WARN)
    checks.append({"status": mapped, "name": "scanner_isolation", "detail": isolation_detail})

    next_run = plan.get("planned", [None])[0] if plan.get("planned") else None
    latest_run = receipts[0] if receipts else None
    return {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "checks": checks,
        "plan": plan,
        "next_run": next_run,
        "latest_run": latest_run,
        "due": due,
    }


def _scanner_sweep_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    latest = _scanner_latest_sweep(target)
    due = _scanner_health(target).get("due")
    due_count = len(due) if isinstance(due, list) else 0
    review: dict[str, Any] | None = None
    if latest is None:
        checks.append({"status": constants.WARN, "name": "scanner_sweeps", "detail": "none, run `brigade work sweep`"})
    else:
        status = str(latest.get("status") or "unknown")
        if status == "failed":
            checks.append({"status": constants.WARN, "name": "scanner_sweep_failed", "detail": latest.get("sweep_id")})
        else:
            checks.append(
                {
                    "status": constants.OK,
                    "name": "scanner_sweep_latest",
                    "detail": f"{latest.get('sweep_id')} [{status}]",
                }
            )
        completed = helpers._parse_iso_datetime(latest.get("completed_at") or latest.get("started_at"))
        if completed is not None:
            age_hours = (helpers._now() - completed).total_seconds() / 3600
            if age_hours > constants.SCANNER_SWEEP_STALE_HOURS:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "scanner_sweep_stale",
                        "detail": f"{latest.get('sweep_id')}={age_hours:.1f}h",
                    }
                )
        review, _ = sweeps_mod._sweep_review_payload(target, str(latest.get("sweep_id") or "latest"))
        if isinstance(review, dict):
            checks.extend(review["issues"])
    return {
        "target": str(target),
        "sweeps_root": str(helpers._scanner_sweeps_root(target)),
        "latest": latest,
        "checks": checks,
        "due_count": due_count,
        "suggested_command": "brigade work sweep" if due_count else None,
        "review": {
            "top_pending_import": review.get("top_pending_import") if isinstance(review, dict) else None,
            "issue_count": len(review.get("issues", [])) if isinstance(review, dict) else 0,
            "issues": review.get("issues", []) if isinstance(review, dict) else [],
        },
    }


def _scanner_health_issue_records(target: Path) -> list[dict[str, Any]]:
    health = _scanner_health(target)
    records: list[dict[str, Any]] = []
    for check in health["checks"]:
        if check.get("status") == constants.OK:
            continue
        name = str(check.get("name"))
        detail = str(check.get("detail"))
        records.append(
            {
                "text": f"Repair scanner health issue {name}: {detail}",
                "kind": "task",
                "source": "scanner-health",
                "type": "workflow",
                "priority": "normal",
                "template": "bugfix",
                "acceptance": [f"`brigade work scanners doctor` no longer reports {name}."],
                "metadata": {
                    "scanner_health_check": name,
                    "scanner_health_status": check.get("status"),
                    "scanner_health_detail": detail,
                    "source_item_key": f"scanner-health:{name}",
                    "source_fingerprint": helpers._stable_hash({"name": name, "detail": detail}),
                },
            }
        )
    return records


def _scanner_source_map(target: Path) -> dict[str, dict[str, Any]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return {}
    by_source: dict[str, dict[str, Any]] = {}
    for scanner in scanners:
        for key in ("source", "id"):
            value = scanner.get(key)
            if isinstance(value, str) and value.strip():
                by_source[value.strip()] = scanner
    return by_source


def scanners_init(*, target: Path, force: bool = False, update_gitignore: bool = True) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = helpers._scanner_config_path(target)
    if path.exists() and not force:
        print(f"error: scanner config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod._format_scanner_toml())
    try:
        _bind_released_unbound_scanner_runs_root(target)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"scanner_config: {path}")
    print(f"scanners: {len(constants.SCANNER_DEFAULTS)}")
    if update_gitignore:
        result = apply_gitignore(target, helpers._work_selection(target, dogfood_cmd.default_handoff_inbox(target)))
        print(f"gitignore: {result}")
    else:
        print("gitignore: skipped")
    print("next_command: brigade work scanners plan")
    return 0


def scanners_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanners": scanners,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not errors else 1
    print(f"work scanners: {target}")
    print(f"config_path: {helpers._scanner_config_path(target)}")
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(f"- {error}")
        return 1
    if not scanners:
        print("scanners: none")
        return 0
    for scanner in scanners:
        status = "enabled" if scanner.get("enabled", True) else "disabled"
        print(f"- {scanner.get('id')} [{status}] {scanner.get('cadence')} source={scanner.get('source')}")
        print(f"  command: {scanner.get('command')}")
        print(f"  output: {scanner.get('output_path')}")
        if scanner.get("import_path"):
            print(f"  import: {scanner.get('import_path')} ({scanner.get('import_format', 'jsonl')})")
    return 0


def scanners_show(*, target: Path, scanner_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    scanners, errors = config_mod._load_scanner_config(target)
    scanner = None
    for item in scanners:
        if item.get("id") == scanner_id:
            scanner = item
            break
    payload = {
        "target": str(target),
        "config_path": str(helpers._scanner_config_path(target)),
        "valid": not errors,
        "errors": errors,
        "scanner": scanner,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if scanner is not None and not errors else 1
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    if scanner is None:
        print(f"error: scanner not found: {scanner_id}", file=sys.stderr)
        return 1
    print(f"scanner: {scanner.get('id')}")
    print(f"enabled: {scanner.get('enabled')}")
    print(f"source: {scanner.get('source')}")
    print(f"cadence: {scanner.get('cadence')}")
    print(f"timeout: {scanner.get('timeout')}")
    print(f"output_path: {scanner.get('output_path')}")
    if scanner.get("import_path"):
        print(f"import_path: {scanner.get('import_path')}")
        print(f"import_format: {scanner.get('import_format', 'jsonl')}")
    print(f"conflict_window: {scanner.get('conflict_window')}")
    print(f"command: {scanner.get('command')}")
    return 0


def scanners_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _scanner_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["valid"] else 1
    print(f"work scanners plan: {target}")
    print(f"config_path: {payload['config_path']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for error in payload["errors"]:
            print(f"- {error}")
        return 1
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    if not planned:
        print("planned: none")
    else:
        print("planned:")
        for item in planned:
            print(
                f"- {item.get('id')} {item.get('start')}-{item.get('end')} "
                f"{item.get('cadence')} output={item.get('output_path')}"
            )
    conflicts = payload.get("conflicts") if isinstance(payload.get("conflicts"), list) else []
    if conflicts:
        print("conflicts:")
        for item in conflicts:
            print(f"- {item.get('type')}: {', '.join(str(v) for v in item.get('scanners', []))}")
    else:
        print("conflicts: none")
    suggestions = payload.get("suggestions") if isinstance(payload.get("suggestions"), list) else []
    if suggestions:
        print("suggested_schedule:")
        for item in suggestions:
            print(f"- {item.get('id')}: {item.get('suggested_cadence')}")
    return 0


def scanners_doctor(*, target: Path, json_output: bool = False, import_issues: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    health = _scanner_health(target)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_dismissed: list[dict[str, Any]] = []
    if import_issues:
        records = _scanner_health_issue_records(target)
        imported, skipped, skipped_dismissed, _rejected = ledger_mod._append_import_records(target, records)
        health["import_issues"] = {
            "created": len(imported),
            "skipped": len(skipped),
            "dismissed": len(skipped_dismissed),
            "imports": imported,
        }
    if json_output:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1
    print(f"work scanners doctor: {target}")
    print(f"config_path: {health['config_path']}")
    for check in health["checks"]:
        helpers._doctor_line(str(check.get("status")), str(check.get("name")), check.get("detail"))
    next_run = health.get("next_run")
    if isinstance(next_run, dict):
        print(f"next_scanner: {next_run.get('id')} {next_run.get('start')} {next_run.get('cadence')}")
    if import_issues:
        print(f"imported_issues: {len(imported)}")
        print(f"skipped_issues: {len(skipped)}")
        print(f"dismissed_issues: {len(skipped_dismissed)}")
    return 0 if not any(check.get("status") == constants.FAIL for check in health["checks"]) else 1


def _select_scanners_for_run(
    target: Path,
    *,
    scanner_id: str | None,
    all_matching: bool,
    due: bool,
    include_disabled: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    scanners, errors = config_mod._load_scanner_config(target)
    if errors:
        return [], [], errors
    if scanner_id:
        selected = [item for item in scanners if item.get("id") == scanner_id]
        if not selected:
            return [], [], [f"scanner not found: {scanner_id}"]
    elif all_matching or due:
        selected = list(scanners)
    else:
        return [], [], ["scanner id, --all, or --due is required"]
    runnable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for scanner in selected:
        if not scanner.get("enabled", True) and not include_disabled:
            if scanner_id:
                return [], [], [f"scanner disabled: {scanner_id}"]
            skipped.append({"scanner": scanner, "reason": "disabled"})
            continue
        if due and not _scanner_is_due(target, scanner):
            skipped.append({"scanner": scanner, "reason": "not_due"})
            continue
        runnable.append(scanner)
    return runnable, skipped, []


def _scanners_run_payload(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    require_selector: bool = True,
    isolated_scanners: bool = False,
) -> tuple[dict[str, Any], int]:
    target = target.expanduser().resolve()
    if not target.is_dir():
        return {
            "target": str(target),
            "errors": [f"--target is not a directory: {target}"],
            "runs": [],
            "skipped": [],
        }, 2
    selector_count = sum(1 for item in (scanner_id, all_matching, due) if bool(item))
    if require_selector and selector_count != 1:
        error = "pass exactly one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not require_selector and selector_count > 1:
        error = "pass only one of scanner id, --all, or --due"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    if not helpers._scanner_config_path(target).is_file():
        error = f"scanner config missing: {helpers._scanner_config_path(target)}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    running = _scanner_running_receipts(target)
    if running and not force:
        error = f"scanner run already in progress: {running[0].get('run_id')}"
        return {"target": str(target), "errors": [error], "runs": [], "skipped": []}, 2
    selected, skipped, errors = _select_scanners_for_run(
        target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
    )
    if errors:
        return {"target": str(target), "errors": errors, "runs": [], "skipped": skipped}, 2
    before_counts = _scanner_import_counts(target)
    runs: list[dict[str, Any]] = []
    contexts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    try:
        for scanner in selected:
            try:
                before_raw = _scanner_inbox_bytes(target)
                before_imports = _scanner_inbox_imports(target)
            except OSError:
                before_raw = b""
                before_imports = []
            before_ids = {
                str(item.get("id"))
                for item in before_imports
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            run = _scanner_run_one(target, scanner, force=force, isolated=isolated_scanners)
            # Cover stamp/receipt failures: authority exists from `_scanner_run_one`.
            runs.append(run)
            if run.get("runs_directory_error"):
                skipped_rows = [
                    {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
                    for item in skipped
                    if isinstance(item.get("scanner"), dict)
                ]
                return {
                    "target": str(target),
                    "errors": [str(run.get("error") or "scanner runs directory is unavailable")],
                    "runs": runs,
                    "skipped": skipped_rows,
                }, 1
            _register_scanner_run_proof(scanner, run)
            stamped_ids = _scanner_stamp_new_imports(
                target=target,
                scanner=scanner,
                run=run,
                before_ids=before_ids,
                before_imports=before_imports,
                before_raw=before_raw,
            )
            run["provenance_imports_stamped"] = len(stamped_ids)
            if stamped_ids:
                run["stamped_import_ids"] = stamped_ids
            _write_scanner_run_receipt(run)
            contexts.append((scanner, run))
        ingest_errors: list[str] = []
        ingest_payloads: list[tuple[dict[str, Any], dict[str, Any], Path, list[dict[str, Any]]]] = []
        if ingest_output:
            for scanner, run in contexts:
                if run.get("status") != "completed":
                    continue
                path, records, errors = _scanner_validate_import_output(target, scanner)
                if errors:
                    ingest_errors.extend(errors)
                    continue
                if path is not None:
                    ingest_payloads.append(
                        (
                            scanner,
                            run,
                            path,
                            _scanner_enrich_import_records(target=target, scanner=scanner, run=run, records=records),
                        )
                    )
            if ingest_errors:
                after_counts = _scanner_import_counts(target)
                payload = {
                    "target": str(target),
                    "runs_root": str(helpers._scanner_runs_root(target)),
                    "selected": len(selected),
                    "completed": len([run for run in runs if run.get("status") == "completed"]),
                    "failed": len([run for run in runs if run.get("status") != "completed"]),
                    "skipped": [
                        {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
                        for item in skipped
                        if isinstance(item.get("scanner"), dict)
                    ],
                    "imports_before": before_counts,
                    "imports_after": after_counts,
                    "ingest_output": True,
                    "ingest_errors": ingest_errors,
                    "runs": runs,
                }
                return payload, 2
            for scanner, run, path, records in ingest_payloads:
                scanner_source = str(scanner.get("source") or "scanner").strip() or "scanner"
                try:
                    existing_imports = _scanner_inbox_imports(target)
                except OSError:
                    run["ingest_output"] = {
                        "path": str(path),
                        "created": 0,
                        "skipped": 0,
                        "dismissed": 0,
                        "rejected": len(records),
                        "rejection_reasons": {"inbox_persistence_failed": len(records)},
                        "records": len(records),
                        "created_import_ids": [],
                        "skipped_source_fingerprints": [],
                        "dismissed_source_fingerprints": [],
                    }
                    _write_scanner_run_receipt(run)
                    continue
                imported, skipped_records, skipped_dismissed, rejected = ledger_mod._append_import_records(
                    target,
                    records,
                    provenance_source=scanner_source,
                    contain_provenance_errors=True,
                    migrate_untrusted_identities=True,
                    preserve_existing_raw=lambda data: _append_scanner_inbox_bytes(target, data),
                    restore_existing_raw=lambda data, exists: _restore_scanner_inbox_bytes(target, data, exists),
                    existing_imports=existing_imports,
                )
                if _scanner_run_proof(scanner, run) is not None:
                    for item in imported:
                        _record_scanner_import_proof(scanner, run, item)
                run["ingest_output"] = {
                    "path": str(path),
                    "created": len(imported),
                    "skipped": len(skipped_records),
                    "dismissed": len(skipped_dismissed),
                    "rejected": len(rejected),
                    "rejection_reasons": {rejected[0]: len(rejected)} if rejected else {},
                    "records": len(records),
                    "created_import_ids": [str(item.get("id")) for item in imported if isinstance(item.get("id"), str)],
                    "skipped_source_fingerprints": [
                        fingerprint
                        for record in skipped_records
                        if (fingerprint := ledger_mod._import_fingerprint(record))
                    ],
                    "dismissed_source_fingerprints": [
                        fingerprint
                        for record in skipped_dismissed
                        if (fingerprint := ledger_mod._import_fingerprint(record))
                    ],
                }
                _write_scanner_run_receipt(run)
        after_counts = _scanner_import_counts(target)
        payload = {
            "target": str(target),
            "runs_root": str(helpers._scanner_runs_root(target)),
            "selected": len(selected),
            "completed": len([run for run in runs if run.get("status") == "completed"]),
            "failed": len([run for run in runs if run.get("status") != "completed"]),
            "skipped": [
                {"scanner_id": item["scanner"].get("id"), "reason": item["reason"]}
                for item in skipped
                if isinstance(item.get("scanner"), dict)
            ],
            "imports_before": before_counts,
            "imports_after": after_counts,
            "ingest_output": ingest_output,
            "ingest_errors": ingest_errors,
            "runs": runs,
        }
        return payload, 0 if payload["failed"] == 0 else 1
    finally:
        for run in runs:
            _release_scanner_run_directory_authority(run)


def scanners_run(
    *,
    target: Path,
    scanner_id: str | None = None,
    all_matching: bool = False,
    due: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    ingest_output: bool = False,
    json_output: bool = False,
    isolated_scanners: bool = False,
) -> int:
    payload, rc = _scanners_run_payload(
        target=target,
        scanner_id=scanner_id,
        all_matching=all_matching,
        due=due,
        include_disabled=include_disabled,
        force=force,
        ingest_output=ingest_output,
        isolated_scanners=isolated_scanners,
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return rc
    print(f"work scanners run: {payload.get('target')}")
    print(f"runs_root: {payload['runs_root']}")
    print(f"selected: {payload['selected']}")
    print(f"completed: {payload['completed']}")
    print(f"failed: {payload['failed']}")
    for item in payload["skipped"]:
        print(f"skipped: {item['scanner_id']} {item['reason']}")
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    for run in runs:
        print(
            f"- {run.get('run_id')} {run.get('scanner_id')} "
            f"[{run.get('status')}] exit={run.get('exit_code')} timed_out={run.get('timed_out')}"
        )
        if run.get("error"):
            print(f"  error: {run.get('error')}")
        if run.get("ingest_output"):
            ingest = run["ingest_output"]
            print(
                "  ingest_output: "
                f"created={ingest.get('created')} skipped={ingest.get('skipped')} dismissed={ingest.get('dismissed')}"
            )
        if run.get("provenance_imports_stamped"):
            print(f"  provenance_imports_stamped: {run.get('provenance_imports_stamped')}")
        print(f"  logs: {run.get('stdout_path')} {run.get('stderr_path')}")
    before_counts = payload.get("imports_before") if isinstance(payload.get("imports_before"), dict) else {}
    after_counts = payload.get("imports_after") if isinstance(payload.get("imports_after"), dict) else {}
    print(f"pending_imports_before: {before_counts.get('total', 0)}")
    print(f"pending_imports_after: {after_counts.get('total', 0)}")
    return rc


def scanners_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    receipts = _scanner_receipts(target)[:limit]
    payload = {"target": str(target), "runs_root": str(helpers._scanner_runs_root(target)), "runs": receipts}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work scanner runs: {target}")
    print(f"runs_root: {payload['runs_root']}")
    if not receipts:
        print("runs: none")
        return 0
    for receipt in receipts:
        print(
            f"- {receipt.get('run_id')} {receipt.get('scanner_id')} "
            f"[{receipt.get('status')}] exit={receipt.get('exit_code')} {receipt.get('started_at')}"
        )
    return 0


def scanners_run_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    matches = [receipt for receipt in _scanner_receipts(target) if str(receipt.get("run_id") or "").startswith(run_id)]
    if not matches:
        print(f"error: scanner run not found: {run_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: scanner run id is ambiguous: {run_id}", file=sys.stderr)
        return 2
    receipt = matches[0]
    if json_output:
        print(json.dumps({"target": str(target), "run": receipt}, indent=2, sort_keys=True))
        return 0
    print(f"scanner_run: {receipt.get('run_id')}")
    print(f"scanner: {receipt.get('scanner_id')}")
    print(f"source: {receipt.get('source')}")
    print(f"status: {receipt.get('status')}")
    print(f"started_at: {receipt.get('started_at')}")
    if receipt.get("completed_at"):
        print(f"completed_at: {receipt.get('completed_at')}")
    print(f"duration_seconds: {receipt.get('duration_seconds')}")
    print(f"exit_code: {receipt.get('exit_code')}")
    print(f"timed_out: {receipt.get('timed_out')}")
    print(f"stdout: {receipt.get('stdout_path')}")
    print(f"stderr: {receipt.get('stderr_path')}")
    if receipt.get("stdout_summary"):
        print(f"stdout_summary: {helpers._short(str(receipt.get('stdout_summary')))}")
    if receipt.get("stderr_summary"):
        print(f"stderr_summary: {helpers._short(str(receipt.get('stderr_summary')))}")
    return 0
