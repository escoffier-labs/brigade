"""Reviewed N-card proposal construction over the existing decision and kernel path."""

from __future__ import annotations

import difflib
import posixpath
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import memory_cmd, memory_proposals as legacy
from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from .card_identity import IdentityIndex, card_identity
from .handoff_cmd.drafts import _render_handoff_draft
from .memory_reflect_candidates import _evidence_roots, _resolved
from .selection import WRITER_INBOXES

SCHEMA = "brigade.memory-proposal.v2"
MAX_SOURCES = 8


def _sources(
    target: Path, care: memory_cmd.MemoryCareConfig, names: list[str]
) -> tuple[list[dict[str, Any]], list[Path]]:
    if not isinstance(names, list) or not 1 <= len(names) <= MAX_SOURCES or len(names) != len(set(names)):
        raise legacy.ProposalError("consolidate requires one to eight distinct source cards")
    paths = memory_cmd._iter_cards(target, care)
    if len(paths) > io.MAX_FILES:
        raise legacy.ProposalError("canonical corpus file limit exceeded")
    allowed = {path.relative_to(target).as_posix() for path in paths}
    sources: list[dict[str, Any]] = []
    for name in sorted(names):
        io.relative(name)
        if name not in allowed:
            raise legacy.ProposalError("source is outside canonical card allowlist")
        text = io.text(target / name, limit=io.MAX_CARD_BYTES)
        assert text is not None
        meta = legacy._meta(text)
        legacy._admit(text, meta)
        identity = card_identity(meta, name)
        delta.blocks(text)
        delta.reservations(text)
        sources.append(
            dict(
                path=name,
                text=text,
                sha256=io.sha256(text.encode()),
                id=identity.card_id,
                aliases=list(identity.aliases),
                explicit_id=identity.explicit,
                metadata=meta,
            )
        )
    roots = [
        {root.rstrip("/") for root in care.card_roots if (target / source["path"]).is_relative_to(target / root)}
        for source in sources
    ]
    if any(len(root) != 1 or root != roots[0] for root in roots):
        raise legacy.ProposalError("sources must share exactly one configured card root")
    return sources, paths


def _evidence(target: Path, ops: list[dict[str, Any]]) -> dict[str, str]:
    refs: set[str] = set()
    for op in ops:
        values = op.get("evidence_ids", []) if isinstance(op, dict) else []
        if isinstance(values, list) and len(values) <= io.MAX_LIST:
            refs.update(ref for ref in values if isinstance(ref, str) and len(ref) <= 1024)
    if len(refs) > io.MAX_LIST:
        raise legacy.ProposalError("op evidence count exceeded")
    resolved = _resolved(target, sorted(refs), _evidence_roots(target))
    digests = {}
    for ref in resolved:
        try:
            bundle = memory_cmd._miseledger_evidence_id(ref)
            path = memory_cmd._miseledger_evidence_path(bundle) if bundle else target / io.relative(ref)
            if path is not None:
                raw = io.read_bytes(path)
                assert raw is not None
                legacy._clean(raw.decode("utf-8"))
                digests[ref] = io.sha256(raw)
        except (OSError, ValueError):
            continue
    return digests


