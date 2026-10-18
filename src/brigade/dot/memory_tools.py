"""Workspace-scoped memory, handoff, policy and installed skill access."""

from __future__ import annotations
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4
from .contracts import MAX_CARD_FILE_BYTES, DotError, require
from .hub_tools import BRIGADE_TIMEOUT, MAX_SMALL_CAPTURE
from .process import CommandRunner, child_environment, child_path, parse_json_bytes
from .runtime_config import Runtime
from .safe_state import private_directory, trusted_directory, redact_credentials
from .state import utc_now

CARD_CHUNK_CHARS = 30000
POLICY_PATHS = (Path(".codex/AGENTS.md"), Path("AGENTS.md"), Path("ai-tools/AGENTS.md"))


class MemoryTools:
    def __init__(self, runtime: Runtime, *, commands: CommandRunner) -> None:
        self.runtime, self.commands = runtime, commands
        self.workspace = runtime.paths.workspace
        self.cards = self.workspace / "memory" / "cards"
        self.handoff_inbox = self.workspace / ".codex" / "memory-handoffs"
        self.clock = utc_now
        self.environment = child_environment(child_path(runtime.paths.bin))

    def _brigade_json(
        self, tail: list[str], *, timeout: float = BRIGADE_TIMEOUT, max_output: int = MAX_SMALL_CAPTURE
    ) -> tuple[Any, str | None]:
        result = self.commands.run(
            [self.runtime.paths.bin.brigade, *tail], timeout=timeout, max_output=max_output, env=self.environment
        )
        if result.timed_out:
            return None, "brigade_timeout"
        if result.overflow:
            return None, "output_too_large"
        value = parse_json_bytes(result.stdout)
        if value is None:
            return None, "brigade_unavailable"
        return value, None

    def memory_search(self, query: str, limit: int = 10) -> dict[str, Any]:
        value, error = self._brigade_json(
            ["memory", "search", "--target", str(self.workspace), "--limit", str(limit), "--json", "--", query],
            timeout=30,
        )
        if error or not isinstance(value, dict) or not isinstance(value.get("matches"), list):
            return {"error": error or "memory_unavailable"}
        keys = ("card_id", "title", "path", "score", "summary", "tags", "card_aliases")
        return dict(
            query=query,
            match_count=value.get("match_count"),
            matches=[
                {key: match.get(key) for key in keys} for match in value["matches"][:limit] if isinstance(match, dict)
            ],
            workspace=str(self.workspace),
            next="Read a card with memory_card(name=<path basename without .md>).",
        )

    def memory_card(self, name: str, offset: int = 0) -> dict[str, Any]:
        directory = descriptor = None
        try:
            directory = trusted_directory(self.cards, allow_group_write=True, code="memory_unavailable")
            try:
                descriptor = os.open(
                    name + ".md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
                )
            except FileNotFoundError:
                return {"error": "card_not_found", "name": name}
            except OSError:
                return {"error": "card_unavailable", "name": name}
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return {"error": "card_unavailable", "name": name}
            if info.st_size > MAX_CARD_FILE_BYTES:
                return {"error": "card_too_large", "name": name, "bytes": info.st_size}
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = stream.read(MAX_CARD_FILE_BYTES + 1)
        finally:
            for item in (descriptor, directory):
                if item is not None:
                    os.close(item)
        if len(data) > MAX_CARD_FILE_BYTES:
            return {"error": "card_too_large", "name": name}
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return {"error": "card_unavailable", "name": name}
        chunk = text[offset : offset + CARD_CHUNK_CHARS]
        following = offset + len(chunk)
        return dict(
            name=name,
            path=str(Path(self.cards) / (name + ".md")),
            bytes=len(data),
            chars=len(text),
            sha256=hashlib.sha256(data).hexdigest(),
            offset=offset,
            next_offset=following if following < len(text) else None,
            content=chunk,
        )

    def handoff_write(self, slug: str, content: str) -> dict[str, Any]:
        if redact_credentials(content) != content:
            return {"error": "content_rejected", "reason": "credential-shaped text"}
        moment = self.clock()
        require(isinstance(moment, datetime) and moment.utcoffset() is not None, "clock_unavailable")
        filename = f"{moment.astimezone():%Y-%m-%d-%H%M}-dot-{slug}.md"
        inbox = trusted_directory(self.handoff_inbox, code="handoff_unavailable")
        lint_name = f"handoff-lint-{uuid4().hex}.md"
        temporary = f".dot-{uuid4().hex}.tmp"
        lint_written = temporary_written = False
        state_fd = None
        try:
            state_fd = private_directory(self.runtime.paths.state_dir, create=True)
            try:
                os.stat(filename, dir_fd=inbox, follow_symlinks=False)
                return {"error": "handoff_exists", "filename": filename}
            except FileNotFoundError:
                pass
            encoded = content.encode("utf-8")
            # Lint a private copy before anything appears in the ingested inbox.
            descriptor = os.open(
                lint_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=state_fd
            )
            lint_written = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
            result = self.commands.run(
                [
                    self.runtime.paths.bin.brigade,
                    "handoff",
                    "lint",
                    "--json",
                    "--target",
                    str(self.workspace),
                    str(Path(self.runtime.paths.state_dir) / lint_name),
                ],
                timeout=30,
                max_output=MAX_SMALL_CAPTURE,
                env=self.environment,
            )
            report = parse_json_bytes(result.stdout) if not (result.timed_out or result.overflow) else None
            entries = report.get("results") if isinstance(report, dict) else None
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                return {"error": "handoff_unavailable", "reason": "lint_unavailable"}
            assert isinstance(report, dict)
            entry = entries[0]
            if entry.get("valid") is not True or report.get("valid") is not True:
                return {
                    "error": "handoff_invalid",
                    "lint_errors": entry.get("errors"),
                    "lint_warnings": entry.get("warnings"),
                    "hints": entry.get("hints"),
                }
            # Publish atomically and never overwrite: write a hidden temp file, then
            # hard-link it to the final name (link fails if the name exists).
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=inbox
            )
            temporary_written = True
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, filename, src_dir_fd=inbox, dst_dir_fd=inbox, follow_symlinks=False)
            except FileExistsError:
                return {"error": "handoff_exists", "filename": filename}
            os.fsync(inbox)
            return dict(
                ok=True,
                filename=filename,
                path=str(Path(self.handoff_inbox) / filename),
                bytes=len(encoded),
                action=entry.get("action"),
                lint_warnings=entry.get("warnings"),
            )
        except DotError:
            raise
        except OSError:
            return {"error": "handoff_unavailable"}
        finally:
            if temporary_written:
                try:
                    os.unlink(temporary, dir_fd=inbox)
                except OSError:
                    pass
            if lint_written:
                try:
                    os.unlink(lint_name, dir_fd=state_fd)
                except OSError:
                    pass
            if state_fd is not None:
                os.close(state_fd)
            os.close(inbox)

    def fleet_policy(self, since_hash: str | None = None) -> dict[str, Any]:
        return policy(self.workspace, since_hash)

    def skill_search(self, query: str, limit: int = 20) -> dict[str, Any]:
        value, error = self._brigade_json(["skills", "search", query, "--target", str(self.workspace), "--json"])
        if error or not isinstance(value, dict):
            return {"error": error or "brigade_unavailable"}
        return {"skills": value.get("skills", [])[:limit], "count": value.get("count", 0)}

    def skill_get(self, name: str, resource: str = "SKILL.md", offset: int = 0) -> dict[str, Any]:
        require(all(part not in (".", "..") for part in resource.split("/")))
        return skill_get(self.workspace, name, resource, offset)


