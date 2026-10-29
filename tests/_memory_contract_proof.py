"""Disposable Phase A fixture. Scripted decisions and opaque handles are NOT crypto.

Only TrustedClient holds content/citations. SQLite models one authority, never a
production store or separate task/proposal database. No remote adapter is tested.
"""

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import stat

A = ("consumer-a", "device-a")
B = ("consumer-b", "device-b")
REVIEWER = ("reviewer", "device-reviewer")
PUBLISHER = ("publisher", "device-publisher")
ADMIN = ("administrator", "device-administrator")
RECOVERY = ("recovery", "device-recovery")
CHECKPOINT = ("checkpoint", "device-checkpoint")
TEAM, CANDIDATE, PERSONAL = tuple(f"card-00000000-0000-4000-8000-{n:012d}" for n in range(1, 4))
MARKERS = ("SYNTHETIC_APPROVED_TEAM", "SYNTHETIC_CANDIDATE_ONLY", "SYNTHETIC_PERSONAL_DENIED")
REFUSAL = {"state": "unavailable", "terminal": False}


@dataclass(frozen=True)
class Artifact:
    item: str = TEAM
    base: str = "rev-0"
    handle: str = "opaque-0"
    sources: tuple[str, ...] = ("source-ref-1",)
    scope: str = "scope-fixture"
    mutation: str = "replace"
    recipients: tuple[str, ...] = (A[1], B[1])
    epoch: int = 0
    policy: int = 0


@dataclass(frozen=True)
class Attempt:
    artifact: Artifact
    review: str | None
    owner: str
    request: str
    scope: str
    head: int
    fence: int
    epoch: int
    publisher: str = ""
    digest: str = ""
    bundle: str = "scripted-two-decisions"
    signatures: int = 2

    @property
    def key(self):
        return ("authority-fixture", "corpus-fixture", self.epoch, self.owner, self.scope, self.request)


class ScriptedVerifier:
    """Declarative exact-equality oracle. Tokens are labels, never signatures."""

    def __init__(self):
        self.reviews = {}
        self.publishers = {}
        self.fail = set()
        self.calls = []
        self.signers = {}

    def verify(self, kind, token, value, db, *, current_roles=True):
        self.calls.append((kind, db.in_transaction))
        registry = self.reviews if kind == "review" else self.publishers
        actor = self.signers.get(token)
        grant = (
            None
            if actor is None
            else db.execute(
                "SELECT 1 FROM grants WHERE principal=? AND device=? AND action=? AND active=1",
                (*actor, "review" if kind == "review" else "publish"),
            ).fetchone()
        )
        return kind not in self.fail and registry.get(token) == value and (grant is not None or not current_roles)


def publisher_core(attempt):
    return replace(attempt, publisher="", digest="")


def request_digest(attempt):
    # Hashes fixture metadata/opaque labels only. No plaintext commitments.
    return sha256(repr(replace(attempt, digest="")).encode()).hexdigest()


