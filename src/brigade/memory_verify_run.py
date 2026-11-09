"""`brigade memory verify run`: check selected cards and open pinned, reviewed proposals (verify slice 9).

One run, in order (phase-memory-verify D3 to D6):

1. Refuse a folder that is also the reflect job's folder, then read the hub's
   `status`. The run continues only when the credential is not the admin
   credential, the hub advertises `verifier_floor` and `verifier_manifests`,
   `verifier` is true and `reflect_principal` is false. Nothing is inferred
   from preflight hold reasons. This happens before any model call or hub write.
2. Resolve both seats with their recorded isolation probes.
3. Read the state of remembered proposals with `proposal_show`. Terminal ones
   free their card and the pending ceiling.
4. Select cards (`memory_verify_cmd.select_cards`). A non-dry run then stores,
   in one atomic state write, every scope's read count snapshot whose
   `read_stats` pagination completed, with `counts_ms` from the program clock.
5. Assemble evidence, call the seats and check the replies
   (`memory_verify_contract.verify_card`). Commit expansions abstain until
   slice 9a supplies the resolver (`commit_resolver`).
6. For each card with an op: build the review packet and the manifest, scan
   the manifest, then publish a stage record with the exact ops, manifest and
   packet (`memory_verify_packets`). Only then attach the manifest with
   `evidence.attach` and open one proposal for that one card with the
   correction ops and an `append_evidence` op citing the manifest, through the
   S19d request id ledger in `memory_reflect_hub.submit`. Once the proposal is
   open, the packet is published as `<proposal-id>.json` from the stage.

At the start of every non-dry run, `Sender.reconcile` reads each stage. A
stage whose ledger entry holds a proposal id is finalized, so a crash between
submit and publication loses no packet. A stage whose request has no result
yet is sent again, identical, under the same request id. A card with such an
unresolved submission is not checked again and does not enter its cooldown.

What reaches the hub: the `status`, `read_stats` and `proposal_show` reads,
the manifest (codes, references, digests, byte ranges and seat ids, no free
text) and the proposal ops. The only free text in an op is the replacement
the contract already checked. Card text, evidence text and model output stay
on this host, in the packet directory described in `memory_verify_packets`.

Failure and quota follow D6. A failed non-dry run adds one to the pause
counter. A completed non-dry run resets it only when it did work: a seat call
answered, or a submission reached a result. A completed run that only skipped
leaves the counter as it was. Three consecutive failures pause the scheduled
job, and only a manual non-dry run that did work clears the pause. A
dry run calls the seats (unless `--no-model`, handled in `memory_verify_cmd`)
but attaches, submits and writes nothing, and leaves the pause counter alone.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory_fairness as fairness
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_proposal_io as io
from . import memory_reflect_hub as ledger
from . import memory_verify_cmd as cmd
from . import memory_verify_config as config
from . import memory_verify_contract as contract
from . import memory_verify_evidence as ev
from . import memory_verify_packets as packets
from . import memory_verify_seat as seats
from . import memory_verify_select as pick
from . import memory_verify_state as vstate
from .memory_verify_cmd import VerifyRunError

CALL_TIMEOUT = 120.0
RUN_SECONDS = 20 * 60
MAX_NEW_PROPOSALS = 4
PENDING_CEILING = pick.PENDING_CEILING
# proposal_show reads per run. The ceiling keeps remembered proposals far below this.
MAX_SHOWS = 64
# Request ids of expired stages, kept until they age out. At the cap, the run opens no new proposal.
TOMBSTONE_DAYS = 90
MAX_TOMBSTONES = 512
DAY_MS = pick.DAY_MS
PACKET_SCHEMA = "brigade.memory-verify-packet.v1"
MANIFEST_SCHEMA = "brigade.memory-verify-manifest.v1"
MANIFEST_KIND = "memory-verify-manifest"
WORKSPACE_ID = packets.WORKSPACE_ID
FLOOR = "verifier_floor"
MANIFESTS = "verifier_manifests"
REFLECT_FOLDER = ".brigade/memory/reflect"
OPEN_STATES = frozenset({"pending", "held"})
EVIDENCE_CONFLICT = "memory_evidence_conflict"
EVIDENCE_CAPS = frozenset({"memory_evidence_principal_cap", "memory_evidence_workspace_cap"})
COMPONENT = re.compile(r"[A-Za-z0-9._:-]{1,128}")
REFLECT_REFUSAL = "the verify folder must not be the reflect job's folder. Give memory-verify its own folder"
DISABLED = cmd.DISABLED
DISABLED_REASON = cmd.DISABLED_REASON
UNRESOLVED_REQUEST = cmd.UNRESOLVED_REQUEST

# Injectable for tests: the seat layer, evidence assembly, the archive and the clock.
READY: Callable[..., tuple[seats.Seat, seats.Seat]] = seats.ready
CALL: Callable[..., seats.Reply] = seats.call
WORK_AREA: Callable[[Path], contextlib.AbstractContextManager[Any]] = seats.work_area
ASSEMBLE: Callable[..., Iterator[ev.Assembly]] = ev.assemble_all
MONOTONIC: Callable[[], float] = time.monotonic
FINALIZE: Callable[[Path, packets.Stage, str], str | None] = packets.finalize


def _engine(verb: str, arguments: list[str]) -> Any:
    """One evidence engine verb, decoded, or None on any failure."""
    from . import component_bins, proc

    binary = component_bins.resolve("brigade-evidence")
    if binary is None:
        return None
    result = proc.run([binary, verb, *arguments], timeout=30.0)
    return result.json() if result.code == 0 else None


def _archive() -> ev.Archive:
    return ev.EngineArchive(_engine)


ARCHIVE: Callable[[], ev.Archive] = _archive


def commit_resolver(workspace: config.Workspace) -> contract.Resolver | None:
    """The git commit resolver for one bound workspace. The single seam for slice 9a.

    TODO(verify slice 9a): return the hardened resolver that follows the
    "Commit resolver contract" in phase-memory-verify D3 rule by rule. Until
    then this returns None, so `verify_card` abstains on every commit
    expansion with `verify_commit_unresolved` and no `replace_block` is opened.
    """
    return None


def _resolver(settings: config.VerifyConfig, scope: str) -> contract.Resolver | None:
    bound = [workspace for workspace in settings.workspaces if workspace.scope == scope]
    # A scope with several roots has no single repository to ask, so nothing resolves.
    return commit_resolver(bound[0]) if len(bound) == 1 else None


# --- Install and pre-run checks -------------------------------------------------------------


def reflect_folder(target: Path) -> bool:
    """True when the folder holds the reflect job's state, so it is the reflect job's folder."""
    try:
        os.lstat(target / REFLECT_FOLDER)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def install_refusal(target: Path) -> str | None:
    """Why `memory-verify` may not be scheduled for `target`, or None. Fixed text only.

    Refuses the reflect job's folder and any credential that is not the
    folder's own explicit memory hub credential: a folder that would fall back
    to the fleet node token, or an override that names a fleet credential,
    including the admin token.
    """
    if reflect_folder(target):
        return REFLECT_REFUSAL
    try:
        settings = hubc.load_settings(target)
    except (hubc.HubConfigError, OSError, ValueError):
        return "memory-verify needs a valid explicit memory hub credential that is not a fleet credential"
    if settings.mode != "read":
        return 'memory-verify needs [hub] mode = "read" in the verify folder'
    if settings.source not in (hubc.SOURCE_ENV, hubc.SOURCE_TABLE):
        return "memory-verify needs its own verifier credential in [memory_hub], never the fleet node or admin token"
    return None


def _preflight(client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    """The `status` checks of D4. Raises before any model call or hub write."""
    status = client.call("status", scopes=list(settings.read_scopes))
    if status.get("credential") == "admin":
        raise VerifyRunError("verify_admin_credential", "verify never runs with the hub admin credential")
    capabilities = status.get("capabilities")
    if not isinstance(capabilities, list) or FLOOR not in capabilities or MANIFESTS not in capabilities:
        raise VerifyRunError(
            "verify_hub_floor_missing", "the hub does not advertise the verifier floor and manifest binding"
        )
    if status.get("verifier") is not True:
        raise VerifyRunError("verify_not_verifier", "the hub does not report this principal as a verifier")
    if status.get("reflect_principal") is not False:
        raise VerifyRunError("verify_reflect_principal", "a verifier principal must not be a reflect principal")
    principal, epoch = status.get("principal"), status.get("log_epoch")
    if not isinstance(principal, str) or not COMPONENT.fullmatch(principal) or not isinstance(epoch, str):
        raise VerifyRunError("verify_hub_status_invalid", "the hub status names no usable principal or log epoch")
    return status


def _hub_ms(status: Mapping[str, Any], fallback: int) -> int:
    """The hub's clock from `status`, so an attachment is never observed after the hub's now."""
    value = status.get("hub_time")
    if isinstance(value, str) and len(value) <= 64:
        with contextlib.suppress(ValueError):
            return min(fallback, int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000))
    return fallback


# --- Remembered proposals ------------------------------------------------------------------


def _refresh(
    client: hubc.MemoryHubClient, state: dict[str, Any], bound: frozenset[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    """Entries whose remembered proposal is no longer open, with `pending` cleared, and the reads that failed.

    Only bound scopes are read: nothing is asked of the hub about a scope no
    longer bound. A read that fails without denying access leaves its proposal
    counted as open.
    """
    updates: dict[str, dict[str, Any]] = {}
    failed = 0
    shown = 0
    for scope, table in state["scopes"].items():
        if scope not in bound:
            continue
        for key, entry in table.get("cards", {}).items():
            proposal = entry.get("pending")
            if not proposal or shown >= MAX_SHOWS:
                continue
            shown += 1
            try:
                reply = client.call("proposal_show", proposal_id=proposal)
                current = reply.get("state")
            except hubc.HubRefusal as exc:
                if exc.denies_access:
                    raise
                if exc.status != 404:
                    failed += 1
                    continue
                current = "missing"  # Compacted or gone: no longer open.
            except hubc.HubUnavailable:
                failed += 1
                continue
            if current in OPEN_STATES:
                continue
            settled = {name: value for name, value in entry.items() if name != "pending"}
            if current == "rejected":
                # Slice 10 refines this into a per-verdict code. Until then the card waits its interval.
                settled["verdict"] = "rejected"
            updates[key] = settled
    return updates, failed


def _open_proposals(state: dict[str, Any]) -> set[str]:
    return {
        entry["pending"]
        for table in state["scopes"].values()
        for entry in table.get("cards", {}).values()
        if entry.get("pending")
    }


def _pending_count(entries: Mapping[str, Mapping[str, Any]]) -> int:
    return sum(1 for entry in entries.values() if entry.get("pending"))


# --- State ---------------------------------------------------------------------------------


def _remember(
    state: dict[str, Any], picked: cmd.Picked, updates: Mapping[str, dict[str, Any]], now: int
) -> dict[str, dict[str, dict[str, Any]]]:
    """Fold this run's selection into the state. The caller saves it in one write.

    Every live card gets a first-seen time. A scope whose `read_stats`
    pagination completed stores its whole snapshot: per-card reads for every
    live card and the scope's `counts_ms`. Returns each bound scope's card table.
    """
    if not picked.same_binding:
        state["scopes"], state["submissions"] = {}, {}
    state["binding"] = picked.binding
    live: dict[str, set[str]] = {}
    for head in picked.heads:
        live.setdefault(head["scope"], set()).add(fairness.key_digest(head["scope"], [head["card_id"]]))
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for scope, source in picked.sources.items():
        table = state["scopes"].setdefault(scope, {})
        keys = live.get(scope, set())
        cards = {
            key: dict(entry) for key, entry in table.get("cards", {}).items() if key in keys or entry.get("pending")
        }
        for key in keys:
            cards.setdefault(key, {"seen": now, "proposed": 0})
        for key, entry in updates.items():
            if key in cards and picked.same_binding:
                cards[key] = dict(entry)
        if scope in picked.complete:
            rows = picked.complete[scope]
            for key in keys:
                reads = rows.get(key, pick.Reads())
                cards[key]["reads"] = [reads.reads_7d, reads.reads_30d, reads.total_reads]
            table["counts_ms"] = now
            table["since_day"] = picked.since.get(scope)
        if source != "fresh" or scope in picked.never:
            table["never_read"] = picked.never.get(scope)
        table["cards"] = cards
        tables[scope] = cards
    submissions = state.setdefault("submissions", {})
    settled = sorted(
        (pending["client_event_id"], digest)
        for digest, pending in submissions.items()
        if "proposal_id" in pending or "refused" in pending
    )
    room = vstate.MAX_SUBMISSIONS - 4 * MAX_NEW_PROPOSALS
    for _, digest in settled[: max(0, len(submissions) - room)]:
        del submissions[digest]
    return tables


def evict_tombstones(submissions: dict[str, dict[str, Any]], now: int) -> int:
    """Drop tombstones whose request is `TOMBSTONE_DAYS` old. Returns the number still live.

    A live tombstone holds its card, so it is never dropped early. When
    `MAX_TOMBSTONES` are live, the run opens no new proposal instead
    (`verify_deferred_tombstones`), which keeps their number bounded.
    """
    live = 0
    for key, pending in list(submissions.items()):
        if not pending.get("expired"):
            continue
        if now - (hubc.event_id_ms(pending["client_event_id"]) or 0) >= TOMBSTONE_DAYS * DAY_MS:
            del submissions[key]
        else:
            live += 1
    return live


def _live_tombstones(state: dict[str, Any]) -> int:
    """Tombstones in the ledger now, counted at each new proposal so ones reconcile just added count too."""
    return sum(1 for pending in state.get("submissions", {}).values() if pending.get("expired"))


def _stranded(digest: str, pending: Mapping[str, Any], staged: set[str]) -> bool:
    """An unsettled request without a stage: it may have opened a proposal, and nothing will replay it."""
    return (
        digest not in staged
        and not pending.get("expired")
        and bool(pending.get("unsettled"))
        and not pending.get("proposal_id")
        and not pending.get("refused")
    )


def backstop(submissions: dict[str, dict[str, Any]], staged: set[str]) -> int:
    """Turn every stranded request into a tombstone, so its card stays held. Returns how many.

    Only a crash between steps of code older than `retire`'s save-first order
    can strand a request. One without a card key, which only code older than
    this slice writes, stays in the ledger as it is and is never forgotten.
    """
    changed = 0
    for digest, pending in list(submissions.items()):
        ops, card = pending.get("ops_sha256"), pending.get("card")
        if _stranded(digest, pending, staged) and isinstance(ops, str) and isinstance(card, str):
            del submissions[digest]
            submissions[ops] = {
                "client_event_id": pending["client_event_id"],
                "ops_sha256": ops,
                "expired": True,
                "card": card,
            }
            changed += 1
    return changed


def _held_cards(state: dict[str, Any]) -> frozenset[str]:
    """Cards whose request may have opened a proposal under an id this job does not know.

    Each has a live tombstone. A stranded unsettled request counts too, because
    `backstop` turns it into a tombstone before selection reads this.
    """
    return frozenset(
        pending["card"]
        for pending in state.get("submissions", {}).values()
        if pending.get("expired") and isinstance(pending.get("card"), str)
    )


def _verdict(result: contract.CardResult) -> str:
    """One code for the card: supported, correction, or the first block's reason or verdict."""
    if result.abstain is not None:
        return result.abstain
    if result.proposal is not None:
        return "correction"
    if result.outcomes and all(outcome.verdict == "supported" for outcome in result.outcomes):
        return "supported"
    for outcome in result.outcomes:
        code = outcome.reason or outcome.verdict
        if code and vstate.CODE.fullmatch(code):
            return code
    return "abstained"


