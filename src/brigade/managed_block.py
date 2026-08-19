"""Hash-stamped managed instruction blocks for foreign harness files.

Brigade injects guidance into files it does not own. Every managed span is
framed by a marker pair that records marker-format version, instruction
profile, and the full SHA-256 of the body so install/check/remove can share
one honest parser:

    <!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:<sha256> -->
    ...body...
    <!-- END BRIGADE INTEGRATION -->

Statuses compare three digests when a desired body is supplied:

- recorded: hash embedded in the begin marker (absent on legacy markers)
- actual: hash of the bytes currently between the markers
- desired: hash of a freshly rendered body

``current`` requires recorded == actual == desired with stamped v1 markers.
``stale`` means the installed span still matches its recorded hash (or is a
clean legacy span Brigade may upgrade) but differs from desired.
``locally_modified`` means the body no longer matches the recorded hash, or a
legacy/unowned span does not match desired.
``malformed`` covers duplicate, orphan, nested, or unparseable markers.

Writes use an atomic same-directory replace and refuse to follow a symlinked
final path component (including a symlink swapped in after the initial
``lstat``) or a parent directory swapped for a symlink. Unchanged content is
a true no-op: no write, no mtime churn.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

MarkerStyle = Literal["html", "hash"]
MARKER_STYLE_HTML: MarkerStyle = "html"
MARKER_STYLE_HASH: MarkerStyle = "hash"

MARKER_FORMAT_VERSION = 1
DEFAULT_KIND = "INTEGRATION"
DEFAULT_PROFILE = "full"
HASH_ABBREV_LEN = 8

# Legacy user-profile markers (issue #438) — recognized and upgraded in place.
LEGACY_USER_PROFILE_START = "<!-- brigade:user-profile:start -->"
LEGACY_USER_PROFILE_END = "<!-- brigade:user-profile:end -->"

_BEGIN_RE = re.compile(
    r"<!--\s*BEGIN BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s+"
    r"v:(\d+)\s+profile:([^\s]+)\s+hash:([0-9a-fA-F]+)\s*-->"
)
_END_RE = re.compile(r"<!--\s*END BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s*-->")
_BEGIN_LOOSE_RE = re.compile(r"<!--\s*BEGIN BRIGADE\b")
_END_LOOSE_RE = re.compile(r"<!--\s*END BRIGADE\b")
_HASH_BEGIN_RE = re.compile(
    r"^#\s*BEGIN BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s+"
    r"v:(\d+)\s+profile:([^\s]+)\s+hash:([0-9a-fA-F]+)\s*$",
    re.MULTILINE,
)
_HASH_END_RE = re.compile(r"^#\s*END BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s*$", re.MULTILINE)
_HASH_BEGIN_LOOSE_RE = re.compile(r"^#\s*BEGIN BRIGADE\b", re.MULTILINE)
_HASH_END_LOOSE_RE = re.compile(r"^#\s*END BRIGADE\b", re.MULTILINE)

STATUS_MISSING = "missing"
STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_LOCALLY_MODIFIED = "locally_modified"
STATUS_MALFORMED = "malformed"

ACTION_NONE = "none"
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_REMOVE = "remove"
ACTION_PRESERVE = "preserve"

WRITE_WRITTEN = "written"
WRITE_NOOP = "noop"
WRITE_SKIPPED_SYMLINK = "skipped_symlink"
WRITE_REFUSED = "refused"
WRITE_ERROR = "error"


@dataclass(frozen=True)
class BlockMeta:
    kind: str
    version: int | None
    profile: str | None
    recorded_hash: str | None
    legacy: bool
    style: MarkerStyle = MARKER_STYLE_HTML


@dataclass(frozen=True)
class ParsedBlock:
    """Structured parse of one managed-block kind inside a file's text."""

    status: str  # ok | missing | malformed
    kind: str
    before: str = ""
    body: str = ""
    after: str = ""
    meta: BlockMeta | None = None
    actual_hash: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class BlockAssessment:
    status: str
    kind: str
    parsed: ParsedBlock
    desired_hash: str | None = None
    recorded_hash: str | None = None
    actual_hash: str | None = None
    profile: str | None = None
    legacy: bool = False
    fix_command: str | None = None
    detail: str | None = None

    @property
    def abbreviated_hash(self) -> str | None:
        digest = self.actual_hash or self.recorded_hash or self.desired_hash
        return abbreviate_hash(digest) if digest else None


@dataclass(frozen=True)
class BlockPlan:
    status: str
    action: str
    kind: str
    rendered: str | None = None
    desired_hash: str | None = None
    detail: str | None = None
    fix_command: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class WriteOutcome:
    status: str
    detail: str | None = None
    warning: str | None = None


def body_hash(body: str) -> str:
    """Full SHA-256 hex digest of a managed body (UTF-8)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def abbreviate_hash(digest: str) -> str:
    """Shorten a digest for status output; stored markers keep the full value."""
    return digest[:HASH_ABBREV_LEN]


def normalize_newlines(text: str) -> str:
    """Define CRLF behavior: hashes and markers operate on LF-normalized text."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_offset_starts(text: str) -> list[int]:
    """Map each normalized-text index to its start offset in ``text``."""
    starts: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        starts.append(index)
        if text.startswith("\r\n", index):
            index += 2
        elif text[index] == "\r":
            index += 1
        else:
            index += 1
    starts.append(length)
    return starts


