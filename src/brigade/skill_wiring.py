"""Never-overwrite wiring for the bundled SKILL.md files that `brigade init` lays down.

An installed SKILL.md is the operator's file once it exists. Init, reconfigure
and `--force` only ever create a missing one. A copy that differs from the
bundled template is left untouched and the new template text is filed as a
skills inbox proposal instead, so the operator reviews it with
`brigade skills inbox diff`. `skills install` and `skills sync` keep their own
semantics and do not use this module.

The destination is walked, read and created relative to a held directory
descriptor for the target root (`brigade.dirfd`), one component at a time. No
component is ever resolved by pathname, so a parent directory swapped for a
link after the check cannot redirect the write, and a Windows junction or
symlink is refused by the same helpers. A link anywhere on the way (a SKILL.md
installed as a link, or a linked parent directory) is kept as it is, with no
write and no proposal, so a skill that lives elsewhere is never reached through
the link. Any race (a component vanishing or changing type mid-walk) resolves to
a kept outcome and never to a write.

A new SKILL.md is published whole or not at all: the text is written and
fsynced to a temporary sibling under the same held directory, then moved to its
name with a no-replace publish (`dirfd.replace_children(replace=False)`, a
link-then-unlink on POSIX and a no-replace rename on Windows, so it never
overwrites). A write that fails or is interrupted leaves no SKILL.md behind, only
a temp file that is removed, so the next run writes the bundled text instead of
keeping a truncated copy as "edited".

An OSError that is not a race (permission denied, a read-only or full disk, an
I/O error) is a kept outcome too: it is reported with the path and errno, the
skill is not recorded, and the remaining skills and the rest of init carry on.

Pristine refresh: an installed copy that is identical to a text a past release
shipped for the same skill (`shipped_history`, newline-folded) has no local
edits, so it is replaced with the current bundled text and recorded in
wired.json like a fresh write. Any other differing copy is never overwritten.
The copy is re-read under a fresh no-follow descriptor and must still be the
same bytes right before the publish (a changed, vanished or linked copy is
kept and reported, never written), and the publish goes through the same
temp/fsync path as a new file, now allowed to replace. The one window left is the
instant between that re-read and the rename. Skills inbox proposals that init
filed for the replaced file are marked superseded, and only after the publish
succeeded.

Newlines: bundled text is written as LF (the same policy as the template copy
loop in `install.py`), and a comparison treats CRLF and LF as the same text so a
Windows checkout, or a copy an older release wrote with CRLF, is not "edited".
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import __version__ as BRIGADE_VERSION
from . import dirfd, shipped_history

WIRED_STATE_REL = ("skills", "wired.json")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK keeps a FIFO swapped in at the destination from hanging the read;
# fstat then rejects anything that is not a regular file.
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _BINARY | _CLOEXEC | getattr(os, "O_NONBLOCK", 0)
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY | _CLOEXEC

# Statuses that mean the destination already holds a skill and was not written.
# (WRITTEN and REFRESHED are the two that did write.)
WRITTEN = "written"
IDENTICAL = "identical"
REFRESHED = "refreshed"
RACED = "changed-during-refresh"
EDITED = "edited"
PRISTINE_STALE = "pristine-stale"
SYMLINKED = "symlinked"
BLOCKED = "not-a-regular-file"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_newlines(data: bytes) -> bytes:
    """Return ``data`` with CRLF folded to LF, the form SKILL.md text is compared in."""
    return data.replace(b"\r\n", b"\n")


def _read_regular(fd: int) -> bytes | None:
    """Read and close an opened destination; None when it is not a regular file."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_RACE_ERRORS = (FileExistsError, FileNotFoundError, NotADirectoryError)


def _parse_recorded(raw: bytes | None) -> tuple[dict[str, Any], bool]:
    """Return ``(records, corrupt)`` for the bytes of wired.json (``None`` is an empty file)."""
    if raw is None:
        return {}, False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}, True
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, dict):
        return {}, True
    return dict(skills), False


def _refusal(parent: int, name: str, exc: OSError) -> str:
    """Name why ``name`` under the held ``parent`` could not be entered: a link, or something else."""
    if dirfd.is_reparse_point_refusal(exc):
        return SYMLINKED
    try:
        info = dirfd.stat_child(parent, name, allow_reparse=True)
    except OSError:
        return BLOCKED
    return SYMLINKED if stat.S_ISLNK(info.st_mode) else BLOCKED


