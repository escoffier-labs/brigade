"""Read and inbox-propose paths for an operator-configured Obsidian vault.

``brigade memory project-vault`` remains the writer for generated
``Brigade Memory/`` content. This module indexes allowlisted roots, searches
them, shows a cited note, and delivers additive proposals into an allowlisted
inbox. The derived index and proposal staging live under the Brigade state
directory and are owner-readable only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import toml_compat as tomllib
from .card_identity import mint_card_id, valid_card_id
from .guard import redact_text
from .localio import utc_now_iso_z, write_text_atomic
from .projection import kernel

CONFIG_REL_PATH = ".brigade/vault.toml"
INDEX_REL_PATH = ".brigade/vault-index/index.json"
INDEX_SCHEMA = "brigade.vault-index.v1"
RECEIPT_SCHEMA = "brigade.vault-index.receipt.v1"
SEARCH_SCHEMA = "brigade.vault-search.v1"
SHOW_SCHEMA = "brigade.vault-show.v1"
DOCTOR_SCHEMA = "brigade.vault-doctor.v1"
PROPOSE_SCHEMA = "brigade.vault-propose.v1"
SCHEMA_VERSION = 1
TRUST_UNTRUSTED_VAULT = "untrusted_vault_content"
VAULT_REDACTED = "redacted:operator-vault"
PROJECTION_FOLDER = "Brigade Memory"
STAGING_REL_PATH = ".brigade/vault-propose"
PROJECTOR = "vault-propose"
DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 100
SNIPPET_CHARS = 240
SHOW_BODY_CHARS = 32_768
INDEX_BODY_CHARS = 512_000
MAX_PROPOSE_CHARS = 512_000
SLUG_MAX_CHARS = 80
TITLE_EXACT_SCORE = 1000
TITLE_TOKEN_SCORE = 50
TAG_TOKEN_SCORE = 30
PATH_TOKEN_SCORE = 10

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_FRONTMATTER_LIST_ITEM = re.compile(r"^[\t ]+-\s+(.*)$")


class VaultConfigError(ValueError):
    """Invalid or unusable vault.toml."""


class VaultSearchError(ValueError):
    """A vault read command cannot proceed."""


class VaultProposeError(ValueError):
    """A vault propose command cannot proceed."""


@dataclass(frozen=True)
class VaultRoot:
    scope: str
    path: str
    optional: bool


@dataclass(frozen=True)
class VaultConfig:
    schema_version: int
    vault: Path
    roots: tuple[VaultRoot, ...]


def config_path(target: Path) -> Path:
    return target.expanduser().resolve() / CONFIG_REL_PATH


def index_path(target: Path) -> Path:
    return target.expanduser().resolve() / INDEX_REL_PATH


def load_config(target: Path) -> VaultConfig | None:
    """Return the configured vault surface, or None when vault.toml is absent."""
    path = config_path(target)
    if not path.is_file():
        return None
    if tomllib is None:
        raise VaultConfigError("vault config requires Python tomllib support")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise VaultConfigError(f"unreadable vault config: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise VaultConfigError(f"invalid vault config: {exc}") from exc
    if not isinstance(data, dict):
        raise VaultConfigError("vault config must be a TOML object")
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise VaultConfigError("schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise VaultConfigError(f"unsupported vault schema_version: {schema_version}")
    vault_raw = data.get("vault")
    if not isinstance(vault_raw, str) or not vault_raw.strip():
        raise VaultConfigError("vault must be a non-empty string")
    vault = Path(vault_raw.strip()).expanduser()
    if not vault.is_absolute():
        vault = target.expanduser().resolve() / vault
    vault = vault.resolve()
    roots_raw = data.get("roots", [])
    if roots_raw is None:
        roots_raw = []
    if not isinstance(roots_raw, list):
        raise VaultConfigError("roots must be an array of tables")
    roots: list[VaultRoot] = []
    seen: set[str] = set()
    for index, item in enumerate(roots_raw, start=1):
        label = f"roots[{index}]"
        if not isinstance(item, dict):
            raise VaultConfigError(f"{label} must be a table")
        scope = item.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise VaultConfigError(f"{label}.scope must be a non-empty string")
        scope = scope.strip()
        if any(part in scope for part in ("/", "\\", "..")):
            raise VaultConfigError(f"{label}.scope must be a name, not a path")
        if scope in seen:
            raise VaultConfigError(f"duplicate root scope: {scope}")
        seen.add(scope)
        rel = item.get("path")
        if not isinstance(rel, str) or not rel.strip():
            raise VaultConfigError(f"{label}.path must be a non-empty string")
        rel = rel.strip().replace("\\", "/")
        _validate_root_path(rel, field=f"{label}.path")
        optional = item.get("optional", False)
        if not isinstance(optional, bool):
            raise VaultConfigError(f"{label}.optional must be true or false")
        roots.append(VaultRoot(scope=scope, path=rel, optional=optional))
    return VaultConfig(schema_version=schema_version, vault=vault, roots=tuple(roots))


def _validate_root_path(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("~") or ".." in path.parts:
        raise VaultConfigError(f"{field} must be a vault-relative path without '..'")


def validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise VaultSearchError(f"--limit must be between {MIN_LIMIT} and {MAX_LIMIT}")
    return limit


def index(*, target: Path, json_output: bool = False) -> int:
    try:
        payload = build_index(target)
    except (VaultConfigError, VaultSearchError) as exc:
        return _error(str(exc))
    return _print_payload(payload, json_output=json_output, human=_format_index)


def search(
    *,
    target: Path,
    query: str,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
    include_archived: bool = False,
    json_output: bool = False,
) -> int:
    try:
        payload = search_payload(
            target,
            query,
            scope=scope,
            limit=limit,
            include_archived=include_archived,
        )
    except (VaultConfigError, VaultSearchError) as exc:
        return _error(str(exc))
    return _print_payload(payload, json_output=json_output, human=_format_search)


def show(*, target: Path, note_id: str, json_output: bool = False) -> int:
    try:
        payload = show_payload(target, note_id)
    except (VaultConfigError, VaultSearchError) as exc:
        return _error(str(exc))
    return _print_payload(payload, json_output=json_output, human=_format_show)


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = doctor_payload(target)
    _print_payload(payload, json_output=json_output, human=_format_doctor)
    return 0 if payload["valid"] else 1


def propose(
    *,
    target: Path,
    title: str,
    scope: str,
    body: str,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    try:
        payload = propose_payload(target, title=title, scope=scope, body=body, dry_run=dry_run)
    except (VaultConfigError, VaultSearchError, VaultProposeError) as exc:
        return _error(str(exc))
    return _print_payload(payload, json_output=json_output, human=_format_propose)


def build_index(target: Path) -> dict[str, Any]:
    """Walk allowlisted roots and write the derived index. Never writes to the vault."""
    target = target.expanduser().resolve()
    config = _require_config(target)
    notes, skipped = _collect_notes(config)
    built_at = utc_now_iso_z()
    document = {
        "schema": INDEX_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at,
        "vault_fingerprint": _vault_fingerprint(config),
        "notes": [_index_record(note) for note in notes],
        "skipped": skipped,
    }
    path = index_path(target)
    _write_owner_only(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return {
        "schema": RECEIPT_SCHEMA,
        "built_at": built_at,
        "indexed": len(notes),
        "skipped": skipped,
        "scopes": _scope_counts(notes, config),
        "index_path": INDEX_REL_PATH,
        "vault": VAULT_REDACTED,
    }


def search_payload(
    target: Path,
    query: str,
    *,
    scope: str | None = None,
    limit: int = DEFAULT_LIMIT,
    include_archived: bool = False,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    limit = validate_limit(limit)
    normalized_query = _normalize(query)
    if not normalized_query:
        raise VaultSearchError("query must not be empty")
    config = _require_config(target)
    selected = _resolve_scope(config, scope)
    notes = _notes_for_search(target, config)
    query_tokens = _query_tokens(normalized_query)
    hits: list[dict[str, Any]] = []
    for note in notes:
        if selected is not None and note["scope"] != selected:
            continue
        if note.get("archived") and not include_archived:
            continue
        score = score_note(note, normalized_query, query_tokens)
        if score <= 0:
            continue
        hits.append(_hit(note, score=score, query_tokens=query_tokens))
    hits.sort(key=lambda item: (-item["score"], item["relative_path"], item["id"]))
    return {
        "schema": SEARCH_SCHEMA,
        "query": query,
        "scope": selected,
        "limit": limit,
        "include_archived": include_archived,
        "match_count": len(hits),
        "hits": hits[:limit],
        "vault": VAULT_REDACTED,
        "trust": TRUST_UNTRUSTED_VAULT,
    }


def show_payload(target: Path, note_id: str) -> dict[str, Any]:
    target = target.expanduser().resolve()
    if not str(note_id or "").strip():
        raise VaultSearchError("note-id must not be empty")
    config = _require_config(target)
    notes = _notes_for_search(target, config)
    note = _resolve_note(notes, note_id.strip())
    live = _reread_note(config, note)
    source = live if live is not None else note
    body = _redact(_collapse(str(source.get("body") or "")))
    truncated = False
    if len(body) > SHOW_BODY_CHARS:
        body = body[:SHOW_BODY_CHARS]
        truncated = True
    return {
        "schema": SHOW_SCHEMA,
        "id": str(source["id"]),
        "title": _redact(str(source.get("title") or "")),
        "scope": str(source["scope"]),
        "relative_path": str(source["relative_path"]),
        "content_hash": str(source["content_hash"]),
        "updated_at": str(source.get("updated_at") or ""),
        "tags": [_redact(str(tag)) for tag in source.get("tags") or []],
        "snippet": _snippet(str(source.get("body") or ""), []),
        "trust": TRUST_UNTRUSTED_VAULT,
        "aliases": [_redact(str(alias)) for alias in source.get("aliases") or []],
        "archived": bool(source.get("archived")),
        "body": body,
        "truncated": truncated,
        "vault": VAULT_REDACTED,
    }


def doctor_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    checks: list[dict[str, str]] = []
    config: VaultConfig | None = None
    try:
        config = load_config(target)
    except VaultConfigError as exc:
        checks.append({"status": "fail", "name": "vault_config", "detail": str(exc)})
    else:
        if config is None:
            checks.append(
                {
                    "status": "warn",
                    "name": "vault_config",
                    "detail": "missing .brigade/vault.toml; vault-search is inert until configured",
                }
            )
        else:
            checks.append({"status": "ok", "name": "vault_config", "detail": CONFIG_REL_PATH})
            if not config.vault.is_dir():
                checks.append(
                    {
                        "status": "fail",
                        "name": "vault_path",
                        "detail": "configured vault is not a directory",
                    }
                )
            else:
                checks.append({"status": "ok", "name": "vault_path", "detail": VAULT_REDACTED})
            if not config.roots:
                checks.append(
                    {
                        "status": "warn",
                        "name": "vault_roots",
                        "detail": "no roots configured; vault-search is inert",
                    }
                )
            else:
                for root in config.roots:
                    resolved = _root_dir(config.vault, root.path)
                    if resolved is None:
                        status = "warn" if root.optional else "fail"
                        checks.append(
                            {
                                "status": status,
                                "name": f"root:{root.scope}",
                                "detail": (
                                    f"optional root missing: {root.scope}"
                                    if root.optional
                                    else f"required root missing or escapes the vault: {root.scope}"
                                ),
                            }
                        )
                    else:
                        checks.append(
                            {
                                "status": "ok",
                                "name": f"root:{root.scope}",
                                "detail": root.path,
                            }
                        )
    index_file = index_path(target)
    index_doc = _read_index(target)
    if not index_file.is_file():
        checks.append(
            {
                "status": "warn",
                "name": "vault_index",
                "detail": f"missing {INDEX_REL_PATH}; run brigade memory vault-index",
            }
        )
    elif index_doc is None:
        checks.append({"status": "fail", "name": "vault_index", "detail": "index is unreadable or unsupported"})
    else:
        mode = _file_mode(index_file)
        if mode is not None and mode & 0o077:
            checks.append(
                {
                    "status": "fail",
                    "name": "vault_index_permissions",
                    "detail": f"index must be owner-readable only (got {mode:04o})",
                }
            )
        else:
            checks.append(
                {
                    "status": "ok",
                    "name": "vault_index_permissions",
                    "detail": f"{mode:04o}" if mode is not None else "ok",
                }
            )
        note_count = len(index_doc.get("notes") or [])
        checks.append(
            {
                "status": "ok",
                "name": "vault_index",
                "detail": f"{INDEX_REL_PATH} notes={note_count} built_at={index_doc.get('built_at')}",
            }
        )
        if config is not None:
            if index_doc.get("vault_fingerprint") != _vault_fingerprint(config):
                checks.append(
                    {
                        "status": "warn",
                        "name": "vault_index_stale",
                        "detail": (
                            "index was built for a different vault or root allowlist; run brigade memory vault-index"
                        ),
                    }
                )
            extra_scopes = _index_root_drift(index_doc, config)
            if extra_scopes:
                checks.append(
                    {
                        "status": "warn",
                        "name": "vault_index_roots",
                        "detail": (
                            "index still contains revoked scope(s): "
                            f"{', '.join(extra_scopes)}; run brigade memory vault-index"
                        ),
                    }
                )
            collisions = _id_collisions(index_doc.get("notes") if isinstance(index_doc.get("notes"), list) else [])
            if collisions:
                checks.append(
                    {
                        "status": "warn",
                        "name": "vault_identity_collisions",
                        "detail": f"{len(collisions)} duplicate note id(s)",
                    }
                )
    valid = not any(check["status"] == "fail" for check in checks)
    return {
        "schema": DOCTOR_SCHEMA,
        "valid": valid,
        "config_path": CONFIG_REL_PATH,
        "index_path": INDEX_REL_PATH,
        "vault": VAULT_REDACTED,
        "checks": checks,
        "next_action": (
            None
            if valid and config is not None and config.roots
            else "write .brigade/vault.toml with vault, [[roots]] {scope, path, optional}, and schema_version = 1"
        ),
    }


def propose_payload(
    target: Path,
    *,
    title: str,
    scope: str,
    body: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stage a proposal outside the vault, then deliver it into an allowlisted inbox."""
    target = target.expanduser().resolve()
    config = _require_config(target)
    selected = _resolve_scope(config, scope)
    if selected is None:
        raise VaultProposeError("vault-propose requires --scope")
    cleaned_title = str(title or "").strip()
    if not cleaned_title:
        raise VaultProposeError("title must not be empty")
    if any(part in cleaned_title for part in ("/", "\\", "\x00")):
        raise VaultProposeError("title must be a name, not a path")
    text = body if isinstance(body, str) else ""
    if len(text) > MAX_PROPOSE_CHARS:
        raise VaultProposeError(f"proposal body exceeds {MAX_PROPOSE_CHARS} characters")
    root = next(item for item in config.roots if item.scope == selected)
    if _path_has_projection_folder(root.path):
        raise VaultProposeError("refusing to write into the Brigade Memory projection")
    slug = _proposal_slug(cleaned_title)
    relative_path = PurePosixPath(root.path).joinpath(f"{slug}.md").as_posix()
    if _path_has_projection_folder(relative_path):
        raise VaultProposeError("refusing to write into the Brigade Memory projection")
    note_id = mint_card_id()
    rendered = _render_proposal(title=cleaned_title, body=text, note_id=note_id)
    digest = f"sha256:{hashlib.sha256(rendered).hexdigest()}"
    payload = {
        "schema": PROPOSE_SCHEMA,
        "dry_run": dry_run,
        "title": _redact(_one_line(cleaned_title)),
        "scope": selected,
        "relative_path": relative_path,
        "id": note_id,
        "content_hash": digest,
        "bytes": len(rendered),
        "rendered": rendered.decode("utf-8"),
        "vault": VAULT_REDACTED,
        "receipt": None,
        "staging": STAGING_REL_PATH,
    }
    inbox_fd = _open_contained_inbox(config.vault, root.path)
    try:
        _reject_existing_note(inbox_fd, f"{slug}.md")
        if dry_run:
            return payload
        return _deliver_proposal(
            target,
            config=config,
            inbox_fd=inbox_fd,
            filename=f"{slug}.md",
            relative_path=relative_path,
            rendered=rendered,
            payload=payload,
        )
    finally:
        os.close(inbox_fd)