class SeatStop(Exception):
    """A seat reply that ends the run: quota stops it cleanly, a fatal status fails it."""

    def __init__(self, code: str, *, fatal: bool) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal


@dataclass
class Calls:
    """Seat calls for one card. Prompt and reply text stay here and in the packet only."""

    area: Any
    records: list[dict[str, Any]] = field(default_factory=list)
    made: int = 0
    ok: int = 0

    def seat(self, seat: seats.Seat) -> Callable[[str], str | None]:
        def call(prompt: str) -> str | None:
            reply = CALL(self.area, seat, prompt, timeout=CALL_TIMEOUT)
            self.made += 1
            good = reply.status == "ok"
            self.records.append(
                {"role": seat.role, "status": reply.status, "prompt": prompt, "reply": reply.text if good else None}
            )
            if good:
                self.ok += 1
                return reply.text
            if reply.status == "seat_quota":
                raise SeatStop("seat_quota", fatal=False)
            if reply.fatal:
                raise SeatStop(reply.status, fatal=True)
            return None

        return call

    def failed(self) -> str | None:
        """The first non-ok seat status this card met, or None."""
        return next((record["status"] for record in self.records if record["status"] != "ok"), None)


# --- Manifest and packet -------------------------------------------------------------------


def _ref(item: ev.Item) -> str:
    """A workspace-relative path, a bundle id or an excerpt id. Never the root's identity."""
    if item.source.startswith("file:"):
        return item.source.split(":", 3)[3]
    return item.source


