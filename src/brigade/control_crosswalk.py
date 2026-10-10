"""Versioned control crosswalk and `brigade evidence controls` evaluation.

The crosswalk is an evidence index and design documentation, not evidence that a
control operated.  It maps Brigade evidence claims to external framework control
identifiers and computes an evidentiary state per mapping from workspace
artifacts.
"""

from __future__ import annotations

import errno
import glob
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Any

from . import (
    agent_request,
    approval,
    attestation,
    attestation_input,
    causal_receipt,
    control_readiness,
    cosign_attestation,
    dirfd,
    evidence_package,
    localio,
    proc,
    receipts_trailer,
    run_journal,
)

SCHEMA = "brigade.control_crosswalk.v1"
EVIDENCE_CONTROLS_SCHEMA = "brigade.evidence_controls.v2"
HEADER_NOTICE = (
    "Evidence index for configured Brigade artifacts. Not a compliance determination, "
    "not evidence that a control operated."
)

VALID_RELATIONSHIPS = frozenset({"supports", "partially-supports", "no-relationship"})
VALID_OBLIGATION_BEARERS = frozenset({"provider", "deployer", "service-organisation", "supplier", "any"})
VALID_STATES = frozenset({"evidenced_passed", "evidenced_failed", "untested", "not_applicable"})
VALID_MAPPING_STATUSES = frozenset({"mapped", "identifiers-not-sourced"})
VALID_DISPOSITIONS = frozenset({"corrected-mismatch", "conditional-support", "unmapped-evidence-property"})
PROVENANCE_FIELDS = (
    "disposition",
    "superseded_control_id",
    "edition",
    "artifact_contract",
    "responsible_actor",
    "verification_limit",
)

_STATE_RULES: dict[str, str] = {
    "EC-01": "verify-receipt-completed",
    "EC-02": "sshsig-test-result-signed-ok",
    "EC-03": "cosign-bundle-exists",
    "EC-04": "agent-request-signed",
    "EC-05": "human-approval-allow-with-sod",
    "EC-06": "run-event-journal-exists",
    "EC-07": "governance-inventory-exists",
    "EC-08": "commit-trailer-receipts",
    "EC-09": "guard-audit-allow",
    "EC-10": "outcome-record-exists",
    "EC-11": "verify-archive-index-exists",
    "EC-12": "evidence-package-manifest",
}


class ControlCrosswalkError(ValueError):
    """Raised when a crosswalk or evaluation invariant is violated."""


def load_crosswalk() -> dict[str, Any]:
    """Load the bundled control-crosswalk template."""
    text = importlib_resources.files(__package__).joinpath("templates/control-crosswalk.json").read_text()
    return json.loads(text)