def _references(
    target: Path,
    care: memory_cmd.MemoryCareConfig,
    paths: list[Path],
    texts: dict[str, str | None],
    removed: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not removed:
        return [], {}
    for rel, text in texts.items():
        if text is None:
            continue
        for link in legacy.LINK.finditer(text):
            parsed = urlsplit(link[1])
            canonical = posixpath.normpath(posixpath.join(posixpath.dirname(rel), unquote(parsed.path)))
            if not parsed.scheme and not parsed.netloc and canonical in removed:
                raise legacy.ProposalError("proposed card reference would break")
    index: IdentityIndex[str] = IdentityIndex()
    for path in paths:
        rel = path.relative_to(target).as_posix()
        text = texts.get(rel) if rel in texts else io.text(path, limit=io.MAX_CARD_BYTES)
        if text is None:
            continue
        identity = card_identity(legacy._meta(text), rel)
        index.claim_identity(identity, rel)
        for key in (identity.card_id, *identity.aliases):
            index.claim(key.lower().removesuffix(".md").removeprefix("cards/"), rel)
    # Reuse v1's fail-closed reference inspection for each removed revision.
    fences: dict[str, str] = {}
    for loser, survivor in sorted(removed.items()):
        after = texts[survivor]
        if after is None:
            raise legacy.ProposalError("duplicate merge destination was removed")
        ident = card_identity(legacy._meta(after), survivor)
        if any(index.resolve(key) != survivor for key in (ident.card_id, *ident.aliases)):
            raise legacy.ProposalError("post-state identity collision")
        _, checked = legacy._references(
            target,
            care,
            [p for p in paths if p.relative_to(target).as_posix() not in removed],
            survivor,
            loser,
            after,
            index,
        )
        fences.update(checked)
    changes = []
    for rel in sorted(fences):
        before = io.text(target / rel, limit=io.MAX_CARD_BYTES, absent=True)
        if before is None:
            continue

        def replace(match: Any, rel: str = rel) -> str:
            parsed = urlsplit(match[1])
            canonical = posixpath.normpath(posixpath.join(posixpath.dirname(rel), unquote(parsed.path)))
            if parsed.scheme or parsed.netloc or canonical not in removed:
                return str(match[0])
            destination = posixpath.relpath(removed[canonical], posixpath.dirname(rel) or ".")
            rewritten = destination + ("#" + parsed.fragment if parsed.fragment else "")
            start, end = match.start(1) - match.start(), match.end(1) - match.start()
            return str(match[0][:start] + rewritten + match[0][end:])

        after = legacy.LINK.sub(replace, before)
        if after != before:
            changes.append(legacy._change(rel, before, after))
    return changes, fences


def _derived(
    texts: dict[str, str | None], sources: list[dict[str, Any]], removed: dict[str, str], proposal_id: str
) -> dict[str, str | None]:
    result = dict(texts)
    for source in sources:
        path = source["path"]
        text = texts[path]
        if text is not None and text != source["text"]:
            delta.reservations(text)
            contributing = [source, *[s for s in sources if removed.get(s["path"]) == path]]
            after = legacy._derive_merge_provenance(
                text, contributing if len(contributing) > 1 else contributing * 2, path, proposal_id
            )
            if len(after.encode()) > io.MAX_CARD_BYTES:
                raise legacy.ProposalError("derived card byte limit exceeded")
            result[path] = after
    return result


def compose(
    *,
    target: Path,
    care: memory_cmd.MemoryCareConfig,
    binding: dict[str, Any],
    source_paths: list[str],
    ops: list[dict[str, Any]],
    anchor_blocks: bool,
    reason: str,
    proposal_id: str,
    reflect: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(ops, list) or len(ops) > io.MAX_LIST or not isinstance(anchor_blocks, bool):
        raise legacy.ProposalError("invalid or oversized op packet")
    sources, paths = _sources(target, care, source_paths)
    scope = legacy._scope([sources[0], sources[0]], {}, binding)
    for source in sources[1:]:
        legacy._scope([sources[0], source], {}, binding)
    texts: dict[str, str | None] = {
        s["path"]: delta.anchor_text(s["text"], s["path"]) if anchor_blocks else s["text"] for s in sources
    }
    evidence = _evidence(target, ops)
    accepted, dropped = [], []
    removed: dict[str, str] = {}
    for number, op in enumerate(ops):
        trial = dict(texts)
        try:
            if not isinstance(op, dict):
                raise legacy.ProposalError("op must be an object")
            delta.apply_op(trial, op, resolved=set(evidence))
            if trial == texts:
                raise legacy.ProposalError("op has no delta")
            trial_removed = dict(removed)
            for name, value in trial.items():
                if value is None and texts[name] is not None:
                    trial_removed[name] = op["card_path"]
            if trial_removed != removed:
                _references(target, care, paths, trial, trial_removed)
            _derived(trial, sources, trial_removed, proposal_id)
            removed = trial_removed
            texts = trial
            accepted.append(op)
        except (ValueError, KeyError, TypeError, OSError):
            dropped.append({"index": number, "reason": "invalid, unresolved, stale or empty op"})
    if not accepted and (ops or not any(texts[s["path"]] != s["text"] for s in sources)):
        raise legacy.ProposalError("no valid delta ops remain")
    evidence = _evidence(target, accepted)
    texts = _derived(texts, sources, removed, proposal_id)
    references, fences = _references(target, care, paths, texts, removed)
    changes = [legacy._change(s["path"], s["text"], texts[s["path"]]) for s in sources if texts[s["path"]] != s["text"]]
    changes.extend(references)
    if not changes:
        raise legacy.ProposalError("no valid delta ops remain")
    for change in changes:
        mode = io.file_mode(target / change["path"])
        change["before_mode"] = mode
        change["after_mode"] = mode if change["after"] is not None else None
    diff = "".join(
        "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in difflib.unified_diff(
                (m["before"] or "").splitlines(keepends=True),
                (m["after"] or "").splitlines(keepends=True),
                fromfile="a/" + m["path"],
                tofile="b/" + m["path"],
            )
        )
        for m in changes
    )
    return dict(
        schema=SCHEMA,
        revision=1,
        id=proposal_id,
        relation="consolidate",
        reason=reason,
        binding=binding,
        scope=scope,
        sources=sources,
        source_paths=sorted(source_paths),
        ops=accepted,
        dropped_ops=dropped,
        anchor_blocks=anchor_blocks,
        op_evidence=evidence,
        reflect=reflect,
        index_fences=fences,
        mutations=changes,
        diff=diff,
        relationship={
            "relation": "consolidate",
            "source_revisions": [{"path": s["path"], "sha256": s["sha256"]} for s in sources],
            "reflect": reflect,
        },
    )


