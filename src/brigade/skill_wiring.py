"""Never-overwrite wiring for the bundled SKILL.md files that `brigade init` lays down.

An installed SKILL.md is the operator's file once it exists. Init, reconfigure
and `--force` only ever create a missing one. A copy that differs from the
bundled template is left untouched and the new template text is filed as a
skills inbox proposal instead, so the operator reviews it with
`brigade skills inbox diff`. `skills install` and `skills sync` keep their own
semantics and do not use this module.

Every path component from the target root down is checked with lstat before a
directory is created or a file is written. A symlink anywhere on the way (a
SKILL.md installed as a link, or a linked parent directory) is kept as it is,
with no write and no proposal, so a skill that lives elsewhere is never reached
through the link.
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
from pathlib import Path
from typing import Any

from . import __version__ as BRIGADE_VERSION

WIRED_STATE_REL = ("skills", "wired.json")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)

# Statuses that mean the destination already holds a skill and was not written.
WRITTEN = "written"
IDENTICAL = "identical"
EDITED = "edited"
PRISTINE_STALE = "pristine-stale"
SYMLINKED = "symlinked"
BLOCKED = "not-a-regular-file"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SkillWirer:
    """Places bundled SKILL.md files under one target and records what init wrote."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self._recorded = self._load_recorded()
        self._written: dict[str, dict[str, str]] = {}

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
        state, existing = self._probe(rel_parts)
        if state == "missing":
            state = self._create(rel_parts, data)
        if state == WRITTEN:
            self._written[rel] = {
                "harness": harness,
                "skill_id": skill_id,
                "sha256": _sha256(data),
                "brigade_version": BRIGADE_VERSION,
                "template_sha256": _sha256(data),
            }
            return WRITTEN
        if state == "existing":
            assert existing is not None
            if existing == data:
                print(f"brigade: kept (identical): {dst}")
                return IDENTICAL
            record = self._recorded.get(rel)
            pristine = isinstance(record, dict) and record.get("sha256") == _sha256(existing)
            label = PRISTINE_STALE if pristine else EDITED
            proposal_id = self._file_proposal(template_dir, data, skill_id=skill_id, label=label, rel=rel)
            suffix = f"; bundled template differs, proposal {proposal_id}" if proposal_id else ""
            print(f"brigade: kept ({label}): {dst}{suffix}")
            return label
        print(f"brigade: kept ({state}): {dst}")
        return state

    def finish(self) -> None:
        """Persist the records of the files init just wrote. A failure is a warning."""
        if not self._written:
            return
        from .skills_cmd import registry

        try:
            payload = {"version": 1, "skills": {**self._recorded, **self._written}}
            with registry._held_state_root(self.target) as anchor:
                registry._write_state_file(
                    anchor, *WIRED_STATE_REL, data=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
                )
        except (OSError, ValueError, registry.SkillsStatePathError) as exc:
            print(f"warning: could not record wired skills in .brigade/skills/wired.json: {exc}", file=sys.stderr)

    def _load_recorded(self) -> dict[str, Any]:
        from .skills_cmd import registry

        try:
            with registry._held_state_root(self.target, writable=False) as anchor:
                raw = registry._read_state_file_bytes(anchor, *WIRED_STATE_REL)
            payload = json.loads(raw.decode("utf-8")) if raw is not None else {}
            skills = payload.get("skills") if isinstance(payload, dict) else None
        except (OSError, ValueError, registry.SkillsStatePathError):
            return {}
        return dict(skills) if isinstance(skills, dict) else {}

    def _probe(self, rel_parts: tuple[str, ...]) -> tuple[str, bytes | None]:
        """Classify the destination without following any link.

        Returns ``(state, bytes)``: ``missing`` (safe to create), ``existing``
        (a plain regular file, with its bytes), ``symlinked``, or
        ``not-a-regular-file``.
        """
        current = self.target
        last = len(rel_parts) - 1
        for index, part in enumerate(rel_parts):
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return "missing", None
            if stat.S_ISLNK(info.st_mode):
                return SYMLINKED, None
            if index < last and not stat.S_ISDIR(info.st_mode):
                return BLOCKED, None
            if index == last and not stat.S_ISREG(info.st_mode):
                return BLOCKED, None
        try:
            fd = os.open(current, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except OSError:
            return SYMLINKED if current.is_symlink() else BLOCKED, None
        with os.fdopen(fd, "rb") as handle:
            return "existing", handle.read()

    def _create(self, rel_parts: tuple[str, ...], data: bytes) -> str:
        """Create the missing tail: lstat each level, mkdir one level at a time, write O_EXCL."""
        current = self.target
        last = len(rel_parts) - 1
        for index, part in enumerate(rel_parts):
            current = current / part
            if index < last:
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
                info = os.lstat(current)
                if stat.S_ISLNK(info.st_mode):
                    return SYMLINKED
                if not stat.S_ISDIR(info.st_mode):
                    return BLOCKED
                continue
            try:
                fd = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o666)
            except FileExistsError:
                # Lost a race with another writer; that file is theirs to keep.
                return SYMLINKED if current.is_symlink() else BLOCKED
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        return WRITTEN

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
        key = f"init:{skill_id}:{digest.hexdigest()}"
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
                    )
            if rc in (0, 1):
                return str(json.loads(out.getvalue())["proposal_id"])
        except (OSError, ValueError, KeyError) as exc:
            print(f"warning: could not file skills inbox proposal for {rel}: {exc}", file=sys.stderr)
            return None
        print(f"warning: could not file skills inbox proposal for {rel} (inbox_add rc={rc})", file=sys.stderr)
        return None