def _confidence(card: contract.CardInput, reply: str | None, count: int) -> dict[str, contract.BlockReply]:
    if reply is None:
        return {}
    try:
        return contract.parse_reply(reply, card, count)
    except contract.ReplyInvalid:
        return {}


def _ranges(item: ev.Item, quote: str) -> list[int] | None:
    data, needle = item.text.encode(), quote.encode()
    start = data.find(needle)
    return None if start < 0 else [start, start + len(needle)]


@dataclass(frozen=True)
class Review:
    manifest: bytes
    packet: bytes
    stance: str


def review(
    card: contract.CardInput,
    result: contract.CardResult,
    assembly: ev.Assembly,
    calls: Calls,
    pair: Sequence[seats.Seat],
) -> Review:
    """The local packet and the manifest that binds it. The manifest carries no free text."""
    items = [*assembly.items, *assembly.excerpts]
    first = next((record for record in calls.records if record["role"] == "propose"), None)
    replies = _confidence(card, first["reply"] if first else None, len(items))
    proposal = result.proposal
    assert proposal is not None
    op_blocks = {op.block_id for op in proposal.ops}
    used: dict[int, list[list[int]]] = {}
    for block_id in sorted(op_blocks):
        block = replies.get(block_id)
        for quote in block.quotes if block else ():
            if 0 <= quote.evidence < len(items):
                span = _ranges(items[quote.evidence], quote.text)
                if span is not None and span not in used.setdefault(quote.evidence, []):
                    used[quote.evidence].append(span)
    shown = [
        {
            "index": index,
            "ref": _ref(item),
            "kind": item.kind,
            "rank": item.rank,
            "authenticated": item.rank == ev.AUTHENTICATED,
            "counted": not item.context_only and item.kind not in ev.CONTEXT_ONLY,
            "observed_ms": item.observed_ms,
            "sha256": item.sha256,
            "text": item.text,
        }
        for index, item in enumerate(items)
    ]
    findings = [
        {"origin": f.origin, "index": f.index, "kind": f.kind, "rules": list(f.rules)} for f in assembly.flagged
    ]
    seat_ids = [{"role": seat.role, "seat": seat.name, "model": seat.model} for seat in pair]
    head = {"scope": card.scope, "card_id": card.card_id, "base_revision": card.revision}
    packet = {
        "schema": PACKET_SCHEMA,
        "card": head,
        "evidence": shown,
        "findings": findings,
        "seats": seat_ids,
        "calls": calls.records,
        "outcomes": [{"block_id": o.block_id, "verdict": o.verdict, "reason": o.reason} for o in result.outcomes],
        "ops": proposal.payload_ops(),
    }
    packet_bytes = io.json_bytes(packet)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "card": head,
        "blocks": [
            {
                "block_id": o.block_id,
                "verdict": o.verdict,
                "confidence": replies[o.block_id].confidence if o.block_id in replies else None,
            }
            for o in result.outcomes
        ],
        "evidence": [
            {
                "ref": shown[index]["ref"],
                "kind": shown[index]["kind"],
                "rank": shown[index]["rank"],
                "authenticated": shown[index]["authenticated"],
                "observed_ms": shown[index]["observed_ms"],
                "sha256": shown[index]["sha256"],
                "ranges": spans,
            }
            for index, spans in sorted(used.items())
        ],
        "findings": [finding["rules"] for finding in findings],
        "seats": seat_ids,
        "prompt_sha256": result.prompt_sha256,
        "packet_sha256": io.sha256(packet_bytes),
        "input_sha256": io.sha256(first["prompt"].encode()) if first else None,
    }
    stance = "contrary" if any(op.type == contract.REPLACE for op in proposal.ops) else "support"
    return Review(manifest=io.json_bytes(manifest), packet=packet_bytes, stance=stance)