def _slice_by_normalized_range(text: str, offsets: list[int], start: int, end: int) -> str:
    return text[offsets[start] : offsets[end]]


def _file_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _with_file_newlines(block: str, newline: str) -> str:
    if newline == "\n":
        return block
    return block.replace("\n", "\r\n")


def begin_marker(
    *,
    kind: str,
    profile: str,
    digest: str,
    version: int = MARKER_FORMAT_VERSION,
    style: MarkerStyle = MARKER_STYLE_HTML,
) -> str:
    if version != MARKER_FORMAT_VERSION:
        raise ValueError(f"unsupported managed-block marker version: {version}")
    if style not in {MARKER_STYLE_HTML, MARKER_STYLE_HASH}:
        raise ValueError(f"unsupported managed-block marker style: {style}")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", kind):
        raise ValueError(f"invalid managed-block kind: {kind!r}")
    if any(ch.isspace() for ch in profile) or not profile:
        raise ValueError(f"invalid managed-block profile: {profile!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("managed-block hash must be a full lowercase sha256 hex digest")
    if style == MARKER_STYLE_HASH:
        return f"# BEGIN BRIGADE {kind} v:{version} profile:{profile} hash:{digest}"
    return f"<!-- BEGIN BRIGADE {kind} v:{version} profile:{profile} hash:{digest} -->"


def end_marker(*, kind: str, style: MarkerStyle = MARKER_STYLE_HTML) -> str:
    if style not in {MARKER_STYLE_HTML, MARKER_STYLE_HASH}:
        raise ValueError(f"unsupported managed-block marker style: {style}")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", kind):
        raise ValueError(f"invalid managed-block kind: {kind!r}")
    if style == MARKER_STYLE_HASH:
        return f"# END BRIGADE {kind}"
    return f"<!-- END BRIGADE {kind} -->"


def render_block(
    body: str,
    *,
    kind: str = DEFAULT_KIND,
    profile: str = DEFAULT_PROFILE,
    style: MarkerStyle = MARKER_STYLE_HTML,
) -> str:
    """Render a stamped block including the trailing newline after the end marker."""
    digest = body_hash(body)
    return (
        f"{begin_marker(kind=kind, profile=profile, digest=digest, style=style)}\n"
        f"{body}\n"
        f"{end_marker(kind=kind, style=style)}\n"
    )


def default_fix_command(*, harness: str = "<harness>") -> str:
    return f"brigade harness sync --target {harness} --scope user --write"


def _legacy_pair_for_kind(kind: str) -> tuple[str, str] | None:
    if kind == DEFAULT_KIND:
        return LEGACY_USER_PROFILE_START, LEGACY_USER_PROFILE_END
    return None


def _marker_hits(
    text: str,
    *,
    kind: str,
    style: MarkerStyle,
) -> tuple[list[re.Match[str]], list[re.Match[str]], list[str]]:
    """Return begin matches, end matches, and malformed loose-marker details for kind."""
    if style == MARKER_STYLE_HASH:
        begin_re, end_re, begin_loose_re, end_loose_re = (
            _HASH_BEGIN_RE,
            _HASH_END_RE,
            _HASH_BEGIN_LOOSE_RE,
            _HASH_END_LOOSE_RE,
        )
    else:
        begin_re, end_re, begin_loose_re, end_loose_re = (
            _BEGIN_RE,
            _END_RE,
            _BEGIN_LOOSE_RE,
            _END_LOOSE_RE,
        )
    begins = [m for m in begin_re.finditer(text) if m.group(1) == kind]
    ends = [m for m in end_re.finditer(text) if m.group(1) == kind]
    details: list[str] = []
    if style == MARKER_STYLE_HASH:
        for match in begin_loose_re.finditer(text):
            line_end = text.find("\n", match.start())
            window = text[match.start() : line_end if line_end != -1 else len(text)]
            if begin_re.fullmatch(window):
                continue
            kind_token = re.match(r"^#\s*BEGIN BRIGADE\s+(\S+)", window)
            token = kind_token.group(1) if kind_token else ""
            if not token or token == kind or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token):
                details.append("begin marker metadata cannot be parsed")
        for match in end_loose_re.finditer(text):
            line_end = text.find("\n", match.start())
            window = text[match.start() : line_end if line_end != -1 else len(text)]
            if end_re.fullmatch(window):
                continue
            kind_token = re.match(r"^#\s*END BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s*$", window)
            if kind_token is None or kind_token.group(1) == kind:
                details.append("end marker cannot be parsed")
        return begins, ends, details

    for match in begin_loose_re.finditer(text):
        close = text.find("-->", match.start())
        window = text[match.start() : close + 3] if close != -1 else text[match.start() : match.start() + 120]
        if begin_re.fullmatch(window):
            continue
        kind_token = re.match(r"<!--\s*BEGIN BRIGADE\s+(\S+)", window)
        token = kind_token.group(1) if kind_token else ""
        if not token or token == kind or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token):
            details.append("begin marker metadata cannot be parsed")
    for match in end_loose_re.finditer(text):
        close = text.find("-->", match.start())
        window = text[match.start() : close + 3] if close != -1 else text[match.start() : match.start() + 80]
        if end_re.fullmatch(window):
            continue
        kind_token = re.match(r"<!--\s*END BRIGADE\s+([A-Za-z][A-Za-z0-9_-]*)\s*-->", window)
        if kind_token is None or kind_token.group(1) == kind:
            details.append("end marker cannot be parsed")
    return begins, ends, details