_ASSESSMENT_BYTE_BUDGET = 64 * 1024 * 1024


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

    def __init__(self, target: Path):
        self.target = target
        self.directories: dict[tuple[str, ...], int] = {(): dirfd.open_directory_nofollow(target, writable=False)}
        self.errors: dict[Path, str] = {}
        self.contents: dict[Path, bytes] = {}
        self.used = 0
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.copied: set[Path] = set()
        self.populations: dict[Path, list[str]] = {}
        self.approval_context: approval.ApprovalVerificationContext | None = None

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
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
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
            permitted = min(limit, _ASSESSMENT_BYTE_BUDGET - self.used)
            if info.st_size > permitted:
                raise _ReadRefusal("read_limit_exceeded")
            raw = bytearray()
            while True:
                chunk = os.read(descriptor, min(65536, permitted + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > permitted:
                    raise _ReadRefusal("read_limit_exceeded")
            result = bytes(raw)
            self.contents[path] = result
            self.used += len(result)
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


@dataclass(frozen=True)
class _Context:
    target: Path
    run_id: str | None
    period: tuple[datetime, datetime] | None
    reader: _AssessmentReader


@dataclass
class _Discovery:
    observations: list[control_readiness.ArtifactObservation] = field(default_factory=list)
    truncated: bool = False
    not_applicable_reason: str | None = None
    verifier_unavailable: bool = False
    window: dict[str, Any] | None = None


# Adapter seam for dedicated verifiers that are not wired yet (#1618).  An
# adapter takes ``(target, artifact_path)`` (EC-12 passes the package
# directory) and returns an ArtifactObservation with level, proposed outcome,
# dimensions and reasons.  The result is checked against the fixed readiness
# vocabulary; anything else is reported as ``verifier_error``.  The evaluator
# still applies relpath, run binding and period.  ``None`` means not wired: the evaluator stops
# at structural observation and reports ``verifier_not_wired``.
VerifierAdapter = Callable[[Path, Path], control_readiness.ArtifactObservation]
_VERIFIER_ADAPTERS: dict[str, VerifierAdapter | None] = {
    "cosign-bundle": None,
    "evidence-package": None,
}

_MAX = control_readiness.MAX_DISCOVERED_PER_CLAIM
_obs = control_readiness.ArtifactObservation
_dims = control_readiness.dims


def _relpath(ctx: _Context, path: Path) -> str:
    try:
        return path.relative_to(ctx.target).as_posix()
    except ValueError:
        return path.name


def _path_refusal(ctx: _Context, path: Path, *, include_leaf: bool = False) -> str | None:
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


def _read_json_object(ctx: _Context, path: Path) -> dict[str, Any] | None:
    """Bounded, regular-file, handle-relative JSON read."""
    try:
        value = attestation_input.strict_json_loads(ctx.reader.read(path))
        return value if isinstance(value, dict) else None
    except (attestation_input.AttestationInputError, OSError):
        return None


def _is_symlink(ctx: _Context, path: Path) -> bool:
    try:
        info = ctx.reader.info(path)
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False
    except OSError as exc:
        ctx.reader.refusal(path, exc)
        return isinstance(exc, _ReadRefusal) and exc.reason == "symlink_refused"


def _is_regular(ctx: _Context, path: Path) -> bool:
    try:
        return stat.S_ISREG(ctx.reader.info(path).st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        ctx.reader.refusal(path, exc)
        return False


def _read_journal(ctx: _Context, path: Path) -> run_journal.JournalReport:
    from .run_checkpoint import MAX_JOURNAL_BYTES

    try:
        snapshot = ctx.reader.copy(path, limit=MAX_JOURNAL_BYTES)
    except _ReadRefusal as exc:
        if exc.reason == "read_limit_exceeded":
            raise run_journal.RunJournalError("bound exceeded: journal byte budget") from exc
        raise
    return run_journal.read_journal_bounded(snapshot)


def _copy_public_trust(ctx: _Context) -> Path:
    root = ctx.target / ".brigade" / "attestation"
    for name in (attestation.DEFAULT_ALLOWED_SIGNERS_NAME, attestation.DEFAULT_REVOKED_KEYS_NAME):
        ctx.reader.copy(root / name)
    return ctx.reader.snapshot_target


def _verify_attestation_snapshot(ctx: _Context, path: Path, **kwargs: Any) -> attestation.AttestationVerifyResult:
    target = _copy_public_trust(ctx)
    envelope = ctx.reader.copy(path)
    ctx.reader.copy(path.parent / "receipt.json")
    return attestation.verify_attestation(envelope, target=target, **kwargs)


def _copy_population(ctx: _Context, root: Path, names: tuple[str, ...] | None = None) -> None:
    children, truncated, error = _bounded_children(ctx, root)
    if truncated or error is not None:
        reason = "read_limit_exceeded" if truncated else error or "discovery_unreadable"
        ctx.reader.errors[root] = reason
        raise _ReadRefusal(reason)
    for child in children:
        if names is None:
            if child.name.endswith(".json"):
                ctx.reader.copy(child)
                if _read_json_object(ctx, child) is None:
                    ctx.reader.errors[child] = "invalid_json"
                    raise _ReadRefusal("invalid_json")
        elif stat.S_ISDIR(ctx.reader.info(child).st_mode):
            # Bind the directory before copying even absent children. A linked
            # population member is a refusal, never silently omitted.
            ctx.reader.directory(child)
            for name in names:
                dependency = child / name
                ctx.reader.copy(dependency)
                if (
                    dependency in ctx.reader.copied
                    and name.endswith(".json")
                    and _read_json_object(ctx, dependency) is None
                ):
                    ctx.reader.errors[dependency] = "invalid_json"
                    raise _ReadRefusal("invalid_json")
        elif _is_symlink(ctx, child):
            ctx.reader.errors[child] = "symlink_refused"
            raise _ReadRefusal("symlink_refused")


def _approval_live_tree(ctx: _Context, key_path: Path) -> str | None:
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
            return _git(ctx, *args, env=env)

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


def _verify_approval_snapshot(ctx: _Context, run_dir: Path) -> approval.ApprovalVerification:
    from .run_checkpoint import MAX_JOURNAL_BYTES

    target = _copy_public_trust(ctx)
    ctx.reader.copy(run_dir / "run.json")
    ctx.reader.copy(run_dir / "events" / "lifecycle.jsonl", limit=MAX_JOURNAL_BYTES)
    for name in ("approvals", "requests"):
        _copy_population(ctx, run_dir / name)
    for name in ("request.json", "agent-request.json"):
        ctx.reader.copy(run_dir / name)
    _copy_population(
        ctx, ctx.target / ".brigade/work/verify-runs", ("receipt.json", "attestation.json", "changes.patch")
    )
    # Compute the live workspace fact at its original location. Never infer
    # freshness or SoD identity from the private dependency snapshot.
    key_path = attestation.resolve_signing_key_path(ctx.target)
    public_key = Path(str(key_path) + ".pub")
    try:
        public_key.relative_to(ctx.target)
    except ValueError as exc:
        raise _ReadRefusal("discovery_unreadable") from exc
    live_tree = (
        ctx.reader.approval_context.live_tree
        if ctx.reader.approval_context is not None
        else _approval_live_tree(ctx, key_path)
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
        live_tree=live_tree,
        live_tree_state="computed" if live_tree is not None else "unavailable",
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


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _first_timestamp(payload: dict[str, Any] | None, *keys: str) -> datetime | None:
    if payload is None:
        return None
    for key in keys:
        parsed = _parse_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _bounded_children(ctx: _Context, root: Path) -> tuple[list[Path], bool, str | None]:
    """Capped directory children as ``(children, truncated, discovery_error)``.

    At most ``_MAX + 1`` names are read, in filesystem order, so the scan stays
    bounded however large the directory is.  The kept names are sorted.  When
    the cap is exceeded the population is truncated and the inspected sample
    depends on filesystem order, so it never validates.  A missing root is an
    empty population; a symlinked or unreadable root is a discovery error, not
    an absence.
    """
    try:
        descriptor = ctx.reader.directory(root)
        if root not in ctx.reader.populations:
            ctx.reader.populations[root] = dirfd.child_names(descriptor, _MAX + 1)
        names = ctx.reader.populations[root]
    except FileNotFoundError:
        return [], False, None
    except OSError as exc:
        return [], False, ctx.reader.refusal(root, exc)
    return [root / name for name in sorted(names)[:_MAX]], len(names) > _MAX, None


def _claim_wide(obs: control_readiness.ArtifactObservation, ctx: _Context) -> None:
    """Scope a whole-claim sentinel (missing tool, verifier or discovery failure).

    Such an observation describes the claim's capability, not one artifact, so
    it is never treated as an unbound artifact under a run scope.
    """
    obs.scope = "in_scope"
    obs.run_binding = None
    obs.timestamp = None
    if obs.proposed == "unavailable":
        reason = obs.reasons[0] if obs.reasons else "verifier_error"
        obs.dimensions["population"] = {"status": "unavailable", "reason": reason}
    _apply_period(obs, ctx)


def _record_discovery_error(out: _Discovery, ctx: _Context, root: Path, error: str | None) -> None:
    if error is None:
        return
    relpath = _relpath(ctx, root)
    if error == "symlink_refused":
        obs = _obs(relpath, "none", "invalid", _dims(population=("unknown", error)), [error])
    else:
        obs = _obs(relpath, "none", "unavailable", _dims(population=("unavailable", error)), [error])
    _claim_wide(obs, ctx)
    out.observations.append(obs)


def _discovery_path_present(ctx: _Context, out: _Discovery, path: Path, *, directory: bool = False) -> bool:
    """Only a missing artifact is absent; denied metadata leaves population unknown."""
    refusal = _path_refusal(ctx, path)
    if refusal is not None:
        _record_discovery_error(out, ctx, path, refusal)
        return False
    try:
        mode = ctx.reader.info(path).st_mode
    except FileNotFoundError:
        return False
    except OSError:
        _record_discovery_error(out, ctx, path, "discovery_unreadable")
        return False
    if directory:
        if stat.S_ISLNK(mode):
            _record_discovery_error(out, ctx, path, "symlink_refused")
            return False
        return stat.S_ISDIR(mode)
    return True


def _adapter_observation(
    adapter: VerifierAdapter, ctx: _Context, artifact: Path, relpath: str
) -> control_readiness.ArtifactObservation:
    """Run a verifier adapter and hold its result to the readiness vocabulary."""
    try:
        snapshot_artifact = ctx.reader.snapshot_target / artifact.relative_to(ctx.target)
        if stat.S_ISDIR(ctx.reader.info(artifact).st_mode):
            manifest = _read_json_object(ctx, artifact / "manifest.json")
            ctx.reader.copy(artifact / "manifest.json")
            if manifest is not None:
                for entry in manifest.get("entries", []):
                    name = entry.get("path") if isinstance(entry, dict) else None
                    if not isinstance(name, str) or not evidence_package._is_safe_entry_path(name):
                        raise _ReadRefusal("discovery_unreadable")
                    ctx.reader.copy(artifact / name)
        else:
            snapshot_artifact = ctx.reader.copy(artifact)
            ctx.reader.copy(artifact.parent / "receipt.json")
        _copy_public_trust(ctx)
        obs = control_readiness.validate_observation(adapter(ctx.reader.snapshot_target, snapshot_artifact))
    except Exception:
        return _verifier_unavailable(relpath, "verifier_error")
    obs.relpath = relpath
    return obs


def _bind(obs: control_readiness.ArtifactObservation, ctx: _Context, candidates: dict[str, object]) -> None:
    """Set scope from explicit run identities recorded by the artifact.

    ``candidates`` maps a field name to its recorded value.  Without a recorded
    identity the artifact is ``unbound`` under a run scope; it is never assumed
    to belong to the requested run.
    """
    present = {name: value for name, value in candidates.items() if isinstance(value, str) and value}
    if ctx.run_id is None:
        obs.scope = "in_scope"
        obs.run_binding = next(iter(sorted(present)), None)
        return
    for name in sorted(present):
        if present[name] == ctx.run_id:
            obs.scope = "in_scope"
            obs.run_binding = name
            return
    obs.scope = "wrong_run" if present else "unbound"
    obs.run_binding = None


def _apply_period(obs: control_readiness.ArtifactObservation, ctx: _Context) -> None:
    if ctx.period is None:
        obs.dimensions["freshness"] = {"status": "not_applicable", "reason": "period_not_supplied"}
        return
    if obs.timestamp is None:
        obs.dimensions["freshness"] = {"status": "unknown", "reason": "timestamp_missing"}
        return
    start, end = ctx.period
    if start <= obs.timestamp <= end:
        obs.dimensions["freshness"] = {"status": "passed", "reason": None}
        return
    obs.dimensions["freshness"] = {"status": "failed", "reason": None}
    if obs.scope == "in_scope":
        obs.scope = "out_of_period"


def _workspace_subject(ctx: _Context) -> str:
    return "not_applicable" if ctx.run_id is None else "unknown"


def _invalid(ctx: _Context, path: Path, reason: str, **dim_overrides: Any) -> control_readiness.ArtifactObservation:
    return _obs(_relpath(ctx, path), "discovered", "invalid", _dims(**dim_overrides), [reason])


def _verify_receipt_children(ctx: _Context, filename: str) -> tuple[list[tuple[Path, Path]], _Discovery]:
    """Bounded verify-run scan.

    Verify receipts bind to a producer run through recorded fields, so a run
    scope still needs the scan; a truncated scan keeps the claim incomplete.
    """
    root = ctx.target / ".brigade" / "work" / "verify-runs"
    children, truncated, error = _bounded_children(ctx, root)
    out = _Discovery(truncated=truncated)
    _record_discovery_error(out, ctx, root, error)
    found = [
        (child, child / filename)
        for child in children
        if _discovery_path_present(ctx, out, child, directory=True)
        and _discovery_path_present(ctx, out, child / filename)
    ]
    return found, out


def _receipt_binding(receipt: dict[str, Any] | None) -> dict[str, object]:
    if receipt is None:
        return {}
    return {"producer_run_id": receipt.get("producer_run_id"), "run_id": receipt.get("run_id")}


def _evaluate_verify_receipt_completed(ctx: _Context) -> _Discovery:
    """EC-01: completed verify receipt whose non-empty command list all exited 0."""
    found, out = _verify_receipt_children(ctx, "receipt.json")
    for run_dir, path in found:
        receipt = None if (_is_symlink(ctx, run_dir) or _is_symlink(ctx, path)) else _read_json_object(ctx, path)
        if _is_symlink(ctx, run_dir) or _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        elif receipt is None:
            obs = _invalid(ctx, path, "invalid_json")
        else:
            obs = _assess_verify_receipt(ctx, run_dir, path, receipt)
        # An unreadable receipt records no run identity, so it stays unbound.
        _bind(obs, ctx, _receipt_binding(receipt))
        obs.timestamp = _first_timestamp(receipt, "completed_at", "started_at")
        _apply_period(obs, ctx)
        out.observations.append(obs)
    return out


def _assess_verify_receipt(
    ctx: _Context, run_dir: Path, path: Path, receipt: dict[str, Any]
) -> control_readiness.ArtifactObservation:
    """EC-01 structure of one verify receipt.

    The stored digest and optional local HMAC are not verified here (the
    integrity adapter is #1618), so integrity is ``not_checked`` and even a
    well-formed successful receipt stops at ``structure_observed``.
    """
    relpath = _relpath(ctx, path)
    if not isinstance(receipt.get("schema_version"), int):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    subject: str | tuple[str, str] = (
        "passed" if receipt.get("run_id") == run_dir.name else ("failed", "run_binding_mismatch")
    )
    integrity = ("not_checked", "verifier_not_wired")

    def observed(
        proposed: str, population: Any, reasons: list[str] | None = None
    ) -> control_readiness.ArtifactObservation:
        return _obs(
            relpath,
            "structure_observed",
            proposed,
            _dims(integrity=integrity, subject=subject, population=population),
            list(reasons or []),
        )

    status = receipt.get("status")
    if not isinstance(status, str):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    commands = receipt.get("commands")
    # Producer receipt statuses: running (nonterminal) and the terminal
    # completed, failed, rejected (a command was refused) and canceled.
    if status in {"failed", "rejected"}:
        return observed("failed", "passed", ["status_failed" if status == "failed" else "status_rejected"])
    if status == "canceled":
        return observed("incomplete", ("unknown", "status_canceled"))
    if status != "completed":
        return observed("incomplete", ("unknown", "status_not_terminal"))
    if not isinstance(commands, list):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    if not commands:
        # An empty intended check list cannot establish that any test ran.
        return observed("incomplete", ("unknown", "empty_commands"))
    if not all(isinstance(c, dict) and type(c.get("exit_code")) is int for c in commands):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    if any(c["exit_code"] != 0 for c in commands):
        return observed("failed", "passed", ["exit_code_nonzero"])
    planned = receipt.get("planned_commands")
    if not isinstance(planned, list) or not planned or not all(isinstance(p, str) and p for p in planned):
        # The intended check list must be recorded and non-empty.
        return observed("incomplete", ("unknown", "planned_commands_missing"))
    # The producer records each command with the same display string as its
    # planned entry, in order.  A different, missing or reordered command is
    # not the planned check.
    if len(planned) != len(commands) or any(c.get("command") != p for c, p in zip(commands, planned, strict=True)):
        return observed("incomplete", ("unknown", "planned_commands_mismatch"))
    if not all(c.get("status") == "completed" for c in commands):
        return observed("incomplete", ("unknown", "command_not_terminal"))
    return observed("structure_observed", "passed", ["verifier_not_wired"])


def _attestation_observation(
    relpath: str,
    result: attestation.AttestationVerifyResult,
    *,
    subject_ok: bool,
) -> control_readiness.ArtifactObservation:
    status = result.status
    level = "claim_validated"
    if status == attestation.STATUS_SIGNED_OK:
        if subject_ok:
            return _obs(
                relpath,
                level,
                "validated",
                _dims(
                    integrity="passed",
                    signature="passed",
                    authorization="passed",
                    subject="passed",
                    population="not_applicable",
                ),
            )
        return _obs(
            relpath,
            level,
            "rejected",
            _dims(
                integrity="passed",
                signature="passed",
                authorization="passed",
                subject=("failed", "subject_mismatch"),
                population="not_applicable",
            ),
        )
    failures: dict[str, Any] = {
        attestation.STATUS_SIGNATURE_MISMATCH: {"signature": ("failed", "signature_mismatch")},
        attestation.STATUS_UNVERIFIABLE_SIGNATURE: {"signature": ("failed", "signature_unverifiable")},
        attestation.STATUS_UNTRUSTED_KEY: {"authorization": ("failed", "untrusted_key"), "signature": "unknown"},
        attestation.STATUS_SUBJECT_MISMATCH: {"subject": ("failed", "subject_mismatch")},
        attestation.STATUS_EVIDENCE_MISSING: {"subject": ("failed", "evidence_missing")},
    }
    if status in failures:
        dim_values: dict[str, Any] = {
            "integrity": "unknown",
            "signature": "unknown",
            "authorization": "unknown",
            "subject": "unknown",
            "population": "not_applicable",
        }
        dim_values.update(failures[status])
        return _obs(relpath, level, "rejected", _dims(**dim_values))
    return _verifier_unavailable(relpath, "verifier_error")


def _verifier_unavailable(relpath: str, reason: str) -> control_readiness.ArtifactObservation:
    return _obs(
        relpath,
        "discovered",
        "unavailable",
        _dims(
            integrity=("unavailable", reason),
            signature=("unavailable", reason),
            authorization=("unavailable", reason),
            subject="unknown",
            population="not_applicable",
        ),
        [reason],
    )


def _ssh_keygen_available() -> bool:
    return shutil.which("ssh-keygen") is not None


def _evaluate_sshsig_test_result_signed_ok(ctx: _Context) -> _Discovery:
    """EC-02: signed SSHSIG Test Result attestation with rederived receipt."""
    found, out = _verify_receipt_children(ctx, "attestation.json")
    for run_dir, path in found:
        receipt = _read_json_object(ctx, run_dir / "receipt.json")
        relpath = _relpath(ctx, path)
        if _is_symlink(ctx, run_dir) or _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        elif not _ssh_keygen_available():
            obs = _verifier_unavailable(relpath, "verifier_tool_unavailable")
            out.verifier_unavailable = True
        else:
            try:
                # Pin re-derivation to this directory's own receipt.
                result = _verify_attestation_snapshot(ctx, path, require_receipt=True, receipt=receipt)
            except Exception:
                obs = _verifier_unavailable(relpath, "verifier_error")
            else:
                # A valid attestation copied from another run directory is not
                # evidence for this one.
                obs = _attestation_observation(
                    relpath, result, subject_ok=result.rederived and result.run_id == run_dir.name
                )
        # An unreadable receipt leaves the attestation unbound; it is never
        # assumed to belong to the requested run.
        _bind(obs, ctx, _receipt_binding(receipt))
        obs.timestamp = _first_timestamp(receipt, "completed_at", "started_at")
        _apply_period(obs, ctx)
        out.observations.append(obs)
    return out


def _cosign_bundle_shape_ok(bundle: dict[str, Any]) -> bool:
    envelope = bundle.get("dsseEnvelope")
    if not isinstance(envelope, dict):
        return False
    signatures = envelope.get("signatures")
    return (
        isinstance(envelope.get("payload"), str)
        and bool(envelope.get("payload"))
        and isinstance(envelope.get("payloadType"), str)
        and isinstance(signatures, list)
        and bool(signatures)
        and all(isinstance(s, dict) and isinstance(s.get("sig"), str) and s.get("sig") for s in signatures)
    )


def _evaluate_cosign_bundle_exists(ctx: _Context) -> _Discovery:
    """EC-03: cosign Sigstore bundle; structural only until a verifier adapter is wired."""
    found, out = _verify_receipt_children(ctx, "attestation.sigstore.json")
    adapter = _VERIFIER_ADAPTERS.get("cosign-bundle")
    for run_dir, path in found:
        receipt = _read_json_object(ctx, run_dir / "receipt.json")
        relpath = _relpath(ctx, path)
        bundle = None if (_is_symlink(ctx, run_dir) or _is_symlink(ctx, path)) else _read_json_object(ctx, path)
        if _is_symlink(ctx, run_dir) or _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        elif bundle is None:
            obs = _invalid(ctx, path, "invalid_json")
        elif bundle.get("mediaType") != cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE:
            obs = _invalid(ctx, path, "media_type_mismatch")
        elif not _cosign_bundle_shape_ok(bundle):
            obs = _invalid(ctx, path, "dsse_signatures_missing")
        elif adapter is not None:
            obs = _adapter_observation(adapter, ctx, path, relpath)
        else:
            not_wired = ("not_checked", "verifier_not_wired")
            obs = _obs(
                relpath,
                "structure_observed",
                "structure_observed",
                _dims(
                    integrity=not_wired,
                    signature=not_wired,
                    authorization=not_wired,
                    subject=not_wired,
                    population="not_applicable",
                ),
                ["verifier_not_wired"],
            )
        _bind(obs, ctx, _receipt_binding(receipt))
        obs.timestamp = _first_timestamp(receipt, "completed_at", "started_at")
        _apply_period(obs, ctx)
        out.observations.append(obs)
    return out


def _run_dirs(ctx: _Context) -> tuple[list[Path], _Discovery]:
    """Run directories to inspect.

    Run-directory artifacts are bound by directory name, so a run scope
    inspects the selected (already validated bare) run directory directly and
    never loses it behind the scan cap.  A workspace scope uses the bounded scan.
    """
    root = ctx.target / ".brigade" / "runs"
    out = _Discovery()
    refusal = _path_refusal(ctx, root)
    if refusal is not None:
        _record_discovery_error(out, ctx, root, refusal)
        return [], out
    try:
        mode = ctx.reader.info(root).st_mode
    except FileNotFoundError:
        return [], out
    except OSError:
        _record_discovery_error(out, ctx, root, "discovery_unreadable")
        return [], out
    if stat.S_ISLNK(mode):
        _record_discovery_error(out, ctx, root, "symlink_refused")
        return [], out
    if not stat.S_ISDIR(mode):
        _record_discovery_error(out, ctx, root, "discovery_unreadable")
        return [], out
    if ctx.run_id is not None:
        selected = root / ctx.run_id
        try:
            mode = ctx.reader.info(selected).st_mode
        except FileNotFoundError:
            return [], out
        except OSError:
            _record_discovery_error(out, ctx, selected, "discovery_unreadable")
            return [], out
        return ([selected] if stat.S_ISDIR(mode) or stat.S_ISLNK(mode) else []), out
    try:
        children, out.truncated, error = _bounded_children(ctx, root)
    except OSError:
        children, error = [], "discovery_unreadable"
    _record_discovery_error(out, ctx, root, error)
    directories = []
    for child in children:
        try:
            mode = ctx.reader.info(child).st_mode
        except OSError:
            _record_discovery_error(out, ctx, child, "discovery_unreadable")
            continue
        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            directories.append(child)
    return directories, out


def _run_scope(obs: control_readiness.ArtifactObservation, ctx: _Context, run_dir: Path) -> None:
    """Run-directory artifacts are bound by their run directory name."""
    _bind(obs, ctx, {"run_dir": run_dir.name})
    if _path_refusal(ctx, run_dir, include_leaf=True) is None:
        obs.timestamp = _first_timestamp(_read_json_object(ctx, run_dir / "run.json"), "started_at", "created_at")
    _apply_period(obs, ctx)


def _agent_request_paths(ctx: _Context, run_dir: Path, out: _Discovery) -> list[Path]:
    """Signed request envelopes recorded for one run directory.

    ``agent_request.record_request`` writes ``requests/<nonce>.json`` and a
    ``request.signed`` lifecycle event.  The legacy root names
    ``agent-request.json`` and ``request.json`` are still discovered.  The
    ``requests/`` scan is bounded; past the cap the population is truncated.
    """
    paths = [
        run_dir / name
        for name in ("agent-request.json", "request.json")
        if _discovery_path_present(ctx, out, run_dir / name)
    ]
    if _is_symlink(ctx, run_dir):
        return paths
    root = run_dir / "requests"
    children, truncated, error = _bounded_children(ctx, root)
    out.truncated = out.truncated or truncated
    _record_discovery_error(out, ctx, root, error)
    paths.extend(child for child in children if child.name.endswith(".json"))
    return paths


def _evaluate_agent_request_signed(ctx: _Context) -> _Discovery:
    """EC-04: signed agent-request envelopes verify and name their run.

    Signature, key trust and the statement's run are checked.  The ordering of
    the request relative to worker dispatch is not independently checked.
    """
    run_dirs, out = _run_dirs(ctx)
    for run_dir in run_dirs:
        if _is_symlink(ctx, run_dir):
            obs = _invalid(ctx, run_dir, "symlink_refused")
            _run_scope(obs, ctx, run_dir)
            out.observations.append(obs)
            continue
        for path in _agent_request_paths(ctx, run_dir, out):
            relpath = _relpath(ctx, path)
            if _is_symlink(ctx, run_dir) or _is_symlink(ctx, path):
                obs = _invalid(ctx, path, "symlink_refused")
            elif not _ssh_keygen_available():
                obs = _verifier_unavailable(relpath, "verifier_tool_unavailable")
                out.verifier_unavailable = True
            else:
                try:
                    result = _verify_attestation_snapshot(
                        ctx,
                        path,
                        expected_predicate_type=agent_request.AGENT_REQUEST_PREDICATE_TYPE,
                    )
                except Exception:
                    obs = _verifier_unavailable(relpath, "verifier_error")
                else:
                    obs = _attestation_observation(relpath, result, subject_ok=result.run_id == run_dir.name)
                    # Signature trust and run identity cannot establish that
                    # signing preceded dispatch. Keep those dimensions, but
                    # cap the claim until an ordering verifier is wired.
                    if obs.proposed == "validated":
                        obs.level = "structure_observed"
                        obs.proposed = "structure_observed"
                        obs.reasons.append("verifier_not_wired")
            _run_scope(obs, ctx, run_dir)
            out.observations.append(obs)
    return out


_APPROVAL_OUTCOMES: dict[str, tuple[str, dict[str, Any]]] = {
    "DENIED": ("failed", {"authorization": ("failed", "approval_denied")}),
    "HELD": ("incomplete", {"authorization": ("unknown", "approval_held")}),
    "APPROVAL-INVALID": ("rejected", {"integrity": ("failed", "approval_invalid")}),
    "APPROVAL-STALE": ("rejected", {"subject": ("failed", "approval_stale")}),
    "APPROVAL-EXPIRED": ("rejected", {"authorization": ("failed", "approval_expired")}),
    "SOD-VIOLATION": ("rejected", {"authorization": ("failed", "sod_failed")}),
    "SOD-INDETERMINATE": ("incomplete", {"authorization": ("unknown", "sod_indeterminate")}),
}


def _evaluate_human_approval_allow_with_sod(ctx: _Context) -> _Discovery:
    """EC-05: verified allow approval with segregation of duties passed."""
    run_dirs, out = _run_dirs(ctx)
    for run_dir in run_dirs:
        relpath = _relpath(ctx, run_dir / "events" / "lifecycle.jsonl")
        if _is_symlink(ctx, run_dir):
            obs = _invalid(ctx, run_dir, "symlink_refused")
            _run_scope(obs, ctx, run_dir)
            out.observations.append(obs)
            continue
        refused = False
        for path in (run_dir / "run.json", run_dir / "events" / "lifecycle.jsonl", run_dir / "approvals"):
            path_refusal = _path_refusal(ctx, path, include_leaf=True)
            if path_refusal is not None:
                _record_discovery_error(out, ctx, path, path_refusal)
                refused = True
                break
        if refused:
            continue
        try:
            verification = _verify_approval_snapshot(ctx, run_dir)
        except _ReadRefusal as exc:
            if exc.reason == "read_limit_exceeded":
                obs = _obs(
                    relpath,
                    "discovered",
                    "incomplete",
                    _dims(integrity="unknown", subject="unknown", population=("unknown", exc.reason)),
                    [exc.reason],
                )
            else:
                obs = _verifier_unavailable(relpath, "verifier_error")
        except Exception:
            obs = _verifier_unavailable(relpath, "verifier_error")
        else:
            unapproved = verification.status == "UNAPPROVED"
            refusal = (
                _approval_journal_refusal(ctx, run_dir, relpath, unapproved=unapproved)
                if verification.status in {"UNAPPROVED", "APPROVAL-INVALID"}
                else None
            )
            if unapproved and refusal is None:
                continue
            obs = refusal if refusal is not None else _approval_observation(relpath, verification)
            if refusal is None and obs.proposed in {"rejected", "invalid"} and not _ssh_keygen_available():
                # Without ssh-keygen the verifier reports APPROVAL-INVALID for
                # any signed approval.  A failure that needs no signature check
                # (journal chain, run binding or approval event shape) is still
                # known; only otherwise is the claim unavailable.
                known = _approval_prerequisite_failure(ctx, run_dir)
                if known is None:
                    obs = _verifier_unavailable(relpath, "verifier_tool_unavailable")
                    out.verifier_unavailable = True
                else:
                    dimension, reason = known
                    dim_values: dict[str, Any] = {
                        "integrity": "unknown",
                        "signature": ("unavailable", "verifier_tool_unavailable"),
                        "authorization": "unknown",
                        "subject": "unknown",
                        "population": "not_applicable",
                    }
                    dim_values[dimension] = ("failed", reason)
                    obs = _obs(relpath, "structure_observed", "rejected", _dims(**dim_values))
        _run_scope(obs, ctx, run_dir)
        out.observations.append(obs)
    return out


def _approval_journal_refusal(
    ctx: _Context, run_dir: Path, relpath: str, *, unapproved: bool = False
) -> control_readiness.ArtifactObservation | None:
    """Recover journal failures and read refusals hidden by approval status.

    UNAPPROVED means no approval was parsed, which can also happen when parsing
    stops at a malformed prefix or partial tail. Only a clean journal permits
    treating that status as absence. APPROVAL-INVALID uses the same status for
    malformed journals and read refusals; a bounded-read refusal cannot
    establish an integrity failure, regardless of signature-tool availability.
    """
    try:
        report = _read_journal(ctx, run_dir / "events" / "lifecycle.jsonl")
    except run_journal.RunJournalError as exc:
        if "bound exceeded" not in str(exc):
            if unapproved:
                return _obs(relpath, "discovered", "rejected", _dims(integrity=("failed", "journal_chain_error")))
            return None
    except OSError:
        return _verifier_unavailable(relpath, "verifier_error")
    else:
        if not any("bound exceeded" in error for error in report.chain_errors):
            if unapproved and (
                report.chain_errors
                or report.partial_tail is not None
                or any(event.run_id != run_dir.name for event in report.events)
            ):
                return _journal_observation(relpath, report, run_dir.name)
            return None
    return _obs(
        relpath,
        "discovered",
        "incomplete",
        _dims(integrity="unknown", subject="unknown", population=("unknown", "read_limit_exceeded")),
    )


def _approval_prerequisite_failure(ctx: _Context, run_dir: Path) -> tuple[str, str] | None:
    """A known approval failure that needs no signature verifier, or None.

    Uses the bounded journal reader and the approval event shape that
    ``approval.verify_run_approval`` checks before any signature: journal
    chain, run binding of every event, readable run metadata, and the latest approval event's nonce,
    attestation path and decodable statement.
    """
    try:
        report = _read_journal(ctx, run_dir / "events" / "lifecycle.jsonl")
    except run_journal.RunJournalError as exc:
        # A read bound is not a known failure; the claim stays unavailable.
        return None if "bound exceeded" in str(exc) else ("integrity", "journal_chain_error")
    except OSError:
        return None
    if any("bound exceeded" in error for error in report.chain_errors):
        return None
    if report.chain_errors:
        return "integrity", "journal_chain_error"
    if report.partial_tail is not None:
        return "integrity", "journal_partial_tail"
    if any(event.run_id != run_dir.name for event in report.events):
        return "subject", "run_binding_mismatch"
    event = approval._latest_approval_event(report.events)
    if event is None:
        return None
    if _read_json_object(ctx, run_dir / "run.json") is None:
        return "integrity", "approval_invalid"
    nonce = event.payload.get("nonce")
    if not isinstance(nonce, str) or not approval._HEX32_RE.fullmatch(nonce):
        return "integrity", "approval_invalid"
    if event.payload.get("attestation_path") != f"approvals/{nonce}.json":
        return "integrity", "approval_invalid"
    envelope = _read_json_object(ctx, run_dir / "approvals" / f"{nonce}.json")
    if envelope is None or approval._decode_statement(envelope)[0] is None:
        return "integrity", "approval_invalid"
    return None


def _approval_observation(
    relpath: str, verification: approval.ApprovalVerification
) -> control_readiness.ArtifactObservation:
    if verification.status == "APPROVED":
        sod = verification.sod or {}
        signed = {"integrity": "passed", "signature": "passed", "subject": "passed", "population": "not_applicable"}
        if sod.get("result") == "PASSED":
            return _obs(relpath, "claim_validated", "validated", _dims(authorization="passed", **signed))
        return _obs(relpath, "claim_validated", "failed", _dims(authorization=("failed", "sod_failed"), **signed))
    proposed, overrides = _APPROVAL_OUTCOMES.get(
        verification.status, ("unavailable", {"integrity": ("unavailable", "verifier_error")})
    )
    dim_values: dict[str, Any] = {
        "integrity": "unknown",
        "signature": "unknown",
        "authorization": "unknown",
        "subject": "unknown",
        "population": "not_applicable",
    }
    dim_values.update(overrides)
    return _obs(relpath, "claim_validated", proposed, _dims(**dim_values))


def _evaluate_run_event_journal_exists(ctx: _Context) -> _Discovery:
    """EC-06: run lifecycle journal with a complete, valid hash chain."""
    run_dirs, out = _run_dirs(ctx)
    for run_dir in run_dirs:
        path = run_dir / "events" / "lifecycle.jsonl"
        if not _discovery_path_present(ctx, out, path):
            continue
        relpath = _relpath(ctx, path)
        if _is_symlink(ctx, run_dir) or _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        else:
            try:
                report = _read_journal(ctx, path)
            except run_journal.RunJournalError as exc:
                if "bound exceeded" in str(exc):
                    obs = _obs(
                        relpath,
                        "discovered",
                        "incomplete",
                        _dims(integrity="unknown", subject="unknown", population=("unknown", "read_limit_exceeded")),
                    )
                else:
                    obs = _invalid(ctx, path, "invalid_json")
            except Exception:
                obs = _verifier_unavailable(relpath, "verifier_error")
            else:
                obs = _journal_observation(relpath, report, run_dir.name)
        _run_scope(obs, ctx, run_dir)
        out.observations.append(obs)
    return out


def _journal_observation(
    relpath: str, report: run_journal.JournalReport, run_dir_name: str
) -> control_readiness.ArtifactObservation:
    # Subject: every event must name the run directory that holds the journal.
    # An intact chain copied from another run is not evidence for this one.
    subject: str | tuple[str, str]
    if any(event.run_id != run_dir_name for event in report.events):
        subject = ("failed", "run_binding_mismatch")
    elif report.events and not report.chain_errors:
        subject = "passed"
    else:
        subject = "unknown"
    if report.chain_errors:
        if any("bound exceeded" in error for error in report.chain_errors):
            return _obs(
                relpath,
                "structure_observed",
                "incomplete",
                _dims(integrity="unknown", subject=subject, population=("unknown", "read_limit_exceeded")),
            )
        return _obs(
            relpath, "claim_validated", "rejected", _dims(integrity=("failed", "journal_chain_error"), subject=subject)
        )
    if report.partial_tail is not None:
        return _obs(
            relpath,
            "claim_validated",
            "incomplete",
            _dims(integrity="passed", subject=subject, population=("unknown", "journal_partial_tail")),
        )
    if not report.events:
        return _obs(
            relpath,
            "structure_observed",
            "incomplete",
            _dims(subject=subject, population=("unknown", "journal_empty")),
        )
    return _obs(
        relpath, "claim_validated", "validated", _dims(integrity="passed", subject=subject, population="passed")
    )


def _structural(
    ctx: _Context, relpath: str, proposed: str, reasons: list[str] | None = None, population: Any = "passed"
) -> control_readiness.ArtifactObservation:
    """Structure observed for a whole-workspace artifact with no integrity verifier."""
    return _obs(
        relpath,
        "structure_observed",
        proposed,
        _dims(
            integrity=("not_checked", "verifier_not_wired"),
            subject=_workspace_subject(ctx),
            population=population,
        ),
        list(reasons or []),
    )


def _workspace_scope(obs: control_readiness.ArtifactObservation, ctx: _Context, timestamp: datetime | None) -> None:
    """Whole-workspace artifacts record no run identity, so they stay unbound under a run scope."""
    _bind(obs, ctx, {})
    obs.timestamp = timestamp
    _apply_period(obs, ctx)


def _evaluate_governance_inventory_exists(ctx: _Context) -> _Discovery:
    """EC-07: governance inventory with the inventory schema."""
    from .governance_inventory import INVENTORY_SCHEMA

    out = _Discovery()
    for path in (
        ctx.target / ".brigade" / "governance" / "inventory.json",
        ctx.target / "governance-inventory.json",
    ):
        if not _discovery_path_present(ctx, out, path):
            continue
        payload = None if _is_symlink(ctx, path) else _read_json_object(ctx, path)
        if _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        elif payload is None:
            obs = _invalid(ctx, path, "invalid_json")
        elif payload.get("schema") != INVENTORY_SCHEMA:
            obs = _invalid(ctx, path, "schema_missing")
        else:
            obs = _structural(ctx, _relpath(ctx, path), "structure_observed")
        _workspace_scope(obs, ctx, _first_timestamp(payload, "generated_at", "created_at"))
        out.observations.append(obs)
    return out


_TRAILER_WINDOW = 20


class _GitReadLimitExceeded(ValueError):
    """Git output crossed the fixed capture or transport byte budget."""


def _git(ctx: _Context | Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Read both Git pipes under proc's fixed byte budgets and ten-second timeout."""
    argv = ["git", *args]
    bounded = proc.run(argv, cwd=ctx.target if isinstance(ctx, _Context) else ctx, env=env, timeout=10)
    if bounded.output_limit_exceeded or bounded.stream_limit_exceeded:
        raise _GitReadLimitExceeded
    # proc decodes UTF-8 with replacement, as the original Git calls did.
    return subprocess.CompletedProcess(argv, bounded.code, bounded.stdout, bounded.stderr)


def _git_read_refusal(ctx: _Context, relpath: str) -> control_readiness.ArtifactObservation:
    obs = _obs(
        relpath,
        "discovered",
        "incomplete",
        _dims(integrity="unknown", subject="unknown", population=("unknown", "read_limit_exceeded")),
        ["read_limit_exceeded"],
    )
    _claim_wide(obs, ctx)
    return obs


def _is_git_repo(target: Path) -> bool | None:
    """True for a repository, False for a reported non-repository, None for a probe error."""
    try:
        result = _git(
            target,
            "rev-parse",
            "--git-dir",
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode == 0:
            return True
        if result.returncode == 128 and result.stderr.startswith("fatal: not a git repository"):
            return False
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _evaluate_commit_trailer_receipts(ctx: _Context) -> _Discovery:
    """EC-08: commits in the most recent ``_TRAILER_WINDOW`` whose Brigade-Receipt trailer matches run.json.

    The window is the claim's defined population, not a truncated scan; older
    history is outside the claim and is reported through ``population.window``.
    Missing ``git`` and whole-history verifier errors are claim-wide sentinels.
    """
    out = _Discovery()
    if shutil.which("git") is None:
        out.verifier_unavailable = True
        obs = _verifier_unavailable("git-history", "verifier_tool_unavailable")
        _claim_wide(obs, ctx)
        out.observations.append(obs)
        return out
    try:
        repo_status = _is_git_repo(ctx.target)
    except _GitReadLimitExceeded:
        out.observations.append(_git_read_refusal(ctx, "git-history"))
        return out
    if repo_status is False:
        out.not_applicable_reason = "not_git_repository"
        return out
    if repo_status is None:
        obs = _verifier_unavailable("git-history", "verifier_error")
        _claim_wide(obs, ctx)
        out.observations.append(obs)
        return out
    try:
        result = _git(ctx, "log", f"-{_TRAILER_WINDOW}", "--format=%H")
    except _GitReadLimitExceeded:
        out.observations.append(_git_read_refusal(ctx, "git-history"))
        return out
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0:
        obs = _verifier_unavailable("git-history", "verifier_error")
        _claim_wide(obs, ctx)
        out.observations.append(obs)
        return out
    commits = [sha for sha in result.stdout.splitlines() if sha][:_TRAILER_WINDOW]
    # Only trailered commits are claim artifacts.  The counts keep one valid
    # trailer from implying that every commit in the window is linked.
    window: dict[str, Any] = {
        "kind": "most_recent_commits",
        "limit": _TRAILER_WINDOW,
        "inspected": len(commits),
        "with_trailer": 0,
        "without_trailer": 0,
        "unreadable": 0,
    }
    out.window = window
    for sha in commits:
        relpath = f"git-commit:{sha[:12]}"
        try:
            msg_out = _git(ctx, "log", "-1", "--format=%B%x00%cI", sha)
        except _GitReadLimitExceeded:
            window["unreadable"] += 1
            out.observations.append(_git_read_refusal(ctx, relpath))
            continue
        except (OSError, subprocess.TimeoutExpired):
            msg_out = None
        if msg_out is None or msg_out.returncode != 0:
            # The commit's trailer is unknown, so it cannot be bound to any run.
            window["unreadable"] += 1
            obs = _verifier_unavailable(relpath, "verifier_error")
            _claim_wide(obs, ctx)
            out.observations.append(obs)
            continue
        message, _, committed = msg_out.stdout.partition("\x00")
        run_id_value: str | None = None
        expected_digest: str | None = None
        for line in message.splitlines():
            if line.startswith("Brigade-Run: "):
                run_id_value = line[len("Brigade-Run: ") :].strip()
            elif line.startswith("Brigade-Receipt: sha256:"):
                expected_digest = line[len("Brigade-Receipt: sha256:") :].strip()
        if not run_id_value or not expected_digest:
            window["without_trailer"] += 1
            continue
        window["with_trailer"] += 1
        obs = _assess_trailer(ctx, relpath, run_id_value, expected_digest)
        _bind(obs, ctx, {"brigade_run_trailer": run_id_value})
        obs.timestamp = _parse_timestamp(committed.strip())
        _apply_period(obs, ctx)
        out.observations.append(obs)
    return out


def _assess_trailer(
    ctx: _Context, relpath: str, run_id_value: str, expected_digest: str
) -> control_readiness.ArtifactObservation:
    if not receipts_trailer._is_bare_run_id(run_id_value):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    run_dir = ctx.target / ".brigade" / "runs" / run_id_value
    run_json = run_dir / "run.json"
    refusal = _path_refusal(ctx, run_json)
    if refusal == "symlink_refused":
        return _obs(
            relpath,
            "discovered",
            "invalid",
            _dims(integrity=("unknown", refusal), subject="unknown", population=("unknown", refusal)),
            [refusal],
        )
    if refusal is not None:
        return _verifier_unavailable(relpath, refusal)
    try:
        run_mode = ctx.reader.info(run_dir).st_mode
    except FileNotFoundError:
        run_mode = 0
    except OSError:
        return _verifier_unavailable(relpath, "discovery_unreadable")
    try:
        receipt_mode = ctx.reader.info(run_json).st_mode
    except FileNotFoundError:
        receipt_mode = 0
    except OSError:
        return _verifier_unavailable(relpath, "discovery_unreadable")
    if stat.S_ISLNK(run_mode) or stat.S_ISLNK(receipt_mode):
        return _obs(
            relpath,
            "discovered",
            "invalid",
            _dims(integrity=("unknown", "symlink_refused"), subject="unknown", population="not_applicable"),
            ["symlink_refused"],
        )
    if not stat.S_ISREG(receipt_mode):
        # No local receipt to compare: the link is unverified, not a digest failure.
        return _obs(
            relpath,
            "discovered",
            "incomplete",
            _dims(integrity=("unknown", "entry_missing"), subject="unknown", population="not_applicable"),
        )
    receipt = _read_json_object(ctx, run_json)
    if receipt is None:
        return _obs(relpath, "structure_observed", "invalid", _dims(), ["invalid_json"])
    recorded_run = receipt.get("run_id")
    identity_valid = isinstance(recorded_run, str) and receipts_trailer._is_bare_run_id(recorded_run)
    subject: str | tuple[str, str] = (
        ("unknown", "schema_missing")
        if not identity_valid
        else "passed"
        if recorded_run == run_dir.name == run_id_value
        else ("failed", "run_binding_mismatch")
    )
    try:
        actual_digest = causal_receipt.receipt_digest(receipt)
    except Exception:
        return _verifier_unavailable(relpath, "verifier_error")
    if actual_digest != expected_digest:
        return _obs(
            relpath,
            "claim_validated",
            "rejected",
            _dims(integrity=("failed", "trailer_digest_mismatch"), subject=subject, population="not_applicable"),
        )
    if not identity_valid:
        return _obs(
            relpath,
            "structure_observed",
            "invalid",
            _dims(integrity="passed", subject=subject, population="not_applicable"),
        )
    if subject != "passed":
        return _obs(
            relpath,
            "claim_validated",
            "rejected",
            _dims(integrity="passed", subject=subject, population="not_applicable"),
        )
    return _obs(
        relpath,
        "claim_validated",
        "validated",
        _dims(integrity="passed", subject="passed", population="not_applicable"),
    )


def _evaluate_guard_audit_allow(ctx: _Context) -> _Discovery:
    """EC-09: content-guard audit with an explicit, unblocked verdict (structural only)."""
    out = _Discovery()
    path = ctx.target / ".brigade" / "work" / "guard" / "audit.json"
    if not _discovery_path_present(ctx, out, path):
        return out
    payload = None if _is_symlink(ctx, path) else _read_json_object(ctx, path)
    relpath = _relpath(ctx, path)
    if _is_symlink(ctx, path):
        obs = _invalid(ctx, path, "symlink_refused")
    elif payload is None:
        obs = _invalid(ctx, path, "invalid_json")
    elif not isinstance(payload.get("summary"), dict):
        # A malformed artifact never implies the control operated.
        obs = _invalid(ctx, path, "schema_missing")
    else:
        blocked = payload["summary"].get("blocked")
        if blocked is True:
            obs = _structural(ctx, relpath, "failed", ["guard_blocked"])
        elif blocked is False:
            obs = _structural(ctx, relpath, "structure_observed")
        else:
            obs = _structural(ctx, relpath, "incomplete", ["no_explicit_verdict"], ("unknown", "no_explicit_verdict"))
    _workspace_scope(obs, ctx, _first_timestamp(payload, "generated_at", "created_at"))
    out.observations.append(obs)
    return out


_MAX_JSONL_RECORDS = 10_000


def _read_jsonl_records(ctx: _Context, path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
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
        if len(records) >= _MAX_JSONL_RECORDS:
            return None, "read_limit_exceeded"
        try:
            value = attestation_input.strict_json_loads(line)
        except attestation_input.AttestationInputError:
            return None, "invalid_jsonl_line"
        if not isinstance(value, dict):
            return None, "invalid_jsonl_line"
        records.append(value)
    return records, None


def _evaluate_jsonl_ledger(
    ctx: _Context, path: Path, valid_record: Callable[[dict[str, Any]], bool], timestamp_keys: tuple[str, ...]
) -> _Discovery:
    out = _Discovery()
    if not _discovery_path_present(ctx, out, path):
        return out
    relpath = _relpath(ctx, path)
    timestamp: datetime | None = None
    if _is_symlink(ctx, path):
        obs = _invalid(ctx, path, "symlink_refused")
    else:
        records, reason = _read_jsonl_records(ctx, path)
        if records is None and reason == "read_limit_exceeded":
            obs = _obs(
                relpath,
                "discovered",
                "incomplete",
                _dims(subject=_workspace_subject(ctx), population=("unknown", "read_limit_exceeded")),
            )
        elif records is None:
            obs = _invalid(ctx, path, reason or "invalid_json")
        elif not records:
            obs = _structural(ctx, relpath, "incomplete", ["no_records"], ("unknown", "no_records"))
        elif not all(valid_record(record) for record in records):
            obs = _invalid(ctx, path, "schema_missing")
        else:
            obs = _structural(ctx, relpath, "structure_observed")
            stamps = [_first_timestamp(record, *timestamp_keys) for record in records]
            # Every record needs a timestamp for the newest one to speak for the ledger.
            timestamp = max(stamps) if stamps and all(s is not None for s in stamps) else None  # type: ignore[type-var]
    _workspace_scope(obs, ctx, timestamp)
    out.observations.append(obs)
    return out


def _evaluate_outcome_record_exists(ctx: _Context) -> _Discovery:
    """EC-10: outcome ledger whose every line is a versioned JSON record (structural only)."""
    return _evaluate_jsonl_ledger(
        ctx,
        ctx.target / "memory" / "outcome" / "records.jsonl",
        lambda record: type(record.get("schema_version")) is int,
        # OutcomeRecord writes ``ts``; the others are tolerated legacy keys.
        ("ts", "recorded_at", "created_at", "timestamp"),
    )


def _evaluate_verify_archive_index_exists(ctx: _Context) -> _Discovery:
    """EC-11: verify archive index whose every line names an archived run (structural only)."""
    return _evaluate_jsonl_ledger(
        ctx,
        ctx.target / ".brigade" / "work" / "verify-archive" / "index.jsonl",
        lambda record: isinstance(record.get("run_id"), str) and bool(record.get("run_id")),
        ("archived_at",),
    )


def _evaluate_evidence_package_manifest(ctx: _Context) -> _Discovery:
    """EC-12: evidence package manifests staged under ``.brigade/evidence-packages/``.

    `brigade receipts export package --out` accepts any path, so discovery covers two
    bounded staging locations: ``.brigade/evidence-packages/manifest.json``
    (the root itself was the output) and ``<package>/manifest.json`` one level
    down.  Deeper nesting is not discovered.  Without the strict package
    verifier adapter (#1618) this checks the manifest schema, the recomputed
    ``entries_sha256`` and that each listed entry is a safe, present regular
    file.  Entry contents are not rehashed, so integrity stays ``not_checked``
    and the claim cannot validate.
    """
    root = ctx.target / ".brigade" / "evidence-packages"
    children, truncated, error = _bounded_children(ctx, root)
    out = _Discovery(truncated=truncated)
    _record_discovery_error(out, ctx, root, error)
    adapter = _VERIFIER_ADAPTERS.get("evidence-package")
    package_dirs = ([root] if error is None and _discovery_path_present(ctx, out, root / "manifest.json") else []) + [
        child for child in children if _discovery_path_present(ctx, out, child, directory=True)
    ]
    for package_dir in package_dirs:
        path = package_dir / "manifest.json"
        if not _discovery_path_present(ctx, out, path):
            continue
        relpath = _relpath(ctx, path)
        manifest = None if (_is_symlink(ctx, package_dir) or _is_symlink(ctx, path)) else _read_json_object(ctx, path)
        if _is_symlink(ctx, package_dir) or _is_symlink(ctx, path):
            obs = _invalid(ctx, path, "symlink_refused")
        elif manifest is None:
            obs = _invalid(ctx, path, "invalid_json")
        elif adapter is not None:
            obs = _adapter_observation(adapter, ctx, package_dir, relpath)
        else:
            obs = _assess_package_manifest(ctx, package_dir, relpath, manifest)
        source = manifest.get("source") if isinstance(manifest, dict) else None
        _bind(
            obs,
            ctx,
            {
                "source.producer_run_id": source.get("producer_run_id"),
                "source.verify_run_id": source.get("verify_run_id"),
            }
            if isinstance(source, dict)
            else {},
        )
        obs.timestamp = _first_timestamp(manifest, "created_at")
        _apply_period(obs, ctx)
        out.observations.append(obs)
    return out


def _assess_package_manifest(
    ctx: _Context, package_dir: Path, relpath: str, manifest: dict[str, Any]
) -> control_readiness.ArtifactObservation:
    entries = manifest.get("entries")
    if manifest.get("schema") != evidence_package.SCHEMA or not isinstance(entries, list):
        return _obs(relpath, "discovered", "invalid", _dims(), ["schema_missing"])
    stored = manifest.get("entries_sha256")
    if not isinstance(stored, str) or localio.canonical_json_digest(entries) != stored:
        return _obs(
            relpath,
            "structure_observed",
            "rejected",
            _dims(
                integrity=("failed", "entries_digest_mismatch"), subject=_workspace_subject(ctx), population="passed"
            ),
        )
    for entry in entries:
        name = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not evidence_package._is_safe_entry_path(name):
            return _obs(relpath, "discovered", "invalid", _dims(), ["entry_path_unsafe"])
        entry_path = package_dir / name
        refusal = _path_refusal(ctx, entry_path)
        if refusal == "symlink_refused":
            return _obs(relpath, "discovered", "invalid", _dims(population=("unknown", refusal)), [refusal])
        if refusal is not None:
            return _verifier_unavailable(relpath, refusal)
        if _is_symlink(ctx, entry_path) or not _is_regular(ctx, entry_path):
            return _obs(
                relpath,
                "structure_observed",
                "rejected",
                _dims(integrity=("failed", "entry_missing"), subject=_workspace_subject(ctx), population="passed"),
            )
    return _obs(
        relpath,
        "structure_observed",
        "structure_observed",
        _dims(
            integrity=("not_checked", "verifier_not_wired"),
            subject=_workspace_subject(ctx),
            population="passed",
        ),
        ["verifier_not_wired"],
    )


@dataclass(frozen=True)
class _Rule:
    evaluate: Callable[[_Context], _Discovery]
    kind: str
    verifier: str | None
    verifier_status: str
    required: frozenset[str]


_WORKSPACE_REQUIRED = frozenset({"integrity", "subject", "population"})
_SIGNED_REQUIRED = frozenset({"integrity", "signature", "authorization", "subject", "population"})

_EVALUATORS: dict[str, _Rule] = {
    "verify-receipt-completed": _Rule(
        _evaluate_verify_receipt_completed,
        "in_module",
        "verify-receipt-integrity",
        "not_wired",
        frozenset({"integrity", "subject", "population"}),
    ),
    "sshsig-test-result-signed-ok": _Rule(
        _evaluate_sshsig_test_result_signed_ok, "delegated", "attestation.verify_attestation", "wired", _SIGNED_REQUIRED
    ),
    "cosign-bundle-exists": _Rule(
        _evaluate_cosign_bundle_exists, "structural", "cosign-bundle", "not_wired", _SIGNED_REQUIRED
    ),
    "agent-request-signed": _Rule(
        _evaluate_agent_request_signed, "delegated", "attestation.verify_attestation", "wired", _SIGNED_REQUIRED
    ),
    "human-approval-allow-with-sod": _Rule(
        _evaluate_human_approval_allow_with_sod, "delegated", "approval.verify_run_approval", "wired", _SIGNED_REQUIRED
    ),
    "run-event-journal-exists": _Rule(
        _evaluate_run_event_journal_exists,
        "delegated",
        "run_journal.read_journal_bounded",
        "wired",
        frozenset({"integrity", "subject", "population"}),
    ),
    "governance-inventory-exists": _Rule(
        _evaluate_governance_inventory_exists, "structural", None, "not_wired", _WORKSPACE_REQUIRED
    ),
    "commit-trailer-receipts": _Rule(
        _evaluate_commit_trailer_receipts, "in_module", "causal_receipt.receipt_digest", "wired", _WORKSPACE_REQUIRED
    ),
    "guard-audit-allow": _Rule(_evaluate_guard_audit_allow, "structural", None, "not_wired", _WORKSPACE_REQUIRED),
    "outcome-record-exists": _Rule(
        _evaluate_outcome_record_exists, "structural", None, "not_wired", _WORKSPACE_REQUIRED
    ),
    "verify-archive-index-exists": _Rule(
        _evaluate_verify_archive_index_exists, "structural", None, "not_wired", _WORKSPACE_REQUIRED
    ),
    "evidence-package-manifest": _Rule(
        _evaluate_evidence_package_manifest, "structural", "evidence-package", "not_wired", _WORKSPACE_REQUIRED
    ),
}

EVALUATOR_VERSION = 2


def _normalize_period(period: tuple[datetime | str, datetime | str] | None) -> tuple[datetime, datetime] | None:
    if period is None:
        return None
    if not isinstance(period, tuple) or len(period) != 2:
        raise ControlCrosswalkError("period must be a (start, end) pair")
    bounds: list[datetime] = []
    for value in period:
        parsed = value if isinstance(value, datetime) else _parse_timestamp(value)
        if parsed is None or parsed.tzinfo is None:
            raise ControlCrosswalkError("period bounds must be timezone-aware ISO-8601 timestamps")
        bounds.append(parsed)
    if bounds[0] > bounds[1]:
        raise ControlCrosswalkError("period start must not be after period end")
    return bounds[0], bounds[1]


def assess_claim(
    target: Path,
    claim_id: str,
    run_id: str | None,
    *,
    period: tuple[datetime | str, datetime | str] | None = None,
) -> control_readiness.ClaimReadiness:
    """Assess one claim under the ``brigade.claim_readiness.v1`` contract.

    ``period`` is an optional explicit assessment period ``(start, end)`` of
    timezone-aware datetimes or ISO-8601 strings.  Without it, freshness is
    reported ``not_applicable`` with ``period_not_supplied``, never as fresh.
    """
    state_rule = _STATE_RULES.get(claim_id)
    if state_rule is None:
        raise ControlCrosswalkError(f"unknown claim: {claim_id}")
    rule = _EVALUATORS.get(state_rule)
    if rule is None:
        raise ControlCrosswalkError(f"unknown state_rule: {state_rule}")
    if run_id is not None and not receipts_trailer._is_bare_run_id(run_id):
        raise ControlCrosswalkError("run id must be a bare run directory name")
    canonical = target.expanduser().resolve()
    normalized_period = _normalize_period(period)
    try:
        reader = _AssessmentReader(canonical)
    except OSError:
        discovery = _Discovery(observations=[_verifier_unavailable(".", "discovery_unreadable")])
    else:
        ctx = _Context(target=canonical, run_id=run_id, period=normalized_period, reader=reader)
        try:
            discovery = rule.evaluate(ctx)
            existing = {(obs.relpath, reason) for obs in discovery.observations for reason in obs.reasons}
            for path, reason in reader.errors.items():
                if (_relpath(ctx, path), reason) in existing:
                    continue
                if reason == "read_limit_exceeded":
                    obs = _obs(
                        _relpath(ctx, path), "discovered", "incomplete", _dims(population=("unknown", reason)), [reason]
                    )
                    _claim_wide(obs, ctx)
                    discovery.observations.append(obs)
                else:
                    _record_discovery_error(discovery, ctx, path, reason)
        finally:
            reader.close()
    required = rule.required | ({"freshness"} if normalized_period is not None else frozenset())
    verifier_status = "unavailable" if discovery.verifier_unavailable else rule.verifier_status
    scan_limit = _TRAILER_WINDOW if state_rule == "commit-trailer-receipts" else _MAX
    return control_readiness.aggregate(
        claim_id=claim_id,
        evaluator={"id": state_rule, "version": EVALUATOR_VERSION, "kind": rule.kind},
        verifier={"name": rule.verifier, "status": verifier_status},
        required=required,
        observations=discovery.observations,
        truncated=discovery.truncated,
        scan_limit=scan_limit,
        run_scoped=run_id is not None,
        period_supplied=normalized_period is not None,
        not_applicable_reason=discovery.not_applicable_reason,
        window=discovery.window,
    )


def _evaluate_claim_state(target: Path, claim_id: str, run_id: str | None) -> str:
    """Compute the legacy evidentiary state for a single claim.

    Conservative projection of :func:`assess_claim`: only ``validated`` maps to
    ``evidenced_passed``.
    """
    state = assess_claim(target, claim_id, run_id).legacy_state()
    if state not in VALID_STATES:
        raise ControlCrosswalkError(f"invalid state {state!r} for {claim_id}")
    return state


def evaluate_controls(
    target: Path,
    *,
    run_id: str | None = None,
    framework_id: str | None = None,
    period: tuple[datetime | str, datetime | str] | None = None,
) -> dict[str, Any]:
    """Evaluate evidence readiness for the crosswalk mappings in scope.

    Each claim is assessed once under ``brigade.claim_readiness.v1``; mapping
    rows carry the conservative legacy ``state`` plus the claim's
    ``validation_level`` and ``evidence_outcome``.  ``period`` is an optional
    explicit assessment period (see :func:`assess_claim`).
    """
    target = target.expanduser().resolve()
    normalized_period = _normalize_period(period)
    crosswalk = load_crosswalk()
    claims_by_id = {c["id"]: c for c in crosswalk["claims"]}
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    assessments: dict[str, control_readiness.ClaimReadiness] = {}
    states: dict[str, str] = {}
    for claim in crosswalk["claims"]:
        assessment = assess_claim(target, claim["id"], run_id, period=normalized_period)
        assessments[claim["id"]] = assessment
        states[claim["id"]] = assessment.legacy_state()
    mappings: list[dict[str, Any]] = []
    for mapping in crosswalk["mappings"]:
        if framework_id is not None and mapping["framework_id"] != framework_id:
            continue
        claim = claims_by_id[mapping["claim_id"]]
        framework = frameworks_by_id[mapping["framework_id"]]
        state = states[mapping["claim_id"]]
        level = assessments[mapping["claim_id"]].level
        outcome = assessments[mapping["claim_id"]].outcome
        if mapping["relationship"] == "no-relationship":
            state = outcome = "not_applicable"
            level = "none"
        row = {
            "claim_id": mapping["claim_id"],
            "claim_title": claim["title"],
            "framework_id": mapping["framework_id"],
            "framework_name": framework["name"],
            "control_id": mapping["control_id"],
            "relationship": mapping["relationship"],
            "obligation_bearer": mapping["obligation_bearer"],
            "applicability_condition": mapping["applicability_condition"],
            "rationale": mapping["rationale"],
            "source_locator": mapping["source_locator"],
            "state": state,
            "validation_level": level,
            "evidence_outcome": outcome,
        }
        row.update({key: mapping[key] for key in PROVENANCE_FIELDS if key in mapping})
        mappings.append(row)
    mappings.sort(key=lambda m: (m["framework_id"], m["control_id"], m["claim_id"]))
    unmapped: list[dict[str, Any]] = []
    for entry in crosswalk.get("unmapped_evidence_properties", []):
        if framework_id is not None and entry["framework_id"] != framework_id:
            continue
        unmapped.append({**entry, "claim_title": claims_by_id[entry["claim_id"]]["title"]})
    unmapped.sort(key=lambda e: (e["framework_id"], e["claim_id"]))
    scope = f"run:{run_id}" if run_id else "workspace"
    return {
        "schema": EVIDENCE_CONTROLS_SCHEMA,
        "crosswalk_version": crosswalk["crosswalk_version"],
        "evaluated_at": localio.utc_now_iso(),
        "scope": scope,
        "notice": HEADER_NOTICE,
        "relationship_semantics": crosswalk["relationship_semantics"],
        "integrity_boundary": crosswalk["integrity_boundary"],
        "readiness_contract": control_readiness.READINESS_CONTRACT,
        "assessment_period": (
            None
            if normalized_period is None
            else {"start": normalized_period[0].isoformat(), "end": normalized_period[1].isoformat()}
        ),
        "evidence_readiness": {claim_id: assessments[claim_id].to_dict() for claim_id in sorted(assessments)},
        "mappings": mappings,
        "unmapped_evidence_properties": unmapped,
    }


_STATE_CONTRACT_DOC: tuple[str, ...] = (
    "## Evidence state contract",
    "",
    "`brigade evidence controls --json` emits schema `brigade.evidence_controls.v2`. Each claim is assessed once "
    "under the `brigade.claim_readiness.v1` contract and reported in `evidence_readiness`, keyed by claim id. "
    "This is a readiness index. It is not a score, an enforcement gate, an audit conclusion, a certification "
    "claim or a guarantee for any tenant. The command exits 0 whatever the outcomes are.",
    "",
    "Each assessment records:",
    "",
    "- `evaluator`: the state rule id, evaluator version and kind (`in_module`, `structural` or `delegated`).",
    "- `verifier`: the dedicated verifier name and status (`wired`, `not_wired`, `not_required` or "
    "`unavailable` when a required tool such as `ssh-keygen` or `git` is missing).",
    "- `validation_level`: the depth of processing actually performed, one of `none`, `discovered`, "
    "`structure_observed` or `claim_validated`. It is the lowest level across in-scope artifacts. Levels are "
    "observations, not a trust ladder.",
    "- `outcome` and `reason`: a stable outcome and a fixed reason code. `reason` is null when no code applies, "
    "as for a validated claim; it is never an empty string. `reason_codes` lists every code observed for in-scope "
    "artifacts and the population.",
    "- `dimensions`: `integrity`, `signature`, `authorization`, `subject`, `freshness` and `population`, each with "
    "a status (`passed`, `failed`, `unknown`, `unavailable`, `not_checked` or `not_applicable`) and a reason code. "
    "`required_dimensions` names the dimensions the claim needs.",
    "- `population`: bounded counts of discovered, in-scope, `wrong_run`, `out_of_period` and `unbound` artifacts, "
    "per-outcome counts, the `scan_limit` and whether discovery was `truncated`. EC-08 adds `window`: the most "
    "recent 20 commits, with counts `inspected`, `with_trailer`, `without_trailer` and `unreadable`. Only commits "
    "carrying both `Brigade-Run` and `Brigade-Receipt` trailers within that window are the claim's artifacts, so "
    "one valid trailer never implies that every commit in the window is linked. Older history is outside the "
    "claim and is not truncation.",
    "- `artifacts`: up to 20 per-artifact observations sorted by relative path, with scope, run binding field, "
    "level, outcome, reason codes and dimensions. Reasons never include artifact content or absolute host paths.",
    "",
    "Outcomes: `validated` (claim-level validation passed every required dimension), `structure_observed`, "
    "`discovered_only`, `failed` (valid evidence of a failed operation), `rejected` (a verifier or integrity check "
    "rejected the artifact), `invalid` (the artifact breaks its own format), `unavailable` (a required verifier or "
    "tool could not run), `incomplete` (partial, unbound, empty or truncated evidence), `absent` and "
    "`not_applicable`.",
    "",
    "Aggregation: per artifact, a failed required dimension yields `rejected`, an unavailable one yields "
    "`unavailable` and an unknown one yields `incomplete`. Required trust failures reject even negative "
    "operational results. For a proposed `failed` outcome, a recorded authorization denial or negative SoD "
    "verdict remains `failed` unless another required dimension failed. `SOD-VIOLATION` remains `rejected`. A passed signature never offsets another failed "
    "dimension. `validated` requires `claim_validated` depth and every required dimension passed or not "
    "applicable. Per claim, the most severe in-scope outcome wins, in the order rejected, invalid, failed, "
    "unavailable, incomplete, absent, discovered_only, structure_observed, validated. A claim is therefore "
    "`validated` only when every in-scope artifact validated, and one valid artifact cannot hide a rejected, "
    "incomplete, structure-only or discovered-only sibling. Claim `validation_level` is the lowest in-scope level "
    "and each claim dimension is the worst in-scope status; the claim outcome is reconciled against both. An "
    "artifact-level `not_applicable` population becomes `passed` at claim level only when discovery enumerated "
    "the population without truncation or unbound artifacts. Wrong-run and out-of-period artifacts are excluded "
    "and counted. Under `--run-id`, artifacts that record no run identity are `unbound`, are never assumed to "
    "belong to the run, and keep the claim `incomplete`. An EC-08 missing history tool, a whole-claim verifier "
    "error or an unreadable discovery root is a claim-wide `unavailable` observation under any scope. "
    "Per-artifact verifier failures retain their recorded scope and reasons; an artifact whose run cannot be "
    "bound remains `unbound` under a selected run.",
    "",
    "Population bounds: a directory scan reads at most 201 entries and inspects the first 200 sorted names it "
    "read. Past the cap the claim is `incomplete` with `population_truncated`, and the inspected sample depends "
    "on filesystem order. With `--run-id`, EC-04, EC-05 and EC-06 inspect `.brigade/runs/<run-id>` directly, so a "
    "selected run is never lost behind the cap. Verify receipts bind to a run through recorded fields, so their "
    "scan stays bounded and can truncate. The run id must be a bare directory name. EC-12 discovers "
    "`.brigade/evidence-packages/manifest.json` and `.brigade/evidence-packages/<package>/manifest.json`; deeper "
    "nesting is not discovered, and no strict package verification runs before #1618. Packages bind through "
    "either their recorded producer-run or verify-run identity. JSONL reads are capped at 8 MiB and 10,000 "
    "records. Discovery and reads refuse symlinked components beneath the canonical assessed target, "
    "including a linked `.brigade` or lifecycle `events/` ancestor. Metadata errors remain "
    "`discovery_unreadable`. Refused discovery leaves the population unknown. "
    "`artifacts_reported_truncated` reports when the 20-artifact output cap omits observed artifacts.",
    "",
    "Verify receipts (EC-01): `running` is nonterminal (`status_not_terminal`). `canceled` is terminal but "
    "`incomplete` (`status_canceled`). `failed` and `rejected` are terminal `failed` outcomes, and a command with a "
    "nonzero exit code is `failed`, provided no required trust dimension failed. A failed or rejected receipt "
    "whose `run_id` differs from its directory is `rejected` with `run_binding_mismatch`. A `completed` receipt "
    "needs a non-empty `planned_commands` list, a `run_id` "
    "matching its directory, and one `completed` command with exit code 0 per planned check whose `command` "
    "display string equals the planned entry at the same position. A different, missing or reordered command is "
    "`incomplete` with `planned_commands_mismatch`. An empty intended check list never shows that a test ran. The "
    "stored receipt digest and optional local HMAC are not verified before #1618, so `integrity` is a required "
    "dimension reported `not_checked` with `verifier_not_wired`, and even a well-formed successful receipt stops "
    "at `structure_observed` (`untested`). A reused receipt (`reused_from`) copies the commands of an earlier run; "
    "it is recorded, reused evidence, not a new execution under the new receipt id. EC-02 pins re-derivation to "
    "the attestation's own directory receipt and rejects an attestation whose run differs from its directory. "
    "EC-06 rejects a journal whose events name another run.",
    "",
    "Signed requests and approvals (EC-04, EC-05): EC-04 discovers the producer path "
    "`.brigade/runs/<run>/requests/<nonce>.json` written by `agent_request.record_request`, plus the legacy "
    "`agent-request.json` and `request.json` names, with a bounded `requests/` scan. It checks signature, key "
    "trust and that the statement names the run directory. It does not independently check that the request "
    "preceded worker dispatch. Its positive outcome and validation depth are capped at `structure_observed` "
    "(`untested`), with `verifier_not_wired`, even when signature trust and run identity pass. The wired "
    "signature verifier does not validate the ordering claim. Full validation requires a verifier that binds "
    "the same signed request to a `request.signed` lifecycle event preceding worker dispatch. "
    "EC-05 reads the latest approval event in the lifecycle journal. Without "
    "`ssh-keygen`, a failure that needs no signature check (a broken or partial journal chain, an event naming "
    "another run, missing or invalid run metadata, or an invalid approval event nonce, path or statement) stays `rejected`; only otherwise is "
    "the claim `unavailable`. A trusted key is a key-trust result; organizational identity and authority remain "
    "the adopting organization's evidence.",
    "",
    "Commit trailers (EC-08): Git stdout and stderr share a fixed capture budget of at most 1 MiB enforced "
    "while reading the subprocess pipes. Head-buffer overflow can conservatively refuse output earlier, "
    "at about 512 KiB. The transport budget is fixed at 16 MiB, with a ten-second timeout. Oversized output "
    "is `incomplete` with `read_limit_exceeded`. An oversized commit message counts as `unreadable`, "
    "never `without_trailer`. A failed Git repository probe is `unavailable` (`verifier_error`); "
    "only a confirmed non-repository is `not_applicable`. A trailer whose local `run.json` is missing is `incomplete` with integrity "
    "`unknown` and `entry_missing`; it is never reported as a digest comparison failure. A symlinked run "
    "directory or `run.json` is refused as `invalid` with `symlink_refused`. A recomputed digest that differs "
    "from the trailer is `rejected` with `trailer_digest_mismatch`.",
    "",
    "Scope: workspace-wide assessments include historical failures and incomplete evidence. Use `--run-id` "
    "for current-run EC-01, EC-02, EC-04, EC-05 and EC-06 evidence. EC-09 consumes operator-saved guard JSON "
    "stdout at `.brigade/work/guard/audit.json`; the producer does not write that file or a timestamp. EC-10 "
    "and EC-11 period filtering uses the latest recorded timestamp for the ledger, without per-record "
    "timestamp validation. Their structural observations cannot validate.",
    "",
    "Freshness: only the Python API accepts an explicit assessment period; the CLI has no period option. Without "
    "one, freshness is `not_applicable` with `period_not_supplied`. It is never reported as fresh. With a period, "
    "an artifact without a timestamp is `unknown` and keeps the claim `incomplete`. EC-10 reads the outcome "
    "record `ts` field.",
    "",
    "Legacy `state`: every mapping row keeps the four legacy values as a conservative projection. Only "
    "`validated` maps to `evidenced_passed`. `failed` and `rejected` map to `evidenced_failed`. `not_applicable` "
    "maps to `not_applicable`. Every other outcome maps to `untested`. Rows also carry `validation_level` and "
    "`evidence_outcome`, and text output shows `[state | level/outcome]`.",
    "",
    "Migration from `brigade.evidence_controls.v1`: existing key names remain, but NIST `control_id` values "
    "use spaces instead of hyphens and reviewed rows are remapped or removed. `crosswalk_version` is 2; "
    "the template schema remains `brigade.control_crosswalk.v1`. New root keys are `relationship_semantics`, "
    "`integrity_boundary`, `unmapped_evidence_properties`, `readiness_contract`, `assessment_period` and "
    "`evidence_readiness`. Mapping rows add `validation_level`, `evidence_outcome` and provenance fields. "
    "`evidenced_passed` is stricter. Claims checked only "
    "for presence or structure (EC-01, EC-03, EC-04, EC-07, EC-09, EC-10, EC-11 and EC-12) now report `untested` with "
    "`structure_observed` until a dedicated verifier is wired. An EC-01 receipt with an empty command list or "
    "commands that do not match `planned_commands` is no longer a pass, and legacy receipts without "
    "`planned_commands` report `untested`. Artifacts now classified `invalid` (for example a previously accepted "
    "malformed cosign bundle or package manifest, or a ledger with a row lacking its required field) report "
    "`untested`. v1 passed EC-08 when any one trailer matched and failed it when none did; now every trailered "
    "commit in the window counts, and a trailer whose local receipt is missing keeps the claim `untested`. "
    "The claim `reason` is null rather than an empty string when no code applies. Receipts with status `failed` or `rejected` now report `evidenced_failed`; v1 counted only "
    "completed receipts and reported them `untested`. A missing `ssh-keygen` or `git` reports `unavailable` "
    "instead of a failure. EC-08 still assesses the most recent 20 commits. Consumers should read "
    "`evidence_readiness` to see what was validated rather than infer validation from `state`.",
)


def render_doc() -> str:
    """Render the static control crosswalk as markdown documentation.

    The document is a pure function of the bundled crosswalk template and does
    not read workspace artifacts.  Per-row evidence states are computed at query
    time by `brigade evidence controls` and are not part of this document.
    """
    crosswalk = load_crosswalk()
    lines: list[str] = []
    lines.append("# Brigade Control Crosswalk")
    lines.append("")
    lines.append(HEADER_NOTICE)
    lines.append("")
    lines.append(
        "Evidence states are computed at query time by `brigade evidence controls` and are not part of this document."
    )
    lines.append("")
    lines.append(f"Crosswalk version: {crosswalk['crosswalk_version']}. Schema: `{SCHEMA}`.")
    lines.append("")
    lines.append(f"Relationship semantics: {crosswalk['relationship_semantics']}")
    lines.append("")
    lines.append(f"Integrity boundary: {crosswalk['integrity_boundary']}")
    lines.append("")
    by_framework: dict[str, list[dict[str, Any]]] = {}
    for mapping in crosswalk["mappings"]:
        by_framework.setdefault(mapping["framework_id"], []).append(mapping)
    unmapped_by_framework: dict[str, list[dict[str, Any]]] = {}
    for entry in crosswalk.get("unmapped_evidence_properties", []):
        unmapped_by_framework.setdefault(entry["framework_id"], []).append(entry)
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    for fw_id in sorted(frameworks_by_id):
        rows = by_framework.get(fw_id, [])
        framework = frameworks_by_id[fw_id]
        lines.append(f"## {framework['name']} (`{fw_id}`)")
        lines.append("")
        lines.append(f"Edition: {framework['edition']}. Source: {framework['source_locator']}")
        if "clause_text_status" in framework:
            lines.append(f"Clause text: {framework['clause_text_status']}.")
        if "note" in framework:
            lines.append(f"Note: {framework['note']}")
        lines.append("")
        if not rows:
            lines.append(f"Not mapped in crosswalk version {crosswalk['crosswalk_version']}.")
        else:
            lines.append("| Control | Claim | Relationship | Obligation | Applicability | Rationale | Source |")
            lines.append("|---------|-------|--------------|------------|---------------|-----------|--------|")
            rows.sort(key=lambda m: (m["control_id"], m["claim_id"]))
            for row in rows:
                control = f"`{row['control_id']}`"
                claim = row["claim_id"]
                rel = row["relationship"]
                obl = row["obligation_bearer"]
                app = row["applicability_condition"]
                rationale = row["rationale"]
                source = row["source_locator"]
                lines.append(f"| {control} | {claim} | {rel} | {obl} | {app} | {rationale} | {source} |")
            provenance_rows = [row for row in rows if "disposition" in row]
            if provenance_rows:
                lines.append("")
                lines.append("### Mapping provenance")
                for row in provenance_rows:
                    lines.append("")
                    lines.append(f"- `{row['control_id']}` / {row['claim_id']}: {row['disposition']}")
                    if "superseded_control_id" in row:
                        lines.append(f"  - Supersedes: `{row['superseded_control_id']}`")
                    lines.append(f"  - Edition: {row['edition']}")
                    lines.append(f"  - Primary locator: {row['source_locator']}")
                    lines.append(f"  - Artifact contract: {row['artifact_contract']}")
                    lines.append(f"  - Responsible actor: {row['responsible_actor']}")
                    lines.append(f"  - Applicability: {row['applicability_condition']}")
                    lines.append(f"  - Support rationale: {row['rationale']}")
                    lines.append(f"  - Verification limit: {row['verification_limit']}")
        unmapped = sorted(unmapped_by_framework.get(fw_id, []), key=lambda e: e["claim_id"])
        if unmapped:
            lines.append("")
            lines.append("### Unmapped evidence properties")
            lines.append("")
            lines.append("These claims have no direct control in this framework. No control identifier is assigned.")
            lines.append("")
            lines.append(
                "| Claim | Property | Disposition | Supersedes | Artifact contract | Responsible actor "
                "| Applicability | Rationale | Verification limit | Rejected control source |"
            )
            lines.append(
                "|-------|----------|-------------|------------|-------------------|-------------------"
                "|---------------|-----------|--------------------|--------|"
            )
            for entry in unmapped:
                lines.append(
                    f"| {entry['claim_id']} | {entry['property']} | {entry['disposition']} | "
                    f"`{entry['superseded_control_id']}` | {entry['artifact_contract']} | "
                    f"{entry['responsible_actor']} | {entry['applicability_condition']} | {entry['rationale']} | "
                    f"{entry['verification_limit']} | {entry['source_locator']} |"
                )
        lines.append("")
    lines.extend(_STATE_CONTRACT_DOC)
    return "\n".join(lines).rstrip() + "\n"


def controls(
    target: Path,
    *,
    run_id: str | None = None,
    framework_id: str | None = None,
    json_output: bool = False,
    render_doc_path: Path | None = None,
) -> int:
    """CLI entry for `brigade evidence controls`."""
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if render_doc_path is not None:
        render_doc_path = render_doc_path.expanduser().resolve()
        render_doc_path.parent.mkdir(parents=True, exist_ok=True)
        localio.write_text_atomic(render_doc_path, render_doc())
        if not json_output:
            return 0

    try:
        evaluated = evaluate_controls(target, run_id=run_id, framework_id=framework_id)
    except ControlCrosswalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(evaluated, indent=2, sort_keys=True))
        return 0
    print(f"evidence controls: {target}")
    print(HEADER_NOTICE)
    print(f"scope: {evaluated['scope']}")
    by_framework: dict[str, list[dict[str, Any]]] = {}
    for mapping in evaluated["mappings"]:
        by_framework.setdefault(mapping["framework_id"], []).append(mapping)
    crosswalk = load_crosswalk()
    frameworks_by_id = {f["id"]: f for f in crosswalk["frameworks"]}
    for fw_id in sorted(frameworks_by_id):
        rows = by_framework.get(fw_id, [])
        print(f"\n{frameworks_by_id[fw_id]['name']} ({fw_id})")
        if not rows:
            print(f"  not mapped in crosswalk version {evaluated['crosswalk_version']}")
        else:
            for row in rows:
                print(
                    f"  [{row['state']} | {row['validation_level']}/{row['evidence_outcome']}] "
                    f"{row['control_id']} -> {row['claim_id']} "
                    f"({row['relationship']}, {row['obligation_bearer']}, {row['applicability_condition']})"
                )
        for entry in evaluated["unmapped_evidence_properties"]:
            if entry["framework_id"] == fw_id:
                print(f"  [unmapped] {entry['claim_id']} {entry['property']} (no direct control)")
    return 0