def _evidence_id(principal: str, manifest: bytes) -> str:
    return f"{principal}/{WORKSPACE_ID}/{io.sha256(manifest)}"


def _manifest_op(card: contract.CardInput, proposal: contract.Proposal, evidence_id: str) -> dict[str, Any]:
    """The `append_evidence` op that binds the manifest to the proposal, pinned to the first corrected block."""
    pinned = proposal.ops[0]
    return {
        "type": contract.APPEND,
        "scope": card.scope,
        "card_id": card.card_id,
        "base_revision": card.revision,
        "block_id": pinned.block_id,
        "expected_block_hash": pinned.expected_block_hash,
        "evidence_ids": [evidence_id],
    }


# --- The run -------------------------------------------------------------------------------


@dataclass
class Tally:
    skipped: dict[str, int]
    cards: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, str]] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    attempted: int = 0
    failed: int = 0
    made: int = 0
    ok: int = 0
    stop: str | None = None
    # Set when a seat call answered or a submission reached a result. A run without it is idle.
    worked: bool = False

    def skip(self, reason: str, amount: int = 1) -> None:
        if amount:
            self.skipped[reason] = self.skipped.get(reason, 0) + amount


def _attach(client: hubc.MemoryHubClient, staged: packets.Stage, observed: int) -> str | None:
    """Attach the staged manifest. None when it is on the hub, else a skip code. Raises when the run must fail."""
    try:
        client.call(
            "evidence.attach",
            scope=staged.scope,
            card_id=staged.card_id,
            workspace_id=WORKSPACE_ID,
            evidence_id=staged.evidence_id,
            artifact_kind=MANIFEST_KIND,
            artifact_sha256=io.sha256(staged.manifest),
            artifact_base64=base64.b64encode(staged.manifest).decode("ascii"),
            observed_at=observed,
            stance=staged.stance,
            client_event_id=hubc.event_id(),
        )
    except hubc.HubRefusal as exc:
        if exc.denies_access:
            raise
        if exc.status == 409 and exc.code == EVIDENCE_CONFLICT:
            return ALREADY_ATTACHED  # The id is the manifest's digest, so this manifest is on the hub.
        if exc.status == 429 and exc.code in hubc.EVIDENCE_QUOTA_REFUSALS:
            return "evidence_quota"
        if exc.code in EVIDENCE_CAPS:
            return "evidence_cap"
        # Final only for an explicit, coded 4xx refusal, as the propose path decides. Anything else retries.
        final = (
            400 <= exc.status < 500
            and exc.status not in (408, 429)
            and exc.body.get("retryable") is not True
            and ledger._code(exc.code)
        )
        return "manifest_refused" if final else MANIFEST_TRANSIENT
    return None


# Results of sending one staged submission. Only UNRESOLVED keeps the card out of its cooldown.
OPENED, DUPLICATE, REFUSED, UNRESOLVED, DROPPED = "opened", "duplicate", "refused", "unresolved", "dropped"
# A correction this run did not submit, because of the per-run cap, the pending ceiling or a quota stop.
# Nothing is staged, the card stays out of its cooldown and the next run recomputes it.
DEFERRED = "deferred"
DEFERRED_CAP = "verify_deferred_cap"
DEFERRED_QUOTA = "verify_deferred_quota"
DEFERRED_TOMBSTONES = "verify_deferred_tombstones"
DEFERRED_PACKET_CAP = "verify_deferred_packet_cap"
MANIFEST_EXPIRED = "verify_manifest_expired"
UNSETTLED_RECOVERED = "verify_unsettled_recovered"
BINDING_INVALID = "memory_manifest_binding_invalid"
MANIFEST_REFUSED = "verify_manifest_refused"
MANIFEST_TRANSIENT = "manifest_transient"
ALREADY_ATTACHED = "already_attached"
# Durable card reasons, kept as the card's verdict code in the state file.
PACKET_UNBOUND = "verify_packet_unbound"
STAGE_EXPIRED = "verify_stage_expired"
OPS_MISMATCH = "verify_stage_ops_mismatch"
SCOPE_UNBOUND = "verify_scope_unbound"
BINDING_UNCHECKED = "verify_binding_unchecked"
APPLICABLE_MANIFESTS = frozenset({"pending_review", "active"})