def _stamped_marker_styles(text: str, *, kind: str) -> set[MarkerStyle]:
    styles: set[MarkerStyle] = set()
    if any(m.group(1) == kind for m in _BEGIN_RE.finditer(text)) or any(
        m.group(1) == kind for m in _END_RE.finditer(text)
    ):
        styles.add(MARKER_STYLE_HTML)
    if any(m.group(1) == kind for m in _HASH_BEGIN_RE.finditer(text)) or any(
        m.group(1) == kind for m in _HASH_END_RE.finditer(text)
    ):
        styles.add(MARKER_STYLE_HASH)
    return styles


def _resolve_marker_style(text: str, *, kind: str, style: MarkerStyle | None) -> MarkerStyle | None:
    stamped = _stamped_marker_styles(text, kind=kind)
    if style is not None:
        if len(stamped) > 1:
            return None
        if stamped and next(iter(stamped)) != style:
            return None
        return style
    if len(stamped) > 1:
        return None
    if stamped:
        return next(iter(stamped))
    return MARKER_STYLE_HTML


def _extract_framed_body(text: str, start_pos: int, start_len: int, end_pos: int) -> tuple[str, str, str] | None:
    after_start = start_pos + start_len
    if end_pos <= start_pos or after_start >= len(text) or text[after_start] != "\n":
        return None
    if end_pos == 0 or text[end_pos - 1] != "\n":
        return None
    before = text[:start_pos]
    body = text[after_start + 1 : end_pos - 1]
    return before, body, text[end_pos:]


def parse_blocks(text: str, *, kind: str = DEFAULT_KIND, style: MarkerStyle | None = None) -> ParsedBlock:
    """Parse the managed span for ``kind`` into a structured block state."""
    offsets = _normalized_offset_starts(text)
    normalized = normalize_newlines(text)
    resolved_style = _resolve_marker_style(normalized, kind=kind, style=style)
    if resolved_style is None:
        stamped = _stamped_marker_styles(normalized, kind=kind)
        if style is not None and stamped and len(stamped) == 1 and next(iter(stamped)) != style:
            detail = "stamped managed markers do not match requested marker style"
        else:
            detail = "stamped html and hash-comment managed markers both present"
        return ParsedBlock(STATUS_MALFORMED, kind, detail=detail)
    begins, ends, loose_details = _marker_hits(normalized, kind=kind, style=resolved_style)
    legacy = _legacy_pair_for_kind(kind)
    legacy_starts = normalized.count(legacy[0]) if legacy else 0
    legacy_ends = normalized.count(legacy[1]) if legacy else 0

    if loose_details and not begins and not ends and legacy_starts == 0 and legacy_ends == 0:
        return ParsedBlock(STATUS_MALFORMED, kind, detail=loose_details[0])

    stamped_present = bool(begins or ends)
    legacy_present = bool(legacy_starts or legacy_ends)

    if stamped_present and legacy_present:
        return ParsedBlock(
            STATUS_MALFORMED,
            kind,
            detail="stamped and legacy managed markers both present",
        )

    if not stamped_present and not legacy_present:
        # Loose unparseable markers for this kind still count as malformed.
        if loose_details:
            return ParsedBlock(STATUS_MALFORMED, kind, detail=loose_details[0])
        return ParsedBlock(STATUS_MISSING, kind)

    if stamped_present:
        if loose_details:
            return ParsedBlock(STATUS_MALFORMED, kind, detail=loose_details[0])
        if len(begins) != 1 or len(ends) != 1:
            detail = "duplicate managed blocks" if len(begins) > 1 or len(ends) > 1 else "orphan managed marker"
            return ParsedBlock(STATUS_MALFORMED, kind, detail=detail)
        begin, end = begins[0], ends[0]
        if end.start() <= begin.start():
            return ParsedBlock(STATUS_MALFORMED, kind, detail="orphan managed marker")
        # Nested / overlapping: another begin or end inside the span.
        inner = normalized[begin.end() : end.start()]
        inner_loose_begin = _HASH_BEGIN_LOOSE_RE if resolved_style == MARKER_STYLE_HASH else _BEGIN_LOOSE_RE
        inner_loose_end = _HASH_END_LOOSE_RE if resolved_style == MARKER_STYLE_HASH else _END_LOOSE_RE
        if inner_loose_begin.search(inner) or inner_loose_end.search(inner):
            return ParsedBlock(STATUS_MALFORMED, kind, detail="nested managed markers")
        if legacy and (legacy[0] in inner or legacy[1] in inner):
            return ParsedBlock(STATUS_MALFORMED, kind, detail="nested managed markers")
        version = int(begin.group(2))
        profile = begin.group(3)
        recorded = begin.group(4).lower()
        if version != MARKER_FORMAT_VERSION:
            return ParsedBlock(STATUS_MALFORMED, kind, detail=f"unsupported marker version: {version}")
        if not re.fullmatch(r"[0-9a-f]{64}", recorded):
            return ParsedBlock(STATUS_MALFORMED, kind, detail="begin marker hash must be a full sha256 digest")
        framed = _extract_framed_body(normalized, begin.start(), len(begin.group(0)), end.start())
        if framed is None:
            return ParsedBlock(STATUS_MALFORMED, kind, detail="managed instruction markers are malformed")
        before, body, after_with_end = framed
        after = after_with_end[len(end.group(0)) :]
        if after.startswith("\n"):
            after = after[1:]
        before = _slice_by_normalized_range(text, offsets, 0, begin.start())
        body_start = begin.start() + len(begin.group(0)) + 1
        body_end = end.start() - 1
        body = normalize_newlines(_slice_by_normalized_range(text, offsets, body_start, body_end))
        after_start = end.start() + len(end.group(0))
        if after_start < len(normalized) and normalized[after_start] == "\n":
            after_start += 1
        after = _slice_by_normalized_range(text, offsets, after_start, len(normalized))
        meta = BlockMeta(
            kind=kind,
            version=version,
            profile=profile,
            recorded_hash=recorded,
            legacy=False,
            style=resolved_style,
        )
        return ParsedBlock(
            "ok",
            kind,
            before=before,
            body=body,
            after=after,
            meta=meta,
            actual_hash=body_hash(body),
        )

    # Legacy path.
    assert legacy is not None
    legacy_start, legacy_end = legacy
    if legacy_starts != 1 or legacy_ends != 1:
        detail = "duplicate managed blocks" if legacy_starts > 1 or legacy_ends > 1 else "orphan managed marker"
        return ParsedBlock(STATUS_MALFORMED, kind, detail=detail)
    start_pos, end_pos = normalized.find(legacy_start), normalized.find(legacy_end)
    if end_pos <= start_pos:
        return ParsedBlock(STATUS_MALFORMED, kind, detail="orphan managed marker")
    inner = normalized[start_pos + len(legacy_start) : end_pos]
    if legacy_start in inner or legacy_end in inner or _BEGIN_LOOSE_RE.search(inner) or _END_LOOSE_RE.search(inner):
        return ParsedBlock(STATUS_MALFORMED, kind, detail="nested managed markers")
    framed = _extract_framed_body(normalized, start_pos, len(legacy_start), end_pos)
    if framed is None:
        return ParsedBlock(STATUS_MALFORMED, kind, detail="managed instruction markers are malformed")
    before, body, after_with_end = framed
    after = after_with_end[len(legacy_end) :]
    if after.startswith("\n"):
        after = after[1:]
    before = _slice_by_normalized_range(text, offsets, 0, start_pos)
    body_start = start_pos + len(legacy_start) + 1
    body_end = end_pos - 1
    body = normalize_newlines(_slice_by_normalized_range(text, offsets, body_start, body_end))
    after_start = end_pos + len(legacy_end)
    if after_start < len(normalized) and normalized[after_start] == "\n":
        after_start += 1
    after = _slice_by_normalized_range(text, offsets, after_start, len(normalized))
    meta = BlockMeta(kind=kind, version=None, profile=None, recorded_hash=None, legacy=True)
    return ParsedBlock(
        "ok",
        kind,
        before=before,
        body=body,
        after=after,
        meta=meta,
        actual_hash=body_hash(body),
    )