def score_note(note: dict[str, Any], normalized_query: str, query_tokens: list[str]) -> int:
    """Rank one note. Weights are the settled vault-search contract."""
    title = _normalize(str(note.get("title") or ""))
    score = TITLE_EXACT_SCORE if title and title == normalized_query else 0
    title_tokens = set(title.split()) if title else set()
    tag_tokens = _tag_tokens(note.get("tags") or [])
    path_tokens = _path_tokens(str(note.get("relative_path") or ""))
    body_counts = _token_counts(str(note.get("body") or ""))
    for token in query_tokens:
        if token in title_tokens:
            score += TITLE_TOKEN_SCORE
        if token in tag_tokens:
            score += TAG_TOKEN_SCORE
        if token in path_tokens:
            score += PATH_TOKEN_SCORE
        score += body_counts.get(token, 0)
    return score


def _require_config(target: Path) -> VaultConfig:
    config = load_config(target)
    if config is None:
        raise VaultConfigError("vault is not configured; write .brigade/vault.toml")
    if not config.vault.is_dir():
        raise VaultConfigError("configured vault is not a directory")
    return config


def _resolve_scope(config: VaultConfig, scope: str | None) -> str | None:
    if scope is None or not str(scope).strip():
        return None
    name = scope.strip()
    known = {root.scope for root in config.roots}
    if name not in known:
        raise VaultSearchError(f"unknown scope: {name}")
    return name


