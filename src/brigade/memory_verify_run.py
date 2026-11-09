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
6. For each card with an op: write nothing yet, build the review packet and the
   manifest, scan the manifest, attach it with `evidence.attach`, then open one
   proposal for that one card with the correction ops and an `append_evidence`
   op citing the manifest. Submission goes through the S19d request id ledger
   in `memory_reflect_hub.submit`. Only after the proposal opens is the packet
   written, owner-only, named by its proposal id.

What reaches the hub: the `status`, `read_stats` and `proposal_show` reads,
the manifest (codes, references, digests, byte ranges and seat ids, no free
text) and the proposal ops. The only free text in an op is the replacement
the contract already checked. Card text, evidence text and model output stay
in the packet on this host.

Packets live under `.brigade/memory/verify/packets/` in an owner-only (0700)
directory as owner-only (0600) files. An open proposal's packet is
`<proposal-id>.json`. When a run sees the proposal is no longer open, it
renames the packet to `<proposal-id>.t<ms>.json`, and a packet is deleted 14
days after that time. New packets never push the directory past 64 MiB: the
oldest terminal packets go first, and if there is still no room the card is
deferred with `verify_packet_cap` and nothing is attached or proposed.

Failure and quota follow D6. Three consecutive failed non-dry runs pause the
scheduled job, and only a successful non-dry manual run clears the pause. A
dry run calls the seats (unless `--no-model`, handled in `memory_verify_cmd`)
but attaches, submits and writes nothing, and leaves the pause counter alone.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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
PACKETS = f"{vstate.ROOT}/packets"
PACKET_CAP = 64 * 1024 * 1024
PACKET_DAYS = 14
DAY_MS = pick.DAY_MS
PACKET_SCHEMA = "brigade.memory-verify-packet.v1"
MANIFEST_SCHEMA = "brigade.memory-verify-manifest.v1"
MANIFEST_KIND = "memory-verify-manifest"
WORKSPACE_ID = "memory-verify"
FLOOR = "verifier_floor"
MANIFESTS = "verifier_manifests"
REFLECT_FOLDER = ".brigade/memory/reflect"
OPEN_STATES = frozenset({"pending", "held"})
EVIDENCE_CONFLICT = "memory_evidence_conflict"
EVIDENCE_CAPS = frozenset({"memory_evidence_principal_cap", "memory_evidence_workspace_cap"})
COMPONENT = re.compile(r"[A-Za-z0-9._:-]{1,128}")
OPEN_PACKET = re.compile(r"([0-9A-HJKMNP-TV-Z]{26})\.json")
DONE_PACKET = re.compile(r"([0-9A-HJKMNP-TV-Z]{26})\.t([0-9]{1,16})\.json")
REFLECT_REFUSAL = "the verify folder must not be the reflect job's folder. Give memory-verify its own folder"

# Injectable for tests: the seat layer, evidence assembly, the archive and the clock.
READY: Callable[..., tuple[seats.Seat, seats.Seat]] = seats.ready
CALL: Callable[..., seats.Reply] = seats.call
WORK_AREA: Callable[[Path], contextlib.AbstractContextManager[Any]] = seats.work_area
ASSEMBLE: Callable[..., Iterator[ev.Assembly]] = ev.assemble_all
MONOTONIC: Callable[[], float] = time.monotonic


def _engine(verb: str, arguments: list[str]) -> Any:
    """One evidence engine verb, decoded, or None on any failure."""
    from . import evidence_runtime, proc

    binary = evidence_runtime.resolve_miseledger()
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