def assess_block(
    text: str,
    *,
    desired: str | None,
    kind: str = DEFAULT_KIND,
    profile: str = DEFAULT_PROFILE,
    owned_digest: str | None = None,
    fix_command: str | None = None,
    style: MarkerStyle | None = None,
) -> BlockAssessment:
    """Classify a file's managed span against an optional desired body."""
    parsed = parse_blocks(text, kind=kind, style=style)
    fix = fix_command or default_fix_command()
    if parsed.status == STATUS_MISSING:
        return BlockAssessment(
            STATUS_MISSING if desired is not None else STATUS_MISSING,
            kind,
            parsed,
            desired_hash=body_hash(desired) if desired is not None else None,
            fix_command=fix,
            detail="managed block is missing",
        )
    if parsed.status == STATUS_MALFORMED:
        return BlockAssessment(
            STATUS_MALFORMED,
            kind,
            parsed,
            desired_hash=body_hash(desired) if desired is not None else None,
            fix_command=fix,
            detail=parsed.detail,
        )
    assert parsed.meta is not None
    recorded = parsed.meta.recorded_hash
    actual = parsed.actual_hash
    desired_digest = body_hash(desired) if desired is not None else None
    legacy = parsed.meta.legacy

    if desired is None:
        # Check without a desired body: report integrity of the installed span.
        if legacy:
            return BlockAssessment(
                STATUS_STALE,
                kind,
                parsed,
                recorded_hash=recorded,
                actual_hash=actual,
                profile=parsed.meta.profile,
                legacy=True,
                fix_command=fix,
                detail="legacy managed markers need hash-stamped upgrade",
            )
        if recorded == actual:
            return BlockAssessment(
                STATUS_CURRENT,
                kind,
                parsed,
                recorded_hash=recorded,
                actual_hash=actual,
                profile=parsed.meta.profile,
                fix_command=fix,
            )
        return BlockAssessment(
            STATUS_LOCALLY_MODIFIED,
            kind,
            parsed,
            recorded_hash=recorded,
            actual_hash=actual,
            profile=parsed.meta.profile,
            fix_command=fix,
            detail="managed block body does not match recorded hash",
        )

    assert desired_digest is not None
    if not legacy and recorded == actual == desired_digest:
        stamped_profile = parsed.meta.profile
        if stamped_profile is not None and stamped_profile != profile:
            return BlockAssessment(
                STATUS_STALE,
                kind,
                parsed,
                desired_hash=desired_digest,
                recorded_hash=recorded,
                actual_hash=actual,
                profile=stamped_profile,
                fix_command=fix,
                detail=f"managed block profile is stale ({stamped_profile} != {profile})",
            )
        return BlockAssessment(
            STATUS_CURRENT,
            kind,
            parsed,
            desired_hash=desired_digest,
            recorded_hash=recorded,
            actual_hash=actual,
            profile=stamped_profile or profile,
            fix_command=fix,
        )

    if legacy:
        if actual == desired_digest:
            return BlockAssessment(
                STATUS_STALE,
                kind,
                parsed,
                desired_hash=desired_digest,
                recorded_hash=recorded,
                actual_hash=actual,
                legacy=True,
                fix_command=fix,
                detail="legacy managed markers need hash-stamped upgrade",
            )
        if owned_digest is not None and owned_digest == actual:
            return BlockAssessment(
                STATUS_STALE,
                kind,
                parsed,
                desired_hash=desired_digest,
                recorded_hash=recorded,
                actual_hash=actual,
                legacy=True,
                fix_command=fix,
                detail="owned legacy managed block is stale",
            )
        return BlockAssessment(
            STATUS_LOCALLY_MODIFIED,
            kind,
            parsed,
            desired_hash=desired_digest,
            recorded_hash=recorded,
            actual_hash=actual,
            legacy=True,
            fix_command=fix,
            detail="legacy managed block does not match desired content",
        )

    if recorded == actual and actual != desired_digest:
        return BlockAssessment(
            STATUS_STALE,
            kind,
            parsed,
            desired_hash=desired_digest,
            recorded_hash=recorded,
            actual_hash=actual,
            profile=parsed.meta.profile,
            fix_command=fix,
            detail="managed block hash is stale",
        )

    return BlockAssessment(
        STATUS_LOCALLY_MODIFIED,
        kind,
        parsed,
        desired_hash=desired_digest,
        recorded_hash=recorded,
        actual_hash=actual,
        profile=parsed.meta.profile,
        fix_command=fix,
        detail="managed block body does not match recorded hash",
    )


