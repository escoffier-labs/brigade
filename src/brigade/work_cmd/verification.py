"""Verify, acceptance, and closeout operations."""

from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4
from .. import config, graphtrail_delta, localio, proc, receipt_schema, receipt_signing, runguard
from .. import verify_manifest, verify_trial
from . import constants, helpers, ledger as ledger_mod
from . import reviews as reviews_mod
from . import scanners as scanners_mod


def _default_verify_commands(target: Path) -> list[str]:
    if (target / "pyproject.toml").is_file() and (target / "tests").is_dir():
        if (target / "src").is_dir():
            return ["PYTHONPATH=src python3 -m pytest -q"]
        return ["python3 -m pytest -q"]
    if (target / "pytest.ini").is_file() or (target / "tests").is_dir():
        return ["python3 -m pytest -q"]
    if (target / "package.json").is_file():
        return ["npm test"]
    return []


def _receipt_git_snapshot(target: Path) -> dict[str, Any] | None:
    try:
        head = helpers._git_value(target, "rev-parse", "HEAD")
        branch = helpers._git_value(target, "rev-parse", "--abbrev-ref", "HEAD")
        status = helpers._git(target, "status", "--porcelain")
    except OSError:
        return None
    if head is None or branch is None or status.returncode != 0:
        return None
    return {"head": head, "branch": branch, "dirty_files": len(status.stdout.splitlines())}


def _high_risk_command_message(executable: str) -> str:
    """Rejection message for a shell/remote executable used as a verify command.

    Mirrors the shell-metacharacter branch by naming the remedy: verify runs the
    command directly with no shell, so a shell interpreter is never a valid
    executable. ``--argv-json`` is deliberately not suggested here because that
    path applies the same high-risk block, so the fix is a resolvable non-shell
    executable, e.g. a chmod +x script invoked by its path.
    """
    return (
        f"high-risk verification command: {executable} "
        "(verify runs with no shell; use a resolvable executable, e.g. a "
        "chmod +x script run by its path like ./scripts/check.sh)"
    )


