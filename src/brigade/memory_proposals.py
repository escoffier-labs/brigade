"""Exact, locally reviewed two-card proposals; canonical edits use one transaction."""

from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlsplit

from . import config as brigade_config, evidence_redaction, localio, memory_cmd, provenance, trust_gate
from . import memory_proposal_io as io
from .card_fingerprint import _upsert_frontmatter_fields, opposite_polarity, strip_frontmatter
from .card_identity import IdentityIndex, card_identity, mint_stable_card_id
from .handoff_cmd.drafts import _render_handoff_draft
from .inbox_lock import held_file_lock
from .projection import kernel
from .selection import WRITER_INBOXES
from .untrusted import scan_untrusted

SCHEMA = "brigade.memory-proposal.v1"
RECEIPT_SCHEMA = "brigade.memory-proposal-apply.v1"
STATE_REL = ".brigade/memory/proposals"
SCOPE_FIELDS = ("namespace", "scope", "owner", "repository", "task", "operator", "branch", "worktree")
SLUG = re.compile(r"proposal-[0-9a-f]{32}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]*)\)")
WIKI = re.compile(r"\[\[([^\]\n]+)\]\]")
REFERENCE_DEFINITION = re.compile(r"(?m)^[ \t]{0,3}\[[^]\r\n]+\]:[ \t]*(?:\r?\n[ \t]*)?(?:<([^>\r\n]+)>|([^ \t\r\n]+))")