def plan_install(
    text: str | None,
    *,
    desired: str,
    kind: str = DEFAULT_KIND,
    profile: str = DEFAULT_PROFILE,
    owned_digest: str | None = None,
    force: bool = False,
    adopt: bool = False,
    fix_command: str | None = None,
    style: MarkerStyle | None = None,
) -> BlockPlan:
    """Plan create/update/no-op/preserve for a managed block inside ``text``."""
    resolved_style = style or MARKER_STYLE_HTML
    desired_digest = body_hash(desired)
    block = render_block(desired, kind=kind, profile=profile, style=resolved_style)
    fix = fix_command or default_fix_command()
    if text is None:
        return BlockPlan(
            STATUS_MISSING, ACTION_CREATE, kind, rendered=block, desired_hash=desired_digest, fix_command=fix
        )

    assessment = assess_block(
        text,
        desired=desired,
        kind=kind,
        profile=profile,
        owned_digest=owned_digest,
        fix_command=fix,
        style=resolved_style,
    )
    parsed = assessment.parsed

    if assessment.status == STATUS_MISSING:
        newline = _file_newline(text)
        prefix = text if (not text or text.endswith(newline)) else text + newline
        return BlockPlan(
            STATUS_MISSING,
            ACTION_CREATE,
            kind,
            rendered=prefix + _with_file_newlines(block, newline),
            desired_hash=desired_digest,
            fix_command=fix,
            detail=assessment.detail,
        )

    if assessment.status == STATUS_CURRENT:
        # Matching stamped content with no ownership record: adopt refreshes state only.
        if owned_digest is None and not adopt:
            # Stamped current block is self-attesting; treat as current.
            return BlockPlan(STATUS_CURRENT, ACTION_NONE, kind, desired_hash=desired_digest, fix_command=fix)
        if owned_digest is not None and owned_digest != desired_digest and not adopt:
            # Content is current per markers; ownership drift is repaired on write of state.
            return BlockPlan(STATUS_CURRENT, ACTION_NONE, kind, desired_hash=desired_digest, fix_command=fix)
        return BlockPlan(STATUS_CURRENT, ACTION_NONE, kind, desired_hash=desired_digest, fix_command=fix)

    if assessment.status == STATUS_STALE:
        assert parsed.status == "ok"
        newline = _file_newline(text)
        rendered = parsed.before + _with_file_newlines(block, newline) + parsed.after
        return BlockPlan(
            STATUS_STALE,
            ACTION_UPDATE,
            kind,
            rendered=rendered,
            desired_hash=desired_digest,
            fix_command=fix,
            detail=assessment.detail,
        )

    if assessment.status in {STATUS_LOCALLY_MODIFIED, STATUS_MALFORMED}:
        can_force = force or (adopt and assessment.status == STATUS_LOCALLY_MODIFIED and parsed.status == "ok")
        if can_force and parsed.status == "ok":
            newline = _file_newline(text)
            return BlockPlan(
                assessment.status,
                ACTION_UPDATE,
                kind,
                rendered=parsed.before + _with_file_newlines(block, newline) + parsed.after,
                desired_hash=desired_digest,
                fix_command=fix,
                detail=assessment.detail,
            )
        detail = assessment.detail
        if force and parsed.status != "ok":
            detail = (detail or "malformed managed block") + "; refuse overwrite of unrecoverable malformed span"
        return BlockPlan(
            assessment.status,
            ACTION_PRESERVE,
            kind,
            desired_hash=desired_digest,
            fix_command=fix,
            detail=detail,
        )

    return BlockPlan(
        STATUS_MALFORMED,
        ACTION_PRESERVE,
        kind,
        desired_hash=desired_digest,
        fix_command=fix,
        detail=assessment.detail or "unhandled managed block state",
    )