@dataclass
class Sender:
    """What sending needs from the run: the hub, the state, its ledger and the run's counters."""

    target: Path
    state: dict[str, Any]
    client: hubc.MemoryHubClient
    tally: Tally
    tables: dict[str, dict[str, dict[str, Any]]]
    epoch: str
    observed: int
    now: int
    pending: int
    state_principal: str = ""
    # Scopes bound in `[[verify.workspace]]` and readable by the hub settings, right now.
    bound: frozenset[str] = frozenset()
    reasons: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def ledger(self) -> dict[str, dict[str, Any]]:
        return self.state.setdefault("submissions", {})

    def save(self) -> None:
        vstate.save(self.target, self.state)

    def entry(self, staged: packets.Stage) -> dict[str, Any] | None:
        key = fairness.key_digest(staged.scope, [staged.card_id])
        found = self.tables.get(staged.scope, {}).get(key)
        if found is None:
            # A scope no longer bound keeps its old table, so its reasons stay with its cards.
            found = self.state["scopes"].get(staged.scope, {}).get("cards", {}).get(key)
        return found

    def record(self, staged: packets.Stage, code: str, *, eligible: bool) -> None:
        """A durable reason on the card's state entry. `eligible` also ends its cooldown."""
        self.tally.skip(code)
        self.reasons[(staged.scope, staged.card_id)] = code
        entry = self.entry(staged)
        if entry is not None:
            entry["verdict"] = code
            if eligible:
                entry.pop("verified", None)
        self.save()

    def remember_open(self, staged: packets.Stage, proposal_id: str) -> None:
        entry = self.entry(staged)
        if entry is not None and entry.get("pending") != proposal_id:
            entry.update(pending=proposal_id, proposed=self.now)
            self.pending += 1
        self.save()

    def open(self, staged: packets.Stage, proposal_id: str) -> None:
        """Remember the open proposal on its card, then publish its packet from the stage."""
        self.remember_open(staged, proposal_id)
        if FINALIZE(self.target, staged, proposal_id) is not None:
            self.tally.skip(packets.MISMATCH)

    def bind_duplicate(self, staged: packets.Stage, proposal_id: str) -> str:
        """Publish the packet under an existing proposal only when that proposal binds this manifest.

        `proposal_show` must list exactly one applicable verifier manifest whose
        `artifact_sha256` is this stage's manifest digest. The hub lists bound
        manifests only to a caller with `review` on every target, so a verifier
        without it never publishes here. Any other answer publishes nothing,
        records `verify_packet_unbound` and discards the stage. A failed read
        keeps the stage for the next run, until it expires.
        """
        self.remember_open(staged, proposal_id)
        try:
            shown = self.client.call("proposal_show", proposal_id=proposal_id)
        except hubc.HubRefusal as exc:
            if exc.denies_access:
                raise
            self.tally.skip(BINDING_UNCHECKED)
            return UNRESOLVED
        except hubc.HubUnavailable:
            self.tally.skip(BINDING_UNCHECKED)
            return UNRESOLVED
        listed = shown.get("verifier_manifests")
        digest = io.sha256(staged.manifest)
        matches = [
            item
            for item in (listed if isinstance(listed, list) else [])
            if isinstance(item, dict)
            and item.get("artifact_kind") == MANIFEST_KIND
            and item.get("state") in APPLICABLE_MANIFESTS
            and item.get("artifact_sha256") == digest
        ]
        self.tally.skip("duplicate_pending")
        if len(matches) == 1:
            if FINALIZE(self.target, staged, proposal_id) is not None:
                self.tally.skip(packets.MISMATCH)
            return DUPLICATE
        self.record(staged, PACKET_UNBOUND, eligible=False)
        packets.drop(self.target, staged)
        return DUPLICATE

    def bury(self, staged: packets.Stage, current: dict[str, Any]) -> None:
        """Replace a request's ledger entry with a tombstone that holds its card."""
        del self.ledger[staged.digest]
        ops = current.get("ops_sha256")
        if ops is not None and not current.get("refused"):
            self.ledger[ops] = {
                "client_event_id": current["client_event_id"],
                "ops_sha256": ops,
                "expired": True,
                "card": fairness.key_digest(staged.scope, [staged.card_id]),
            }

    def retire(self, staged: packets.Stage, current: dict[str, Any] | None, code: str) -> str:
        """Discard a stage whose request will not be sent again, under one rule for every path.

        Stage expiry, manifest expiry, an unbound scope and an ops mismatch all
        end here. A request that may have opened a proposal (`unsettled`: sent
        without a definite answer) becomes a tombstone, which keeps the card's
        hold. Any other request is forgotten and the card stays eligible. A
        request with a known proposal id is left in the ledger as it is.
        """
        if current is not None and not current.get("proposal_id") and self.ledger.get(staged.digest) is current:
            if current.get("unsettled"):
                self.bury(staged, current)
            else:
                del self.ledger[staged.digest]
        # Save the tombstone or the forgotten request first. A crash before the stage goes leaves
        # the stage for the next run, never an unsettled request without a hold.
        self.record(staged, code, eligible=True)
        packets.drop(self.target, staged)
        return DROPPED

    def send(self, staged: packets.Stage, *, replay: bool) -> str:
        """Attach the staged manifest and submit the staged ops through the request id ledger.

        A replay sends the identical request again, so the hub answers it
        idempotently under the same request id. A stage whose ops no longer
        hash to the digest the ledger keeps is never sent, and no new id is
        minted from it.
        """
        tally, remembered = self.tally, self.ledger.get(staged.digest)
        if remembered is None:
            # No request was published for it under this binding, so nothing can be replayed.
            packets.drop(self.target, staged)
            return DROPPED
        if remembered.get("ops_sha256") != staged.ops_sha256:
            # The stage no longer matches the request the ledger published, so it is never sent and
            # no id is minted from it. The request itself follows the shared retirement rule.
            return self.retire(staged, remembered, OPS_MISMATCH)
        if remembered.get("proposal_id"):
            if not remembered.get("created"):
                # Only `created`, saved with the id, proves this request opened the proposal. A
                # duplicate, or an id whose provenance was never saved, publishes only once bound.
                return self.bind_duplicate(staged, remembered["proposal_id"])
            self.open(staged, remembered["proposal_id"])
            tally.skip("packets_recovered")
            tally.worked = True
            return OPENED
        if remembered.get("refused"):
            packets.drop(self.target, staged)
            return REFUSED
        if replay and staged.manifest_stale(self.now):
            return self.retire(staged, remembered, MANIFEST_EXPIRED)
        hold = (
            tally.stop
            if tally.stop is not None
            else "verify_deferred_proposals"
            if len(tally.proposals) >= MAX_NEW_PROPOSALS
            else "pending_ceiling"
            if self.pending >= PENDING_CEILING
            else None
        )
        if hold is not None:
            tally.skip(hold)
            return UNRESOLVED
        blocked = _attach(self.client, staged, self.observed)
        if blocked == ALREADY_ATTACHED or (blocked is None and staged.attached_ms is None):
            if staged.attached_ms is None:
                # A fresh attach starts the hub's unbound clock now. One already on the hub started
                # it after the stage was written, so the stage's creation time is the safe bound.
                attached = self.now if blocked is None else staged.created_ms
                staged = replace(staged, attached_ms=attached)
                packets.stage(self.target, staged)
            blocked = None
        if blocked is not None:
            tally.skip(blocked)
            if blocked == "evidence_quota":
                tally.stop = blocked  # Back pressure: stop proposing without a failure.
            if blocked in ("manifest_refused", MANIFEST_TRANSIENT):
                tally.attempted += 1
                tally.failed += 1
            if blocked != "manifest_refused":
                return UNRESOLVED  # The stage and its request id stay, and the next run replays them.
            remembered["refused"] = MANIFEST_REFUSED
            self.save()
            packets.drop(self.target, staged)
            return REFUSED
        # Unsettled until the hub answers this request id definitely, so a lost reply is never
        # mistaken for a refusal.
        remembered["unsettled"] = True
        self.save()
        sent = ledger.submit(
            self.client,
            self.ledger,
            staged.digest,
            list(staged.ops),
            base_epoch=self.epoch,
            save=self.save,
            fields={
                "ops_sha256": staged.ops_sha256,
                "unsettled": True,
                "card": fairness.key_digest(staged.scope, [staged.card_id]),
            },
            provenance=True,
        )
        current = self.ledger.get(staged.digest)
        if current is not None and sent.outcome != ledger.UNAVAILABLE:
            current.pop("unsettled", None)
            self.save()
        tally.refused.extend({"card_id": staged.card_id, "code": code} for code in sent.refusals)
        if sent.outcome == ledger.QUOTA:
            tally.stop = "proposal_quota"
            tally.skip("proposal_quota")
            return UNRESOLVED
        if sent.outcome in (ledger.DENIED, ledger.UNAVAILABLE):
            raise VerifyRunError(sent.code or "hub_unavailable", "hub refused the verify request")
        if replay and sent.outcome == ledger.FAILED and sent.code == BINDING_INVALID and current is not None:
            # The manifest aged past the hub's unbound lifetime: not a verdict on the correction.
            current.pop("refused", None)
            return self.retire(staged, current, MANIFEST_EXPIRED)
        tally.attempted += 1
        if sent.outcome == ledger.FAILED:
            tally.failed += 1
            current = self.ledger.get(staged.digest)
            if current is not None and not current.get("refused"):
                tally.skip("submission_unresolved")
                return UNRESOLVED  # Retryable: the stage stays and the next run replays it.
            packets.drop(self.target, staged)
            return REFUSED if current is not None else UNRESOLVED
        tally.worked = True
        if sent.proposal_id is None:
            packets.drop(self.target, staged)
            tally.skip("duplicate_pending")
            return REFUSED
        if sent.outcome == ledger.DUPLICATE:
            return self.bind_duplicate(staged, sent.proposal_id)
        self.open(staged, sent.proposal_id)
        tally.proposals.append({"scope": staged.scope, "card_id": staged.card_id, "proposal_id": sent.proposal_id})
        return OPENED

    def reconcile(self) -> set[tuple[str, str]]:
        """At run start: check, then finalize or replay every stage.

        A stage whose scope is no longer bound and readable is discarded with
        `verify_scope_unbound`, so nothing is sent to that scope. A stage of an
        unresolved submission older than `STAGE_DAYS` is discarded with
        `verify_stage_expired`. Both follow `retire`: an unsettled request
        becomes a tombstone that holds its card, and any other is forgotten.
        Returns the cards whose submission is still unresolved.
        """
        found, invalid = packets.staged(self.target, ledger._submission_digest)
        self.tally.skip("packets_stage_invalid", invalid)
        # Removing an invalid stage can leave its request without one. Hold its card all the same.
        self.tally.skip(UNSETTLED_RECOVERED, backstop(self.ledger, {staged.digest for staged in found}))
        self.save()
        unresolved = set()
        for staged in found:
            current = self.ledger.get(staged.digest)
            ours = current is not None and bool(current.get("proposal_id")) and bool(current.get("created"))
            if current is not None and ours and current.get("ops_sha256") == staged.ops_sha256:
                # This request opened the proposal and the stage holds its only packet. Publishing it
                # is local (no hub request), so it happens before the scope rule, which would drop it.
                self.open(staged, current["proposal_id"])
                self.tally.skip("packets_recovered")
                self.tally.worked = True
                continue
            if staged.scope not in self.bound:
                self.retire(staged, current, SCOPE_UNBOUND)
                continue
            if not ours and staged.expired(self.now):
                self.retire(staged, current, STAGE_EXPIRED)
                continue
            if self.send(staged, replay=True) == UNRESOLVED:
                unresolved.add((staged.scope, staged.card_id))
        return unresolved