class ProposalError(ValueError):
    """A safe input refusal (2) or stale/decision/transaction refusal (1)."""

    def __init__(self, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def _boundary(exit_code: int) -> Callable:
    def decorate(function: Callable) -> Callable:
        @functools.wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            # Invalid operator syntax is distinct from a stale archived revision.
            if "proposal_id" in kwargs:
                value = kwargs["proposal_id"]
                if not isinstance(value, str) or not SLUG.fullmatch(value):
                    raise ProposalError("invalid proposal ID")
            if "digest" in kwargs:
                value = kwargs["digest"]
                if not isinstance(value, str) or not HEX.fullmatch(value):
                    raise ProposalError("invalid proposal digest")
            if "reason" in kwargs:
                _reason(kwargs["reason"])
            try:
                return function(*args, **kwargs)
            except ProposalError as exc:
                if exit_code == 1:
                    exc.exit_code = 1
                raise
            except (OSError, ValueError, KeyError, TypeError, kernel.ProjectionError) as exc:
                # Parser and OS errors can contain excluded source bytes or paths.
                raise ProposalError("unsafe, malformed or stale proposal input/state", exit_code=exit_code) from exc

        return wrapped

    return decorate


def _target(target: Path) -> Path:
    path = Path(os.path.abspath(target.expanduser()))
    with io.directory(path):
        pass
    return path.resolve()


def _reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > io.MAX_REASON_BYTES:
        raise ProposalError("reason must be nonempty and bounded")
    _clean(reason)
    return reason


def _clean(text: str) -> None:
    if scan_untrusted(text).flagged:
        raise ProposalError("injection scan denied proposed material")


def _root(target: Path) -> Path:
    return target / STATE_REL


def _outside_roots(path: Path, target: Path, care: memory_cmd.MemoryCareConfig) -> None:
    if any(path.is_relative_to(target / root) for root in care.card_roots):
        raise ProposalError("proposal state or notification overlaps a canonical card root")


def _configuration(target: Path) -> tuple[memory_cmd.MemoryCareConfig, dict[str, Any]]:
    digests: dict[str, str] = {}
    for name in (
        brigade_config.CONFIG_REL_PATH,
        *[f"{d}/config.json" for d in brigade_config.LEGACY_WORKSPACE_DIRNAMES],
        memory_cmd.CONFIG_REL_PATH,
    ):
        raw = io.read_bytes(target / name, absent=True)
        digests[name] = io.sha256(raw) if raw is not None else "absent"
    cfg = brigade_config.load_config(target)
    care = memory_cmd._config_or_default(target)
    for values in (care.card_roots, care.index_paths, care.include_paths, care.exclude_paths):
        if len(values) > io.MAX_LIST:
            raise ProposalError("configuration list limit exceeded")
        for value in values:
            io.relative(value)
    io.relative(care.output_path)
    if len(care.index_paths) > io.MAX_INDEXES or not care.card_roots:
        raise ProposalError("card roots or index count unsupported")
    # Existing readers are reused only after their paths and bytes are checked.
    for root in care.card_roots:
        with io.directory(target / root):
            pass
    workspace = target.stat()
    binding = {
        "owner": cfg.selection.owner if cfg else "this-repo",
        "workspace": str(target),
        "workspace_fingerprint": io.sha256(io.json_bytes([str(target), workspace.st_dev, workspace.st_ino])),
        "config_digests": digests,
        "available_scope": {
            "depth": cfg.selection.depth if cfg else "repo",
            "harnesses": cfg.selection.harnesses if cfg else [],
        },
        "care_config": dict(vars(care)),
    }
    # JSON roundtrip gives stable arrays rather than in-memory tuple comparisons.
    return care, json.loads(io.json_bytes(binding))


def _finding(target: Path, care: memory_cmd.MemoryCareConfig, issue_id: str) -> dict[str, Any]:
    # _scan_path may use a legacy fallback. Validate both candidate locations first.
    primary = target / care.output_path / "scan-latest.json"
    io.read_bytes(primary, absent=True)
    if care.output_path == memory_cmd.DEFAULT_OUTPUT_PATH:
        io.read_bytes(target / memory_cmd.LEGACY_OUTPUT_PATH / "scan-latest.json", absent=True)
    scan_path = memory_cmd._scan_path(target, care)
    raw = io.read_bytes(scan_path)
    scan = memory_cmd._load_scan_payload(target, care)
    if not isinstance(scan, dict) or scan.get("target") != str(target):
        raise ProposalError("saved scan target does not match selected workspace")
    if scan != json.loads(raw or b""):
        raise ProposalError("saved scan changed during inspection")
    issues = scan.get("issues")
    if not isinstance(issues, list) or len(issues) > io.MAX_FILES:
        raise ProposalError("saved finding list invalid or oversized")
    found = [x for x in issues if isinstance(x, dict) and x.get("id") == issue_id]
    if len(found) != 1 or found[0].get("issue_type") != "contradictory":
        raise ProposalError("one saved contradictory finding is required")
    issue = found[0]
    paths = issue.get("evidence_references")
    if not isinstance(paths, list) or len(paths) != 2 or len(set(paths)) != 2:
        raise ProposalError("finding must name exactly two distinct canonical card paths")
    for path in paths:
        io.relative(path)
    if not isinstance(issue.get("source_fingerprint"), str) or not issue["source_fingerprint"]:
        raise ProposalError("saved finding fingerprint missing")
    return issue


def _body_offset(text: str) -> int:
    # Only LF/CRLF fences delimit frontmatter. Body separators are opaque bytes.
    match = re.match(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", text, re.MULTILINE | re.DOTALL)
    return match.end() if match else 0


def _update_metadata(text: str, fields: dict[str, Any]) -> str:
    offset = _body_offset(text)
    header = text[:offset] if offset else "---\n---\n"
    ending = "\r\n" if header.startswith("---\r\n") else "\n"
    updated = _upsert_frontmatter_fields(header.replace("\r\n", "\n"), fields)
    if not updated.endswith("\n"):
        updated += "\n"
    return updated.replace("\n", ending) + text[offset:]


def _meta(text: str) -> dict[str, Any]:
    normalized = text.replace("\r\n", "\n")
    meta, valid = memory_cmd._parse_frontmatter(normalized)
    if normalized.lstrip().startswith("---") and not valid:
        raise ProposalError("malformed canonical frontmatter")
    if valid:
        offset = _body_offset(normalized)
        if not offset:
            raise ProposalError("unsupported canonical frontmatter fences")
        header = normalized[:offset].removesuffix("\n").split("\n")
        lines = header[1:-1]
        seen: set[str] = set()
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if ":" not in line or line[0].isspace():
                raise ProposalError("unsupported structured canonical metadata")
            key = line.split(":", 1)[0].strip()
            if key.startswith(("'", '"')) or key in seen:
                raise ProposalError("ambiguous canonical metadata")
            seen.add(key)
        for key in ("provenance", "metadata", "trust", "injection_scan", "injection"):
            value = meta.get(key)
            if isinstance(value, str) and value.startswith("{"):
                try:
                    meta[key] = json.loads(value)
                except ValueError:
                    raise ProposalError("malformed provenance or trust metadata") from None
    return meta


def _admit(text: str, meta: dict[str, Any]) -> None:
    contexts = [meta]
    envelopes: list[dict[str, Any]] = []
    if "metadata" in meta:
        nested = meta["metadata"]
        if not isinstance(nested, dict) or not isinstance(nested.get("provenance"), dict):
            raise ProposalError("malformed provenance envelope")
        contexts.append(nested)
    for context in contexts:
        if "provenance" in context:
            env = context["provenance"]
            if not isinstance(env, dict):
                raise ProposalError("malformed provenance envelope")
            envelopes.append(env)
    labels: set[str] = set()
    # Each declared channel must agree and be eligible. A preferred envelope
    # cannot hide a stale sibling or a scalar quarantine/injection declaration.
    for context in [*contexts, *envelopes]:
        for key in ("trust", "trust_label"):
            if key in context:
                value = context[key]
                label = value.get("label") if isinstance(value, dict) else value
                if label not in ("reviewed", "verified"):
                    raise ProposalError("source trust label is ineligible")
                labels.add(label)
        injection_contexts = [context]
        if isinstance(context.get("trust"), dict):
            injection_contexts.append(context["trust"])
        for injection_context in injection_contexts:
            for key in ("injection_status", "injection_scan", "injection"):
                if key in injection_context:
                    value = injection_context[key]
                    status = value.get("status") if isinstance(value, dict) else value
                    if status != "clean":
                        raise ProposalError("source injection status is ineligible")
    if len(labels) > 1:
        raise ProposalError("conflicting source trust declarations")
    body = strip_frontmatter(text.replace("\r\n", "\n"))
    for env in envelopes:
        record = {"provenance": env, "text": body}
        if trust_gate.trust_label_of(env) not in ("reviewed", "verified"):
            raise ProposalError("source provenance trust is ineligible")
        if provenance.injection_status(env) != "clean" or trust_gate.promotion_blocker(record):
            raise ProposalError("source provenance integrity is ineligible")
    _clean(text)


def _scope_declarations(meta: dict[str, Any]) -> dict[str, Any]:
    nested = meta.get("metadata", {})
    active = trust_gate.envelope_from_record(meta) if "provenance" in meta or "metadata" in meta else {}
    contexts = [meta, nested, active]
    # Inspect both envelopes when a source declares both, even though consumers
    # prefer the top-level one. Conflicting declarations cannot hide in shadow.
    if isinstance(nested, dict) and isinstance(nested.get("provenance"), dict):
        contexts.append(nested["provenance"])
    result: dict[str, Any] = {}
    for context in contexts:
        if not isinstance(context, dict):
            raise ProposalError("malformed source scope metadata")
        for key in SCOPE_FIELDS:
            if key not in context:
                continue
            value = context[key]
            if key == "repository" and isinstance(value, dict):
                value = value.get("id")
            if not isinstance(value, str) or not value.strip():
                raise ProposalError(f"malformed source {key}")
            if key in result and result[key] != value:
                raise ProposalError(f"conflicting source {key} declarations")
            result[key] = value
    return result


def _scope(sources: list[dict[str, Any]], issue: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    metas = [_scope_declarations(x["metadata"]) for x in sources]
    scope: dict[str, Any] = {}
    for key in SCOPE_FIELDS:
        if key in metas[0] or key in metas[1]:
            if key not in metas[0] or key not in metas[1] or metas[0][key] != metas[1][key]:
                raise ProposalError(f"source {key} does not match")
            scope[key] = metas[0][key]
        if key == "owner" and key in scope and scope[key] != binding["owner"]:
            raise ProposalError("source owner does not match selected owner")
        if key == "worktree" and key in scope and scope[key] != binding["workspace"]:
            raise ProposalError("source worktree does not match selected workspace")
        for context in (issue, issue.get("context", {}), issue.get("metadata", {})):
            if not isinstance(context, dict):
                raise ProposalError("malformed finding context")
            if key in context:
                expected = scope.get(key, binding["owner"] if key == "owner" else None)
                if expected is None or context[key] != expected:
                    raise ProposalError(f"finding {key} conflicts with source/owner context")
    return scope


def _sources(
    target: Path, care: memory_cmd.MemoryCareConfig, issue: dict[str, Any], survivor: str
) -> tuple[list[dict[str, Any]], list[Path]]:
    paths = memory_cmd._iter_cards(target, care)
    if len(paths) > io.MAX_FILES:
        raise ProposalError("canonical corpus file limit exceeded")
    allowlist = {p.relative_to(target).as_posix() for p in paths}
    pair = issue["evidence_references"]
    if survivor not in pair or any(p not in allowlist for p in pair):
        raise ProposalError("finding sources must be in current card allowlist")
    roots = [tuple(root for root in care.card_roots if (target / p).is_relative_to(target / root)) for p in pair]
    if len(roots[0]) != 1 or roots[0] != roots[1]:
        raise ProposalError("pair must share exactly one configured card root")
    order = [survivor, next(p for p in pair if p != survivor)]
    sources: list[dict[str, Any]] = []
    for rel in order:
        text = io.text(target / rel, limit=io.MAX_CARD_BYTES)
        assert text is not None
        meta = _meta(text)
        _admit(text, meta)
        identity = card_identity(meta, rel)
        sources.append(
            {
                "path": rel,
                "text": text,
                "sha256": io.sha256(text.encode()),
                "id": identity.card_id,
                "aliases": list(identity.aliases),
                "explicit_id": identity.explicit,
                "metadata": meta,
            }
        )
    return sources, paths


def _citations(target: Path, sources: list[dict[str, Any]], issue: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for source in sources:
        pointers: list[dict[str, Any]] = []
        refs: list[str] = []
        for key in ("evidence", "sources", "source", "refs", "links"):
            value = source["metadata"].get(key, [])
            items = value if isinstance(value, list) else [value]
            if len(items) > io.MAX_LIST or not all(isinstance(x, str) and len(x) <= 1024 for x in items):
                raise ProposalError("source citation list malformed or oversized")
            refs.extend(items)
        if len(refs) > io.MAX_LIST:
            raise ProposalError("source citation count exceeded")
        for ref in dict.fromkeys(refs):
            parsed = urlsplit(ref)
            if parsed.scheme in ("https", "http") and parsed.netloc:
                pointers.append({"pointer": ref, "status": "external-not-fetched"})
                continue
            try:
                rel = io.relative(ref.removeprefix("receipt:").split("#", 1)[0])
                raw = io.read_bytes(target / rel, absent=True)
                if raw is None:
                    raise ValueError("missing")
                pointers.append({"pointer": ref, "status": "local-resolved", "sha256": io.sha256(raw)})
            except (OSError, ValueError):
                pointers.append(
                    {"pointer": ref, "status": "excluded", "reason": "invalid or unresolvable local pointer"}
                )
        citations.append(
            {
                "path": source["path"],
                "card_id": source["id"],
                "sha256": source["sha256"],
                "provenance": source["metadata"].get("provenance", source["metadata"].get("metadata")),
                "pointers": pointers,
                "finding_id": issue["id"],
                "finding_fingerprint": issue["source_fingerprint"],
            }
        )
    return citations


def _change(path: str, before: str | None, after: str | None) -> dict[str, Any]:
    return {
        "path": path,
        "kind": "remove" if after is None else "replace",
        "before": before,
        "after": after,
        "before_sha256": io.sha256(before.encode()) if before is not None else "absent",
        "after_sha256": io.sha256(after.encode()) if after is not None else "absent",
    }


def _post_identity(target: Path, paths: list[Path], survivor: str, loser: str, after: str) -> IdentityIndex[str]:
    index: IdentityIndex[str] = IdentityIndex()
    total = 0
    for path in paths:
        rel = path.relative_to(target).as_posix()
        if rel == loser:
            continue
        text = after if rel == survivor else io.text(path, limit=io.MAX_CARD_BYTES)
        assert text is not None
        total += len(text.encode())
        if total > io.MAX_CORPUS_BYTES:
            raise ProposalError("canonical corpus byte limit exceeded")
        meta = _meta(text) if rel == survivor else memory_cmd._parse_frontmatter(text)[0]
        identity = card_identity(meta, rel)
        index.claim_identity(identity, rel)
        # Wiki consumers also normalize historical case/path/stem spellings.
        for key in (identity.card_id, *identity.aliases):
            index.claim(key.lower().removesuffix(".md").removeprefix("cards/"), rel)
    survivor_identity = card_identity(_meta(after), survivor)
    governed = {survivor_identity.card_id, *survivor_identity.aliases}
    governed.update(key.lower().removesuffix(".md").removeprefix("cards/") for key in tuple(governed))
    if any(index.resolve(key) != survivor for key in governed):
        raise ProposalError("post-state identity collision with another card")
    return index


def _references(
    target: Path,
    care: memory_cmd.MemoryCareConfig,
    paths: list[Path],
    survivor: str,
    loser: str,
    after: str,
    index: IdentityIndex[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    # Canonical cards and configured indexes define the reference corpus.
    # Derived vaults and workspace documents do not expand edit authority.
    documents = set(paths)
    documents.update(target / name for name in care.index_paths)
    mutations: list[dict[str, Any]] = []
    index_fences: dict[str, str] = {}
    total = 0
    for path in sorted(documents):
        rel = path.relative_to(target).as_posix()
        if rel == loser:
            continue
        original = io.text(path, limit=io.MAX_CARD_BYTES, absent=True)
        if rel in care.index_paths:
            index_fences[rel] = io.sha256(original.encode()) if original is not None else "absent"
        if original is None:
            continue
        total += len(original.encode())
        if total > io.MAX_CORPUS_BYTES:
            raise ProposalError("reference corpus byte limit exceeded")
        content = after if rel == survivor else original
        # Reference definitions are fenced rather than rewritten. Resolve the
        # destination relative to this referrer, including nested index files.
        for definition in REFERENCE_DEFINITION.finditer(content):
            raw = definition.group(1) or definition.group(2)
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                continue
            canonical = posixpath.normpath(posixpath.join(posixpath.dirname(rel), unquote(parsed.path)))
            if canonical == loser:
                raise ProposalError("unhandled removed-card reference definition")
        recognized: list[tuple[int, int]] = []

        def replace(match: re.Match[str], rel: str = rel, recognized: list[tuple[int, int]] = recognized) -> str:
            raw = match.group(1)
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc:
                return match.group(0)
            destination = unquote(parsed.path)
            canonical = posixpath.normpath(posixpath.join(posixpath.dirname(rel), destination))
            if canonical != loser:
                return match.group(0)
            recognized.append(match.span())
            if raw != destination + ("#" + parsed.fragment if parsed.fragment else "") or parsed.query:
                raise ProposalError("ambiguous removed-card reference")
            if rel not in care.index_paths:
                raise ProposalError("non-index direct reference would break")
            anchor = parsed.fragment
            if anchor and not re.fullmatch(r"[A-Za-z0-9_-]+", anchor):
                raise ProposalError("unsafe anchor in removed-card reference")
            destination = posixpath.relpath(survivor, posixpath.dirname(rel) or ".")
            rewritten = destination + ("#" + anchor if anchor else "")
            start, end = match.start(1) - match.start(), match.end(1) - match.start()
            return match.group(0)[:start] + rewritten + match.group(0)[end:]

        rewritten = LINK.sub(replace, content)
        for match in re.finditer(re.escape(loser), content):
            if not any(start <= match.start() < end for start, end in recognized):
                # Revision metadata and migrated wiki aliases are deliberately
                # opaque identifiers, rather than direct Markdown file links.
                if rel == survivor and match.start() < _body_offset(content):
                    continue
                if any(m.start() <= match.start() < m.end() for m in WIKI.finditer(content)):
                    continue
                raise ProposalError("ambiguous removed-card reference")
        if rel in care.index_paths and rewritten != original:
            _clean(rewritten)
            mutations.append(_change(rel, original, rewritten))
        for match in WIKI.finditer(content):
            key = match.group(1).split("|", 1)[0].split("#", 1)[0]
            key = key.lower().removesuffix(".md").removeprefix("cards/")
            migration = card_identity(_meta(after), survivor)
            governed = {
                k.lower().removesuffix(".md").removeprefix("cards/") for k in (migration.card_id, *migration.aliases)
            }
            if key in governed and index.is_collision(key):
                raise ProposalError("ambiguous canonical wiki reference")
    return mutations, index_fences


def _reviewed_trust_fields(meta: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "trust_label" in meta:
        fields["trust_label"] = "reviewed"
    if "trust" in meta:
        fields["trust"] = {**meta["trust"], "label": "reviewed"} if isinstance(meta["trust"], dict) else "reviewed"
    return fields


def _encoded_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: io.json_bytes(value).decode().strip() if isinstance(value, dict) else value
        for key, value in fields.items()
    }


def _derive_merge_provenance(after: str, sources: list[dict[str, Any]], survivor: str, proposal_id: str) -> str:
    declared = ["provenance" in s["metadata"] or "metadata" in s["metadata"] for s in sources]
    meta = sources[0]["metadata"]
    if not any(declared):
        labelled = [any(key in s["metadata"] for key in ("trust", "trust_label")) for s in sources]
        if not any(labelled):
            return after
        if not all(labelled):
            raise ProposalError(
                "mixed declared-label and unlabelled merge requires separate content trust review; use supersede"
            )
        # Admission limits both inputs to consistent reviewed/verified labels.
        # A transformed legacy body can retain at most reviewed trust.
        after = _update_metadata(after, _encoded_fields(_reviewed_trust_fields(meta)))
        _admit(after, _meta(after))
        return after
    if not all(declared):
        raise ProposalError(
            "mixed declared-envelope and legacy merge requires separate content trust review; use supersede"
        )
    envelopes = [trust_gate.envelope_from_record(s["metadata"]) for s in sources]
    # Admission already limits sources to reviewed/verified. Transformation
    # cannot inherit verified status or exceed the weakest source's reviewed.
    body = strip_frontmatter(after.replace("\r\n", "\n"))
    exact_body = after[_body_offset(after) :]
    for text in (exact_body, body):
        verdict = evidence_redaction.apply_origin_redaction(text, origin="workspace")
        if verdict.status != "clean" or verdict.persisted_text != text:
            raise ProposalError("merged result redaction would change exact preview")
    status, count, rules = trust_gate.scan_injection(body)
    if status != "clean":
        raise ProposalError("merged result injection scan denied preview")
    repositories = [env["repository"] for env in envelopes]
    env = provenance.build_envelope(
        source_system="brigade",
        source_kind="memory-proposal",
        source_producer="brigade",
        origin="workspace",
        repository_id=repositories[0]["id"],
        repository_revision=repositories[0].get("revision") if repositories[0] == repositories[1] else None,
        session_id=None,
        session_harness=None,
        collection_id="memory-proposals",
        item_id=proposal_id,
        locator_kind="repo-relative",
        locator_value=survivor,
        attribution="observed",
        modality="mixed",
        trust_label="reviewed",
        trust_assigned_by="brigade:memory-proposal-derived",
        trust_assigned_at=None,
        injection_status=status,
        injection_count=count,
        injection_rules=rules,
        text=body,
        raw_bytes=None,
        content_scope="item.text.utf8.v1",
        captured_at=None,
        ingested_at=None,
        redaction=verdict.record(),
    )
    fields = _reviewed_trust_fields(meta)
    if "provenance" in meta:
        fields["provenance"] = env
    if "metadata" in meta:
        nested = meta["metadata"]
        fields["metadata"] = {**nested, **_reviewed_trust_fields(nested), "provenance": env}
    after = _update_metadata(after, _encoded_fields(fields))
    _admit(after, _meta(after))
    return after


def _compose(
    target: Path,
    care: memory_cmd.MemoryCareConfig,
    issue: dict[str, Any],
    binding: dict[str, Any],
    survivor: str,
    relation: str,
    reason: str,
    proposal_id: str,
) -> dict[str, Any]:
    sources, paths = _sources(target, care, issue, survivor)
    declared = ["provenance" in source["metadata"] or "metadata" in source["metadata"] for source in sources]
    if relation == "merge" and any(declared) and not all(declared):
        raise ProposalError(
            "mixed declared-envelope and legacy merge requires separate content trust review; use supersede"
        )
    scope = _scope(sources, issue, binding)
    left, right = sources
    collision = sorted(set([left["id"], *left["aliases"]]) & set([right["id"], *right["aliases"]]))
    if not collision:
        raise ProposalError("saved pair no longer has an identity collision")
    signal = (
        "opposite-polarity"
        if opposite_polarity(strip_frontmatter(left["text"]), strip_frontmatter(right["text"]))
        else "none"
    )
    if relation == "merge" and signal != "none":
        raise ProposalError("opposite polarity requires explicit supersede")
    survivor_id = (
        left["id"]
        if left["explicit_id"]
        else mint_stable_card_id(f"memory-proposal:{binding['workspace_fingerprint']}:{survivor}:{left['sha256']}")
    )
    aliases = list(dict.fromkeys([left["id"], *left["aliases"], right["id"], *right["aliases"]]))
    aliases = [a for a in aliases if a != survivor_id]
    if len(aliases) > io.MAX_LIST or any(
        not isinstance(a, str) or not a.strip() or len(a) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in a)
        for a in aliases
    ):
        raise ProposalError("migration alias limit exceeded")
    citations = _citations(target, sources, issue)
    supporting = sources if relation == "merge" else [left]
    evidence = list(dict.fromkeys(p["pointer"] for c in citations[: len(supporting)] for p in c["pointers"]))
    if len(evidence) > io.MAX_LIST:
        raise ProposalError("merged evidence count exceeded")
    revision_ref = f"{right['path']}@sha256:{right['sha256']}"
    relation_key = "merged_from" if relation == "merge" else "supersedes"
    prior_rel = left["metadata"].get(relation_key, [])
    if (
        not isinstance(prior_rel, list)
        or len(prior_rel) >= io.MAX_LIST
        or not all(isinstance(x, str) for x in prior_rel)
    ):
        raise ProposalError("existing relation metadata malformed or oversized")
    fields = {
        "id": survivor_id,
        "aliases": aliases,
        "evidence": evidence,
        relation_key: list(dict.fromkeys([*prior_rel, revision_ref])),
        "memory_proposal": [proposal_id, "revision:1", relation],
        "memory_proposal_sources": [f"{s['path']}@sha256:{s['sha256']}" for s in sources],
    }
    after = _update_metadata(left["text"], fields)
    if relation == "merge":
        losing_body = right["text"][_body_offset(right["text"]) :]
        if posixpath.dirname(survivor) != posixpath.dirname(right["path"]):
            # Moving a relative destination changes its meaning. This slice
            # refuses it rather than inventing a second link-rewriting engine.
            for match in LINK.finditer(losing_body):
                parsed = urlsplit(match.group(1))
                if not parsed.scheme and not parsed.netloc and parsed.path and not parsed.path.startswith("/"):
                    raise ProposalError("merge would move relative Markdown links")
            if re.search(r"(?m)^ {0,3}\[[^]\n]+\]:", losing_body):
                raise ProposalError("merge would move relative Markdown reference definitions")
        after += "\n\n" + losing_body
        after = _derive_merge_provenance(after, sources, survivor, proposal_id)
    reparsed = card_identity(_meta(after), survivor)
    if not set(aliases).issubset(reparsed.aliases):
        raise ProposalError("migration aliases did not survive canonical parsing")
    if len(after.encode()) > io.MAX_CARD_BYTES:
        raise ProposalError("survivor after-text size limit exceeded")
    _clean(after)
    identity_index = _post_identity(target, paths, survivor, right["path"], after)
    indexes, index_fences = _references(target, care, paths, survivor, right["path"], after, identity_index)
    relationship = {
        "relation": relation,
        "source_revision": {"path": right["path"], "card_id": right["id"], "sha256": right["sha256"]},
        "replacement_revision": {"path": survivor, "card_id": survivor_id, "sha256": io.sha256(after.encode())},
        "source_provenance": [s["metadata"] for s in sources],
    }
    mutations = [_change(survivor, left["text"], after), _change(right["path"], right["text"], None), *indexes]
    for mutation in mutations:
        mode = io.file_mode(target / mutation["path"])
        mutation["before_mode"] = mode
        mutation["after_mode"] = mode if mutation["after"] is not None else None
    return {
        "schema": SCHEMA,
        "revision": 1,
        "id": proposal_id,
        "issue_id": issue["id"],
        "finding": issue,
        "finding_digest": io.sha256(io.json_bytes(issue)),
        "binding": binding,
        "scope": scope,
        "reason": reason,
        "survivor": survivor,
        "relation": relation,
        "classification": {
            "finding_kind": "identity-collision",
            "collision_keys": collision,
            "assertion_signal": signal,
        },
        "sources": sources,
        "citations": citations,
        "alias_migration": aliases,
        "relationship": relationship,
        "index_fences": index_fences,
        "mutations": mutations,
    }


@contextmanager
def _locked(target: Path) -> Iterator[None]:
    path = _root(target) / "proposal.lock"
    io.protect_file(path)
    with held_file_lock(path, deadline_seconds=1):
        # The helper releases locks on all exceptions. This is independent of
        # Brigade run's workspace run.lock.
        io.read_bytes(path, private=True)
        yield


@_boundary(2)
def create_payload(*, target: Path, issue_id: str, survivor: str, relation: str, reason: str) -> dict[str, Any]:
    target = _target(target)
    survivor = io.relative(survivor)
    reason = _reason(reason)
    if relation not in ("merge", "supersede"):
        raise ProposalError("relation must be merge or supersede")
    care, binding = _configuration(target)
    _outside_roots(_root(target), target, care)
    issue = _finding(target, care, issue_id)
    proposal_id = "proposal-" + uuid.uuid4().hex
    proposal = _compose(target, care, issue, binding, survivor, relation, reason, proposal_id)
    config_names = (
        brigade_config.CONFIG_REL_PATH,
        *[f"{d}/config.json" for d in brigade_config.LEGACY_WORKSPACE_DIRNAMES],
    )
    if all(binding["config_digests"][name] == "absent" for name in config_names):
        inbox = WRITER_INBOXES["claude"]
    else:
        writers = sorted(h for h in binding["available_scope"]["harnesses"] if h in WRITER_INBOXES)
        if not writers:
            raise ProposalError("configured selection has no tracked writer inbox route")
        writer = binding["owner"] if binding["owner"] in writers else writers[0]
        inbox = WRITER_INBOXES[writer]
    proposal["proposal_path"] = f"{STATE_REL}/{proposal_id}/revision-1.json"
    proposal["handoff_path"] = f"{inbox}/{proposal_id}.md"
    _outside_roots(target / proposal["handoff_path"], target, care)
    proposal["digest"] = io.sha256(io.json_bytes(proposal))
    notification = _render_handoff_draft(
        handoff_type="decision",
        title=f"Reviewed memory proposal {proposal_id}",
        summary="Inspect the exact proposal preview. Handoff lint does not authorize canonical changes.",
        facts=[
            f"Digest: {proposal['digest']}",
            f"Relation: {relation}; finding: {issue_id}",
            f"Run brigade memory proposal show {proposal_id} --target . --json, then review with --digest and --reason before apply.",
        ],
        evidence=[proposal["proposal_path"], *[f"{s['path']} sha256:{s['sha256']}" for s in proposal["sources"]]],
        action="no-card",
        target_card=None,
        target_document=".learnings/memory-proposals.md",
        suggested_content="Review the immutable proposal through brigade memory proposal; this notification cannot apply it.",
    )
    notification += "\n## Memory proposal notification\n\n" + proposal["proposal_path"] + "\n"
    with _locked(target):
        # No canonical writes occur at creation. Revalidate after acquiring state lock.
        _validate_live(target, proposal)
        with io.directory((target / proposal["handoff_path"]).parent, create=True, private=True):
            pass
        io.write_exclusive(target / proposal["proposal_path"], io.json_bytes(proposal))
        io.write_exclusive(target / proposal["handoff_path"], notification.encode())
    return proposal


def _load(target: Path, proposal_id: str, digest: str | None = None) -> dict[str, Any]:
    if not isinstance(proposal_id, str) or not SLUG.fullmatch(proposal_id):
        raise ProposalError("invalid proposal ID")
    path = _root(target) / proposal_id / "revision-1.json"
    proposal = io.read_object(path, private=True)
    if proposal.get("schema") != SCHEMA or proposal.get("revision") != 1 or proposal.get("id") != proposal_id:
        raise ProposalError("unsupported proposal revision")
    actual = proposal.get("digest")
    if not isinstance(actual, str) or not HEX.fullmatch(actual):
        raise ProposalError("invalid proposal digest")
    material = {k: v for k, v in proposal.items() if k != "digest"}
    if io.sha256(io.json_bytes(material)) != actual or (digest is not None and digest != actual):
        raise ProposalError("proposal digest does not match reviewed material")
    for source in proposal["sources"]:
        if io.sha256(source["text"].encode()) != source["sha256"]:
            raise ProposalError("retained source hash mismatch")
    return proposal


def _validate_owner(target: Path, proposal: dict[str, Any]) -> None:
    for name in (
        brigade_config.CONFIG_REL_PATH,
        *[f"{d}/config.json" for d in brigade_config.LEGACY_WORKSPACE_DIRNAMES],
    ):
        io.read_bytes(target / name, absent=True)
    cfg = brigade_config.load_config(target)
    workspace = target.stat()
    fingerprint = io.sha256(io.json_bytes([str(target), workspace.st_dev, workspace.st_ino]))
    if (cfg.selection.owner if cfg else "this-repo") != proposal["binding"]["owner"] or fingerprint != proposal[
        "binding"
    ]["workspace_fingerprint"]:
        raise ProposalError("owner or workspace binding drift")


def _validate_binding(target: Path, proposal: dict[str, Any]) -> memory_cmd.MemoryCareConfig:
    care, binding = _configuration(target)
    if binding != proposal["binding"]:
        raise ProposalError("owner, scope or config drift")
    _outside_roots(_root(target), target, care)
    _outside_roots(target / io.relative(proposal["handoff_path"]), target, care)
    issue = _finding(target, care, proposal["issue_id"])
    if io.sha256(io.json_bytes(issue)) != proposal["finding_digest"]:
        raise ProposalError("selected finding drift")
    return care


def _validate_live(target: Path, proposal: dict[str, Any]) -> None:
    care = _validate_binding(target, proposal)
    for mutation in proposal["mutations"]:
        raw = io.read_bytes(target / io.relative(mutation["path"]), limit=io.MAX_CARD_BYTES, absent=True)
        digest = io.sha256(raw) if raw is not None else "absent"
        if (
            digest != mutation["before_sha256"]
            or io.file_mode(target / mutation["path"], absent=True) != mutation["before_mode"]
        ):
            raise ProposalError("source or index before-state drift")
    fresh = _compose(
        target,
        care,
        proposal["finding"],
        proposal["binding"],
        proposal["survivor"],
        proposal["relation"],
        _reason(proposal["reason"]),
        proposal["id"],
    )
    for key in fresh:
        if fresh[key] != proposal[key]:
            raise ProposalError("proposal source, identity, citation, reference or scope drift")


@_boundary(1)
def show_payload(*, target: Path, proposal_id: str) -> dict[str, Any]:
    target = _target(target)
    proposal = _load(target, proposal_id)
    _validate_owner(target, proposal)
    return proposal


def _decision_evidence(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "memory-proposal-decision",
        "id": proposal["id"],
        "revision": proposal["revision"],
        "digest": proposal["digest"],
        "owner": proposal["binding"]["owner"],
        "scope": proposal["scope"],
        "workspace_fingerprint": proposal["binding"]["workspace_fingerprint"],
    }


def _decision(target: Path, proposal: dict[str, Any], label: str) -> dict[str, Any] | None:
    path = trust_gate.work_events_path(target)
    if path.exists() or path.is_symlink():
        io.protect_file(path)
    raw = io.text(path, limit=io.MAX_STATE_BYTES, absent=True, private=True)
    events: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ProposalError("malformed provenance event ledger")
        if (
            event.get("schema") == trust_gate.EVENT_SCHEMA
            and event.get("schema_version") == 1
            and event.get("content_scope") == SCHEMA
            and event.get("operator_command")
            == f"operator:brigade memory proposal {'review' if label == 'edit-accepted' else 'reject'}"
        ):
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and all(evidence.get(k) == v for k, v in _decision_evidence(proposal).items())
                and isinstance(evidence.get("reason"), str)
                and evidence["reason"].strip()
            ):
                _reason(evidence["reason"])
                events.append(event)
        if len(events) > io.MAX_FILES:
            raise ProposalError("proposal decision count exceeded")
    return trust_gate.matching_transition(
        events, item_ref=f"memory-proposal:{proposal['id']}:1", to_label=label, envelope_content_hash=proposal["digest"]
    )


def _require_accepted(target: Path, proposal: dict[str, Any]) -> None:
    if _decision(target, proposal, "edit-rejected"):
        raise ProposalError("proposal digest was rejected")
    if not _decision(target, proposal, "edit-accepted"):
        raise ProposalError("exact proposal edit has not been accepted")


def _review(target: Path, proposal_id: str, digest: str, reason: str, *, reject: bool) -> dict[str, Any]:
    target = _target(target)
    reason = _reason(reason)
    with _locked(target):
        proposal = _load(target, proposal_id, digest)
        _validate_owner(target, proposal)
        if reject:
            if _already_applied(target, proposal) is not None:
                raise ProposalError("committed proposal requires a separate reviewed compensating change")
        else:
            _validate_live(target, proposal)
        io.protect_file(trust_gate.work_events_path(target))
        if _decision(target, proposal, "edit-rejected"):
            if reject:
                return {"status": "rejected", "id": proposal_id, "digest": digest}
            raise ProposalError("proposal digest was rejected; rejection is terminal")
        label = "edit-rejected" if reject else "edit-accepted"
        if not _decision(target, proposal, label):
            event = trust_gate.build_provenance_event(
                item_ref=f"memory-proposal:{proposal_id}:1",
                from_label="pending-edit",
                to_label=label,
                envelope_content_hash=digest,
                content_scope=SCHEMA,
                operator_command=f"operator:brigade memory proposal {'reject' if reject else 'review'}",
                evidence={**_decision_evidence(proposal), "reason": reason},
            )
            trust_gate.append_work_event(target, event)
            io.read_bytes(trust_gate.work_events_path(target), limit=io.MAX_STATE_BYTES, private=True)
        return {"status": "rejected" if reject else "accepted", "id": proposal_id, "digest": digest}


@_boundary(1)
def review_payload(*, target: Path, proposal_id: str, digest: str, reason: str) -> dict[str, Any]:
    return _review(target, proposal_id, digest, reason, reject=False)


@_boundary(1)
def reject_payload(*, target: Path, proposal_id: str, digest: str, reason: str) -> dict[str, Any]:
    return _review(target, proposal_id, digest, reason, reject=True)


def _unfinished(target: Path, destinations: set[Path]) -> None:
    root = kernel.operations_root(target)
    if io.read_bytes(root / ".probe", absent=True) is not None:
        raise ProposalError("unexpected operation state")
    if root.exists():
        with io.directory(root):
            pass
        for entry in root.iterdir():
            with io.directory(entry):
                pass
            io.read_bytes(entry / "journal.json", absent=True)
    for operation in kernel.unfinished_operations(target):
        if destinations.intersection(operation.destinations):
            raise ProposalError(
                f"unfinished overlapping operation; run brigade projection recover {operation.operation_id} --target ."
            )


def _receipt_path(target: Path, proposal: dict[str, Any]) -> Path:
    return _root(target) / proposal["id"] / "apply-receipt.json"


def _already_applied(target: Path, proposal: dict[str, Any]) -> dict[str, Any] | None:
    path = _receipt_path(target, proposal)
    data = io.read_bytes(path, absent=True, private=True)
    if data is None:
        return None
    domain = json.loads(data)
    if (
        not isinstance(domain, dict)
        or domain.get("schema") != RECEIPT_SCHEMA
        or domain.get("proposal_digest") != proposal["digest"]
        or domain.get("relationship") != proposal["relationship"]
        or domain.get("decision") != _decision_evidence(proposal)
        or domain.get("after") != {m["path"]: m["after_sha256"] for m in proposal["mutations"]}
    ):
        raise ProposalError("retained apply receipt mismatch")
    operation_id = domain.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"memory-proposal-[0-9a-f]{32}", operation_id):
        raise ProposalError("retained operation ID malformed")
    kernel_path = kernel.operation_dir(target, operation_id) / "receipt.json"
    raw = io.read_bytes(kernel_path, absent=True)
    receipt = json.loads(raw) if raw is not None else {}
    if (
        receipt.get("terminal_state") != "committed"
        or receipt.get("operation_id") != operation_id
        or receipt.get("projector") != "memory-proposal"
        or receipt.get("source_digest") != proposal["digest"]
    ):
        raise ProposalError(
            f"apply marker without committed kernel receipt; run brigade projection recover {operation_id} --target ."
        )
    expected = {m["path"]: (m["before_sha256"], m["after_sha256"]) for m in proposal["mutations"]}
    expected[path.relative_to(target).as_posix()] = ("absent", io.sha256(data))
    recorded = {
        d.get("display_path", d.get("path")): (d.get("before_digest"), d.get("after_digest"))
        for d in receipt.get("destinations", [])
    }
    if recorded != expected:
        raise ProposalError("committed kernel receipt mutation mismatch")
    for mutation in proposal["mutations"]:
        raw = io.read_bytes(target / mutation["path"], limit=io.MAX_CARD_BYTES, absent=True)
        if (io.sha256(raw) if raw is not None else "absent") != mutation["after_sha256"] or io.file_mode(
            target / mutation["path"], absent=True
        ) != mutation["after_mode"]:
            raise ProposalError("committed after-state drift; a separate reviewed edit is required")
    return {
        "status": "already-applied",
        "id": proposal["id"],
        "digest": proposal["digest"],
        "operation_id": operation_id,
    }


def _cas_read(path: Path) -> bytes | None:
    # Kernel passes a descriptor-bound /proc/self/fd/<parent>/leaf path. Its
    # parent is an intentional descriptor link, not an attacker-selected path.
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = io._regular(fd)
        if before.st_size > io.MAX_STATE_BYTES:
            raise OSError("CAS file size limit exceeded")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(io.MAX_STATE_BYTES + 1)
        after = io._regular(fd)
        if len(data) > io.MAX_STATE_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError("CAS input changed")
        return data
    except FileNotFoundError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _mutation(target: Path, change: dict[str, Any], *, private: bool = False) -> kernel.MutationSpec:
    expected = change["before"].encode() if change["before"] is not None else None
    after = change["after"].encode() if change["after"] is not None else None

    def check(path: Path) -> None:
        actual = _cas_read(path)
        mode = path.stat(follow_symlinks=False).st_mode & 0o777 if actual is not None else None
        if actual != expected or (not private and mode != change["before_mode"]):
            # ProjectionError inside a commit skips rollback. Always OSError.
            raise OSError("exact revision changed at publication boundary")

    def writer(path: Path, data: bytes) -> None:
        check(path)
        localio.write_bytes_atomic(path, data)

    def remover(path: Path) -> None:
        check(path)
        path.unlink()

    return kernel.mutation(
        destination=target / change["path"],
        display_path=change["path"],
        mutation="remove" if after is None else ("create" if expected is None else "replace"),
        expected_before=change["before_sha256"],
        desired_after=change["after_sha256"],
        staged_bytes=after,
        mode=0o600 if private else change["after_mode"],
        writer=writer if after is not None else None,
        remover=remover if after is None else None,
    )


@_boundary(1)
def apply_payload(*, target: Path, proposal_id: str, digest: str) -> dict[str, Any]:
    target = _target(target)
    with _locked(target):
        proposal = _load(target, proposal_id, digest)
        _validate_owner(target, proposal)
        _require_accepted(target, proposal)
        receipt_path = _receipt_path(target, proposal)
        destinations = {target / m["path"] for m in proposal["mutations"]} | {receipt_path}
        _unfinished(target, destinations)
        already = _already_applied(target, proposal)
        if already:
            return already
        _validate_live(target, proposal)
        operation_id = "memory-proposal-" + uuid.uuid4().hex
        domain = {
            "schema": RECEIPT_SCHEMA,
            "proposal_id": proposal_id,
            "revision": 1,
            "proposal_digest": digest,
            "operation_id": operation_id,
            "decision": _decision_evidence(proposal),
            "relationship": proposal["relationship"],
            "after": {m["path"]: m["after_sha256"] for m in proposal["mutations"]},
        }
        receipt_change = _change(receipt_path.relative_to(target).as_posix(), None, io.json_bytes(domain).decode())
        specs = [_mutation(target, m) for m in proposal["mutations"]]
        specs.append(_mutation(target, receipt_change, private=True))

        def validate(*args: Any) -> None:
            live = _load(target, proposal_id, digest)
            if live != proposal:
                raise ProposalError("immutable proposal changed")
            _require_accepted(target, live)
            _validate_live(target, live)

        plan = kernel.build_plan(
            operation_id=operation_id,
            projector="memory-proposal",
            source_fingerprint=digest,
            mutations=specs,
            target=target,
            validators=[validate],
        )
        receipt = kernel.execute(plan, target=target)
        return {
            "status": receipt.terminal_state,
            "id": proposal_id,
            "digest": digest,
            "operation_id": operation_id,
            "recovery_command": receipt.recovery_command,
            "receipt": receipt.to_dict(),
        }