def validate_live(target: Path, proposal: dict[str, Any]) -> None:
    care, binding = legacy._configuration(target)
    if binding != proposal["binding"]:
        raise legacy.ProposalError("owner, scope or config drift")
    legacy._outside_roots(legacy._root(target), target, care)
    legacy._outside_roots(target / io.relative(proposal["handoff_path"]), target, care)
    fresh = compose(
        target=target,
        care=care,
        binding=binding,
        source_paths=proposal["source_paths"],
        ops=proposal["ops"],
        anchor_blocks=proposal["anchor_blocks"],
        reason=proposal["reason"],
        proposal_id=proposal["id"],
        reflect=proposal["reflect"],
    )
    if any(fresh[key] != proposal[key] for key in fresh if key != "dropped_ops"):
        raise legacy.ProposalError("proposal source, block, evidence or reference drift")
    for rel, digest in proposal["index_fences"].items():
        raw = io.read_bytes(target / io.relative(rel), absent=True)
        if (io.sha256(raw) if raw is not None else "absent") != digest:
            raise legacy.ProposalError("index before-state drift")


def create_payload(
    *,
    target: Path,
    source_paths: list[str],
    ops: list[dict[str, Any]],
    anchor_blocks: bool,
    reason: str,
    reflect: dict[str, str] | None = None,
) -> dict[str, Any]:
    target = legacy._target(target)
    reason = legacy._reason(reason)
    care, binding = legacy._configuration(target)
    legacy._outside_roots(legacy._root(target), target, care)
    ident = "proposal-" + uuid.uuid4().hex
    proposal = compose(
        target=target,
        care=care,
        binding=binding,
        source_paths=source_paths,
        ops=ops,
        anchor_blocks=anchor_blocks,
        reason=reason,
        proposal_id=ident,
        reflect=reflect,
    )
    writers = sorted(h for h in binding["available_scope"]["harnesses"] if h in WRITER_INBOXES)
    if not writers and any(
        value != "absent" for key, value in binding["config_digests"].items() if key != memory_cmd.CONFIG_REL_PATH
    ):
        raise legacy.ProposalError("configured selection has no tracked writer inbox route")
    writer = binding["owner"] if binding["owner"] in writers else writers[0] if writers else "claude"
    proposal["proposal_path"] = f"{legacy.STATE_REL}/{ident}/revision-1.json"
    proposal["handoff_path"] = f"{WRITER_INBOXES[writer]}/{ident}.md"
    legacy._outside_roots(target / proposal["handoff_path"], target, care)
    proposal["digest"] = io.sha256(io.json_bytes(proposal))
    notification = _render_handoff_draft(
        handoff_type="decision",
        title=f"Reviewed memory proposal {ident}",
        summary="Inspect the exact typed delta preview before review and apply.",
        facts=[f"Digest: {proposal['digest']}", "Relation: consolidate"],
        evidence=[proposal["proposal_path"]],
        action="no-card",
        target_card=None,
        target_document=".learnings/memory-proposals.md",
        suggested_content="Review this immutable proposal through brigade memory proposal.",
    )
    notification += "\n## Memory proposal notification\n\n" + proposal["proposal_path"] + "\n"
    with legacy._locked(target):
        validate_live(target, proposal)
        io.write_exclusive(target / proposal["proposal_path"], io.json_bytes(proposal))
        io.write_exclusive(target / proposal["handoff_path"], notification.encode())
    return proposal