def _run(target: Path, state: dict[str, Any], *, dry_run: bool, list_ids: bool, now: int) -> dict[str, Any]:
    if reflect_folder(target):
        raise VerifyRunError("verify_reflect_folder", REFLECT_REFUSAL)
    settings = cmd.configured(target)
    hub_settings, client = cmd._hub(target)
    status = _preflight(client, hub_settings)
    pair = READY(target, settings.propose_seat, settings.confirm_seat)
    early = cmd._binding(client.identity, status["principal"], status["log_epoch"])
    bound = frozenset(scope for scope in settings.scopes() if scope in hub_settings.read_scopes)
    updates, unread = _refresh(client, state, bound) if state.get("binding") == early else ({}, 0)
    blocked: frozenset[str] = frozenset()
    if state.get("binding") == early:
        staged_now = packets.staged_digests(target)
        recovered = backstop(state.setdefault("submissions", {}), staged_now)
        if recovered and not dry_run:
            vstate.save(target, state)
        blocked = _held_cards(state)
    else:
        recovered = 0
    picked = cmd.select_cards(target, state, settings, hub_settings, client, now, pending=updates, blocked=blocked)
    if not picked.same_binding:
        updates = {}
    tally = Tally(skipped=picked.skipped)
    tally.skip("proposal_show_failed", unread)
    tally.skip(UNSETTLED_RECOVERED, recovered)
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    unresolved: set[tuple[str, str]] = set()
    if not dry_run:
        tables = _remember(state, picked, updates, now)
        evict_tombstones(state.setdefault("submissions", {}), now)
        vstate.save(target, state)  # The one write that carries every complete read count snapshot.
    known = {key: entry for cards in tables.values() for key, entry in cards.items()} if not dry_run else picked.entries
    sender = Sender(
        target,
        state,
        client,
        tally,
        tables,
        picked.log_epoch,
        _hub_ms(status, now),
        now,
        _pending_count(known),
        status["principal"],
        bound,
    )
    if not dry_run:
        unresolved = sender.reconcile()
        for name, count in packets.maintain(target, _open_proposals(state), now).items():
            tally.skip(name, count)
    by_card = {(head["scope"], head["card_id"]): head for head in picked.heads}
    chosen = [by_card[(card.scope, card.card_id)] for card, _lane in picked.selection.selected]
    deadline = MONOTONIC() + RUN_SECONDS
    today = now // DAY_MS
    with WORK_AREA(target) as area:
        for position, assembly in enumerate(ASSEMBLE(chosen, settings, ARCHIVE(), now_ms=now)):
            # A seat quota or the wall clock ends verification. A proposal quota only ends proposing.
            if tally.stop == "seat_quota":
                tally.skip("seat_quota_deferred")
                continue
            if MONOTONIC() >= deadline:
                tally.skip("verify_deferred_time")
                continue
            head = chosen[position]
            if (head["scope"], head["card_id"]) in unresolved:
                tally.skip("submission_unresolved_card")
                continue
            key = fairness.key_digest(head["scope"], [head["card_id"]])
            entry = tables.get(head["scope"], {}).get(key)
            if entry is not None and entry.get("pending"):
                tally.skip("pending_proposal")  # Reconcile opened its proposal this run.
                continue
            calls = Calls(area)
            try:
                card = contract.CardInput.from_text(
                    scope=head["scope"],
                    card_id=head["card_id"],
                    revision=head.get("revision", 0),
                    text=head.get("text", ""),
                    updated_ms=_updated_ms(head),
                )
            except contract.Abstain as exc:
                tally.cards.append({"scope": head["scope"], "card_id": head["card_id"], "abstain": exc.code})
                if entry is not None:
                    entry.update(verified=today, verdict=exc.code)
                continue
            try:
                result = contract.verify_card(
                    card,
                    assembly,
                    propose=calls.seat(pair[0]),
                    confirm=calls.seat(pair[1]),
                    resolve_commit=_resolver(settings, card.scope),
                )
            except SeatStop as exc:
                tally.made += calls.made
                tally.ok += calls.ok
                if exc.fatal:
                    raise VerifyRunError(
                        exc.code, "a verify seat failed its isolation or authentication check"
                    ) from None
                tally.stop = "seat_quota"
                tally.skip("seat_quota")
                continue
            tally.made += calls.made
            tally.ok += calls.ok
            tally.worked = tally.worked or calls.ok > 0
            report = {"scope": card.scope, **result.report(), "evidence": assembly.report()["counts"]}
            tally.cards.append(report)
            failed = calls.failed()
            if failed is not None:
                tally.skip(failed)
            sent = None
            if result.proposal is not None:
                sent = _propose(
                    sender, card=card, result=result, assembly=assembly, calls=calls, pair=pair, dry_run=dry_run
                )
            # A card whose submission has no result, or whose correction was deferred, stays out of its
            # cooldown: an unresolved request is replayed, and a deferred correction is recomputed.
            if failed is None and entry is not None and sent not in (UNRESOLVED, DEFERRED):
                entry.update(verified=today, revision=card.revision, verdict=_verdict(result))
                found = assembly.evidence()
                if found is not None:
                    entry["evidence"] = found.digest
                else:
                    entry.pop("evidence", None)
            reason = sender.reasons.get((card.scope, card.card_id))
            if reason is not None and entry is not None:
                entry["verdict"] = reason  # A recorded submission reason outlasts the verdict summary.
    if tally.made and not tally.ok and tally.stop != "seat_quota":
        raise VerifyRunError("verify_seats_failed", "every model call in the run failed")
    payload: dict[str, Any] = {
        "status": "completed",
        "dry_run": dry_run,
        "model": True,
        **cmd.report(picked, state, list_ids=list_ids),
        "cards": tally.cards,
        "proposals": tally.proposals,
        "refused": tally.refused,
        "calls": {"made": tally.made, "ok": tally.ok},
        "idle": not tally.worked,
    }
    if tally.attempted and tally.failed == tally.attempted:
        payload.update(status="failed", code="all_proposals_refused", reason="the hub refused every verify proposal")
    return payload