def _notes_for_search(target: Path, config: VaultConfig) -> list[dict[str, Any]]:
    document = _read_index(target)
    if document is None or document.get("vault_fingerprint") != _vault_fingerprint(config):
        build_index(target)
        document = _read_index(target)
        if document is None:
            raise VaultSearchError("failed to build vault index")
    notes_raw = document.get("notes")
    if not isinstance(notes_raw, list):
        return []
    return [note for note in notes_raw if isinstance(note, dict)]


def _collect_notes(config: VaultConfig) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    notes: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for root in config.roots:
        resolved = _root_dir(config.vault, root.path)
        if resolved is None:
            skipped.append(
                {
                    "scope": root.scope,
                    "reason": "optional-missing" if root.optional else "missing-or-escaped",
                }
            )
            if root.optional:
                continue
            raise VaultSearchError(f"required root missing or escapes the vault: {root.scope}")
        for path in _iter_markdown(resolved):
            relative = path.relative_to(config.vault).as_posix()
            note = _read_note(path, scope=root.scope, relative_path=relative)
            if note is not None:
                notes.append(note)
    notes.sort(key=lambda item: (item["scope"], item["relative_path"], item["id"]))
    return notes, skipped


def _root_dir(vault: Path, relative: str) -> Path | None:
    candidate = (vault / relative).resolve()
    if not _is_within(candidate, vault):
        return None
    if not candidate.is_dir() or candidate.is_symlink():
        return None
    return candidate