def _refresh(client: hubc.MemoryHubClient, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Entries whose remembered proposal is no longer open, with `pending` cleared."""
    updates: dict[str, dict[str, Any]] = {}
    shown = 0
    for table in state["scopes"].values():
        for key, entry in table.get("cards", {}).items():
            proposal = entry.get("pending")
            if not proposal or shown >= MAX_SHOWS:
                continue
            shown += 1
            try:
                reply = client.call("proposal_show", proposal_id=proposal)
                current = reply.get("state")
            except hubc.HubRefusal as exc:
                if exc.status != 404:
                    raise
                current = "missing"  # Compacted or gone: no longer open.
            if current in OPEN_STATES:
                continue
            settled = {name: value for name, value in entry.items() if name != "pending"}
            if current == "rejected":
                # Slice 10 refines this into a per-verdict code. Until then the card waits its interval.
                settled["verdict"] = "rejected"
            updates[key] = settled
    return updates


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


# --- Packets -------------------------------------------------------------------------------


@contextlib.contextmanager
def _packets(target: Path) -> Iterator[int]:
    with io.directory(target / PACKETS, create=True, private=True) as fd:
        yield fd


def _listing(fd: int) -> list[tuple[str, int]]:
    """(name, size) of every regular file in the packet directory."""
    found = []
    for name in os.listdir(fd):
        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            found.append((name, metadata.st_size))
    return found


def maintain_packets(target: Path, open_ids: set[str], now: int) -> dict[str, int]:
    """Mark packets of settled proposals terminal at `now`, then delete those terminal for 14 days."""
    counts = {"packets_settled": 0, "packets_expired": 0}
    with _packets(target) as fd:
        for name, _size in _listing(fd):
            opened = OPEN_PACKET.fullmatch(name)
            if opened and opened[1] not in open_ids:
                os.rename(name, f"{opened[1]}.t{now}.json", src_dir_fd=fd, dst_dir_fd=fd)
                counts["packets_settled"] += 1
                continue
            done = DONE_PACKET.fullmatch(name)
            if done and now - int(done[2]) >= PACKET_DAYS * DAY_MS:
                os.unlink(name, dir_fd=fd)
                counts["packets_expired"] += 1
        os.fsync(fd)
    return counts


def _room(target: Path, size: int) -> bool:
    """Whether a packet of `size` bytes fits under the cap, deleting the oldest terminal packets first."""
    with _packets(target) as fd:
        listing = _listing(fd)
        total = sum(entry[1] for entry in listing)
        terminal = sorted(
            (int(done[2]), name, entry_size)
            for name, entry_size in listing
            if (done := DONE_PACKET.fullmatch(name)) is not None
        )
        for _, name, entry_size in terminal:
            if total + size <= PACKET_CAP:
                break
            os.unlink(name, dir_fd=fd)
            total -= entry_size
        return total + size <= PACKET_CAP


def _write_packet(target: Path, proposal_id: str, data: bytes) -> None:
    io.write_exclusive(target / PACKETS / f"{proposal_id}.json", data)


# --- Seat calls ----------------------------------------------------------------------------


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

    def skip(self, reason: str, amount: int = 1) -> None:
        if amount:
            self.skipped[reason] = self.skipped.get(reason, 0) + amount


def _attach(
    client: hubc.MemoryHubClient, card: contract.CardInput, shown: Review, evidence_id: str, observed: int
) -> str | None:
    """Attach the manifest. None when it is on the hub, else a skip code. Raises when the run must fail."""
    try:
        client.call(
            "evidence.attach",
            scope=card.scope,
            card_id=card.card_id,
            workspace_id=WORKSPACE_ID,
            evidence_id=evidence_id,
            artifact_kind=MANIFEST_KIND,
            artifact_sha256=io.sha256(shown.manifest),
            artifact_base64=base64.b64encode(shown.manifest).decode("ascii"),
            observed_at=observed,
            stance=shown.stance,
            client_event_id=hubc.event_id(),
        )
    except hubc.HubRefusal as exc:
        if exc.denies_access:
            raise
        if exc.status == 409 and exc.code == EVIDENCE_CONFLICT:
            return None  # The id is the manifest's digest, so the same manifest is already attached.
        if exc.status == 429 and exc.code in hubc.EVIDENCE_QUOTA_REFUSALS:
            return "evidence_quota"
        if exc.code in EVIDENCE_CAPS:
            return "evidence_cap"
        return "manifest_refused"
    return None


def _run(target: Path, state: dict[str, Any], *, dry_run: bool, list_ids: bool, now: int) -> dict[str, Any]:
    if reflect_folder(target):
        raise VerifyRunError("verify_reflect_folder", REFLECT_REFUSAL)
    settings = cmd.configured(target)
    hub_settings, client = cmd._hub(target)
    status = _preflight(client, hub_settings)
    pair = READY(target, settings.propose_seat, settings.confirm_seat)
    early = cmd._binding(client.identity, status["principal"], status["log_epoch"])
    updates = _refresh(client, state) if state.get("binding") == early else {}
    picked = cmd.select_cards(target, state, settings, hub_settings, client, now, pending=updates)
    if not picked.same_binding:
        updates = {}
    tally = Tally(skipped=picked.skipped)
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    if not dry_run:
        tables = _remember(state, picked, updates, now)
        vstate.save(target, state)  # The one write that carries every complete read count snapshot.
        for name, count in maintain_packets(target, _open_proposals(state), now).items():
            tally.skip(name, count)
    known = {key: entry for cards in tables.values() for key, entry in cards.items()} if not dry_run else picked.entries
    pending = _pending_count(known)
    by_card = {(head["scope"], head["card_id"]): head for head in picked.heads}
    chosen = [by_card[(card.scope, card.card_id)] for card, _lane in picked.selection.selected]
    observed = _hub_ms(status, now)
    deadline = MONOTONIC() + RUN_SECONDS
    submissions = state.setdefault("submissions", {}) if not dry_run else {}
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
            key = fairness.key_digest(head["scope"], [head["card_id"]])
            entry = tables.get(head["scope"], {}).get(key)
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
            report = {"scope": card.scope, **result.report(), "evidence": assembly.report()["counts"]}
            tally.cards.append(report)
            failed = calls.failed()
            if failed is not None:
                tally.skip(failed)
            elif entry is not None:
                entry.update(verified=today, revision=card.revision, verdict=_verdict(result))
                found = assembly.evidence()
                if found is not None:
                    entry["evidence"] = found.digest
                else:
                    entry.pop("evidence", None)
            if result.proposal is None:
                continue
            pending = _propose(
                target,
                state,
                client,
                tally,
                card=card,
                result=result,
                assembly=assembly,
                calls=calls,
                pair=pair,
                entry=entry,
                submissions=submissions,
                principal=status["principal"],
                epoch=picked.log_epoch,
                observed=observed,
                pending=pending,
                dry_run=dry_run,
                now=now,
            )
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
    target: Path,
    state: dict[str, Any],
    client: hubc.MemoryHubClient,
    tally: Tally,
    *,
    card: contract.CardInput,
    result: contract.CardResult,
    assembly: ev.Assembly,
    calls: Calls,
    pair: Sequence[seats.Seat],
    entry: dict[str, Any] | None,
    submissions: dict[str, dict[str, Any]],
    principal: str,
    epoch: str,
    observed: int,
    pending: int,
    dry_run: bool,
    now: int,
) -> int:
    """Attach and submit one card's proposal. Returns the updated pending count."""
    proposal = result.proposal
    assert proposal is not None
    corrections = proposal.payload_ops()
    digest = ledger._submission_digest(corrections)
    remembered = submissions.get(digest, {})
    if remembered.get("proposal_id"):
        tally.skip("already_proposed")
        if entry is not None:
            entry.update(pending=remembered["proposal_id"])
        return pending
    if remembered.get("refused"):
        tally.skip("refused")
        return pending
    if tally.stop is not None:
        tally.skip(tally.stop)
        return pending
    if len(tally.proposals) >= MAX_NEW_PROPOSALS:
        tally.skip("verify_deferred_proposals")
        return pending
    if pending >= PENDING_CEILING:
        tally.skip("pending_ceiling")
        return pending
    shown = review(card, result, assembly, calls, pair)
    if contract.scan_problem(shown.manifest.decode()):
        tally.skip("verify_manifest_unclean")
        return pending
    if dry_run:
        tally.skip("dry_run_proposal")
        return pending
    if not _room(target, len(shown.packet)):
        tally.skip("verify_packet_cap")
        return pending
    evidence_id = _evidence_id(principal, shown.manifest)
    blocked = _attach(client, card, shown, evidence_id, observed)
    if blocked is not None:
        tally.skip(blocked)
        if blocked == "evidence_quota":
            tally.stop = blocked  # Back pressure: stop proposing without a failure.
        elif blocked == "manifest_refused":
            tally.attempted += 1
            tally.failed += 1
        return pending
    ops = [*corrections, _manifest_op(card, proposal, evidence_id)]
    sent = ledger.submit(client, submissions, digest, ops, base_epoch=epoch, save=lambda: vstate.save(target, state))
    tally.refused.extend({"card_id": card.card_id, "code": code} for code in sent.refusals)
    if sent.outcome == ledger.QUOTA:
        tally.stop = "proposal_quota"
        tally.skip("proposal_quota")
        return pending
    if sent.outcome in (ledger.DENIED, ledger.UNAVAILABLE):
        raise VerifyRunError(sent.code or "hub_unavailable", "hub refused the verify request")
    tally.attempted += 1
    if sent.outcome == ledger.FAILED:
        tally.failed += 1
        return pending
    if sent.proposal_id is not None and entry is not None:
        entry.update(pending=sent.proposal_id, proposed=now)
    if sent.outcome == ledger.DUPLICATE:
        tally.skip("duplicate_pending")
        return pending + (sent.proposal_id is not None)
    assert sent.proposal_id is not None
    _write_packet(target, sent.proposal_id, shown.packet)
    vstate.save(target, state)
    tally.proposals.append({"scope": card.scope, "card_id": card.card_id, "proposal_id": sent.proposal_id})
    return pending + 1


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
    """One verify run with the seats. A non-dry run counts toward, or clears, the pause."""
    cmd._check_overlap(target)
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
            vstate.record_run(state, "failed" if payload["status"] == "failed" else "completed")
            vstate.save(target, state)
        payload["schedule"] = {key: state[key] for key in ("consecutive_failures", "paused")}
        return payload
