"""Evidence assembly for the memory verification pass (D3), one card at a time.

A card's cited evidence is read only inside the workspace roots bound to the
card's scope in `[[verify.workspace]]`, and only under the evidence roots local
reflection uses. Evidence bundles, related excerpts and receipt digests come
only from the archive projects listed for that scope, and a project listed by
two scopes belongs to neither. Anything whose ownership is absent or ambiguous
is `evidence_unreachable` and is never read.

Root handles. Every bound root is opened once per run as a directory handle
and checked against the `(st_dev, st_ino)` pinned when the configuration was
parsed. Evidence paths resolve under that handle one component at a time with
no-follow opens, never through the root's path string. The handle stays open
while any evidence under the root is read, so a root that is renamed, replaced
or removed and recreated after the check is never read through its new path.

Containment. Every directory and file on the way must sit on the root's
device. On Linux each opened descriptor must also report the root's mount id
in `/proc/self/fdinfo`, so a bind mount of another directory on the same
device is refused. When the mount id cannot be read, the root is refused. Other
POSIX systems have no per-descriptor mount id, so they check the device only.
That leaves one residual there: a same-device mount inside a root, which those
systems do not create by default and only an administrator can add.

On POSIX the root opens with `O_DIRECTORY | O_NOFOLLOW` at the path resolved
at parse time. Windows has neither flag. There the root opens through the
handle-relative helper, which refuses a final reparse point, and the opened
handle's file index is checked against the pinned identity the same way.
Children open through the same helper, which refuses reparse points. A
platform with neither backend, or a filesystem with no stable inode, makes
every root unreachable, so identity is never assumed.

Trust. Only the authenticated fields of a verify receipt whose bytes match
the digest the evidence archive recorded at import rank `authenticated`. The
same receipt's command output is a separate item at medium rank, and a
receipt the archive cannot vouch for is medium throughout. Handoffs, evidence
files and bundles are medium. Related excerpts are low.

Every item is cut to its byte bound on a UTF-8 boundary and scanned with the
memory scan bundle, both as read and exactly as it would reach a prompt. A
blocked or flagged item is dropped and recorded with its rule ids only.

No function here calls a model or the hub, and `Assembly.report()` carries
reason codes, counts, kinds, ranks, digests and rule ids, never evidence text.
"""

from __future__ import annotations

import contextlib
import functools
import hmac
import json
import os
import re
import stat
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from . import dirfd
from . import localio
from . import memory_cmd
from . import memory_content_scan as scan
from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from . import memory_reflect_candidates as candidates
from . import memory_verify_config as vconfig
from . import memory_verify_select as select

UNREACHABLE = "evidence_unreachable"
FLAGGED = "evidence_flagged"
NO_EVIDENCE = "no_evidence"
UNCORROBORATED = "uncorroborated"

AUTHENTICATED, MEDIUM, LOW = "authenticated", "medium", "low"
RECEIPT_FIELDS = "receipt_fields"
RECEIPT_OUTPUT = "receipt_output"
HANDOFF = "handoff"
EVIDENCE_FILE = "evidence_file"
EVIDENCE_BUNDLE = "evidence_bundle"
EXCERPT = "excerpt"
# Quotes from these kinds add context but can never meet the corroboration rule.
CONTEXT_ONLY = frozenset({RECEIPT_OUTPUT, EXCERPT})

ITEM_BYTES = 8 * 1024
DEFAULT_CARD_BYTES = 32 * 1024
MIN_CARD_BYTES = 8 * 1024
MAX_CARD_BYTES = 64 * 1024
MAX_ITEMS = 8
EXCERPT_BYTES = 4 * 1024
MAX_EXCERPTS = 3
# A cited file is read whole for the digest check. A larger file is unreachable.
READ_LIMIT = 1024 * 1024
COPY_SPAN = 200
# The independence test compares every source byte (up to READ_LIMIT) plus the item text.
# Past this many candidate checks, or this many fingerprint positions in one input, the
# pair counts as copied, so a crafted repetitive input cannot make the test slow. Both
# caps fail toward "not independent".
COPY_PROBES = 100_000
MAX_PRINT_POSITIONS = 1 << 16
MAX_DEPTH = 32

HANDOFF_ROOTS = frozenset({"memory/handoff-inbox", *candidates.WRITER_INBOXES.values()})
RUN_DIR = re.compile(r"\.brigade/work/verify-runs/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/(.+)")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TERM = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
ANCHOR = re.compile(r"\s*\^c-[A-Za-z0-9]+\s*$")
STOP = frozenset(
    "this that with from have been were will would should could there their them they what when where which "
    "while about after before into onto over under than then also only just more most some such very each".split()
)
QUERY_TITLE_TERMS = 8
QUERY_RARE_TERMS = 4

ROOT_UNAVAILABLE = "verify_root_unavailable: a bound workspace root could not be opened and proven by identity"


class RootUnavailable(OSError):
    """Fixed text only. Never formatted with a configured path."""

    def __init__(self) -> None:
        super().__init__(ROOT_UNAVAILABLE)


class _Absent(Exception):
    """The path does not exist under this root. Any other failure refuses the item."""


# --- Root handles -------------------------------------------------------------------------


def _nt() -> Any:
    return dirfd._nt_dirfd()