def _verify_parse_command(command: str, target: Path) -> tuple[list[str] | None, dict[str, str], str | None]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return None, {}, f"invalid command: {exc}"
    if not parts:
        return None, {}, "empty command"
    env: dict[str, str] = {}
    argv = list(parts)
    while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
        key, value = argv.pop(0).split("=", 1)
        env[key] = value
    if not argv:
        return None, env, "command contains only environment assignments"
    executable = Path(argv[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, env, _high_risk_command_message(executable)
    if any(constants.SCANNER_SHELL_META_RE.search(part) for part in argv):
        return (
            None,
            env,
            (
                "high-risk verification command contains shell metacharacters "
                "(use --argv-json to pass a pre-parsed argv list instead)"
            ),
        )
    if "/" in argv[0]:
        executable_path = Path(argv[0]).expanduser()
        if not executable_path.is_absolute():
            executable_path = target / executable_path
        if not executable_path.exists():
            return None, env, f"verification command is not resolvable: {argv[0]}"
    elif shutil.which(argv[0]) is None:
        return None, env, f"verification command is not resolvable: {argv[0]}"
    return argv, env, None


def _verify_parse_argv(argv: list[str], target: Path) -> tuple[list[str] | None, dict[str, str], str | None]:
    """Resolve a pre-parsed verification argv (e.g. from --argv-json).

    The argv arrived pre-split (no shlex/shell parsing happens on it), so the
    shell-metacharacter heuristic in ``_verify_parse_command`` does not apply:
    there is no shell involved and no ambiguity for it to guard against.
    """
    if not argv:
        return None, {}, "empty command"
    executable = Path(argv[0]).name
    if executable in constants.SCANNER_HIGH_RISK_COMMANDS:
        return None, {}, _high_risk_command_message(executable)
    if "/" in argv[0]:
        executable_path = Path(argv[0]).expanduser()
        if not executable_path.is_absolute():
            executable_path = target / executable_path
        if not executable_path.exists():
            return None, {}, f"verification command is not resolvable: {argv[0]}"
    elif shutil.which(argv[0]) is None:
        return None, {}, f"verification command is not resolvable: {argv[0]}"
    return list(argv), {}, None


def _verify_execution_argv(argv: list[str], target: Path) -> list[str]:
    execution_argv = list(argv)
    if "/" in execution_argv[0]:
        executable_path = Path(execution_argv[0]).expanduser()
        if not executable_path.is_absolute():
            executable_path = target / executable_path
        execution_argv[0] = str(executable_path)
    return execution_argv


_VERIFY_CANCELED_RC = 130
_VERIFY_INTERRUPTED_COMMAND_STATUS = "interrupted"
_VERIFY_CANCELED_RECEIPT_STATUS = "canceled"


def _verify_child_popen_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": proc._WINDOWS_NEW_PROCESS_GROUP}


def _decode_verify_child_output(stdout: str | bytes | None, stderr: str | bytes | None) -> tuple[str, str]:
    stdout_text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
    stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    return stdout_text, stderr_text


def _terminate_verify_child(process: subprocess.Popen[bytes]) -> None:
    try:
        proc._terminate_processes((process,), terminate_grace=0.5, kill_grace=0.5)
    except KeyboardInterrupt:
        proc._terminate_processes((process,), terminate_grace=0.0, kill_grace=0.0)


def _run_verify_child_process(
    execution_argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[str, int | None, str, str]:
    """Run one verify child in its own process group; return (status, exit_code, stdout, stderr)."""
    popen_kwargs = _verify_child_popen_kwargs()
    try:
        process = subprocess.Popen(
            execution_argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
            **popen_kwargs,
        )
    except OSError as exc:
        return "failed", 127, "", str(exc)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        stdout_text, stderr_text = _decode_verify_child_output(stdout, stderr)
        if process.returncode == 0:
            return "completed", 0, stdout_text, stderr_text
        return "failed", process.returncode, stdout_text, stderr_text
    except KeyboardInterrupt:
        _terminate_verify_child(process)
        stdout_text, stderr_text = "", ""
        try:
            stdout, stderr = process.communicate(timeout=proc._TIMED_OUT_DRAIN_SECONDS)
            stdout_text, stderr_text = _decode_verify_child_output(stdout, stderr)
        except KeyboardInterrupt:
            _terminate_verify_child(process)
        except subprocess.TimeoutExpired as exc:
            stdout_text, stderr_text = _decode_verify_child_output(exc.output, exc.stderr)
        return _VERIFY_INTERRUPTED_COMMAND_STATUS, None, stdout_text, stderr_text
    except subprocess.TimeoutExpired as exc:
        _terminate_verify_child(process)
        stdout_text, stderr_text = _decode_verify_child_output(exc.stdout, exc.stderr)
        try:
            process.communicate(timeout=proc._TIMED_OUT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        return "timed_out", None, stdout_text, stderr_text


def _active_work_session_id(target: Path) -> str | None:
    current = helpers._current_path(target)
    if not current.is_file():
        return None
    session_id = current.read_text().strip()
    return session_id or None


def _stamp_manifest_command_metadata(receipt: dict[str, Any], manifest: verify_manifest.VerifyManifest) -> None:
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return
    for index, command in enumerate(commands):
        if not isinstance(command, dict) or index >= len(manifest.checks):
            continue
        spec = manifest.checks[index]
        command["check_id"] = spec.check_id
        command["check_role"] = spec.check_role
        if spec.obligation_id:
            command["obligation_id"] = spec.obligation_id
        verify_trial.stamp_verify_command_failure_taxonomy(command)


def _build_subject_binding(
    target: Path,
    manifest: verify_manifest.VerifyManifest,
    receipt: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any] | None:
    fingerprint = verify_manifest.resolve_subject_fingerprint(target, manifest)
    if fingerprint is None:
        return None
    verifier_session = verify_trial.verifier_session_id() or f"verify-{receipt.get('run_id')}"
    binding: dict[str, Any] = {
        "binding_mode": manifest.binding_mode,
        "artifact_kind": manifest.artifact_kind,
        "artifact_id": manifest.artifact_id,
        "content_fingerprint": fingerprint,
        "manifest_binding": verify_manifest.build_manifest_binding(target, manifest),
        "verifier_identity": {
            "verifier_id": manifest.verifier_id,
            "session_id": verifier_session,
        },
    }
    if manifest.binding_mode == "fixture_eval":
        binding["fixture_binding"] = {
            "manifest_id": manifest.fixture_manifest_id,
            "case_id": manifest.fixture_case_id,
            "check_id": manifest.fixture_check_id,
        }
        return binding
    if manifest.subject_path is None:
        return None
    patch_path = run_dir / "changes.patch"
    try:
        patch_bytes = patch_path.read_bytes()
    except OSError:
        patch_bytes = b""
    owned_delta = verify_trial.owned_delta_sha256(patch_bytes, manifest.subject_path)
    subject_hash = verify_trial.subject_hash_for_path(target, manifest.subject_path)
    if subject_hash is None:
        return None
    binding["patch_source"] = manifest.patch_source
    session_dir = helpers._active_session_dir(target)
    session_payload = helpers._read_session(session_dir) if session_dir is not None else None
    if isinstance(session_payload, dict) and session_payload.get("id"):
        session_id = str(session_payload["id"])
    else:
        session_id = _active_work_session_id(target)
    binding["producer_binding"] = verify_trial.build_producer_binding(
        session_id=session_id,
        session_payload=session_payload,
        subject_path=manifest.subject_path,
        owned_delta=owned_delta,
    )
    binding["patch_binding"] = {
        "baseline_commit": receipt.get("baseline_commit"),
        "tree_fingerprint": receipt.get("tree_fingerprint"),
        "changes_patch_sha256": receipt.get("changes_patch_sha256"),
        "subject_path": manifest.subject_path,
        "subject_hash": subject_hash,
    }
    return binding


def _apply_manifest_scoring_fields(
    target: Path,
    receipt: dict[str, Any],
    manifest: verify_manifest.VerifyManifest,
    run_dir: Path,
) -> None:
    receipt["verify_manifest_id"] = manifest.manifest_id
    if manifest.required_utility_check_ids:
        receipt["required_utility_check_ids"] = list(manifest.required_utility_check_ids)
    _stamp_manifest_command_metadata(receipt, manifest)
    subject_binding = _build_subject_binding(target, manifest, receipt, run_dir)
    if subject_binding is not None:
        receipt["subject_binding"] = subject_binding
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)


def _finalize_verify_receipt(
    target: Path,
    run_dir: Path,
    receipt: dict[str, Any],
    *,
    started,
    rc: int,
    canceled: bool,
) -> tuple[dict[str, Any], int]:
    completed_at = helpers._now()
    receipt["completed_at"] = completed_at.isoformat()
    receipt["duration_seconds"] = (completed_at - started).total_seconds()
    if canceled:
        receipt["status"] = _VERIFY_CANCELED_RECEIPT_STATUS
        receipt.setdefault(
            "interruption",
            {"kind": "keyboard-interrupt", "detail": "verification canceled by user"},
        )
        rc = _VERIFY_CANCELED_RC
    else:
        command_statuses = [c.get("status") for c in receipt["commands"] if isinstance(c, dict)]
        if rc == 0:
            receipt["status"] = "completed"
        elif any(status in ("failed", "timed_out") for status in command_statuses):
            receipt["status"] = "failed"
        else:
            receipt["status"] = "rejected"
    commands = receipt.get("commands")
    if isinstance(commands, list):
        for command in commands:
            if isinstance(command, dict):
                verify_trial.stamp_verify_command_failure_taxonomy(command)
    verify_trial.stamp_verify_receipt_failure_taxonomy(receipt)
    try:
        git = _receipt_git_snapshot(target)
        if git is not None:
            receipt["git"] = git
    except Exception:
        pass
    log_digests: dict[str, str] = {}
    for command in receipt["commands"]:
        if not isinstance(command, dict):
            continue
        for key in ("stdout_log_path", "stderr_log_path"):
            value = command.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            try:
                log_name = str(path.relative_to(run_dir))
            except ValueError:
                log_name = path.name
            try:
                log_digests[log_name] = localio.file_sha256(path)
            except OSError:
                continue
    delta = receipt.get("code_graph_delta")
    if isinstance(delta, dict):
        sidecar_value = delta.get("sidecar_path")
        if isinstance(sidecar_value, str) and sidecar_value:
            sidecar_path = Path(sidecar_value)
            if sidecar_path.is_file():
                try:
                    log_name = str(sidecar_path.relative_to(run_dir))
                except ValueError:
                    log_name = sidecar_path.name
                try:
                    log_digests[log_name] = localio.file_sha256(sidecar_path)
                except OSError:
                    pass
    try:
        receipt["digests"] = {
            "algorithm": "sha256",
            "logs": dict(sorted(log_digests.items())),
            "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
        }
        signing_key = receipt_signing.load_key(target)
        if signing_key is not None:
            key, key_id = signing_key
            receipt["digests"]["signature"] = receipt_signing.sign(receipt["digests"]["receipt_sha256"], key)
            receipt["digests"]["key_id"] = key_id
    except Exception:
        receipt.pop("digests", None)
    helpers._write_json(run_dir / "receipt.json", receipt)
    try:
        _write_verify_markdown(run_dir, receipt)
    except Exception:
        pass
    try:
        _prune_verify_runs(target)
    except Exception:
        pass
    return receipt, rc


def _safe_finalize_verify_receipt(
    target: Path,
    run_dir: Path,
    receipt: dict[str, Any],
    *,
    started,
    rc: int,
    canceled: bool,
) -> tuple[dict[str, Any], int]:
    try:
        return _finalize_verify_receipt(target, run_dir, receipt, started=started, rc=rc, canceled=canceled)
    except BaseException:
        if canceled:
            receipt["status"] = _VERIFY_CANCELED_RECEIPT_STATUS
            receipt.setdefault(
                "interruption",
                {"kind": "keyboard-interrupt", "detail": "verification canceled by user"},
            )
            rc = _VERIFY_CANCELED_RC
        elif receipt.get("status") == "running":
            receipt["status"] = "failed"
        receipt.setdefault("completed_at", helpers._now().isoformat())
        try:
            helpers._write_json(run_dir / "receipt.json", receipt)
        except OSError:
            pass
        return receipt, rc


VERIFY_RUNS_KEEP = config.DEFAULT_VERIFY_RUNS_KEEP

VERIFY_ARCHIVE_INDEX_NAME = "index.jsonl"
VERIFY_ARCHIVE_INDEX_SCHEMA = "brigade.verify_archive_index.v1"
VERIFY_ARCHIVE_INDEX_SCHEMA_VERSION = 1


def _verify_archive_root(target: Path, archive_root: Path | None = None) -> Path | None:
    """Resolve the verify-evidence archive root, or None when archival is disabled."""
    if archive_root is None:
        enabled, resolved = config.resolve_verify_archive(target)
        if not enabled:
            return None
    else:
        resolved = archive_root.expanduser()
        if not resolved.is_absolute():
            resolved = target / resolved
    runs_root = helpers._verify_runs_root(target).resolve(strict=False)
    resolved = resolved.resolve(strict=False)
    try:
        resolved.relative_to(runs_root)
        overlaps = True
    except ValueError:
        try:
            runs_root.relative_to(resolved)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        raise ValueError(f"verify archive root overlaps verify runs root: {resolved}")
    return resolved


def _verify_archive_index_entry(
    run_dir: Path,
    dest: Path,
    receipt_file_sha256: str | None,
    *,
    already_archived: bool,
) -> dict[str, Any]:
    receipt = _verify_read_receipt(dest)
    digests = receipt.get("digests") if receipt is not None and isinstance(receipt.get("digests"), dict) else {}
    return {
        "schema": VERIFY_ARCHIVE_INDEX_SCHEMA,
        "schema_version": VERIFY_ARCHIVE_INDEX_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "archived_at": helpers._now().isoformat(),
        "source_run_dir": str(run_dir),
        "archive_run_dir": str(dest),
        "already_archived": already_archived,
        "receipt_file_sha256": receipt_file_sha256,
        "receipt_schema_version": receipt.get("schema_version") if receipt is not None else None,
        "receipt_sha256": digests.get("receipt_sha256"),
        "signature": digests.get("signature"),
        "key_id": digests.get("key_id"),
        "status": receipt.get("status") if receipt is not None else None,
        "started_at": receipt.get("started_at") if receipt is not None else None,
        "completed_at": receipt.get("completed_at") if receipt is not None else None,
    }


def _append_verify_archive_index(archive_root: Path, entry: dict[str, Any]) -> None:
    index_path = archive_root / VERIFY_ARCHIVE_INDEX_NAME
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


def _assert_archived_receipt_integrity(archived_receipt: Path) -> None:
    """Re-verify a copied receipt's self-declared digest against its archived bytes."""
    try:
        payload = json.loads(archived_receipt.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError(f"verify archive receipt is unreadable: {archived_receipt}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OSError(f"verify archive receipt is not a JSON object: {archived_receipt}")
    digests = payload.get("digests")
    if not isinstance(digests, dict):
        return  # legacy receipts without a digests block have no self-digest to re-verify
    expected = digests.get("receipt_sha256")
    if not isinstance(expected, str) or not expected:
        return
    actual = localio.canonical_json_digest(payload, exclude_keys={"digests"})
    if actual != expected:
        raise OSError(f"verify archive receipt digest mismatch: {archived_receipt}")


def _verify_archive_tree_manifest(root: Path) -> dict[str, tuple[str, str | None]]:
    """Hash one regular directory tree, rejecting links and special files."""
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise OSError(f"verify archive tree is unreadable: {root}: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise OSError(f"verify archive tree is not a regular directory: {root}")
    manifest: dict[str, tuple[str, str | None]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise OSError(f"verify archive tree is unreadable: {directory}: {exc}") from exc
        for child in children:
            relative = child.relative_to(root).as_posix()
            try:
                child_stat = child.lstat()
            except OSError as exc:
                raise OSError(f"verify archive entry is unreadable: {child}: {exc}") from exc
            if stat.S_ISLNK(child_stat.st_mode):
                raise OSError(f"verify archive tree contains a symlink: {child}")
            if stat.S_ISDIR(child_stat.st_mode):
                manifest[relative] = ("directory", None)
                pending.append(child)
                continue
            if stat.S_ISREG(child_stat.st_mode):
                manifest[relative] = ("file", localio.file_sha256(child))
                continue
            raise OSError(f"verify archive tree contains a special file: {child}")
    return manifest


def _expected_verify_archive_manifest(
    run_dir: Path, source_manifest: dict[str, tuple[str, str | None]]
) -> dict[str, tuple[str, str | None]]:
    """Return the tree manifest after recovery-checkpoint export stripping (#636)."""
    from brigade import run_checkpoint

    expected = dict(source_manifest)
    prefix = f"events/{run_checkpoint.CHECKPOINT_DIR_NAME}/"
    for relative, (kind, _digest) in list(expected.items()):
        if kind != "file" or not relative.startswith(prefix) or not relative.endswith(".json"):
            continue
        path = run_dir / relative
        raw = path.read_bytes()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and run_checkpoint.is_checkpoint_artifact_reference(parsed):
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if path.name != f"{sha}.json":
            raise OSError(f"verify archive checkpoint export filename digest mismatch: {relative}")
        reference = run_checkpoint.checkpoint_artifact_reference(sha256=sha, byte_size=len(raw))
        ref_bytes = (json.dumps(reference, indent=2, sort_keys=True) + "\n").encode("utf-8")
        expected[relative] = ("file", hashlib.sha256(ref_bytes).hexdigest())
    return expected


def _archive_verify_run(run_dir: Path, archive_root: Path) -> dict[str, Any]:
    """Copy one run dir into the archive and append an index entry. Raises on failure.

    Integrity is checked twice: the archived receipt bytes must match the source
    bytes, and a receipt that carries ``digests.receipt_sha256`` must still
    re-hash to that value after the copy. Callers must treat any exception as
    "do not delete the original". Recovery-checkpoint bodies are private and are
    replaced with the closed artifact-reference shape before the archive lands
    (issue #636); local source run dirs are left unchanged.
    """
    from brigade import run_checkpoint

    run_id = run_dir.name
    dest = archive_root / run_id
    source_manifest = _verify_archive_tree_manifest(run_dir)
    expected_manifest = _expected_verify_archive_manifest(run_dir, source_manifest)
    receipt_path = run_dir / "receipt.json"
    source_receipt_sha = localio.file_sha256(receipt_path) if receipt_path.is_file() else None
    if dest.is_symlink():
        raise OSError(f"verify archive conflict: {dest} is a symlink")
    if dest.exists():
        # Re-archive is safe when evidence matches the source, or when the
        # existing archive is already the privacy-normalized export of source.
        dest_manifest = _verify_archive_tree_manifest(dest)
        if dest_manifest not in (source_manifest, expected_manifest):
            raise OSError(f"verify archive conflict: {dest} already exists with different evidence")
        dest_receipt = dest / "receipt.json"
        dest_sha = localio.file_sha256(dest_receipt) if dest_receipt.is_file() else None
        if dest_sha != source_receipt_sha:
            raise OSError(f"verify archive conflict: {dest} already exists with different evidence")
        if dest_sha is not None:
            _assert_archived_receipt_integrity(dest_receipt)
        run_checkpoint.strip_checkpoint_bodies_for_export(dest)
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(dest)
        entry = _verify_archive_index_entry(run_dir, dest, dest_sha, already_archived=True)
        _append_verify_archive_index(archive_root, entry)
        return entry
    archive_root.mkdir(parents=True, exist_ok=True)
    staging = archive_root / f".{run_id}.staging-{uuid4().hex[:8]}"
    shutil.copytree(run_dir, staging, symlinks=True)
    try:
        if _verify_archive_tree_manifest(staging) != source_manifest:
            raise OSError(f"verify archive copy tree mismatch: {run_id}")
        if source_receipt_sha is not None:
            copied_sha = localio.file_sha256(staging / "receipt.json")
            if copied_sha != source_receipt_sha:
                raise OSError(f"verify archive copy integrity check failed: {run_id}")
            _assert_archived_receipt_integrity(staging / "receipt.json")
        # Export boundary (#636): never archive private recovery-checkpoint
        # bodies. Replace them with the closed artifact-reference shape after
        # the byte-identical copy check so local recovery semantics stay intact.
        run_checkpoint.strip_checkpoint_bodies_for_export(staging)
        run_checkpoint.assert_export_tree_has_no_checkpoint_bodies(staging)
        if _verify_archive_tree_manifest(staging) != expected_manifest:
            raise OSError(f"verify archive export privacy normalize mismatch: {run_id}")
        os.rename(staging, dest)
        if _verify_archive_tree_manifest(run_dir) != source_manifest:
            raise OSError(f"verify archive source changed during copy: {run_id}")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    archived_receipt_sha = localio.file_sha256(dest / "receipt.json") if source_receipt_sha is not None else None
    entry = _verify_archive_index_entry(run_dir, dest, archived_receipt_sha, already_archived=False)
    _append_verify_archive_index(archive_root, entry)
    return entry


def _prune_verify_runs(target: Path, keep: int | None = None, archive_root: Path | None = None) -> int:
    """Cap retained verify-run directories, archiving receipt evidence before deletion.

    Run dirs are timestamp-prefixed (sortable by name); the newest ``keep`` are
    retained locally. Older dirs are first copied into the verify archive (with
    an append-only index entry carrying the receipt digest, signature, and
    schema version) and only then removed, so pruning never destroys receipt
    evidence. A run dir whose archival fails is kept locally. Best-effort: an
    archival or removal error never aborts a verify run.
    """
    if keep is None:
        try:
            keep = config.resolve_verify_runs_keep(target)
        except Exception:
            keep = VERIFY_RUNS_KEEP
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return 0
    try:
        resolved_archive = _verify_archive_root(target, archive_root)
    except Exception:
        return 0
    run_dirs = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda p: p.name, reverse=True)
    removed = 0
    # Oldest first so the append-only archive index grows in chronological order.
    for stale in reversed(run_dirs[keep:]):
        if resolved_archive is not None:
            try:
                _archive_verify_run(stale, resolved_archive)
            except Exception:
                continue  # prune safety: never delete evidence that failed to archive
        try:
            shutil.rmtree(stale)
            removed += 1
        except OSError:
            continue
    return removed


def _normalize_command_identity(command: str | list[str]) -> str:
    """Canonicalize one command without discarding execution-affecting prefixes."""
    if isinstance(command, list):
        return shlex.join(command)
    try:
        return shlex.join(shlex.split(command))
    except ValueError:
        return command


def _planned_commands_display(commands: list[str | list[str]]) -> list[str]:
    return [shlex.join(command) if isinstance(command, list) else command for command in commands]


def _planned_commands_identity(commands: list[str | list[str]]) -> list[str]:
    return [_normalize_command_identity(command) for command in commands]


def _receipt_planned_commands_identity(receipt: dict[str, Any]) -> list[str] | None:
    """Return normalized command identity from a verify receipt."""
    planned = receipt.get("planned_commands")
    if isinstance(planned, list) and all(isinstance(item, str) for item in planned):
        return [_normalize_command_identity(item) for item in planned]
    commands = receipt.get("commands")
    if not isinstance(commands, list):
        return None
    display: list[str] = []
    for command in commands:
        if isinstance(command, dict):
            command_text = command.get("command")
            if isinstance(command_text, str):
                display.append(_normalize_command_identity(command_text))
    return display or None


def _verify_receipt_evidence_ref(receipt: dict[str, Any]) -> str:
    path = receipt.get("path")
    if isinstance(path, str) and path:
        return str(Path(path) / "receipt.json")
    run_id = receipt.get("run_id")
    target = receipt.get("target")
    if isinstance(run_id, str) and run_id and isinstance(target, str) and target:
        return str(Path(target) / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json")
    return ""


def canonical_verify_evidence_ref(target: Path, receipt: dict[str, Any]) -> str:
    """Return the stable verification identity, following ``reused_from`` when present.

    A reused receipt points at an earlier command result. Scoring and capture must
    key both paths as one signal so the same verification cannot inflate helped
    counts. Receipts that merely share content without ``reused_from`` stay distinct.
    """
    target = target.expanduser().resolve()
    current = receipt
    seen: set[str] = set()
    while True:
        reused = current.get("reused_from")
        if not isinstance(reused, str) or not reused.strip():
            return _verify_receipt_evidence_ref(current)
        run_id = reused.strip()
        if run_id in seen:
            return _verify_receipt_evidence_ref(current)
        seen.add(run_id)
        next_receipt, _error = _resolve_verify_receipt(target, run_id)
        if next_receipt is None:
            return str(helpers._verify_runs_root(target) / run_id / "receipt.json")
        current = next_receipt


def _verify_receipt_has_outcome_capture(target: Path, receipt: dict[str, Any]) -> bool:
    from .. import outcome_cmd

    evidence_ref = canonical_verify_evidence_ref(target, receipt)
    if not evidence_ref:
        return False
    try:
        resolved = Path(evidence_ref).expanduser().resolve()
    except OSError:
        resolved = None
    for record in outcome_cmd.load_scoring_records(target):
        if not record.evidence_ref:
            continue
        try:
            if resolved is not None and Path(record.evidence_ref).expanduser().resolve() == resolved:
                return True
        except OSError:
            if record.evidence_ref == evidence_ref:
                return True
    return False


def _find_uncaptured_failed_verify_receipt(target: Path, planned_identity: list[str]) -> dict[str, Any] | None:
    for receipt in _verify_receipts(target):
        if _receipt_planned_commands_identity(receipt) != planned_identity:
            continue
        if receipt.get("status") != "failed":
            return None
        if _verify_receipt_has_outcome_capture(target, receipt):
            return None
        return receipt
    return None


def _receipt_capture_artifact_id(receipt: dict[str, Any] | None) -> str | None:
    """Return the artifact id stamped on a verify receipt for outcome capture, if any."""
    if not isinstance(receipt, dict):
        return None
    capture = receipt.get("outcome_capture")
    if not isinstance(capture, dict):
        return None
    artifact_id = capture.get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id.strip():
        return artifact_id.strip()
    return None


def _stamp_outcome_capture(receipt: dict[str, Any], capture: str | None, capture_kind: str) -> None:
    """Record the intended outcome artifact on the receipt before digests are sealed."""
    if not capture:
        return
    receipt["outcome_capture"] = {
        "artifact_id": capture,
        "artifact_kind": capture_kind,
    }


def _capture_before_retry_message(run_id: str, *, artifact_id: str) -> str:
    return f"brigade outcome capture {artifact_id} --run-id {run_id}"


def _enforce_capture_before_retry(
    target: Path,
    planned_identity: list[str],
    *,
    mode: str,
    capture_artifact_id: str | None = None,
) -> int | None:
    """Block or warn when the latest matching failed receipt has no outcome capture."""
    if mode == "off":
        return None
    failed = _find_uncaptured_failed_verify_receipt(target, planned_identity)
    if failed is None:
        return None
    from .. import outcome_cmd

    artifact_id = outcome_cmd.resolve_capture_artifact_id(
        _receipt_capture_artifact_id(failed),
        capture_artifact_id,
    )
    message = _capture_before_retry_message(str(failed.get("run_id") or ""), artifact_id=artifact_id)
    if mode == "block":
        print(f"error: {message}", file=sys.stderr)
        return 1
    print(f"warning: {message}", file=sys.stderr)
    return None


def _latest_verify_receipt(target: Path) -> dict[str, Any] | None:
    receipts = _verify_receipts(target)
    return receipts[0] if receipts else None


def _verify_receipt_reference(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    digests = receipt.get("digests") if isinstance(receipt.get("digests"), dict) else {}
    return {
        "run_id": receipt.get("run_id"),
        "status": receipt.get("status"),
        "path": receipt.get("path"),
        "digest": digests.get("receipt_sha256"),
    }


def _verify_read_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("path", str(receipt.parent))
    return data


def _verify_receipts(target: Path) -> list[dict[str, Any]]:
    root = helpers._verify_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_verify_read_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _resolve_verify_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _verify_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "verification run not found: latest")
    matches = [run for run in receipts if str(run.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"verification run not found: {run_id}"
    if len(matches) > 1:
        return None, f"verification run id is ambiguous: {run_id}"
    return matches[0], None


def _verification_task_from_session(payload: dict[str, Any]) -> dict[str, Any] | None:
    task = payload.get("task")
    return task if isinstance(task, dict) else None


def _verification_evidence_payload(target: Path, session: tuple[Path, dict[str, Any]] | None = None) -> dict[str, Any]:
    from .. import handoff_cmd

    target = target.expanduser().resolve()
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    latest_session = session or (sessions[0] if sessions else None)
    session_info = helpers._session_info(latest_session[0], latest_session[1]) if latest_session else None
    task = _verification_task_from_session(latest_session[1]) if latest_session else None
    latest_verify = _verify_receipt_reference(_latest_verify_receipt(target))
    sweep_health = scanners_mod._scanner_sweep_health(target)
    review_health = reviews_mod._review_health(target)
    handoff_drafts = handoff_cmd.draft_queue_payload(target)
    return {
        "target": str(target),
        "session": session_info,
        "task": task,
        "task_acceptance": task.get("acceptance")
        if isinstance(task, dict) and isinstance(task.get("acceptance"), list)
        else [],
        "latest_verify": latest_verify,
        "scanner_sweep": {
            "latest": sweep_health.get("latest"),
            "issue_count": sweep_health.get("review", {}).get("issue_count")
            if isinstance(sweep_health.get("review"), dict)
            else 0,
            "top_issue": sweep_health.get("review", {}).get("top_issue")
            if isinstance(sweep_health.get("review"), dict)
            else None,
            "due_count": sweep_health.get("due_count"),
        },
        "code_review": {
            "latest_run": review_health.get("latest_run"),
            "latest_unclosed_run": review_health.get("latest_unclosed_run"),
            "unresolved_finding_count": review_health.get("unresolved_finding_count"),
            "top_unresolved_finding": review_health.get("top_unresolved_finding"),
        },
        "handoff_drafts": {
            "counts": handoff_drafts.get("counts"),
            "issue_count": handoff_drafts.get("issue_count"),
            "top_issue": handoff_drafts.get("top_issue"),
            "latest_ingest_run": handoff_drafts.get("latest_ingest_run"),
        },
    }


def _verify_plan_payload(
    target: Path,
    commands: list[str] | None = None,
    *,
    manifest_id: str | None = None,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    manifest: verify_manifest.VerifyManifest | None = None
    if manifest_id:
        manifest, error = verify_manifest.resolve_manifest(target, manifest_id)
        if manifest is None:
            return {
                "target": str(target),
                "verify_runs_root": str(helpers._verify_runs_root(target)),
                "commands": [],
                "blockers": [error or f"verify manifest not found: {manifest_id}"],
                "evidence": _verification_evidence_payload(target),
                "suggested_command": f'brigade work verify run --manifest "{manifest_id}"',
            }
        planned_commands = manifest.planned_commands
    else:
        planned_commands = commands if commands is not None else _default_verify_commands(target)
    evidence = _verification_evidence_payload(target)
    blockers: list[str] = []
    if not planned_commands:
        blockers.append("no verification commands found; pass --command or --manifest")
    for command in planned_commands:
        _, _, error = _verify_parse_command(command, target)
        if error:
            blockers.append(f"{command}: {error}")
    payload = {
        "target": str(target),
        "verify_runs_root": str(helpers._verify_runs_root(target)),
        "commands": planned_commands,
        "blockers": blockers,
        "evidence": evidence,
        "suggested_command": "brigade work verify run"
        if planned_commands
        else 'brigade work verify run --command "..."',
    }
    if manifest is not None:
        payload["manifest_id"] = manifest.manifest_id
        payload["scoreable"] = True
        payload["suggested_command"] = f'brigade work verify run --manifest "{manifest.manifest_id}"'
    elif commands is not None:
        payload["scoreable"] = False
    return payload


def _write_verify_markdown(run_dir: Path, receipt: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Verification",
        "",
        f"- Run: `{receipt.get('run_id')}`",
        f"- Status: {receipt.get('status')}",
        f"- Target: `{receipt.get('target')}`",
        f"- Started: {receipt.get('started_at')}",
        f"- Completed: {receipt.get('completed_at')}",
        "",
    ]
    lines.extend(_verify_identity_lines(receipt))
    if lines[-1] != "":
        lines.append("")
    lines.extend(
        [
            "## Commands",
            "",
        ]
    )
    for command in receipt.get("commands", []):
        if not isinstance(command, dict):
            continue
        lines.append(f"- `{command.get('command')}`: exit={command.get('exit_code')} status={command.get('status')}")
    lines.extend(["", "## Evidence", ""])
    evidence = receipt.get("evidence") if isinstance(receipt.get("evidence"), dict) else {}
    session = evidence.get("session") if isinstance(evidence.get("session"), dict) else None
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    if session:
        lines.append(f"- Session: `{session.get('id')}` status={session.get('status')}")
    if latest_verify:
        lines.append(f"- Previous verification: `{latest_verify.get('run_id')}` status={latest_verify.get('status')}")
    delta = receipt.get("code_graph_delta")
    if isinstance(delta, dict):
        lines.append(f"- Code graph delta: {delta.get('summary') or 'code graph delta unavailable'}")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _verify_identity_binding(receipt: dict[str, Any]) -> str | None:
    baseline = receipt.get("baseline_commit")
    fingerprint = receipt.get("tree_fingerprint")
    patch_hash = receipt.get("changes_patch_sha256")
    if not isinstance(baseline, str) or not baseline:
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if not isinstance(patch_hash, str) or not patch_hash:
        return None
    return f"verified tree {fingerprint} = baseline {baseline} + patch {patch_hash}"


def _verify_identity_lines(receipt: dict[str, Any]) -> list[str]:
    baseline = receipt.get("baseline_commit")
    fingerprint = receipt.get("tree_fingerprint")
    patch_hash = receipt.get("changes_patch_sha256")
    if not isinstance(baseline, str) or not baseline:
        return []
    if not isinstance(fingerprint, str) or not fingerprint:
        return []
    if not isinstance(patch_hash, str) or not patch_hash:
        return []
    binding = _verify_identity_binding(receipt)
    assert binding is not None
    return [
        f"- Baseline commit: `{baseline}`",
        f"- Tree fingerprint: `{fingerprint}`",
        f"- Patch hash: `{patch_hash}`",
        f"- {binding}",
    ]


def _git_with_index(target: Path, index_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        return subprocess.run(
            ["git", "-C", str(target), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, stdout="", stderr=f"git timed out after {exc.timeout:g}s")
    except OSError:
        return subprocess.CompletedProcess(["git"], 127, stdout="", stderr="git unavailable")


def _stamp_harness_session(receipt: dict[str, Any]) -> None:
    """Record the caller's Claude harness session when BRIGADE_CLAUDE_SESSION is valid 16-hex."""
    claude_session = os.environ.get("BRIGADE_CLAUDE_SESSION")
    if claude_session and re.fullmatch(r"[0-9a-f]{16}", claude_session):
        receipt["harness_session"] = {"harness": "claude", "fingerprint": claude_session}


def _tree_fingerprint(target: Path) -> str | None:
    """Git tree object for HEAD plus tracked and non-ignored untracked files."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_file = Path(tmpdir) / "index"
            read_tree = _git_with_index(target, index_file, "read-tree", "HEAD")
            if read_tree.returncode != 0:
                return None
            add_all = _git_with_index(target, index_file, "add", "-A")
            if add_all.returncode != 0:
                return None
            write_tree = _git_with_index(target, index_file, "write-tree")
            if write_tree.returncode != 0:
                return None
            value = write_tree.stdout.strip()
            return value or None
    except OSError:
        return None


def _capture_verify_identity(target: Path, run_dir: Path) -> dict[str, Any]:
    unavailable: dict[str, Any] = {
        "schema_version": receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION,
        "baseline_commit": None,
        "tree_fingerprint": None,
        "changes_patch_sha256": None,
    }
    patch_path = run_dir / "changes.patch"
    try:
        baseline_commit = helpers._git_value(target, "rev-parse", "HEAD")
        tree_fingerprint = _tree_fingerprint(target)
        if baseline_commit is None or tree_fingerprint is None:
            return unavailable
        runguard.collect_changes_patch(target, patch_path)
        patch_bytes = patch_path.read_bytes()
        if (
            helpers._git_value(target, "rev-parse", "HEAD") != baseline_commit
            or _tree_fingerprint(target) != tree_fingerprint
        ):
            patch_path.unlink(missing_ok=True)
            return unavailable
    except (OSError, runguard.RunGuardError):
        patch_path.unlink(missing_ok=True)
        return unavailable
    return {
        "schema_version": receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION,
        "baseline_commit": baseline_commit,
        "tree_fingerprint": tree_fingerprint,
        "changes_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
    }


def _run_verify_commands(
    target: Path,
    commands: list[str | list[str]],
    timeout: int,
    *,
    graphtrail_timeout: float,
    manifest: verify_manifest.VerifyManifest | None = None,
    capture: str | None = None,
    capture_kind: str = "skill",
) -> tuple[dict[str, Any], int]:
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-work-verify-{uuid4().hex[:6]}"
    run_dir = helpers._verify_runs_root(target) / run_id
    receipt: dict[str, Any] | None = None
    graph_delta_before: dict[str, Any] | None = None
    rc = 0
    canceled = False
    finalized = False
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        identity = _capture_verify_identity(target, run_dir)
        receipt = {
            "run_id": run_id,
            "target": str(target),
            "status": "running",
            "started_at": started.isoformat(),
            "timeout": timeout,
            "path": str(run_dir),
            "evidence": _verification_evidence_payload(target),
            "commands": [],
            "planned_commands": _planned_commands_display(commands),
        }
        receipt.update(identity)
        _stamp_harness_session(receipt)
        _stamp_outcome_capture(receipt, capture, capture_kind)
        try:
            graph_delta_before = graphtrail_delta.capture_before(target, run_dir, timeout=graphtrail_timeout)
        except KeyboardInterrupt:
            canceled = True
            rc = _VERIFY_CANCELED_RC
            receipt.setdefault(
                "interruption",
                {"kind": "keyboard-interrupt", "detail": "verification canceled by user"},
            )
        if not canceled:
            for index, command in enumerate(commands, start=1):
                if isinstance(command, list):
                    argv, env_assignments, error = _verify_parse_argv(command, target)
                    display_command = shlex.join(command)
                else:
                    argv, env_assignments, error = _verify_parse_command(command, target)
                    display_command = command
                command_result: dict[str, Any] = {
                    "command": display_command,
                    "env": sorted(env_assignments),
                    "started_at": helpers._now().isoformat(),
                }
                stdout_path = run_dir / f"command-{index}-stdout.log"
                stderr_path = run_dir / f"command-{index}-stderr.log"
                if error or argv is None:
                    command_result.update(
                        {
                            "status": "rejected",
                            "exit_code": 2,
                            "stderr_summary": error,
                            "stdout_summary": "",
                            "stdout_log_path": str(stdout_path),
                            "stderr_log_path": str(stderr_path),
                        }
                    )
                    stdout_path.write_text("")
                    stderr_path.write_text(str(error or "invalid command") + "\n")
                    if rc == 0:
                        rc = 2
                    receipt["commands"].append(command_result)
                    continue
                run_env = os.environ.copy()
                run_env.update(env_assignments)
                execution_argv = _verify_execution_argv(argv, target)
                command_started = helpers._now()
                status, exit_code, stdout, stderr = _run_verify_child_process(
                    execution_argv,
                    cwd=target,
                    env=run_env,
                    timeout=timeout,
                )
                command_completed = helpers._now()
                stdout_path.write_text(stdout)
                stderr_path.write_text(stderr)
                command_result.update(
                    {
                        "status": status,
                        "exit_code": exit_code,
                        "completed_at": command_completed.isoformat(),
                        "duration_seconds": (command_completed - command_started).total_seconds(),
                        "argv": argv,
                        "stdout_summary": scanners_mod._scanner_run_summary(stdout),
                        "stderr_summary": scanners_mod._scanner_run_summary(stderr),
                        "stdout_log_path": str(stdout_path),
                        "stderr_log_path": str(stderr_path),
                    }
                )
                if status == _VERIFY_INTERRUPTED_COMMAND_STATUS:
                    canceled = True
                    rc = _VERIFY_CANCELED_RC
                    receipt["commands"].append(command_result)
                    break
                if status == "timed_out":
                    rc = 124
                elif status == "failed" and exit_code is not None and rc == 0:
                    rc = exit_code
                elif status == "failed" and rc == 0:
                    rc = 127
                receipt["commands"].append(command_result)
            if canceled and "code_graph_delta" not in receipt:
                try:
                    receipt["code_graph_delta"] = graphtrail_delta.capture_after_and_diff(
                        target, run_dir, graph_delta_before, timeout=graphtrail_timeout
                    )
                except (Exception, KeyboardInterrupt):
                    receipt["code_graph_delta"] = graphtrail_delta._status(
                        "unavailable",
                        "code graph delta unavailable: verification canceled before graph capture completed",
                    )
            elif not canceled:
                receipt["code_graph_delta"] = graphtrail_delta.capture_after_and_diff(
                    target, run_dir, graph_delta_before, timeout=graphtrail_timeout
                )
    except KeyboardInterrupt:
        canceled = True
        rc = _VERIFY_CANCELED_RC
        if receipt is not None:
            receipt.setdefault(
                "interruption",
                {"kind": "keyboard-interrupt", "detail": "verification canceled by user"},
            )
            if "code_graph_delta" not in receipt and graph_delta_before is not None:
                try:
                    receipt["code_graph_delta"] = graphtrail_delta.capture_after_and_diff(
                        target, run_dir, graph_delta_before, timeout=graphtrail_timeout
                    )
                except (Exception, KeyboardInterrupt):
                    receipt["code_graph_delta"] = graphtrail_delta._status(
                        "unavailable",
                        "code graph delta unavailable: verification canceled before graph capture completed",
                    )
    finally:
        if receipt is not None and not finalized:
            if manifest is not None:
                _apply_manifest_scoring_fields(target, receipt, manifest, run_dir)
            receipt, rc = _safe_finalize_verify_receipt(
                target,
                run_dir,
                receipt,
                started=started,
                rc=rc,
                canceled=canceled,
            )
            finalized = True
    assert receipt is not None
    return receipt, rc


def _write_reused_receipt(
    target: Path,
    latest: dict[str, Any],
    planned_display: list[str],
    timeout: int,
    *,
    capture: str | None = None,
    capture_kind: str = "skill",
) -> tuple[dict[str, Any], int]:
    """Write a fresh receipt dir that records a reused passing run (no commands executed)."""
    started = helpers._now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-work-verify-{uuid4().hex[:6]}"
    run_dir = helpers._verify_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    identity = _capture_verify_identity(target, run_dir)
    completed_at = helpers._now()
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "target": str(target),
        "status": "completed",
        "started_at": started.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": (completed_at - started).total_seconds(),
        "timeout": timeout,
        "path": str(run_dir),
        "commands": copy.deepcopy(latest.get("commands", [])),
        "planned_commands": planned_display,
    }
    receipt.update(identity)
    _stamp_harness_session(receipt)
    _stamp_outcome_capture(receipt, capture, capture_kind)
    reused_from = latest.get("run_id")
    if isinstance(reused_from, str) and reused_from:
        receipt["reused_from"] = reused_from
    git = _receipt_git_snapshot(target)
    if git is not None:
        receipt["git"] = git
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    signing_key = receipt_signing.load_key(target)
    if signing_key is not None:
        key, key_id = signing_key
        receipt["digests"]["signature"] = receipt_signing.sign(receipt["digests"]["receipt_sha256"], key)
        receipt["digests"]["key_id"] = key_id
    helpers._write_json(run_dir / "receipt.json", receipt)
    _write_verify_markdown(run_dir, receipt)
    try:
        _prune_verify_runs(target)
    except Exception:
        pass
    return receipt, 0


def _resolve_closeout_session(target: Path, session_id: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    sessions, _ = helpers._collect_sessions(helpers._work_root(target))
    if session_id == "latest":
        return (sessions[0][0], sessions[0][1], None) if sessions else (None, None, "work session not found: latest")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path, payload in sessions:
        payload_id = str(payload.get("id") or path.name)
        if (
            payload_id == session_id
            or path.name == session_id
            or payload_id.startswith(session_id)
            or path.name.startswith(session_id)
        ):
            matches.append((path, payload))
    if not matches:
        path = helpers._resolve_session(target, session_id)
        payload = helpers._read_session(path)
        if payload is not None:
            return path, payload, None
        return None, None, f"work session not found: {session_id}"
    if len(matches) > 1:
        return None, None, f"work session id is ambiguous: {session_id}"
    return matches[0][0], matches[0][1], None


def _work_closeout_path(target: Path, closeout_id: str) -> Path:
    return helpers._work_closeouts_root(target) / closeout_id / "closeout.json"


def _latest_work_closeout_payload(target: Path) -> dict[str, Any] | None:
    root = helpers._work_closeouts_root(target)
    if not root.is_dir():
        return None
    closeouts: list[dict[str, Any]] = []
    for child in root.iterdir():
        payload = helpers._read_json(child / "closeout.json") if child.is_dir() else None
        if isinstance(payload, dict):
            payload.setdefault("path", str(child / "closeout.json"))
            closeouts.append(payload)
    closeouts.sort(key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)
    return closeouts[0] if closeouts else None


def _write_work_closeout_markdown(path: Path, closeout: dict[str, Any]) -> None:
    lines = [
        "# Brigade Work Closeout",
        "",
        f"- Closeout: `{closeout.get('closeout_id')}`",
        f"- Status: {closeout.get('status')}",
        f"- Ready: {closeout.get('ready')}",
        f"- Session: `{closeout.get('session', {}).get('id') if isinstance(closeout.get('session'), dict) else ''}`",
        f"- Verification: `{closeout.get('verification', {}).get('run_id') if isinstance(closeout.get('verification'), dict) else ''}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = closeout.get("blockers") if isinstance(closeout.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Evidence", ""])
    for key in ("task", "scanner_sweep", "code_review", "handoff_drafts"):
        value = closeout.get(key)
        lines.append(f"- {key}: `{json.dumps(value, sort_keys=True, default=str)[:500]}`")
    path.with_name("closeout.md").write_text("\n".join(lines) + "\n")


def _work_closeout_payload(target: Path, session_id: str, *, write: bool = False) -> tuple[dict[str, Any] | None, int]:
    target = target.expanduser().resolve()
    session_path, session_payload, error = _resolve_closeout_session(target, session_id)
    if session_path is None or session_payload is None:
        print(f"error: {error}", file=sys.stderr)
        return None, 1 if error and "not found" in error else 2
    evidence = _verification_evidence_payload(target, (session_path, session_payload))
    latest_verify = evidence.get("latest_verify") if isinstance(evidence.get("latest_verify"), dict) else None
    task = evidence.get("task") if isinstance(evidence.get("task"), dict) else None
    task_acceptance = evidence.get("task_acceptance") if isinstance(evidence.get("task_acceptance"), list) else []
    scanner_sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    code_review = evidence.get("code_review") if isinstance(evidence.get("code_review"), dict) else {}
    handoff_drafts = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    blockers: list[str] = []
    if session_payload.get("status") != "ended":
        blockers.append(f"work session is not ended: {session_payload.get('status')}")
    if latest_verify is None:
        blockers.append("no verification receipt found")
    elif latest_verify.get("status") != "completed":
        blockers.append(
            f"latest verification did not complete: {latest_verify.get('run_id')} [{latest_verify.get('status')}]"
        )
    if task is not None and not task_acceptance:
        blockers.append(f"task has no acceptance criteria: {task.get('id')}")
    latest_sweep = scanner_sweep.get("latest") if isinstance(scanner_sweep.get("latest"), dict) else None
    if latest_sweep and latest_sweep.get("status") == "failed":
        blockers.append(f"latest scanner sweep failed: {latest_sweep.get('sweep_id')}")
    if int(scanner_sweep.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved review issue(s): {scanner_sweep.get('issue_count')}")
    if code_review.get("latest_unclosed_run"):
        run = code_review["latest_unclosed_run"]
        if isinstance(run, dict):
            blockers.append(f"review run is not closed out: {run.get('run_id')}")
    if int(code_review.get("unresolved_finding_count") or 0) > 0:
        blockers.append(f"code review has unresolved finding(s): {code_review.get('unresolved_finding_count')}")
    if int(handoff_drafts.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoff_drafts.get('issue_count')}")
    now = helpers._now()
    closeout_id = f"{now.strftime('%Y%m%d-%H%M%S')}-work-closeout-{uuid4().hex[:6]}"
    closeout = {
        "schema_version": receipt_schema.WORK_CLOSEOUT_SCHEMA_VERSION,
        "closeout_id": closeout_id,
        "target": str(target),
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "created_at": now.isoformat(),
        "session": helpers._session_info(session_path, session_payload),
        "session_path": str(session_path),
        "task": ledger_mod._task_summary(task) if task else None,
        "acceptance_criteria": task_acceptance,
        "verification": {
            "run_id": latest_verify.get("run_id"),
            "status": latest_verify.get("status"),
            "path": latest_verify.get("path"),
            "command_count": len(latest_verify.get("commands") or []),
        }
        if latest_verify
        else None,
        "scanner_sweep": scanner_sweep,
        "code_review": code_review,
        "handoff_drafts": handoff_drafts,
        "blockers": blockers,
    }
    if write:
        path = _work_closeout_path(target, closeout_id)
        helpers._write_json(path, closeout)
        _write_work_closeout_markdown(path, closeout)
        session_payload["closeout"] = {
            "closeout_id": closeout_id,
            "status": closeout["status"],
            "ready": closeout["ready"],
            "path": str(path),
            "created_at": closeout["created_at"],
        }
        helpers._write_json(session_path / "session.json", session_payload)
        closeout["path"] = str(path)
    return closeout, 0 if closeout["ready"] else 1


def verify_plan(
    *,
    target: Path,
    commands: list[str] | None = None,
    manifest_id: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _verify_plan_payload(target, commands, manifest_id=manifest_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not payload["blockers"] else 1
    print(f"work verify plan: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    commands = payload.get("commands") if isinstance(payload.get("commands"), list) else []
    print(f"commands: {len(commands)}")
    for command in commands:
        print(f"- {command}")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    print(f"run: {payload['suggested_command']}")
    return 0 if not blockers else 1


def _attach_miseledger_indexing(
    receipt: dict[str, object],
    *,
    target: Path,
    json_output: bool,
) -> None:
    import contextlib
    import io

    from .. import receipts_cmd

    if json_output:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            indexing = receipts_cmd.index_miseledger_receipts(target=target, quiet=True)
    else:
        indexing = receipts_cmd.index_miseledger_receipts(target=target, quiet=False)
        print(indexing["message"])
    receipt["miseledger_indexing"] = {key: value for key, value in indexing.items() if key != "message"}


def verify_run(
    *,
    target: Path,
    commands: list[str | list[str]] | None = None,
    manifest_id: str | None = None,
    timeout: int = 900,
    graphtrail_timeout: float | None = None,
    json_output: bool = False,
    capture: str | None = None,
    capture_kind: str = "skill",
    reuse: bool = True,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if timeout < 1:
        print("error: --timeout must be a positive integer", file=sys.stderr)
        return 2
    manifest: verify_manifest.VerifyManifest | None = None
    if manifest_id:
        manifest, error = verify_manifest.resolve_manifest(target, manifest_id)
        if manifest is None:
            print(f"error: {error}", file=sys.stderr)
            return 1 if error and "not found" in error else 2
        planned = manifest.planned_commands
    else:
        planned = commands if commands is not None else _default_verify_commands(target)
    if not planned:
        print("error: no verification commands found; pass --command or --manifest", file=sys.stderr)
        return 2
    try:
        effective_graphtrail_timeout = config.resolve_graphtrail_delta_timeout(target, graphtrail_timeout)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    planned_display = _planned_commands_display(planned)
    planned_identity = _planned_commands_identity(planned)
    try:
        capture_before_retry = config.resolve_capture_before_retry(target)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    blocked_rc = _enforce_capture_before_retry(
        target,
        planned_identity,
        mode=capture_before_retry,
        capture_artifact_id=capture,
    )
    if blocked_rc is not None:
        return blocked_rc
    try:
        receipt = None
        if reuse and manifest is None:
            fingerprint = _tree_fingerprint(target)
            latest = _latest_verify_receipt(target)
            if (
                fingerprint is not None
                and latest is not None
                and latest.get("status") == "completed"
                and latest.get("tree_fingerprint") == fingerprint
                and latest.get("planned_commands") == planned_display
            ):
                receipt, rc = _write_reused_receipt(
                    target,
                    latest,
                    planned_display,
                    timeout,
                    capture=capture,
                    capture_kind=capture_kind,
                )
        if receipt is None:
            receipt, rc = _run_verify_commands(
                target,
                planned,
                timeout,
                graphtrail_timeout=effective_graphtrail_timeout,
                manifest=manifest,
                capture=capture,
                capture_kind=capture_kind,
            )
    except KeyboardInterrupt:
        print("error: verification canceled by user", file=sys.stderr)
        return _VERIFY_CANCELED_RC
    if json_output:
        if capture:
            # Record the outcome in the same command (closes the loop without a
            # second manual step) while keeping stdout valid JSON.
            import contextlib
            import io

            from .. import outcome_cmd

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                outcome_cmd.capture(
                    target=target,
                    artifact_id=capture,
                    artifact_kind=capture_kind,
                    run_id=receipt["run_id"],
                    json_output=False,
                )
            _attach_miseledger_indexing(receipt, target=target, json_output=True)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return rc
    print(f"work verify run: {target}")
    print(f"run: {receipt['run_id']}")
    print(f"status: {receipt['status']}")
    print(f"commands: {len(receipt['commands'])}")
    for command in receipt["commands"]:
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
    print(f"receipt: {Path(str(receipt['path'])) / 'receipt.json'}")
    if capture:
        from .. import outcome_cmd

        outcome_cmd.capture(
            target=target,
            artifact_id=capture,
            artifact_kind=capture_kind,
            run_id=receipt["run_id"],
            json_output=False,
        )
        _attach_miseledger_indexing(receipt, target=target, json_output=False)
    return rc


def verify_runs(*, target: Path, json_output: bool = False, limit: int = 20) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    runs = _verify_receipts(target)[:limit]
    payload = {"target": str(target), "verify_runs_root": str(helpers._verify_runs_root(target)), "runs": runs}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work verify runs: {target}")
    print(f"verify_runs_root: {payload['verify_runs_root']}")
    if not runs:
        print("runs: none")
        return 0
    for run in runs:
        print(
            f"- {run.get('run_id')} [{run.get('status')}] commands={len(run.get('commands') or [])} {run.get('started_at')}"
        )
    return 0


def verify_show(*, target: Path, run_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    run, error = _resolve_verify_receipt(target, run_id)
    if run is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps(run, indent=2, sort_keys=True))
        return 0
    print(f"work verify run: {run.get('run_id')}")
    print(f"status: {run.get('status')}")
    print(f"target: {run.get('target')}")
    print(f"started: {run.get('started_at')}")
    print(f"completed: {run.get('completed_at')}")
    baseline = run.get("baseline_commit")
    fingerprint = run.get("tree_fingerprint")
    patch_hash = run.get("changes_patch_sha256")
    if (
        isinstance(baseline, str)
        and baseline
        and isinstance(fingerprint, str)
        and fingerprint
        and isinstance(patch_hash, str)
        and patch_hash
    ):
        print(f"baseline commit: {baseline}")
        print(f"tree fingerprint: {fingerprint}")
        print(f"patch hash: {patch_hash}")
        binding = _verify_identity_binding(run)
        if binding:
            print(binding)
    for command in run.get("commands", []):
        if isinstance(command, dict):
            print(f"- {command.get('command')} [{command.get('status')}] exit={command.get('exit_code')}")
            if command.get("stdout_summary"):
                print(f"  stdout: {helpers._short(str(command.get('stdout_summary')), 140)}")
            if command.get("stderr_summary"):
                print(f"  stderr: {helpers._short(str(command.get('stderr_summary')), 140)}")
    return 0


def closeout(*, target: Path, session_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload, rc = _work_closeout_payload(target, session_id, write=True)
    if payload is None:
        return rc
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return rc
    print(f"work closeout: {payload['closeout_id']}")
    print(f"status: {payload['status']}")
    print(f"ready: {payload['ready']}")
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    print(f"session: {session.get('id')}")
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else None
    if verification:
        print(f"verification: {verification.get('run_id')} [{verification.get('status')}]")
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    if blockers:
        print("blockers:")
        for blocker in blockers:
            print(f"  - {blocker}")
    if payload.get("path"):
        print(f"receipt: {payload['path']}")
    return rc


def _acceptance_payload(target: Path) -> dict[str, Any]:
    tasks = [task for task in ledger_mod._read_task_ledger(target).get("tasks", []) if isinstance(task, dict)]
    pending = [task for task in tasks if task.get("status", "pending") == "pending"]
    done = [task for task in tasks if task.get("status") == "done"]
    pending_with_acceptance = [task for task in pending if ledger_mod._task_acceptance(task)]
    pending_missing = [task for task in pending if not ledger_mod._task_acceptance(task)]
    done_with_completion = [task for task in done if task.get("completion")]
    done_missing_completion = [task for task in done if not task.get("completion")]
    done_missing_completed_acceptance = [
        task
        for task in done
        if ledger_mod._task_acceptance(task) and not ledger_mod._normalize_acceptance(task.get("completed_acceptance"))
    ]
    review_payload = reviews_mod._review_findings_payload(target)
    review_groups = review_payload.get("groups") if isinstance(review_payload.get("groups"), dict) else {}
    review_outcomes = review_groups.get("by_resolution") if isinstance(review_groups.get("by_resolution"), dict) else {}
    latest_closeout = _latest_work_closeout_payload(target)
    closeout_summary = None
    if latest_closeout is not None:
        closeout_summary = {
            "closeout_id": latest_closeout.get("closeout_id"),
            "status": latest_closeout.get("status"),
            "ready": latest_closeout.get("ready"),
            "path": latest_closeout.get("path"),
            "acceptance_count": len(latest_closeout.get("acceptance_criteria") or []),
            "blocker_count": len(latest_closeout.get("blockers") or []),
        }
    issues: list[dict[str, Any]] = []
    if pending_missing:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_pending_missing",
                "detail": f"{len(pending_missing)} pending task(s) missing acceptance",
            }
        )
    if done_missing_completion:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completion",
                "detail": f"{len(done_missing_completion)} done task(s) missing completion evidence",
            }
        )
    if done_missing_completed_acceptance:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_done_missing_completed_acceptance",
                "detail": f"{len(done_missing_completed_acceptance)} done task(s) missing completion-time acceptance evidence",
            }
        )
    if int(review_payload.get("unresolved_count") or 0) > 0:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_review_findings_unresolved",
                "detail": f"{review_payload.get('unresolved_count')} review finding(s) unresolved",
            }
        )
    if done and latest_closeout is None:
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_missing",
                "detail": "completed tasks exist but no work closeout receipt was found",
            }
        )
    elif latest_closeout is not None and not latest_closeout.get("ready"):
        issues.append(
            {
                "status": constants.WARN,
                "name": "acceptance_work_closeout_blocked",
                "detail": f"latest work closeout is not ready: {latest_closeout.get('closeout_id')}",
            }
        )
    return {
        "target": str(target),
        "task_count": len(tasks),
        "pending_count": len(pending),
        "done_count": len(done),
        "pending_with_acceptance": [task.get("id") for task in pending_with_acceptance],
        "pending_missing_acceptance": [task.get("id") for task in pending_missing],
        "done_with_completion": [task.get("id") for task in done_with_completion],
        "done_missing_completion": [task.get("id") for task in done_missing_completion],
        "done_missing_completed_acceptance": [task.get("id") for task in done_missing_completed_acceptance],
        "review_findings": {
            "count": review_payload.get("count"),
            "unresolved_count": review_payload.get("unresolved_count"),
            "outcomes": dict(sorted(review_outcomes.items())),
            "top_unresolved": review_payload.get("top_unresolved"),
        },
        "review_finding_pending_count": int(review_outcomes.get("pending") or 0),
        "latest_work_closeout": closeout_summary,
        "coverage": {
            "pending_with_acceptance": len(pending_with_acceptance),
            "pending_missing_acceptance": len(pending_missing),
            "done_with_completion": len(done_with_completion),
            "done_missing_completion": len(done_missing_completion),
            "done_with_completed_acceptance": len(done) - len(done_missing_completed_acceptance),
            "done_missing_completed_acceptance": len(done_missing_completed_acceptance),
            "review_findings_resolved": int(review_payload.get("count") or 0)
            - int(review_payload.get("unresolved_count") or 0),
            "review_findings_unresolved": int(review_payload.get("unresolved_count") or 0),
            "work_closeout_ready": 1 if latest_closeout is not None and latest_closeout.get("ready") else 0,
            "work_closeout_missing": 1 if latest_closeout is None else 0,
        },
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def acceptance(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _acceptance_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"work acceptance: {target}")
    print(f"tasks: {payload['task_count']}")
    print(f"pending_missing_acceptance: {len(payload['pending_missing_acceptance'])}")
    print(f"done_missing_completion: {len(payload['done_missing_completion'])}")
    print(f"done_missing_completed_acceptance: {len(payload['done_missing_completed_acceptance'])}")
    print(f"review_findings_pending: {payload['review_finding_pending_count']}")
    review_findings = payload.get("review_findings") if isinstance(payload.get("review_findings"), dict) else {}
    print(f"review_findings_unresolved: {review_findings.get('unresolved_count', 0)}")
    closeout = payload.get("latest_work_closeout") if isinstance(payload.get("latest_work_closeout"), dict) else None
    print(f"work_closeout: {closeout.get('closeout_id') if closeout else 'none'}")
    return 0