class Authority:
    profile = "local-solo-fixture"

    def __init__(self, root: Path):
        root.mkdir(mode=0o700)
        self.root = root
        path = root / "authority.sqlite"
        path.touch(mode=0o600)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.trace = []
        self.db.set_trace_callback(self.trace.append)
        self.db.executescript("""
            CREATE TABLE meta (head INTEGER, policy INTEGER, fence INTEGER, epoch INTEGER);
            INSERT INTO meta VALUES (0, 0, 0, 0);
            CREATE TABLE grants (principal TEXT, device TEXT, action TEXT, start INTEGER,
                                 active INTEGER, PRIMARY KEY(principal, device, action));
            CREATE TABLE items (item TEXT PRIMARY KEY, revision TEXT, approved INTEGER, owner TEXT);
            CREATE TABLE revisions (item TEXT, revision TEXT, handle TEXT, sequence INTEGER,
                                    recipients TEXT, PRIMARY KEY(item, revision));
            CREATE TABLE outcomes (scoped TEXT PRIMARY KEY, digest TEXT, state TEXT, head INTEGER);
            CREATE TABLE audit (scoped TEXT PRIMARY KEY, state TEXT, head INTEGER);
        """)
        for actor, actions in (
            (A, ("read", "propose")),
            (B, ("read",)),
            (REVIEWER, ("review",)),
            (PUBLISHER, ("publish", "outcome", "cancel")),
            (ADMIN, ("admin",)),
            (RECOVERY, ("recover",)),
            (CHECKPOINT, ("checkpoint",)),
        ):
            for action in actions:
                self.db.execute("INSERT INTO grants VALUES (?, ?, ?, 0, 1)", (*actor, action))
        for n, (item, approved, owner) in enumerate(
            ((TEAM, 1, None), (CANDIDATE, 0, None), (PERSONAL, 1, "private-owner"))
        ):
            self.db.execute("INSERT INTO items VALUES (?, 'rev-0', ?, ?)", (item, approved, owner))
            self.db.execute(
                "INSERT INTO revisions VALUES (?, 'rev-0', ?, 0, ?)", (item, f"opaque-{n}", json.dumps((A[1], B[1])))
            )
        self.verifier = ScriptedVerifier()
        self.ledger_complete = True
        self.restore_epoch = 0
        self.checkpoint_gap = False
        # Independent control-plane copies, deliberately outside backend storage.
        self.checkpoints = []
        self.checkpoint(CHECKPOINT)

    @property
    def head(self):
        return self.db.execute("SELECT head FROM meta").fetchone()[0]

    def allowed(self, actor, action):
        return self.db.execute(
            "SELECT start FROM grants WHERE principal=? AND device=? AND action=? AND active=1",
            (*actor, action),
        ).fetchone()

    def checkpoint(self, actor):
        previous_gap = self.checkpoint_gap
        # Every unsuccessful construction or retention path leaves a sticky gap.
        self.checkpoint_gap = True
        if self.allowed(actor, "checkpoint") is None:
            return False
        self.checkpoints.append((self.head, self.snapshot_digest(self.db)))
        self.checkpoint_gap = previous_gap
        return True

    def revoke(self, actor):
        self.db.execute("UPDATE grants SET active=0 WHERE principal=? AND device=?", actor)
        self.advance("policy")

    def advance(self, field):
        if field not in {"policy", "fence", "epoch"}:
            raise ValueError("unknown fixture transition")
        self.db.execute(f"UPDATE meta SET {field}={field}+1")
        self.checkpoint(CHECKPOINT)

    def enroll(self, actor, new):
        if self.allowed(actor, "admin") is None:
            return REFUSAL.copy()
        self.db.execute("INSERT INTO grants VALUES (?, ?, 'read', ?, 1)", (*new, self.head + 1))
        self.advance("policy")
        return {"state": "future-only"}

    def artifact(self):
        _, policy, _, epoch = self.db.execute("SELECT * FROM meta").fetchone()
        base = self.db.execute("SELECT revision FROM items WHERE item=?", (TEAM,)).fetchone()[0]
        recipients = tuple(
            row[0]
            for row in self.db.execute("SELECT device FROM grants WHERE action='read' AND active=1 ORDER BY device")
        )
        return Artifact(base=base, recipients=recipients, policy=policy, epoch=epoch)

    def review(self, actor, artifact):
        if self.allowed(actor, "review") is None:
            return None
        token = f"scripted-review-{len(self.verifier.reviews)}"
        self.verifier.reviews[token] = artifact
        self.verifier.signers[token] = actor
        return token

    def authorize(self, actor, request, artifact, review):
        head, _, fence, epoch = self.db.execute("SELECT * FROM meta").fetchone()
        attempt = Attempt(artifact, review, actor[0], request, "scope-fixture", head, fence, epoch)
        if self.allowed(actor, "publish") is None:
            return attempt
        token = f"scripted-publisher-{len(self.verifier.publishers)}"
        self.verifier.publishers[token] = publisher_core(attempt)
        self.verifier.signers[token] = actor
        attempt = replace(attempt, publisher=token)
        return replace(attempt, digest=request_digest(attempt))

    def _lookup(self, attempt):
        row = self.db.execute(
            "SELECT digest, state, head FROM outcomes WHERE scoped=?", (json.dumps(attempt.key),)
        ).fetchone()
        if row is None:
            return None
        if row[0] != attempt.digest:
            return {"state": "conflict", "terminal": True}
        return {"state": row[1], "terminal": True, "head": row[2]}

    def outcome(self, actor, attempt):
        owner = self.allowed(actor, "outcome") is not None and actor[0] == attempt.owner
        if not owner and self.allowed(actor, "recover") is None:
            return REFUSAL.copy()
        found = self._lookup(attempt)
        if found is not None:
            return found
        return {
            "state": "absent" if self.ledger_complete and attempt.epoch >= self.restore_epoch else "ambiguous",
            "terminal": False,
        }

    def _terminal(self, attempt, state):
        scoped = json.dumps(attempt.key)
        self.db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?)", (scoped, attempt.digest, state, self.head))
        self.db.execute("INSERT INTO audit VALUES (?, ?, ?)", (scoped, state, self.head))
        return {"state": state, "terminal": True, "head": self.head}

    @staticmethod
    def bounded(attempt):
        if type(attempt) is not Attempt or type(attempt.artifact) is not Artifact:
            return False
        if type(attempt.bundle) is not str or type(attempt.signatures) is not int:
            return False
        artifact = attempt.artifact
        texts = (
            attempt.owner,
            attempt.request,
            attempt.scope,
            attempt.publisher,
            attempt.digest,
            artifact.item,
            artifact.base,
            artifact.handle,
            artifact.scope,
            artifact.mutation,
        )
        numbers = (attempt.head, attempt.fence, attempt.epoch, artifact.epoch, artifact.policy)
        if not all(type(text) is str for text in texts):
            return False
        if not all(type(number) is int and number >= 0 for number in numbers):
            return False
        if attempt.review is not None and type(attempt.review) is not str:
            return False
        if type(artifact.sources) is not tuple or not all(type(source) is str for source in artifact.sources):
            return False
        recipients = attempt.artifact.recipients
        if type(recipients) is not tuple or not all(type(device) is str for device in recipients):
            return False
        return (
            len(repr(attempt).encode()) <= 4096
            and attempt.signatures == 2
            and len(attempt.artifact.recipients) <= 4
            and len(set(attempt.artifact.recipients)) == len(attempt.artifact.recipients)
        )

    def _valid(self, attempt, *, current_roles=True):
        return (
            self.bounded(attempt)
            and request_digest(attempt) == attempt.digest
            and self.verifier.verify("review", attempt.review, attempt.artifact, self.db, current_roles=current_roles)
            and self.verifier.verify(
                "publisher", attempt.publisher, publisher_core(attempt), self.db, current_roles=current_roles
            )
        )

    def commit(self, actor, attempt):
        return self._transition(actor, attempt, cancel=False)

    def cancel(self, actor, attempt):
        return self._transition(actor, attempt, cancel=True)

    def _transition(self, actor, attempt, *, cancel):
        result = REFUSAL.copy()
        wrote = False
        self.trace.clear()
        if not self.bounded(attempt):
            return result
        self.db.execute("BEGIN IMMEDIATE")
        try:
            owner = actor[0] == attempt.owner
            authorized = self.allowed(actor, "cancel" if cancel else "publish") is not None
            recovery = cancel and self.allowed(actor, "recover") is not None
            if not ((owner and authorized) or recovery):
                return result
            # Authenticate/authorize before lookup, including historical retry.
            found = self._lookup(attempt)
            if found is not None:
                return found
            if not self.ledger_complete or attempt.epoch < self.restore_epoch:
                return {"state": "ambiguous", "terminal": False}
            if not self._valid(attempt, current_roles=not cancel):
                return result
            if cancel:
                result = self._terminal(attempt, "abandoned")
            else:
                head, policy, fence, epoch = self.db.execute("SELECT * FROM meta").fetchone()
                artifact = attempt.artifact
                base = self.db.execute("SELECT revision FROM items WHERE item=?", (artifact.item,)).fetchone()
                current_recipients = tuple(
                    row[0]
                    for row in self.db.execute(
                        "SELECT device FROM grants WHERE action='read' AND active=1 ORDER BY device"
                    )
                )
                valid = (
                    attempt.head == head
                    and attempt.fence == fence
                    and attempt.epoch == epoch
                    and artifact.epoch == epoch
                    and artifact.policy == policy
                    and artifact.scope == attempt.scope == "scope-fixture"
                    and artifact.mutation == "replace"
                    and artifact.item == TEAM
                    and base is not None
                    and base[0] == artifact.base
                    and artifact.recipients == current_recipients
                )
                if not valid:
                    result = self._terminal(attempt, "conflict")
                else:
                    revision = f"rev-{head + 1}"
                    self.db.execute(
                        "INSERT INTO revisions VALUES (?, ?, ?, ?, ?)",
                        (artifact.item, revision, artifact.handle, head + 1, json.dumps(artifact.recipients)),
                    )
                    self.db.execute("UPDATE items SET revision=? WHERE item=?", (revision, artifact.item))
                    self.db.execute("UPDATE meta SET head=head+1")
                    result = self._terminal(attempt, "committed")
            wrote = True
            self.db.execute("COMMIT")
        finally:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
        if wrote:
            self.checkpoint(CHECKPOINT)
        return result

    def read(self, actor, item, sink, revision):
        grant = self.allowed(actor, "read")
        if grant is None or sink != "local-client":
            return None
        row = self.db.execute("SELECT revision, approved, owner FROM items WHERE item=?", (item,)).fetchone()
        if row is None or not row[1] or (row[2] is not None and row[2] != actor[0]):
            return None
        revision = revision or row[0]
        record = self.db.execute(
            "SELECT handle, sequence, recipients FROM revisions WHERE item=? AND revision=?", (item, revision)
        ).fetchone()
        if record is None or grant[0] > record[1] or actor[1] not in json.loads(record[2]):
            return None
        return (revision, record[0])

    @staticmethod
    def snapshot_digest(db):
        # Compare logical snapshots; online backup rewrites SQLite header bytes.
        return sha256("\n".join(db.iterdump()).encode()).hexdigest()

    def backup(self):
        # SQLite online backup API, not a file copy of a live database.
        with sqlite3.connect(":memory:") as destination:
            self.db.backup(destination)
            return "\n".join(destination.iterdump())

    def restore(self, actor, backup):
        if (
            self.allowed(actor, "recover") is None
            or not backup
            or not self.ledger_complete
            or not self.checkpoints
            or self.checkpoint_gap
        ):
            return REFUSAL.copy()
        expected = self.checkpoints[-1]
        # Check the retained snapshot floor before executing any fixture dump SQL.
        if type(backup) is not str or sha256(backup.encode()).hexdigest() != expected[1]:
            return REFUSAL.copy()
        with sqlite3.connect(":memory:") as candidate:
            try:
                candidate.executescript(backup)
                if self.snapshot_digest(candidate) != expected[1]:
                    return REFUSAL.copy()
            except (sqlite3.DatabaseError, TypeError):
                return REFUSAL.copy()
            candidate.backup(self.db)
        # Missing old-epoch operations stay ambiguous, while new-epoch work can proceed.
        # Preserve old-operation ambiguity even if the post-restore checkpoint fails.
        self.restore_epoch = self.db.execute("SELECT epoch FROM meta").fetchone()[0] + 1
        self.advance("epoch")
        return {"state": "restored"}

    def diagnostics(self):
        return json.dumps({"profile": self.profile, "head": self.head})

    def side_effects(self):
        return [(".", "directory", stat.S_IMODE(self.root.stat().st_mode))] + [
            (str(p.relative_to(self.root)), "directory" if p.is_dir() else "file", stat.S_IMODE(p.stat().st_mode))
            for p in sorted(self.root.rglob("*"))
        ]