def plan_remove(
    text: str | None,
    *,
    kind: str = DEFAULT_KIND,
    owned_digest: str | None = None,
    force: bool = False,
    fix_command: str | None = None,
    style: MarkerStyle | None = None,
) -> BlockPlan:
    """Plan surgical removal of the managed span (plus one trailing newline)."""
    fix = fix_command or default_fix_command()
    if text is None:
        return BlockPlan(STATUS_MISSING, ACTION_NONE, kind, fix_command=fix, detail="managed block is absent")

    assessment = assess_block(text, desired=None, kind=kind, owned_digest=owned_digest, fix_command=fix, style=style)
    parsed = assessment.parsed

    if assessment.status == STATUS_MISSING:
        return BlockPlan(STATUS_MISSING, ACTION_NONE, kind, fix_command=fix, detail="managed block is absent")

    if assessment.status == STATUS_MALFORMED:
        if not force:
            return BlockPlan(
                STATUS_MALFORMED,
                ACTION_PRESERVE,
                kind,
                fix_command=fix,
                detail=assessment.detail,
            )
        return BlockPlan(
            STATUS_MALFORMED,
            ACTION_PRESERVE,
            kind,
            fix_command=fix,
            detail=(assessment.detail or "malformed") + "; refuse removal of unrecoverable malformed span",
        )

    assert parsed.status == "ok" and parsed.actual_hash is not None
    if assessment.status == STATUS_LOCALLY_MODIFIED and not force:
        return BlockPlan(
            STATUS_LOCALLY_MODIFIED,
            ACTION_PRESERVE,
            kind,
            fix_command=fix,
            detail=assessment.detail or "owned instruction block was edited",
        )

    # Ownership ledger still gates removal unless --force: a stamped span whose
    # body matches its recorded hash is only removed when Brigade owns it (or
    # force is set). Legacy spans use the same rule.
    if not force and owned_digest is not None and owned_digest != parsed.actual_hash:
        return BlockPlan(
            STATUS_LOCALLY_MODIFIED,
            ACTION_PRESERVE,
            kind,
            fix_command=fix,
            detail="owned instruction block was edited",
        )
    if not force and owned_digest is None:
        return BlockPlan(
            STATUS_LOCALLY_MODIFIED,
            ACTION_PRESERVE,
            kind,
            fix_command=fix,
            detail="owned instruction block was edited",
        )

    rendered = parsed.before + parsed.after
    return BlockPlan(
        assessment.status if assessment.status != STATUS_LOCALLY_MODIFIED else STATUS_LOCALLY_MODIFIED,
        ACTION_REMOVE,
        kind,
        rendered=rendered,
        desired_hash=None,
        fix_command=fix,
        detail="remove managed span",
    )


def _lstat_mode(path: Path) -> int | None:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError:
        return None


def path_is_symlink(path: Path) -> bool:
    mode = _lstat_mode(path)
    return mode is not None and stat.S_ISLNK(mode)