class SkillWirer:
    """Places bundled SKILL.md files under one target and records what init wrote."""

    def __init__(self, target: Path) -> None:
        # Resolved once: the root is the operator's choice (a linked checkout is
        # fine); everything below it is walked without following a link.
        self.target = target.expanduser().resolve()
        self._recorded = self._load_recorded()
        self._written: dict[str, dict[str, str]] = {}

    @property
    def recorded(self) -> dict[str, Any]:
        """Hashes recorded for skills init wrote on earlier runs, keyed by workspace-relative path."""
        return self._recorded

    def place(
        self,
        rel_parts: tuple[str, ...],
        template_dir: Path,
        text: str,
        *,
        skill_id: str,
        harness: str,
    ) -> str:
        """Place one SKILL.md and print why a kept copy was kept. Returns the status."""
        rel = "/".join(rel_parts)
        data = text.encode("utf-8")
        dst = self.target.joinpath(*rel_parts)
        try:
            state, existing = self._walk(rel_parts, data, self._pristine_check(skill_id, data))
        except OSError as exc:
            # Not a race (those are classified inside the walk): the destination could
            # not be read, created or published. Keep what is there, say why, move on.
            code = f"errno {exc.errno}" if exc.errno is not None else type(exc).__name__
            print(f"warning: could not place {dst}: {exc.strerror or exc} ({code})", file=sys.stderr)
            print(f"brigade: kept ({BLOCKED}): {dst}")
            return BLOCKED
        if state in (WRITTEN, REFRESHED):
            self._written[rel] = {
                "harness": harness,
                "skill_id": skill_id,
                "sha256": _sha256(data),
                "brigade_version": BRIGADE_VERSION,
                "template_sha256": _sha256(data),
            }
            if state == REFRESHED:
                print(f"brigade: refreshed (pristine): {dst}")
                self._withdraw_proposals(skill_id, rel)
            return state
        if state == "existing":
            assert existing is not None
            if normalize_newlines(existing) == normalize_newlines(data):
                print(f"brigade: kept (identical): {dst}")
                return IDENTICAL
            record = self._recorded.get(rel)
            recorded_sha = record.get("sha256") if isinstance(record, dict) else None
            pristine = recorded_sha in (_sha256(existing), _sha256(normalize_newlines(existing)))
            label = PRISTINE_STALE if pristine else EDITED
            proposal_id = self._file_proposal(template_dir, data, skill_id=skill_id, label=label, rel=rel)
            suffix = f"; bundled template differs, proposal {proposal_id}" if proposal_id else ""
            print(f"brigade: kept ({label}): {dst}{suffix}")
            return label
        print(f"brigade: kept ({state}): {dst}")
        return state

    def finish(self) -> None:
        """Persist the records of the files init just wrote. A failure is a warning.

        The read-merge-write runs under the skills mutation lock and re-reads the
        file inside it, so two inits do not drop each other's records. A corrupt
        file is copied aside before it is replaced.
        """
        if not self._written:
            return
        from .skills_cmd import registry
        from .skills_cmd.mutation import skill_mutation

        try:
            with skill_mutation(self.target), registry._held_state_root(self.target) as anchor:
                raw = registry._read_state_file_bytes(anchor, *WIRED_STATE_REL)
                current, corrupt = _parse_recorded(raw)
                if corrupt and raw is not None:
                    backup = f"{WIRED_STATE_REL[1]}.corrupt-{_sha256(raw)[:12]}"
                    registry._write_state_file(anchor, WIRED_STATE_REL[0], backup, data=raw)
                    print(
                        f"warning: .brigade/skills/wired.json was unreadable; kept a copy as .brigade/skills/{backup}",
                        file=sys.stderr,
                    )
                payload = {"version": 1, "skills": {**current, **self._written}}
                registry._write_state_file(
                    anchor, *WIRED_STATE_REL, data=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
                )
        except (OSError, ValueError, registry.SkillsStatePathError) as exc:
            print(f"warning: could not record wired skills in .brigade/skills/wired.json: {exc}", file=sys.stderr)

    def _load_recorded(self) -> dict[str, Any]:
        """Read-only view of the records, used only to label a differing copy."""
        from .skills_cmd import registry

        try:
            with registry._held_state_root(self.target, writable=False) as anchor:
                raw = registry._read_state_file_bytes(anchor, *WIRED_STATE_REL)
        except (OSError, ValueError, registry.SkillsStatePathError):
            return {}
        return _parse_recorded(raw)[0]

    def _walk(
        self, rel_parts: tuple[str, ...], data: bytes, pristine: Callable[[bytes], bool] | None = None
    ) -> tuple[str, bytes | None]:
        """Classify the destination and create it if missing, all under a held root descriptor.

        Returns ``(state, bytes)``: ``written`` (created now), ``existing`` (a
        plain regular file, with its bytes), ``symlinked`` or ``not-a-regular-file``.
        Missing parents are created one level at a time; every open is relative
        to the descriptor of the level above, so no later swap of a parent can
        redirect it.
        """
        held: list[int] = []
        try:
            try:
                parent = dirfd.open_directory_nofollow(self.target)
            except OSError:
                return BLOCKED, None
            held.append(parent)
            for part in rel_parts[:-1]:
                child, refusal = self._enter_directory(parent, part)
                if child is None:
                    return refusal or BLOCKED, None
                held.append(child)
                parent = child
            return self._place_file(parent, rel_parts[-1], data, pristine)
        finally:
            for fd in reversed(held):
                os.close(fd)

    def _enter_directory(self, parent: int, name: str) -> tuple[int | None, str | None]:
        """Open (creating if absent) one child directory without following a link."""
        for _ in range(3):
            try:
                return dirfd.open_child_directory(parent, name), None
            except FileNotFoundError:
                pass
            except OSError as exc:
                return None, _refusal(parent, name, exc)
            try:
                dirfd.mkdir_child(parent, name, 0o777)
            except FileExistsError:
                continue  # raced with another creator: reopen it, still no-follow
            except _RACE_ERRORS as exc:
                return None, _refusal(parent, name, exc)
        return None, BLOCKED

    def _place_file(
        self, parent: int, name: str, data: bytes, pristine: Callable[[bytes], bool] | None
    ) -> tuple[str, bytes | None]:
        try:
            fd = dirfd.open_child_file(parent, name, _READ_FLAGS)
        except FileNotFoundError:
            fd = None
        except OSError as exc:
            return _refusal(parent, name, exc), None
        if fd is not None:
            existing = _read_regular(fd)
            if existing is None:
                return BLOCKED, None
            if pristine is not None and pristine(existing) and normalize_newlines(existing) != normalize_newlines(data):
                return self._refresh(parent, name, existing, data)
            return "existing", existing
        return self._publish(parent, name, data)

    def _pristine_check(self, skill_id: str, data: bytes) -> Callable[[bytes], bool] | None:
        """A test for "this installed text is a past bundled release of this skill", or None to never refresh.

        Both the installed text and the text about to be written must be recorded
        bundled texts of the skill, so a rendered or unrecorded text is never published.
        """
        known = shipped_history.digests(shipped_history.skill_key(skill_id))
        if shipped_history.skill_digest(data) not in known:
            return None
        return lambda body: shipped_history.skill_digest(body) in known

    def _refresh(self, parent: int, name: str, expected: bytes, data: bytes) -> tuple[str, bytes | None]:
        """Replace a pristine past-release copy with ``data`` if it is still exactly ``expected``.

        Returns ``(refreshed, None)``; ``(changed-during-refresh, None)`` when the
        copy vanished or no longer holds ``expected``; a link or special file is
        reported as such. A failed publish (not a race) falls back to the kept path
        so the operator still gets the inbox proposal.
        """
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            # Slow disk I/O (write + fsync) happens before the compare, so the window
            # between "still pristine" and the rename is only the re-read and the rename.
            self._write_temp(parent, temporary, data)
            try:
                fd = dirfd.open_child_file(parent, name, _READ_FLAGS)
            except FileNotFoundError:
                return RACED, None
            except OSError as exc:
                return _refusal(parent, name, exc), None
            current = _read_regular(fd)
            if current is None:
                return BLOCKED, None
            if current != expected:
                return RACED, None
            dirfd.replace_children(parent, temporary, name, replace=True)
        except _RACE_ERRORS as exc:
            return _refusal(parent, name, exc), None
        except OSError as exc:
            code = f"errno {exc.errno}" if exc.errno is not None else type(exc).__name__
            print(f"warning: could not refresh {name}: {exc.strerror or exc} ({code})", file=sys.stderr)
            return "existing", expected
        finally:
            with contextlib.suppress(OSError):
                dirfd.unlink_child(parent, temporary)
        with contextlib.suppress(OSError):
            dirfd.fsync_directory(parent)
        return REFRESHED, None

    @staticmethod
    def _write_temp(parent: int, temporary: str, data: bytes) -> None:
        fd = dirfd.open_child_file(parent, temporary, _CREATE_FLAGS, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _withdraw_proposals(self, skill_id: str, rel: str) -> None:
        """Mark inbox proposals init filed for this file superseded; the file now holds the bundled text."""
        from .skills_cmd.inbox import supersede_pending

        try:
            ids = supersede_pending(self.target, f"init:{skill_id}:{rel}:", "refreshed to current bundled text")
        except (OSError, ValueError) as exc:
            print(f"warning: could not withdraw skills inbox proposals for {rel}: {exc}", file=sys.stderr)
            return
        for proposal_id in ids:
            print(f"brigade: superseded skills inbox proposal {proposal_id}")

    def _publish(self, parent: int, name: str, data: bytes, *, replace: bool = False) -> tuple[str, bytes | None]:
        """Write ``data`` to a temp sibling, then publish it under ``name``; replaces only when ``replace`` is set."""
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        try:
            self._write_temp(parent, temporary, data)
            dirfd.replace_children(parent, temporary, name, replace=replace)
        except _RACE_ERRORS as exc:
            # Lost a race with another writer (the name now exists); that file is theirs to keep.
            return _refusal(parent, name, exc), None
        finally:
            # The temp file is gone after a successful publish; after any failure it must not linger.
            with contextlib.suppress(OSError):
                dirfd.unlink_child(parent, temporary)
        with contextlib.suppress(OSError):
            dirfd.fsync_directory(parent)
        return WRITTEN, None

    def _file_proposal(self, template_dir: Path, data: bytes, *, skill_id: str, label: str, rel: str) -> str | None:
        """File the bundled text as an inbox proposal; return its id, or None with a warning."""
        from .skills_cmd import inbox_add

        files: dict[str, bytes] = {}
        for path in sorted(template_dir.rglob("*")):
            if path.is_file():
                files[path.relative_to(template_dir).as_posix()] = path.read_bytes()
        files["SKILL.md"] = data
        digest = hashlib.sha256()
        for name in sorted(files):
            digest.update(name.encode("utf-8") + b"\0" + files[name] + b"\0")
        # The destination is part of the key: the same bundled text proposed for two
        # different installed copies is two proposals, and rejecting one must not mute the other.
        key = f"init:{skill_id}:{rel}:{digest.hexdigest()}"
        summary = f"Bundled {skill_id} template differs from the installed copy at {rel} ({label}); the installed file was kept."
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory(prefix="brigade-init-skill-") as staging:
                source = Path(staging) / skill_id
                for name, content in files.items():
                    (source / name).parent.mkdir(parents=True, exist_ok=True)
                    (source / name).write_bytes(content)
                with contextlib.redirect_stdout(out):
                    rc = inbox_add(
                        target=self.target,
                        source=source,
                        skill_id=skill_id,
                        summary=summary,
                        json_output=True,
                        dedupe_key=key,
                        provenance=f"brigade://bundled-skills/{skill_id}@{BRIGADE_VERSION}",
                    )
            if rc in (0, 1):
                return str(json.loads(out.getvalue())["proposal_id"])
        except (OSError, ValueError, KeyError) as exc:
            print(f"warning: could not file skills inbox proposal for {rel}: {exc}", file=sys.stderr)
            return None
        print(f"warning: could not file skills inbox proposal for {rel} (inbox_add rc={rc})", file=sys.stderr)
        return None