class TrustedClient:
    """Named plaintext exception: in-process content, returned excerpts, sink recorder."""

    def __init__(self, authority):
        self.authority = authority
        self.contents = {f"opaque-{i}": (marker, "synthetic-source:1") for i, marker in enumerate(MARKERS)}
        self.sinks = []
        self.pending = None

    def read(self, actor, item, *, sink="local-client", revision=None, limit=5, excerpt_bytes=4096):
        if not (1 <= limit <= 5 and 0 <= excerpt_bytes <= 4096):
            return REFUSAL.copy()
        record = self.authority.read(actor, item, sink, revision)
        if record is None:
            return REFUSAL.copy()
        revision, handle = record
        content = self.contents.get(handle)
        if content is None or len(content[0].encode()) > 16384:
            return REFUSAL.copy()
        excerpt = content[0].encode()[:excerpt_bytes].decode("utf-8", errors="ignore")
        self.sinks.append((sink, item, revision))
        return {"item": item, "revision": revision, "citation": content[1], "excerpt": excerpt}

    def reconcile(self):
        result = self.authority.outcome(PUBLISHER, self.pending)
        if result.get("terminal"):
            self.pending = None
        if result["state"] == "absent":
            return {"state": "exact-retry"}
        if result["state"] == "ambiguous":
            return {"state": "ambiguous"}
        return result

    def retry(self, supplied=None):
        if supplied is not None and supplied != self.pending:
            return REFUSAL.copy()
        result = self.authority.commit(PUBLISHER, self.pending)
        if result.get("terminal"):
            self.pending = None
        if result["state"] == "ambiguous":
            return {"state": "ambiguous"}
        return result


class AdapterA:
    label = "harness-a"
    persona = "helper"

    def __init__(self, client, identity=A):
        self.client = client
        self.identity = identity

    def read(self, item, **kwargs):
        return self.client.read(self.identity, item, **kwargs)

    def propose(self):
        if self.client.authority.allowed(self.identity, "propose") is None:
            return REFUSAL.copy()
        return {"state": "local-candidate"}


class AdapterB(AdapterA):
    label = "harness-b"

    def __init__(self, client, identity=B):
        super().__init__(client, identity)