def _iter_markdown(root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            name = entry.name
            if name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and name.endswith(".md"):
                    found.append(Path(entry.path))
            except OSError:
                continue
    return sorted(found)


def _read_note(path: Path, *, scope: str, relative_path: str) -> dict[str, Any] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    frontmatter = parse_frontmatter(text)
    body = _without_frontmatter(text)
    title = _note_title(frontmatter, body, path.stem)
    tags = _string_list(frontmatter.get("tags"))
    aliases = _note_aliases(frontmatter, relative_path)
    note_id = _note_id(frontmatter, relative_path)
    return {
        "id": note_id,
        "title": title,
        "scope": scope,
        "relative_path": relative_path,
        "content_hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "updated_at": _mtime_iso(path),
        "tags": tags,
        "archived": _is_archived(frontmatter, relative_path),
        "aliases": aliases,
        "body": body[:INDEX_BODY_CHARS],
    }


def _reread_note(config: VaultConfig, note: dict[str, Any]) -> dict[str, Any] | None:
    relative = str(note.get("relative_path") or "")
    if not relative:
        return None
    path = (config.vault / relative).resolve()
    if not _is_within(path, config.vault) or path.is_symlink() or not path.is_file():
        return None
    return _read_note(path, scope=str(note["scope"]), relative_path=relative)


def _note_id(frontmatter: dict[str, Any], relative_path: str) -> str:
    canonical = frontmatter.get("canonical_id")
    if isinstance(canonical, str) and canonical.strip():
        valid = valid_card_id(canonical)
        return valid if valid is not None else canonical.strip()
    for key in ("id", "card_id"):
        valid = valid_card_id(frontmatter.get(key))
        if valid is not None:
            return valid
    return PurePosixPath(relative_path.replace("\\", "/")).as_posix()


def _note_aliases(frontmatter: dict[str, Any], relative_path: str) -> list[str]:
    aliases = _string_list(frontmatter.get("aliases"))
    for key in ("canonical_id", "id", "card_id"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            aliases.append(value.strip())
            valid = valid_card_id(value)
            if valid is not None:
                aliases.append(valid)
    aliases.append(relative_path)
    aliases.append(PurePosixPath(relative_path).stem)
    return list(dict.fromkeys(item for item in aliases if item))


def _note_title(frontmatter: dict[str, Any], body: str, stem: str) -> str:
    raw = frontmatter.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return stem


def _is_archived(frontmatter: dict[str, Any], relative_path: str) -> bool:
    raw = frontmatter.get("archived")
    if raw is True:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"true", "yes", "1"}:
        return True
    status = frontmatter.get("status")
    if isinstance(status, str) and status.strip().lower() == "archived":
        return True
    parts = relative_path.replace("\\", "/").lower().split("/")
    return any(part in {"archive", "archived"} for part in parts)


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse a small YAML-like frontmatter block used by Obsidian notes."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for raw in lines[1:end]:
        list_item = _FRONTMATTER_LIST_ITEM.match(raw)
        if list_item is not None and current_list is not None:
            current_list.append(_unquote(list_item.group(1).strip()))
            continue
        if ":" not in raw or raw[:1].isspace():
            continue
        if current_key is not None and current_list is not None:
            data[current_key] = current_list
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            current_key = None
            current_list = None
            continue
        if value == "" or value == "|":
            current_key = key
            current_list = []
            continue
        if value.startswith("[") and value.endswith("]"):
            data[key] = _parse_inline_list(value)
            current_key = None
            current_list = None
            continue
        data[key] = _coerce_scalar(value)
        current_key = None
        current_list = None
    if current_key is not None and current_list is not None:
        data[current_key] = current_list
    return data


def _without_frontmatter(text: str) -> str:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return text
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return text


def _parse_inline_list(value: str) -> list[str]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for part in inner.split(","):
        cleaned = _unquote(part.strip())
        if cleaned:
            items.append(cleaned)
    return items


def _coerce_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered == "null":
        return None
    return _unquote(value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _index_record(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note["id"],
        "title": note["title"],
        "scope": note["scope"],
        "relative_path": note["relative_path"],
        "content_hash": note["content_hash"],
        "updated_at": note["updated_at"],
        "tags": list(note.get("tags") or []),
        "archived": bool(note.get("archived")),
        "aliases": list(note.get("aliases") or []),
        "body": note.get("body") or "",
    }


def _hit(note: dict[str, Any], *, score: int, query_tokens: list[str]) -> dict[str, Any]:
    return {
        "id": str(note["id"]),
        "title": _redact(str(note.get("title") or "")),
        "scope": str(note["scope"]),
        "relative_path": str(note["relative_path"]),
        "content_hash": str(note["content_hash"]),
        "updated_at": str(note.get("updated_at") or ""),
        "tags": [_redact(str(tag)) for tag in note.get("tags") or []],
        "snippet": _snippet(str(note.get("body") or ""), query_tokens),
        "trust": TRUST_UNTRUSTED_VAULT,
        "score": score,
    }


def _snippet(body: str, query_tokens: list[str]) -> str:
    collapsed = _collapse(body)
    if not collapsed:
        return ""
    lower = collapsed.lower()
    pos = -1
    for token in query_tokens:
        if not token:
            continue
        found = lower.find(token)
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
    if pos < 0:
        excerpt = collapsed[:SNIPPET_CHARS]
    else:
        start = max(0, pos - SNIPPET_CHARS // 4)
        excerpt = collapsed[start : start + SNIPPET_CHARS]
    return _redact(excerpt)


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _collapse(value: str) -> str:
    return " ".join(value.split())


def _query_tokens(normalized_query: str) -> list[str]:
    return [token for token in normalized_query.split() if token]


def _tag_tokens(tags: object) -> set[str]:
    tokens: set[str] = set()
    for tag in tags if isinstance(tags, list) else []:
        text = _normalize(str(tag))
        if not text:
            continue
        tokens.add(text)
        tokens.update(part for part in re.split(r"[\s/_-]+", text) if part)
    return tokens


def _path_tokens(relative_path: str) -> set[str]:
    tokens: set[str] = set()
    posix = relative_path.replace("\\", "/")
    for part in posix.split("/"):
        if part.endswith(".md"):
            part = part[:-3]
        lowered = part.lower()
        if not lowered:
            continue
        tokens.add(lowered)
        tokens.update(piece for piece in re.split(r"[\s._-]+", lowered) if piece)
    return tokens


def _token_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _WORD_RE.findall(text.lower()):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _resolve_note(notes: list[dict[str, Any]], note_id: str) -> dict[str, Any]:
    wanted = note_id.strip()
    wanted_folded = wanted.casefold()
    matches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for note in notes:
        keys = {str(note.get("id") or ""), str(note.get("relative_path") or "")}
        keys.update(str(alias) for alias in note.get("aliases") or [])
        folded = {key.casefold() for key in keys if key}
        if wanted in keys or wanted_folded in folded:
            path = str(note.get("relative_path") or "")
            if path in seen_paths:
                continue
            seen_paths.add(path)
            matches.append(note)
    if not matches:
        raise VaultSearchError(f"unknown note-id: {note_id}")
    if len(matches) > 1:
        raise VaultSearchError(f"ambiguous note-id: {note_id}")
    return matches[0]


def _id_collisions(notes: object) -> list[str]:
    owners: dict[str, set[str]] = {}
    if not isinstance(notes, list):
        return []
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = str(note.get("id") or "")
        path = str(note.get("relative_path") or "")
        if not note_id:
            continue
        owners.setdefault(note_id, set()).add(path)
    return sorted(note_id for note_id, paths in owners.items() if len(paths) > 1)


def _scope_counts(notes: list[dict[str, Any]], config: VaultConfig) -> list[dict[str, Any]]:
    counts = {root.scope: 0 for root in config.roots}
    for note in notes:
        scope = str(note.get("scope") or "")
        if scope in counts:
            counts[scope] += 1
    return [
        {
            "scope": root.scope,
            "path": root.path,
            "optional": root.optional,
            "indexed": counts.get(root.scope, 0),
        }
        for root in config.roots
    ]


def _read_index(target: Path) -> dict[str, Any] | None:
    path = index_path(target)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != INDEX_SCHEMA:
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    return payload


def _write_owner_only(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    write_text_atomic(path, data)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _vault_fingerprint(config: VaultConfig) -> str:
    """Hash the vault path and the sorted allowlist (scope, path, optional)."""
    roots = [
        {"optional": root.optional, "path": root.path, "scope": root.scope}
        for root in sorted(config.roots, key=lambda item: (item.scope, item.path, item.optional))
    ]
    payload = json.dumps(
        {"roots": roots, "vault": str(config.vault)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _index_root_drift(index_doc: dict[str, Any], config: VaultConfig) -> list[str]:
    """Return indexed scopes that are no longer in the configured allowlist."""
    configured = {root.scope for root in config.roots}
    notes = index_doc.get("notes")
    if not isinstance(notes, list):
        return []
    indexed = {
        str(note.get("scope") or "") for note in notes if isinstance(note, dict) and str(note.get("scope") or "")
    }
    return sorted(scope for scope in indexed if scope not in configured)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return utc_now_iso_z()
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _containment_primitives_available() -> bool:
    return bool(getattr(os, "O_NOFOLLOW", 0)) and bool(getattr(os, "O_DIRECTORY", 0))


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags(mode: int) -> int:
    return mode | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _path_has_projection_folder(relative: str) -> bool:
    return PROJECTION_FOLDER in PurePosixPath(relative.replace("\\", "/")).parts


def _proposal_slug(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", title.strip()).strip("-").lower()
    if not cleaned:
        raise VaultProposeError("title does not produce a usable filename")
    return cleaned[:SLUG_MAX_CHARS]


def _yaml_scalar(value: str) -> str:
    special_prefix = {"-", "'", '"', "[", "{", "*", "&", "!", "|", ">", "%", "@", "`"}
    if not value or value != value.strip() or value[:1] in special_prefix:
        return json.dumps(value)
    if any(char in value for char in (":", "#", "{", "}", "[", "]", ",", "\n")):
        return json.dumps(value)
    return value


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _render_proposal(*, title: str, body: str, note_id: str) -> bytes:
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        "---",
        f"title: {_yaml_scalar(title)}",
        f"canonical_id: {note_id}",
        f"id: {note_id}",
        "---",
        "",
    ]
    if normalized.strip():
        lines.append(normalized.rstrip("\n"))
        lines.append("")
    else:
        lines.append(f"# {title}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _open_contained_inbox(vault: Path, relative: str) -> int:
    """Open the inbox directory without following any symlink component."""
    if not _containment_primitives_available():
        raise VaultProposeError("vault containment checks are unavailable on this platform")
    try:
        vault_stat = os.lstat(vault)
    except OSError as exc:
        raise VaultProposeError(f"configured vault is unreadable: {exc}") from exc
    if stat.S_ISLNK(vault_stat.st_mode) or not stat.S_ISDIR(vault_stat.st_mode):
        raise VaultProposeError("configured vault must be a real directory")
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise VaultProposeError("inbox path is unsafe")
    flags = _directory_flags()
    try:
        descriptor = os.open(vault, flags)
    except OSError as exc:
        raise VaultProposeError(f"configured vault is not a contained directory: {exc}") from exc
    for part in parts:
        try:
            nxt = os.open(part, flags, dir_fd=descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise VaultProposeError(f"required inbox missing or escapes the vault: {exc}") from exc
        os.close(descriptor)
        try:
            opened = os.fstat(nxt)
        except OSError as exc:
            os.close(nxt)
            raise VaultProposeError(f"inbox parent is unreadable: {exc}") from exc
        if not stat.S_ISDIR(opened.st_mode):
            os.close(nxt)
            raise VaultProposeError("inbox parent chain contains a symlink or non-directory")
        descriptor = nxt
    return descriptor


def _reject_existing_note(inbox_fd: int, filename: str) -> None:
    if not _containment_primitives_available():
        raise VaultProposeError("vault containment checks are unavailable on this platform")
    try:
        existing = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=inbox_fd,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise VaultProposeError(f"refusing to overwrite an existing note: {exc}") from exc
    os.close(existing)
    raise VaultProposeError("refusing to overwrite an existing note")


def _prepare_staging_dir(target: Path, vault: Path) -> Path:
    if not _containment_primitives_available():
        raise VaultProposeError("vault containment checks are unavailable on this platform")
    staging = target / STAGING_REL_PATH
    if staging.exists() and (staging.is_symlink() or not staging.is_dir()):
        raise VaultProposeError("staging directory must be a real owner-only directory")
    staging.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(staging, 0o700)
    except OSError as exc:
        raise VaultProposeError(f"could not set owner-only staging permissions: {exc}") from exc
    try:
        if staging.is_symlink():
            raise VaultProposeError("staging directory must not be a symlink")
        resolved = staging.resolve()
        vault_resolved = vault.resolve()
    except OSError as exc:
        raise VaultProposeError(f"unable to resolve staging directory: {exc}") from exc
    if _is_within(resolved, vault_resolved):
        raise VaultProposeError("staging directory resolves inside the vault")
    mode = _file_mode(staging)
    if mode is None or mode & 0o077:
        raise VaultProposeError(
            f"staging directory must be owner-accessible only (got {mode:04o})"
            if mode is not None
            else "staging directory permissions are unreadable"
        )
    return staging


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise VaultProposeError("short write to staged proposal")
        view = view[written:]


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stage_proposal(staging_dir: Path, data: bytes) -> tuple[int, Path, bytes]:
    """Write the proposal outside the vault and hold the validated fd open."""
    if not _containment_primitives_available():
        raise VaultProposeError("vault containment checks are unavailable on this platform")
    path = staging_dir / f"{uuid.uuid4().hex}.md"
    flags = _file_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise VaultProposeError(f"could not create staged proposal: {exc}") from exc
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        validated = _read_exact(descriptor, len(data) + 1)
        if validated != data:
            raise VaultProposeError("staged bytes changed during write")
        mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
        if mode & 0o077:
            raise VaultProposeError(f"staged proposal must be owner-readable only (got {mode:04o})")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, path, validated
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _write_via_held_parent(parent_fd: int, filename: str, data: bytes) -> None:
    if not _containment_primitives_available():
        raise VaultProposeError("vault containment checks are unavailable on this platform")
    flags = _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        descriptor = os.open(filename, flags, 0o644, dir_fd=parent_fd)
    except OSError as exc:
        raise OSError(f"contained inbox write failed: {exc}") from exc
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _deliver_proposal(
    target: Path,
    *,
    config: VaultConfig,
    inbox_fd: int,
    filename: str,
    relative_path: str,
    rendered: bytes,
    payload: dict[str, Any],
) -> dict[str, Any]:
    staging_dir = _prepare_staging_dir(target, config.vault)
    staged_fd, staged_path, validated = _stage_proposal(staging_dir, rendered)
    try:
        # Deliver the bytes already read from the held fd. Do not re-open by path.
        delivered = validated
        source_digest = hashlib.sha256(delivered).hexdigest()
        operation_id = f"vault-propose-{source_digest[:16]}-{uuid.uuid4().hex[:12]}"
        destination = config.vault / relative_path

        def writer(_destination: Path, data: bytes) -> None:
            if data != delivered:
                raise VaultProposeError("delivery bytes do not match the held staged source")
            _write_via_held_parent(inbox_fd, filename, data)

        plan = kernel.build_plan(
            operation_id=operation_id,
            projector=PROJECTOR,
            source_fingerprint=source_digest,
            mutations=[
                kernel.mutation(
                    destination=destination,
                    mutation="create",
                    expected_before=kernel.ABSENT,
                    desired_after=kernel.content_digest(delivered),
                    staged_bytes=delivered,
                    display_path=f"vault/{relative_path}",
                    writer=writer,
                    propagate_error=True,
                )
            ],
            target=target,
        )
        receipt = kernel.execute(plan, target=target).to_dict()
        payload["receipt"] = receipt
        if receipt.get("terminal_state") != "committed":
            raise VaultProposeError(
                str(receipt.get("diagnostic") or f"proposal delivery {receipt.get('terminal_state')}")
            )
        return payload
    except kernel.ProjectionError as exc:
        raise VaultProposeError(exc.diagnostic) from exc
    finally:
        os.close(staged_fd)
        staged_path.unlink(missing_ok=True)


def _redact(value: str) -> str:
    return redact_text(value)


def _format_index(payload: dict[str, Any]) -> None:
    print(f"memory vault-index: indexed={payload['indexed']} scopes={len(payload['scopes'])}")
    for scope in payload["scopes"]:
        print(f"- {scope['scope']}  {scope['indexed']} note(s)")


def _format_search(payload: dict[str, Any]) -> None:
    print(f"memory vault-search: {payload['query']} ({payload['match_count']} hit(s))")
    for hit in payload["hits"]:
        print(f"- [{hit['scope']}] {hit['relative_path']}  [{hit['score']}]  {hit['title']}")
        if hit.get("snippet"):
            print(f"  {hit['snippet']}")


def _format_show(payload: dict[str, Any]) -> None:
    print(f"memory vault-show: {payload['id']}")
    print(f"title: {payload['title']}")
    print(f"scope: {payload['scope']}")
    print(f"relative_path: {payload['relative_path']}")
    print(f"content_hash: {payload['content_hash']}")
    print(f"trust: {payload['trust']}")
    print(payload["body"])


def _format_doctor(payload: dict[str, Any]) -> None:
    print("memory vault-doctor")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")


def _format_propose(payload: dict[str, Any]) -> None:
    label = "memory vault-propose dry-run" if payload.get("dry_run") else "memory vault-propose"
    print(f"{label}: {payload['relative_path']}")
    print(f"id: {payload['id']}")
    print(f"scope: {payload['scope']}")
    print(f"bytes: {payload['bytes']}")
    if payload.get("dry_run"):
        print()
        print(payload["rendered"], end="" if str(payload["rendered"]).endswith("\n") else "\n")


def _print_payload(payload: dict[str, Any], *, json_output: bool, human: Any) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        human(payload)
    return 0


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2