def read_text_nofollow(path: Path) -> str:
    """Read UTF-8 text without following a symlinked final component."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) in {getattr(os, "ELOOP", 62), getattr(os, "EMLINK", 31)} or path_is_symlink(
            path
        ):
            raise OSError(f"refusing symlinked path: {path}") from exc
        raise
    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            raise OSError(f"refusing symlinked path: {path}")
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"managed-block path is not a regular file: {path}")
        data = os.read(fd, st.st_size)
    finally:
        os.close(fd)
    return data.decode("utf-8")


def _managed_parent_containment_available() -> bool:
    supports: set[object] = getattr(os, "supports_dir_fd", set())
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and os.open in supports
        and os.mkdir in supports
        and os.unlink in supports
    )


def _managed_directory_flags(*, follow_descriptor: bool = False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if not follow_descriptor:
        flags |= os.O_NOFOLLOW
    return flags


def _is_held_descriptor_dir(path: Path) -> bool:
    return path.parent in {Path("/proc/self/fd"), Path("/dev/fd")} and path.name.isdigit()


def _is_swapped_parent_error(exc: OSError) -> bool:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return True
    text = str(exc).lower()
    return "symlink" in text or "swapped" in text


def _create_managed_parent(parent: Path) -> int:
    parts = parent.parts
    if not parts:
        raise OSError("unsafe managed-block parent")
    flags = _managed_directory_flags()
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("unsafe managed-block parent")
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                if _is_swapped_parent_error(exc):
                    raise OSError("refusing swapped managed-block parent") from exc
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_managed_parent(parent: Path) -> int:
    parent = Path(parent)
    if _is_held_descriptor_dir(parent):
        descriptor = os.open(parent, _managed_directory_flags(follow_descriptor=True))
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError("managed-block parent is not a directory")
        return descriptor
    try:
        return os.open(parent, _managed_directory_flags())
    except FileNotFoundError:
        return _create_managed_parent(parent if parent.is_absolute() else parent.resolve())
    except OSError as exc:
        if _is_swapped_parent_error(exc):
            raise OSError("refusing swapped managed-block parent") from exc
        raise


def _reject_symlink_parents(path: Path) -> None:
    parent = path
    while parent != parent.parent:
        try:
            mode = os.lstat(parent).st_mode
        except FileNotFoundError:
            parent = parent.parent
            continue
        if stat.S_ISLNK(mode):
            raise OSError("refusing swapped managed-block parent")
        parent = parent.parent


def write_text_nofollow_atomic(
    path: Path,
    data: str,
    *,
    lstat_probe: Callable[[Path], int | None] | None = None,
) -> WriteOutcome:
    """Atomically replace ``path`` without following a symlinked final component.

    ``lstat_probe`` is a test seam for the symlink-swap race between the
    preflight check and the publish step. Parent directories are opened with
    ``O_NOFOLLOW`` and the replacement is published through that descriptor.
    """
    probe = lstat_probe or _lstat_mode
    mode = probe(path)
    if mode is not None and stat.S_ISLNK(mode):
        warning = f"skipping symlinked managed-block target: {path}"
        warnings.warn(warning, stacklevel=2)
        return WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)
    if mode is not None and not stat.S_ISREG(mode):
        detail = f"managed-block target is not a regular file: {path}"
        return WriteOutcome(WRITE_REFUSED, detail=detail)

    if not _managed_parent_containment_available():
        try:
            _reject_symlink_parents(path.parent)
        except OSError as exc:
            return WriteOutcome(WRITE_REFUSED, detail=str(exc))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            mode_after = probe(path)
            if mode_after is not None and stat.S_ISLNK(mode_after):
                warning = f"skipping symlinked managed-block target after swap: {path}"
                warnings.warn(warning, stacklevel=2)
                tmp_path.unlink(missing_ok=True)
                return WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)
            if mode_after is not None and not stat.S_ISREG(mode_after):
                tmp_path.unlink(missing_ok=True)
                return WriteOutcome(WRITE_REFUSED, detail=f"managed-block target is not a regular file: {path}")
            os.replace(tmp_path, path)
        except BaseException as exc:
            tmp_path.unlink(missing_ok=True)
            return WriteOutcome(WRITE_ERROR, detail=str(exc))
        return WriteOutcome(WRITE_WRITTEN)

    try:
        parent_fd = _open_managed_parent(path.parent)
    except OSError as exc:
        status = WRITE_REFUSED if _is_swapped_parent_error(exc) or "swapped" in str(exc).lower() else WRITE_ERROR
        return WriteOutcome(status, detail=str(exc))
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        mode_after = probe(path)
        if mode_after is not None and stat.S_ISLNK(mode_after):
            warning = f"skipping symlinked managed-block target after swap: {path}"
            warnings.warn(warning, stacklevel=2)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            return WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)
        if mode_after is not None and not stat.S_ISREG(mode_after):
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            return WriteOutcome(WRITE_REFUSED, detail=f"managed-block target is not a regular file: {path}")
        try:
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            named = None
        if named is not None and stat.S_ISLNK(named.st_mode):
            warning = f"skipping symlinked managed-block target after swap: {path}"
            warnings.warn(warning, stacklevel=2)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            return WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except OSError as exc:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        status = WRITE_REFUSED if _is_swapped_parent_error(exc) or "swapped" in str(exc).lower() else WRITE_ERROR
        return WriteOutcome(status, detail=str(exc))
    except BaseException as exc:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        return WriteOutcome(WRITE_ERROR, detail=str(exc))
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent_fd)
    return WriteOutcome(WRITE_WRITTEN)


def install_block(
    path: Path,
    desired: str,
    *,
    kind: str = DEFAULT_KIND,
    profile: str = DEFAULT_PROFILE,
    owned_digest: str | None = None,
    force: bool = False,
    adopt: bool = False,
    fix_command: str | None = None,
    lstat_probe: Callable[[Path], int | None] | None = None,
    style: MarkerStyle | None = None,
) -> tuple[BlockPlan, WriteOutcome]:
    """Install or update a managed block with symlink-safe atomic publish."""
    probe = lstat_probe or _lstat_mode
    mode = probe(path)
    if mode is not None and stat.S_ISLNK(mode):
        warning = f"skipping symlinked managed-block target: {path}"
        warnings.warn(warning, stacklevel=2)
        plan = BlockPlan(
            "symlink",
            ACTION_PRESERVE,
            kind,
            desired_hash=body_hash(desired),
            detail=warning,
            warning=warning,
            fix_command=fix_command or default_fix_command(),
        )
        return plan, WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)

    text: str | None
    if not path.exists():
        text = None
    else:
        try:
            text = read_text_nofollow(path)
        except (OSError, UnicodeDecodeError) as exc:
            plan = BlockPlan(
                STATUS_MALFORMED,
                ACTION_PRESERVE,
                kind,
                desired_hash=body_hash(desired),
                detail=str(exc),
                fix_command=fix_command or default_fix_command(),
            )
            return plan, WriteOutcome(WRITE_ERROR, detail=str(exc))

    plan = plan_install(
        text,
        desired=desired,
        kind=kind,
        profile=profile,
        owned_digest=owned_digest,
        force=force,
        adopt=adopt,
        fix_command=fix_command,
        style=style,
    )
    if plan.action == ACTION_NONE:
        return plan, WriteOutcome(WRITE_NOOP)
    if plan.action == ACTION_PRESERVE or plan.rendered is None:
        return plan, WriteOutcome(WRITE_REFUSED, detail=plan.detail)

    # Byte-identical no-op even when the planner said update (defensive).
    if text is not None and text == plan.rendered:
        return BlockPlan(
            STATUS_CURRENT,
            ACTION_NONE,
            kind,
            desired_hash=plan.desired_hash,
            fix_command=plan.fix_command,
        ), WriteOutcome(WRITE_NOOP)

    outcome = write_text_nofollow_atomic(path, plan.rendered, lstat_probe=lstat_probe)
    return plan, outcome


def remove_block(
    path: Path,
    *,
    kind: str = DEFAULT_KIND,
    owned_digest: str | None = None,
    force: bool = False,
    fix_command: str | None = None,
    lstat_probe: Callable[[Path], int | None] | None = None,
    style: MarkerStyle | None = None,
) -> tuple[BlockPlan, WriteOutcome]:
    """Remove a managed block surgically with symlink-safe atomic publish."""
    probe = lstat_probe or _lstat_mode
    mode = probe(path)
    if mode is not None and stat.S_ISLNK(mode):
        warning = f"skipping symlinked managed-block target: {path}"
        warnings.warn(warning, stacklevel=2)
        plan = BlockPlan(
            "symlink",
            ACTION_PRESERVE,
            kind,
            detail=warning,
            warning=warning,
            fix_command=fix_command or default_fix_command(),
        )
        return plan, WriteOutcome(WRITE_SKIPPED_SYMLINK, detail=warning, warning=warning)
    if mode is None:
        plan = plan_remove(
            None, kind=kind, owned_digest=owned_digest, force=force, fix_command=fix_command, style=style
        )
        return plan, WriteOutcome(WRITE_NOOP)

    try:
        text = read_text_nofollow(path)
    except (OSError, UnicodeDecodeError) as exc:
        plan = BlockPlan(STATUS_MALFORMED, ACTION_PRESERVE, kind, detail=str(exc), fix_command=fix_command)
        return plan, WriteOutcome(WRITE_ERROR, detail=str(exc))

    plan = plan_remove(text, kind=kind, owned_digest=owned_digest, force=force, fix_command=fix_command, style=style)
    if plan.action == ACTION_NONE:
        return plan, WriteOutcome(WRITE_NOOP)
    if plan.action == ACTION_PRESERVE or plan.rendered is None:
        return plan, WriteOutcome(WRITE_REFUSED, detail=plan.detail)
    if text == plan.rendered:
        return plan, WriteOutcome(WRITE_NOOP)
    outcome = write_text_nofollow_atomic(path, plan.rendered, lstat_probe=lstat_probe)
    return plan, outcome


def check_block(
    path: Path,
    *,
    desired: str | None,
    kind: str = DEFAULT_KIND,
    profile: str = DEFAULT_PROFILE,
    owned_digest: str | None = None,
    fix_command: str | None = None,
    style: MarkerStyle | None = None,
) -> BlockAssessment:
    """Classify a path's managed block; missing file is ``missing``."""
    if path_is_symlink(path):
        return BlockAssessment(
            STATUS_MALFORMED,
            kind,
            ParsedBlock(STATUS_MALFORMED, kind, detail=f"managed-block target is a symlink: {path}"),
            desired_hash=body_hash(desired) if desired is not None else None,
            fix_command=fix_command or default_fix_command(),
            detail=f"managed-block target is a symlink: {path}",
        )
    if not path.exists():
        return BlockAssessment(
            STATUS_MISSING,
            kind,
            ParsedBlock(STATUS_MISSING, kind),
            desired_hash=body_hash(desired) if desired is not None else None,
            fix_command=fix_command or default_fix_command(),
            detail="managed block is missing",
        )
    try:
        text = normalize_newlines(read_text_nofollow(path))
    except (OSError, UnicodeDecodeError) as exc:
        return BlockAssessment(
            STATUS_MALFORMED,
            kind,
            ParsedBlock(STATUS_MALFORMED, kind, detail=str(exc)),
            desired_hash=body_hash(desired) if desired is not None else None,
            fix_command=fix_command or default_fix_command(),
            detail=str(exc),
        )
    return assess_block(
        text,
        desired=desired,
        kind=kind,
        profile=profile,
        owned_digest=owned_digest,
        fix_command=fix_command,
        style=style,
    )