def _updated_ms(head: Mapping[str, Any]) -> int | None:
    value = head.get("updated_at")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _propose(
    sender: Sender,
    *,
    card: contract.CardInput,
    result: contract.CardResult,
    assembly: ev.Assembly,
    calls: Calls,
    pair: Sequence[seats.Seat],
    dry_run: bool,
) -> str | None:
    """Stage, attach and submit one card's proposal. None when nothing was sent, else the send result."""
    tally = sender.tally
    proposal = result.proposal
    assert proposal is not None
    corrections = proposal.payload_ops()
    digest = ledger._submission_digest(corrections)
    remembered = sender.ledger.get(digest, {}) if not dry_run else {}
    if remembered.get("proposal_id"):
        tally.skip("already_proposed")
        entry = sender.tables.get(card.scope, {}).get(fairness.key_digest(card.scope, [card.card_id]))
        if entry is not None:
            entry.update(pending=remembered["proposal_id"])
        return None
    if remembered.get("refused"):
        tally.skip("refused")
        return None
    if tally.stop is not None:
        tally.skip(DEFERRED_QUOTA)
        return DEFERRED
    if _live_tombstones(sender.state) >= MAX_TOMBSTONES:
        # Fail closed: a new request could become one more tombstone, and live ones are never dropped.
        tally.skip(DEFERRED_TOMBSTONES)
        return DEFERRED
    if len(tally.proposals) >= MAX_NEW_PROPOSALS or sender.pending >= PENDING_CEILING:
        tally.skip(DEFERRED_CAP)
        return DEFERRED
    shown = review(card, result, assembly, calls, pair)
    if contract.scan_problem(shown.manifest.decode()):
        tally.skip("verify_manifest_unclean")
        return None
    if dry_run:
        tally.skip("dry_run_proposal")
        return None
    evidence_id = _evidence_id(sender.state_principal, shown.manifest)
    staged = packets.Stage(
        digest=digest,
        scope=card.scope,
        card_id=card.card_id,
        evidence_id=evidence_id,
        stance=shown.stance,
        manifest=shown.manifest,
        packet=shown.packet,
        ops=(*corrections, _manifest_op(card, proposal, evidence_id)),
        created_ms=sender.now,
    )
    record = staged.record()
    # The stage and, later, the packet it publishes both take room until the stage is removed.
    if not packets.room(sender.target, len(record) + len(shown.packet)):
        tally.skip(DEFERRED_PACKET_CAP)
        return DEFERRED
    # Publish the request id and the digest of the full ops before anything is sent, so a
    # transient failure from here on leaves a request the next run can replay exactly.
    current = sender.ledger.get(digest)
    if current is None or current.get("ops_sha256") != staged.ops_sha256:
        request = hubc.event_id()
        tombstone = sender.ledger.get(staged.ops_sha256)
        if tombstone is not None and tombstone.get("expired") and tombstone.get("ops_sha256") == staged.ops_sha256:
            # Identical ops to an expired request: resend under its id, so the hub's idempotency
            # answers with the original proposal if that request ever landed.
            del sender.ledger[staged.ops_sha256]
            request = tombstone["client_event_id"]
            tally.skip("request_id_reused")
        elif fairness.key_digest(card.scope, [card.card_id]) in _held_cards(sender.state):
            # An earlier request for this card may hold an open proposal this job cannot name. The
            # hub dedupes only identical proposals, so different ops would open a second one.
            tally.skip(UNRESOLVED_REQUEST)
            return None
        sender.ledger[digest] = {
            "client_event_id": request,
            "ops_sha256": staged.ops_sha256,
            "card": fairness.key_digest(card.scope, [card.card_id]),
        }
        sender.save()
    packets.stage(sender.target, staged)
    return sender.send(staged, replay=False)