def _open_root(workspace: vconfig.Workspace) -> int:
    """A directory handle for the root, proven to be the directory pinned at parse time."""
    try:
        if dirfd.posix_available():
            # The realpath recorded at parse time. O_NOFOLLOW refuses a final link, and
            # the identity check below refuses any other substitution on the way.
            fd = os.open(workspace.resolved, dirfd.directory_flags())
        elif dirfd.nt_available():
            fd = _nt().open_root_directory(workspace.path, writable=False)
        else:
            raise RootUnavailable()
    except OSError:
        raise RootUnavailable() from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_ino == 0
            or (metadata.st_dev, metadata.st_ino) != workspace.identity
        ):
            raise RootUnavailable()
    except BaseException:
        os.close(fd)
        raise
    return fd


def _mount(fd: int) -> int | None:
    """The descriptor's mount id where the platform reports one, else None. Raises OSError when it must and cannot."""
    return dirfd.mount_id(fd) if dirfd.mount_ids_required() else None


@dataclass(frozen=True)
class HeldRoot:
    workspace: vconfig.Workspace
    fd: int
    device: int
    # The root's mount id on Linux. Every descriptor opened under it must match.
    mount: int | None = None

    def contains(self, fd: int, metadata: os.stat_result) -> bool:
        if metadata.st_dev != self.device:
            return False
        if self.mount is None:
            return True
        try:
            return dirfd.mount_id(fd) == self.mount
        except OSError:
            return False


class Roots:
    """Every bound root, opened once and held for the whole run. Use as a context manager.

    A root that fails its identity check is held as unavailable, and every
    path under it is unreachable for this run.
    """

    def __init__(self, config: vconfig.VerifyConfig) -> None:
        self.config = config
        self._held: dict[vconfig.Identity, HeldRoot | None] = {}
        # A project listed by two scopes is owned by neither: its bundles, excerpts and
        # receipt records could belong to either, so no scope reads them.
        self._listing: dict[str, set[str]] = {}
        for workspace in config.workspaces:
            for project in workspace.archive_projects:
                self._listing.setdefault(project, set()).add(workspace.scope)

    def __enter__(self) -> Roots:
        try:
            for workspace in self.config.workspaces:
                try:
                    fd = _open_root(workspace)
                except RootUnavailable:
                    self._held[workspace.identity] = None
                    continue
                try:
                    mount = _mount(fd)
                except OSError:
                    # Linux without a readable mount id cannot prove containment: refuse the root.
                    os.close(fd)
                    self._held[workspace.identity] = None
                    continue
                self._held[workspace.identity] = HeldRoot(workspace, fd, workspace.identity[0], mount)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        for held in self._held.values():
            if held is not None:
                with contextlib.suppress(OSError):
                    os.close(held.fd)
        self._held.clear()

    def bound(self, scope: str) -> bool:
        return any(workspace.scope == scope for workspace in self.config.workspaces)

    def roots(self, scope: str) -> list[HeldRoot | None]:
        """The scope's roots in configuration order. None marks a root that failed its identity check."""
        return [self._held.get(w.identity) for w in self.config.workspaces if w.scope == scope]

    def projects(self, scope: str) -> tuple[str, ...]:
        """Archive projects listed for this scope and for no other scope, sorted."""
        listed = {p for w in self.config.workspaces if w.scope == scope for p in w.archive_projects}
        return tuple(sorted(project for project in listed if self._listing.get(project) == {scope}))


# --- Reads under a held root --------------------------------------------------------------