def read_file(path: Path, maximum: int = 1024 * 1024) -> tuple[bytes, os.stat_result]:
    directory = trusted_directory(path.parent, code="source_unavailable")
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("source_unavailable")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("source_unavailable")
    return raw, info


def policy(workspace: Path, since_hash: str | None = None) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    banned: list[str] = []
    for relative in POLICY_PATHS:
        path = workspace / relative
        raw, info = read_file(path)
        content = raw.decode("utf-8")

        def compact(match):
            phrases = re.findall(r'^- "(.*)"\s*$', match.group(), re.M)
            banned.extend(p for p in phrases if p not in banned)
            return "Banned phrases (compact): " + json.dumps(phrases, ensure_ascii=False)

        content = re.sub(
            r"<!-- SLOP-BANLIST:START[^>]*-->.*?<!-- SLOP-BANLIST:END -->",
            compact,
            content,
            flags=re.S,
        )
        sources.append(
            dict(
                path=str(path),
                sha256=hashlib.sha256(raw).hexdigest(),
                mtime=info.st_mtime,
                content=content,
            )
        )
    digest_hash = hashlib.sha256(
        json.dumps([(s["path"], s["sha256"]) for s in sources], separators=(",", ":")).encode()
    ).hexdigest()
    if since_hash == digest_hash:
        return dict(hash=digest_hash, changed=False)
    # Digest is generated each call from the sections actually present on disk.
    sections = {
        "authorization": (
            "Instruction precedence and authorization",
            "Git and publication",
            "PR ownership through merge",
            "Credential automation",
        ),
        "stop_boundaries": (
            "Instruction precedence and authorization",
            "Working limits",
        ),
        "routing": (
            "Public web research and scraping",
            "Dispatch and routing authority",
            "Guidance",
        ),
        "writing_rules": ("Writing and formatting", "Answer shape"),
        "memory_handoff_rules": ("Memory handoffs",),
    }
    digest = {}
    for key, names in sections.items():
        excerpts = []
        for source in sources:
            for name in names:
                match = re.search(
                    r"^#{2,3} " + re.escape(name) + r"\s*\n(.*?)(?=^#{1,3} |\Z)",
                    source["content"],
                    re.M | re.S,
                )
                if match:
                    paragraphs = match[1].strip().split("\n\n")
                    excerpt = "\n\n".join(paragraphs[:2])[:1200]
                    if excerpt not in excerpts:
                        excerpts.append(excerpt)
        digest[key] = "\n\n".join(excerpts)
    return dict(
        hash=digest_hash,
        changed=True,
        sources=sources,
        banned_phrases=banned,
        digest=digest,
        title="Dot operating digest",
        source_of_truth="The control host; reread fleet_policy at task start and before dispatch",
    )


def skill_get(workspace: Path, name: str, resource: str = "SKILL.md", offset: int = 0) -> dict[str, Any]:
    # Brigade's descriptor-anchored registry reader is the installed MCP read path.
    from brigade.skills_cmd.registry import _anchored_registry_text

    content = _anchored_registry_text(Path(workspace), name, *resource.split("/"))
    if content is None:
        return {"error": "skill_not_found"}
    raw = content.encode("utf-8")
    if len(raw) > 1024 * 1024 or offset > len(content):
        return {"error": "source_too_large_or_offset_invalid"}
    end = offset + 24000
    return dict(
        name=name,
        resource=resource,
        sha256=hashlib.sha256(raw).hexdigest(),
        content=content[offset:end],
        next_offset=end if end < len(content) else None,
    )