def _failure(exc: BaseException) -> dict[str, Any]:
    """A fixed code and reason. Never hub text, card text or credential-bearing detail."""
    if isinstance(exc, store.limits.ReplicaLimitError):
        code, reason = "verify_replica_limit", "verify replica bound exceeded before snapshot decoding"
    elif isinstance(exc, (VerifyRunError, seats.SeatRefused)):
        code, reason = exc.code, str(exc)
    elif isinstance(exc, vstate.VerifyStateError):
        code, reason = "verify_state", str(exc)
    elif isinstance(exc, ledger.ReflectError):
        code, reason = "verify_proposal_invalid", "hub returned no open verify proposal"
    elif isinstance(exc, hubc.HubRefusal):
        code = exc.code if isinstance(exc.code, str) and cmd.HUB_CODE.fullmatch(exc.code) else "hub_refused"
        reason = "hub refused the verify request"
    elif isinstance(exc, hubc.HubUnavailable):
        code, reason = "hub_unavailable", "verify could not reach the hub"
    elif isinstance(exc, hubc.HubConfigError):
        code, reason = "verify_hub_config", "invalid hub memory configuration or replica metadata"
    elif isinstance(exc, OSError):
        code, reason = "verify_io", "verify could not safely read or write local state"
    else:
        code, reason = "verify_invalid", "verify received invalid state or data"
    return {"status": "failed", "code": code, "reason": reason}


def run_payload(
    *, target: Path, dry_run: bool, list_ids: bool, scheduled: bool, now_ms: int | None = None
) -> dict[str, Any]:
    """One verify run with the seats. A non-dry run counts toward, or clears, the pause.

    With `[verify] enabled = false` it refuses with `verify_disabled` before any
    seat call, hub request or state write. Only `run --dry-run --no-model`,
    which calls no seat, still previews selection while the pass is disabled.
    """
    cmd._check_overlap(target)
    if not cmd.configured(target).enabled:
        raise VerifyRunError(DISABLED, DISABLED_REASON)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    with vstate.locked(target):
        state = vstate.load(target)
        schedule = {key: state[key] for key in ("consecutive_failures", "paused")}
        if scheduled and not dry_run and state["paused"]:
            return {"status": "paused", "schedule": schedule}
        try:
            payload = _run(target, state, dry_run=dry_run, list_ids=list_ids, now=now)
        except (
            hubc.HubConfigError,
            hubc.HubUnavailable,
            hubc.HubRefusal,
            ledger.ReflectError,
            OSError,
            ValueError,
            UnicodeError,
            RecursionError,
        ) as exc:
            payload = _failure(exc)
        if not dry_run:
            # The pause counter rule: a failed run adds one. A completed run resets it only when it
            # did work (a seat call answered, or a submission reached a result). An idle run leaves it.
            if payload["status"] == "failed":
                vstate.record_run(state, "failed")
            elif not payload.get("idle"):
                vstate.record_run(state, "completed")
            vstate.save(target, state)
        payload["schedule"] = {key: state[key] for key in ("consecutive_failures", "paused")}
        return payload