def _component(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise OSError("path component is not contained")
    if not dirfd.posix_available() and ":" in name:
        raise OSError("path component is not contained")
    return name


def _open_dir(parent: int, name: str) -> int:
    if dirfd.posix_available():
        return os.open(name, dirfd.directory_flags(), dir_fd=parent)
    return int(_nt().open_child_directory(parent, name, writable=False))


def _open_file(parent: int, name: str) -> int:
    if dirfd.posix_available():
        return os.open(name, dirfd.file_flags(os.O_RDONLY | os.O_NONBLOCK), dir_fd=parent)
    return int(_nt().open_file(parent, name, os.O_RDONLY | getattr(os, "O_BINARY", 0)))


def _fixed(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def read_under(root: HeldRoot, rel: str) -> tuple[bytes, os.stat_result]:
    """Read `rel` under the held root, one no-follow component at a time.

    Raises `_Absent` when a component does not exist, and OSError or ValueError
    for anything unsafe: a link, a mount crossing, a non-regular or multi-link
    file, a file over the read limit, or a file that changed during the read.
    """
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH:
        raise OSError("evidence path is too deep")
    opened: list[int] = []
    current = root.fd
    try:
        for name in parts[:-1]:
            try:
                current = _open_dir(current, _component(name))
            except (FileNotFoundError, NotADirectoryError):
                raise _Absent() from None
            opened.append(current)
            metadata = os.fstat(current)
            if not stat.S_ISDIR(metadata.st_mode) or not root.contains(current, metadata):
                raise OSError("evidence path leaves the bound root")
        name = _component(parts[-1])
        try:
            fd = _open_file(current, name)
        except FileNotFoundError:
            raise _Absent() from None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not root.contains(fd, before):
                raise OSError("evidence must be a regular single-link file on the root's mount")
            if before.st_size > READ_LIMIT:
                raise ValueError("evidence file is too large")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(fd, min(65536, READ_LIMIT + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > READ_LIMIT:
                    raise ValueError("evidence file is too large")
            after = os.fstat(fd)
            if _fixed(before) != _fixed(after):
                raise OSError("evidence changed during the read")
            if dirfd.posix_available():
                live = os.stat(name, dir_fd=current, follow_symlinks=False)
                if (live.st_dev, live.st_ino) != (after.st_dev, after.st_ino):
                    raise OSError("evidence was replaced during the read")
            return b"".join(chunks), after
        finally:
            os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


# --- Archive ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceiptRecord:
    """What the evidence archive recorded when it imported one verify receipt."""

    project: str
    path: str
    digest: str
    digest_source: str


@dataclass(frozen=True)
class Bundle:
    """The eligible, owned sources of one bundle, and a reason code for every source dropped."""

    texts: tuple[str, ...]
    observed_ms: int | None = None
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class Excerpt:
    excerpt_id: str
    text: str
    observed_ms: int | None = None


class Archive(Protocol):
    def receipt_records(self, project: str, run_id: str, path: str) -> Sequence[ReceiptRecord] | None: ...

    def bundle(self, bundle_id: str, projects: tuple[str, ...]) -> Bundle | None: ...

    def search(self, project: str, query: str, limit: int) -> Sequence[Excerpt] | None: ...


def _moment_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class EngineArchive:
    """The local evidence archive through its engine. Every failure reads as absent.

    Related excerpts use `search --project`. A bundle's project is the single
    project filter its engine recorded. A receipt record is the archive item
    for that run, read with `show`, and must name the same project and path.

    `runner(verb, arguments)` runs one engine verb and returns its decoded JSON
    reply, or None on any failure. The code that wires the pass supplies it, so
    this module never resolves or names an engine executable.
    """

    def __init__(self, runner: Callable[[str, list[str]], Any]) -> None:
        self._runner = runner

    def _run(self, verb: str, arguments: list[str]) -> Any:
        try:
            return self._runner(verb, arguments)
        except (OSError, ValueError):
            return None

    def search(self, project: str, query: str, limit: int) -> list[Excerpt] | None:
        payload = self._run("search", [query, "--project", project, "--limit", str(limit), "--json"])
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return None
        found = []
        for row in results[:limit]:
            if not isinstance(row, dict) or row.get("integrity_mismatch") is not False:
                continue
            excerpt_id, snippet = row.get("id"), row.get("snippet")
            if isinstance(excerpt_id, str) and RUN_ID.fullmatch(excerpt_id) and isinstance(snippet, str):
                found.append(Excerpt(excerpt_id, snippet, _moment_ms(row.get("created_at"))))
        return found

    def bundle(self, bundle_id: str, projects: tuple[str, ...]) -> Bundle | None:
        """A bundle's sources, only through the engine's regenerated, eligibility-filtered reply.

        The engine's cache file is never read: it can hold bodies the public
        reply suppresses, and reading it skips the engine's MAC check and its
        revalidation of every source. `evidence show` regenerates the bundle
        from current sources and collapses each ineligible source to a stub
        with a reason code. Every stub is dropped with that code. Each eligible
        source must carry a literal `integrity_mismatch: false` and an eligible
        label from the engine's closed set. Read again with `show`, it must be
        live, repeat that label, report a clean injection status and carry its
        body, and have exactly one of `projects` as its project. The text used
        is the body from that reply, so the text and its current eligibility
        come from one response. The public reply does not carry the
        bundle's project filter, so ownership is checked per source. Anything
        else is dropped with a code. A malformed reply makes the bundle
        unreachable.
        """
        payload = self._run("evidence", ["show", bundle_id, "--json"])
        if not isinstance(payload, dict) or payload.get("id") != bundle_id:
            return None
        results = payload.get("results")
        if not isinstance(results, list) or len(results) > MAX_BUNDLE_SOURCES:
            return None
        texts: list[str] = []
        dropped: list[str] = []
        for row in results:
            if not isinstance(row, dict):
                return None
            if "eligibility_status" in row:
                reason = row.get("reason_code")
                known = row.get("eligibility_status") == "ineligible" and reason in INELIGIBLE
                dropped.append(str(reason) if known else "ineligible")
                continue
            item_id, snippet = row.get("id"), row.get("snippet")
            if not isinstance(item_id, str) or ITEM_ID.fullmatch(item_id) is None or not isinstance(snippet, str):
                return None
            if row.get("integrity_mismatch") is not False:
                dropped.append("integrity_mismatch")
                continue
            label = row.get("trust_label")
            refused = _label_refusal(label)
            if refused:
                dropped.append(refused)
                continue
            shown = self._run("show", [item_id, "--json"])
            if not (
                isinstance(shown, dict)
                and shown.get("id") == item_id
                and shown.get("live") is True
                and shown.get("tombstoned") is False
                and shown.get("integrity_mismatch") is False
            ):
                dropped.append("source_unverified")
                continue
            # The label and injection state now, from the same reply as the text used.
            refused = _shown_refusal(shown, label)
            if refused:
                dropped.append(refused)
                continue
            meta = shown.get("metadata")
            if not isinstance(meta, dict) or meta.get("project") not in projects:
                dropped.append("source_unowned")
                continue
            texts.append(shown["text"])
        observed = _moment_ms(payload.get("regenerated_at")) or _moment_ms(payload.get("generated_at"))
        return Bundle(tuple(texts), observed, tuple(dropped))

    def receipt_records(self, project: str, run_id: str, path: str) -> list[ReceiptRecord] | None:
        """Archive records of one verify receipt, or None when any reply is malformed or ambiguous.

        The engine has no typed lookup by project, external id and path, so the
        search is narrowed by every filter it offers: project, source,
        collection and item kind. Each hit must carry those identities exactly
        and a literal `integrity_mismatch: false`. Each hit is then read with
        `show`, and the shown record must repeat the hit's id, source,
        collection, kind and integrity flag. A record for this run must also
        name this project and path exactly, with a well formed digest and a
        known digest source. Any omitted, mistyped or contradicting field fails
        the whole lookup, so the receipt stays medium.
        """
        arguments = [run_id, "--project", project, "--source", RECEIPT_SOURCE["kind"]]
        arguments += [
            "--collection",
            RECEIPT_COLLECTION["external_id"],
            "--kind",
            RECEIPT_KIND,
            "--limit",
            "20",
            "--json",
        ]
        payload = self._run("search", arguments)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) > 20:
            return None
        external_id = f"brigade:work-verify:{run_id}"
        records = []
        for hit in results:
            if not _receipt_hit(hit):
                return None
            item = self._run("show", [hit["id"], "--json"])
            if not _receipt_shown(item, hit):
                return None
            if item["external_id"] != external_id:
                continue  # Another receipt whose text mentions this run id.
            record = _receipt_metadata(item["metadata"], project, run_id, path)
            if record is None:
                return None
            records.append(record)
        return records


RECEIPT_SOURCE = {"kind": "brigade", "name": "Brigade"}
RECEIPT_COLLECTION = {
    "external_id": "brigade:work:verify-runs",
    "kind": "brigade_work_verify_runs",
    "name": "Brigade work verify runs",
}
RECEIPT_KIND = "brigade_work_verify_receipt"
DIGEST_SOURCES = frozenset({"receipt_digest", "file_sha256"})
# The engine's reason codes for a source it refuses to show. Any other code is recorded as `ineligible`.
INELIGIBLE = frozenset(
    {
        "parse_error",
        "integrity_mismatch",
        "legacy_unknown",
        "trust_unknown",
        "trust_quarantined",
        "injection_not_clean",
        "source_missing",
    }
)
# The evidence engine's closed set of trust labels, and the ones whose content it treats as eligible.
TRUST_LABELS = frozenset({"unknown", "untrusted", "reviewed", "verified", "quarantined"})
ELIGIBLE_LABELS = frozenset({"untrusted", "reviewed", "verified"})


def _label_refusal(label: object) -> str | None:
    """A drop reason for a label outside the engine's set or one whose content it refuses, else None."""
    if not isinstance(label, str) or label not in TRUST_LABELS:
        return "trust_label_invalid"
    if label == "quarantined":
        return "trust_quarantined"
    if label not in ELIGIBLE_LABELS:
        return "trust_unknown"
    return None


def _shown_refusal(shown: Mapping[str, Any], label: object) -> str | None:
    """Why the per-source reply cannot vouch for its text now, else None.

    The shown label must be in the closed set, eligible, and equal to the
    bundle row's label, both at the top level and in the provenance envelope.
    The envelope's injection status must be `clean`. The body must be present,
    which the engine only allows for content it still considers eligible.
    """
    current = shown.get("trust_label")
    refused = _label_refusal(current)
    if refused:
        return refused
    envelope = shown.get("provenance")
    trust = envelope.get("trust") if isinstance(envelope, dict) else None
    injection = trust.get("injection") if isinstance(trust, dict) else None
    if not isinstance(trust, dict) or trust.get("label") != current or current != label:
        return "trust_label_changed"
    if not isinstance(injection, dict) or injection.get("status") != "clean":
        return "injection_not_clean"
    if (
        shown.get("untrusted_body_omitted")
        or shown.get("integrity_body_omitted")
        or not isinstance(shown.get("text"), str)
    ):
        return "source_body_withheld"
    return None


MAX_BUNDLE_SOURCES = 200
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _receipt_hit(hit: object) -> bool:
    """A search hit that names exactly the receipt source, collection and kind, with no integrity mismatch."""
    return (
        isinstance(hit, dict)
        and isinstance(hit.get("id"), str)
        and ITEM_ID.fullmatch(hit["id"]) is not None
        and hit.get("integrity_mismatch") is False
        and hit.get("source_kind") == RECEIPT_SOURCE["kind"]
        and hit.get("collection_name") == RECEIPT_COLLECTION["name"]
        and hit.get("collection_kind") == RECEIPT_COLLECTION["kind"]
        and hit.get("kind") == RECEIPT_KIND
    )


def _receipt_shown(item: object, hit: Mapping[str, Any]) -> bool:
    """The shown record is the same live record as the search hit, with exact identities."""
    if not isinstance(item, dict):
        return False
    source, collection = item.get("source"), item.get("collection")
    return (
        item.get("id") == hit["id"]
        and isinstance(item.get("external_id"), str)
        and item.get("kind") == hit["kind"]
        and item.get("live") is True
        and item.get("tombstoned") is False
        and item.get("integrity_mismatch") is False
        and isinstance(source, dict)
        and {key: source.get(key) for key in RECEIPT_SOURCE} == RECEIPT_SOURCE
        and source.get("kind") == hit["source_kind"]
        and isinstance(collection, dict)
        and {key: collection.get(key) for key in RECEIPT_COLLECTION} == RECEIPT_COLLECTION
        and collection.get("name") == hit["collection_name"]
        and collection.get("kind") == hit["collection_kind"]
        and isinstance(item.get("metadata"), dict)
    )


def _receipt_metadata(meta: Mapping[str, Any], project: str, run_id: str, path: str) -> ReceiptRecord | None:
    """The record's provenance, only when every field is present, typed and equal to what was asked."""
    digest, source = meta.get("digest"), meta.get("digest_source")
    if (
        meta.get("receipt_type") != "work_verify"
        or meta.get("run_id") != run_id
        or meta.get("project") != project
        or meta.get("path") != path
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or DIGEST.fullmatch(digest.removeprefix("sha256:")) is None
        or source not in DIGEST_SOURCES
    ):
        return None
    return ReceiptRecord(project, path, digest, source)


# --- Items --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    kind: str
    rank: str
    text: str
    sha256: str
    # Identity of the producing file for the independence test, never shown to a model.
    source: str
    run_id: str | None
    observed_ms: int | None
    compare: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class Flag:
    """A dropped item: where it came from (`cited` index or `excerpt` index), its kind and rule ids."""

    origin: str
    index: int
    kind: str
    rules: tuple[str, ...]


def cut(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes of `text`, never ending inside a multi-byte sequence."""
    data = text.encode()
    if len(data) <= limit:
        return text
    end = max(0, limit)
    # data[end] is the first byte left out. A continuation byte there means the
    # sequence straddles the cut, so back off to its lead byte and drop it too.
    while end > 0 and data[end] & 0xC0 == 0x80:
        end -= 1
    return data[:end].decode()


def _rendered(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1)


def _item(kind: str, rank: str, text: str, source: str, run_id: str | None, observed: int | None, raw: bytes) -> Item:
    return Item(
        kind=kind,
        rank=rank,
        text=text,
        sha256=io.sha256(text.encode()),
        source=source,
        run_id=run_id,
        observed_ms=observed,
        compare=raw + b"\n" + text.encode(),
    )


AUTH_TOP = ("run_id", "status", "started_at", "completed_at", "tree_fingerprint", "baseline_commit", "manifest_id")
AUTH_COMMAND = ("argv", "exit_code", "status", "started_at", "completed_at")
OUTPUT_TOP = ("summary", "note", "notes")
OUTPUT_COMMAND = ("stdout_summary", "stderr_summary")


def _scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def split_receipt(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The fields Brigade itself writes into a verify receipt, and its command output, as two objects.

    Only fields named here cross into the authenticated object. Unknown fields
    are left out of both, so a new receipt field never becomes authenticated
    by accident.
    """
    fields: dict[str, Any] = {key: payload[key] for key in AUTH_TOP if key in payload and _scalar(payload[key])}
    git = payload.get("git")
    if isinstance(git, dict) and isinstance(git.get("head"), str):
        fields["commit"] = git["head"]
    output: dict[str, Any] = {key: payload[key] for key in OUTPUT_TOP if isinstance(payload.get(key), str)}
    commands = payload.get("commands")
    if isinstance(commands, list):
        auth_commands, out_commands = [], []
        for command in commands:
            if not isinstance(command, dict):
                continue
            entry: dict[str, Any] = {}
            for key in AUTH_COMMAND:
                value = command.get(key)
                if key == "argv":
                    if isinstance(value, list) and all(isinstance(part, str) for part in value):
                        entry[key] = value
                elif key in command and _scalar(value):
                    entry[key] = value
            auth_commands.append(entry)
            out_commands.append({key: command[key] for key in OUTPUT_COMMAND if isinstance(command.get(key), str)})
        fields["commands"] = auth_commands
        if any(out_commands):
            output["commands"] = out_commands
    return fields, output


def receipt_authenticated(raw: bytes, payload: Mapping[str, Any], record: ReceiptRecord | None) -> bool:
    """Whether these exact bytes match the digest the archive recorded for the receipt.

    The archive records either the sha256 of the file bytes or the receipt's own
    canonical digest. For the second, the bytes read now must carry that digest
    and must still hash to it.
    """
    if record is None:
        return False
    want = record.digest.removeprefix("sha256:")
    if not DIGEST.fullmatch(want):
        return False
    if record.digest_source == "file_sha256":
        return hmac.compare_digest(io.sha256(raw), want)
    if record.digest_source == "receipt_digest":
        digests = payload.get("digests")
        declared = digests.get("receipt_sha256") if isinstance(digests, dict) else None
        if not isinstance(declared, str) or declared.removeprefix("sha256:") != want:
            return False
        actual = localio.canonical_json_digest(dict(payload), exclude_keys={"digests"})
        return hmac.compare_digest(actual, want)
    return False


@dataclass(frozen=True)
class _Prints:
    positions: dict[int, list[int]]
    saturated: bool


@functools.lru_cache(maxsize=16)
def _fingerprints(data: bytes, span: int) -> _Prints:
    """Winnowing fingerprints: in every window of `span - k + 1` consecutive k-grams, the rightmost minimal hash.

    With k = span // 4, two inputs sharing a run of `span` bytes contain one
    identical window of k-gram hashes, so both select the same hash at the
    same offset inside the run. One pass over the input, O(n) time. Past
    MAX_PRINT_POSITIONS selected positions (low-entropy input selects many) the
    set is marked saturated and any shared hash counts as copied.
    """
    k = max(1, span // 4)
    w = span - k + 1
    positions: dict[int, list[int]] = {}
    window: deque[tuple[int, int]] = deque()
    last = -1
    stored = 0
    for i in range(len(data) - k + 1):
        value = hash(data[i : i + k])
        while window and window[-1][0] >= value:
            window.pop()
        window.append((value, i))
        if window[0][1] <= i - w:
            window.popleft()
        if i >= w - 1 and window[0][1] != last:
            value, last = window[0]
            positions.setdefault(value, []).append(last)
            stored += 1
            if stored > MAX_PRINT_POSITIONS:
                return _Prints(positions, True)
    return _Prints(positions, False)


def _run_length(a: bytes, b: bytes, i: int, j: int, span: int) -> int:
    """Bytes shared around a[i] and b[j], back plus ahead, each side capped at `span`.

    Each side is found by binary search over slice equality, which holds for
    every length up to the true run, so a check costs O(log span) comparisons.
    """
    low, high = 0, min(i, j, span)
    while low < high:
        middle = (low + high + 1) // 2
        if a[i - middle : i] == b[j - middle : j]:
            low = middle
        else:
            high = middle - 1
    back = low
    low, high = 0, min(len(a) - i, len(b) - j, span)
    while low < high:
        middle = (low + high + 1) // 2
        if a[i : i + middle] == b[j : j + middle]:
            low = middle
        else:
            high = middle - 1
    return back + low


def shares_span(a: bytes, b: bytes, span: int = COPY_SPAN) -> bool:
    """Whether `a` and `b` share a run of at least `span` identical bytes, over every byte of both.

    Each input is fingerprinted once (see `_fingerprints`). Every position pair
    under a shared fingerprint is checked with byte comparisons, extending back
    and ahead by binary search, so a hash collision never counts. The cost is
    one O(n) pass per input of up to READ_LIMIT plus ITEM_BYTES bytes, and at
    most COPY_PROBES pair checks of O(log span) slice comparisons. Past either
    cap (probes or saturation) the pair counts as copied.
    """
    if min(len(a), len(b)) < span:
        return False
    first, second = _fingerprints(a, span), _fingerprints(b, span)
    # A saturated input stopped fingerprinting early, so a copied run after the cap would
    # leave no shared fingerprint. Saturation alone therefore counts as copied.
    if first.saturated or second.saturated:
        return True
    shared = first.positions.keys() & second.positions.keys()
    k = max(1, span // 4)
    probes = 0
    for value in shared:
        for i in first.positions[value]:
            for j in second.positions[value]:
                probes += 1
                if probes > COPY_PROBES:
                    return True
                if a[i : i + k] != b[j : j + k]:
                    continue
                if _run_length(a, b, i, j, span) >= span:
                    return True
    return False


def independent(first: Item, second: Item) -> bool:
    """Different files, different run ids, and no copied span of COPY_SPAN bytes either way."""
    if first.source == second.source:
        return False
    if first.run_id is not None and first.run_id == second.run_id:
        return False
    return not shares_span(first.compare, second.compare)


def corroborated(items: Sequence[Item]) -> bool:
    """At least two independently produced items, one with authenticated receipt fields.

    Command output and excerpts never count toward the pair.
    """
    anchors = [item for item in items if item.rank == AUTHENTICATED and item.kind == RECEIPT_FIELDS]
    others = [item for item in items if item.kind not in CONTEXT_ONLY]
    return any(independent(anchor, other) for anchor in anchors for other in others if other is not anchor)


# --- Assembly -----------------------------------------------------------------------------


@dataclass
class Assembly:
    card_id: str
    scope: str
    items: list[Item] = field(default_factory=list)
    excerpts: list[Item] = field(default_factory=list)
    flagged: list[Flag] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    abstain: str | None = None

    def count(self, reason: str, amount: int = 1) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + amount

    def evidence(self) -> select.Evidence | None:
        """The digest and newest UTC day of the cited items, for selection. None without any."""
        if not self.items:
            return None
        digest = io.sha256(io.json_bytes(sorted([item.kind, item.sha256] for item in self.items)))
        days = [item.observed_ms // select.DAY_MS for item in self.items if item.observed_ms is not None]
        return select.Evidence(digest=digest, newest_day=max(days) if days else 0)

    def report(self) -> dict[str, Any]:
        """Codes, counts, kinds, ranks, digests and rule ids. Never evidence text or paths."""
        return {
            "card_id": self.card_id,
            "abstain": self.abstain,
            "counts": dict(sorted(self.counts.items())),
            "items": [{"kind": i.kind, "rank": i.rank, "sha256": i.sha256} for i in self.items],
            "excerpts": [{"kind": i.kind, "rank": i.rank, "sha256": i.sha256} for i in self.excerpts],
            "flagged": [
                {"origin": f.origin, "index": f.index, "kind": f.kind, "rules": list(f.rules)} for f in self.flagged
            ],
        }


def _ref(value: str) -> str:
    return value.removeprefix("receipt:").split("#", 1)[0].strip()


def _evidence_root(rel: str) -> str | None:
    return next((root for root in candidates.EVIDENCE_ROOTS if rel.startswith(root + "/")), None)


def _locate(roots: Roots, scope: str, rel: str) -> tuple[HeldRoot, bytes, os.stat_result] | None:
    """The single root of the scope that holds `rel`. None when it is under none, several, or an unsafe one."""
    found: list[tuple[HeldRoot, bytes, os.stat_result]] = []
    for held in roots.roots(scope):
        if held is None:
            # A root that failed its identity check cannot prove the path is absent there.
            return None
        try:
            data, metadata = read_under(held, rel)
        except _Absent:
            continue
        except (OSError, ValueError):
            return None
        found.append((held, data, metadata))
    return found[0] if len(found) == 1 else None


def _record(archive: Archive, projects: tuple[str, ...], run_id: str, rel: str) -> ReceiptRecord | None:
    """The one archive record of this receipt under the scope's projects, else None."""
    records: list[ReceiptRecord] = []
    for project in projects:
        found = archive.receipt_records(project, run_id, rel)
        # A record that names another project or path than asked is a contradiction, not noise.
        if found is None or any(record.project != project or record.path != rel for record in found):
            return None
        records.extend(found)
    # Re-imports of one receipt agree. Two projects or two digests make the record ambiguous.
    if len({(record.project, record.digest, record.digest_source) for record in records}) != 1:
        return None
    return records[0]


def _file_items(
    archive: Archive, projects: tuple[str, ...], held: HeldRoot, rel: str, raw: bytes, metadata: os.stat_result
) -> list[Item] | None:
    text = raw.decode("utf-8")
    source = f"file:{held.workspace.identity[0]}:{held.workspace.identity[1]}:{rel}"
    observed = metadata.st_mtime_ns // 1_000_000
    root = _evidence_root(rel)
    run = RUN_DIR.fullmatch(rel)
    if run and run[2] == "receipt.json":
        payload = _json(text)
        if isinstance(payload, dict):
            run_id = payload.get("run_id")
            if run_id != run[1]:
                return None
            record = _record(archive, projects, run_id, rel)
            rank = AUTHENTICATED if receipt_authenticated(raw, payload, record) else MEDIUM
            fields, output = split_receipt(payload)
            observed = _moment_ms(payload.get("completed_at")) or observed
            items = [_item(RECEIPT_FIELDS, rank, _rendered(fields), source, run_id, observed, raw)]
            if output:
                items.append(_item(RECEIPT_OUTPUT, MEDIUM, _rendered(output), source, run_id, observed, raw))
            return items
    if run:
        return [_item(RECEIPT_OUTPUT, MEDIUM, text, source, run[1], observed, raw)]
    kind = HANDOFF if root in HANDOFF_ROOTS else EVIDENCE_FILE
    run_id = None
    payload = _json(text) if rel.endswith(".json") else None
    if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) and RUN_ID.fullmatch(payload["run_id"]):
        run_id = payload["run_id"]
    return [_item(kind, MEDIUM, text, source, run_id, observed, raw)]


# Bundle locators: the full `<engine>://evidence/<id>` form and the short `<engine>:<id>` form, as
# card evidence front matter writes them. Parsed here without touching the engine's cache location.
LOCATOR = memory_cmd.EVIDENCE_LOCATOR_PREFIX
SHORT_LOCATOR = LOCATOR.split("://", 1)[0] + ":"
BUNDLE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def bundle_ref(ref: str) -> str | None:
    """The bundle id a ref names, or None when it names no bundle."""
    for prefix in (LOCATOR, SHORT_LOCATOR):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix).strip()
    return None


def _attested(ref: str) -> bool:
    """An S19e-1 attested id, `<principal>/<workspace>/<local>`, whose bytes have no hub read action."""
    parts = ref.split("/")
    return len(parts) == 3 and ":" in parts[0] and all(parts)


def _cited(
    roots: Roots, archive: Archive, scope: str, ref: str, projects: tuple[str, ...], assembly: Assembly
) -> tuple[list[Item] | None, bool]:
    """Items for one cited ref, or None when unreachable. The flag says whether it was an attested id."""
    if _attested(ref):
        return None, True
    bundle_id = bundle_ref(ref)
    if bundle_id is not None:
        if not BUNDLE_ID.fullmatch(bundle_id):
            return None, False
        if not projects:
            return None, False
        bundle = archive.bundle(bundle_id, projects)
        if bundle is None:
            return None, False
        for reason in bundle.dropped:
            assembly.count(f"bundle_source_{reason}")
        # Only eligible sources the scope owns remain. None left means nothing to read.
        if not bundle.texts:
            return None, False
        text = "\n".join(bundle.texts)
        raw = text.encode()
        return [_item(EVIDENCE_BUNDLE, MEDIUM, text, f"bundle:{bundle_id}", None, bundle.observed_ms, raw)], False
    try:
        rel = io.relative(ref)
    except ValueError:
        return None, False
    if _evidence_root(rel) is None:
        return None, False
    located = _locate(roots, scope, rel)
    if located is None:
        return None, False
    held, raw, metadata = located
    try:
        return _file_items(archive, projects, held, rel, raw, metadata), False
    except UnicodeDecodeError:
        return None, False


def _scan(full: str, final: str) -> tuple[str, ...] | None:
    """Rule ids when the item must be dropped, else None. Private text may reach a local prompt."""
    verdict, rules = scan.scan_verdict([full, final])
    return rules if verdict in (scan.BLOCKED, scan.FLAGGED) else None


def card_parts(text: str) -> tuple[list[str], str, list[str]]:
    """The card's cited refs in order, its title, and its claim block texts without anchors."""
    normalized = text.replace("\r\n", "\n")
    meta, valid = memory_cmd._parse_frontmatter(normalized)
    refs = memory_cmd._evidence_refs(meta) if valid else []
    title = meta.get("title") if valid and isinstance(meta.get("title"), str) else ""
    if not title:
        heading = re.search(r"^# (.+)$", normalized, re.MULTILINE)
        title = heading[1] if heading else ""
    try:
        blocks = [ANCHOR.sub("", block.text) for block in delta.blocks(normalized)]
    except ValueError:
        blocks = []
    return refs, str(title), blocks


def query(title: str, blocks: Sequence[str]) -> str:
    """A deterministic excerpt query: title terms, then the card's rarest block terms.

    Terms are ASCII words of four to 32 characters, lowercased, without stop
    words, so the query never carries punctuation, paths or a leading dash.
    """
    title_terms = [term for term in (t.lower() for t in TERM.findall(title)) if term not in STOP]
    counts: dict[str, int] = {}
    for block in blocks:
        for term in TERM.findall(block):
            lowered = term.lower()
            if lowered not in STOP:
                counts[lowered] = counts.get(lowered, 0) + 1
    rare = sorted(counts, key=lambda term: (counts[term], -len(term), term))
    chosen = list(dict.fromkeys(title_terms[:QUERY_TITLE_TERMS]))
    chosen.extend(term for term in rare if term not in chosen and len(chosen) < QUERY_TITLE_TERMS + QUERY_RARE_TERMS)
    return " ".join(chosen)


def _excerpts(assembly: Assembly, archive: Archive, projects: tuple[str, ...], text: str, limit: int) -> None:
    if limit <= 0 or not projects:
        return
    _refs, title, blocks = card_parts(text)
    terms = query(title, blocks)
    if not terms:
        return
    pooled: list[Excerpt] = []
    for project in projects:
        found = archive.search(project, terms, limit)
        if found is None:
            assembly.count("excerpts_unavailable")
            continue
        pooled.extend(found)
    seen: set[str] = set()
    index = 0
    for excerpt in pooled:
        if excerpt.excerpt_id in seen or len(assembly.excerpts) >= limit:
            continue
        seen.add(excerpt.excerpt_id)
        final = cut(excerpt.text, EXCERPT_BYTES)
        rules = _scan(excerpt.text, final)
        if rules is not None:
            assembly.flagged.append(Flag("excerpt", index, EXCERPT, rules))
            assembly.count(FLAGGED)
        else:
            raw = excerpt.text.encode()
            item = _item(EXCERPT, LOW, final, f"excerpt:{excerpt.excerpt_id}", None, excerpt.observed_ms, raw)
            assembly.excerpts.append(item)
        index += 1


def assemble(
    head: Mapping[str, Any],
    roots: Roots,
    archive: Archive,
    *,
    card_bytes: int = DEFAULT_CARD_BYTES,
    excerpts: int = MAX_EXCERPTS,
) -> Assembly:
    """Every input a card's model call may see, or the reason it gets none.

    `head` is a replica head: `scope`, `card_id` and `text`. The returned
    assembly's `abstain` is one of the four pre-call reason codes, or None when
    the evidence meets the corroboration rule and a call may be made.
    """
    if type(card_bytes) is not int or not MIN_CARD_BYTES <= card_bytes <= MAX_CARD_BYTES:
        raise ValueError(f"cited evidence per card must be from {MIN_CARD_BYTES} to {MAX_CARD_BYTES} bytes")
    if type(excerpts) is not int or not 0 <= excerpts <= MAX_EXCERPTS:
        raise ValueError(f"related excerpts per card must be from 0 to {MAX_EXCERPTS}")
    scope, card_id, text = str(head.get("scope")), str(head.get("card_id")), head.get("text")
    assembly = Assembly(card_id=card_id, scope=scope)
    if not isinstance(text, str):
        assembly.abstain = NO_EVIDENCE
        return assembly
    refs = list(dict.fromkeys(ref for ref in (_ref(value) for value in card_parts(text)[0]) if ref))
    if not refs:
        assembly.abstain = NO_EVIDENCE
        return assembly
    if not roots.bound(scope):
        assembly.count(UNREACHABLE, len(refs))
        assembly.abstain = UNREACHABLE
        return assembly
    projects = roots.projects(scope)
    budget = card_bytes
    flagged_any = False
    for index, ref in enumerate(refs):
        if index >= MAX_ITEMS:
            assembly.count("evidence_over_limit", len(refs) - MAX_ITEMS)
            break
        found, attested = _cited(roots, archive, scope, ref, projects, assembly)
        if found is None:
            assembly.count(UNREACHABLE)
            if attested:
                assembly.count("attested_unreachable")
            continue
        for item in found:
            if len(assembly.items) >= MAX_ITEMS:
                assembly.count("evidence_over_limit")
                continue
            if budget <= 0:
                assembly.count("evidence_over_budget")
                continue
            final = cut(cut(item.text, ITEM_BYTES), budget)
            rules = _scan(item.text, final)
            if rules is not None:
                assembly.flagged.append(Flag("cited", index, item.kind, rules))
                assembly.count(FLAGGED)
                flagged_any = True
                continue
            if final != item.text:
                assembly.count("evidence_cut")
                item = replace(item, text=final, sha256=io.sha256(final.encode()))
            budget -= len(final.encode())
            assembly.items.append(item)
    if not assembly.items:
        assembly.abstain = FLAGGED if flagged_any else UNREACHABLE
    elif not corroborated(assembly.items):
        assembly.abstain = UNCORROBORATED
    else:
        # Excerpts never change the decision, so a card that abstains costs no archive search.
        _excerpts(assembly, archive, projects, text, excerpts)
    return assembly


def assemble_all(
    heads: Sequence[Mapping[str, Any]],
    config: vconfig.VerifyConfig,
    archive: Archive,
    *,
    card_bytes: int = DEFAULT_CARD_BYTES,
    excerpts: int = MAX_EXCERPTS,
) -> Iterator[Assembly]:
    """Assemble each card while every bound root stays held, in input order."""
    with Roots(config) as roots:
        for head in heads:
            yield assemble(head, roots, archive, card_bytes=card_bytes, excerpts=excerpts)
