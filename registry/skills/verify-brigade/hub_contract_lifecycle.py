"""Fleet Hub wire contract, lifecycle cases (v2 slice S1b).

Message and question expiry, the sealed-content lifecycle, and the message
`client_id` replay window, as clients see them. These cases need days to
pass, so they run against a second hub that reads its wall clock from a file
the drive writes: the same clock seam the storage parity drive uses
(`hub_clock.py` for the Python hub, `{clock_file}` in a launch template for
any other). The core cases keep their own hub on real time, because a
one-second lease and the long polls need it.

Like `hub_contract.py`, nothing here imports `brigade.*`. The cases record
what the Python hub does today (contract v1 is frozen), including the 32-row
sealed purge batch: the sequence keeps fewer than 32 eligible rows, so a hub
that projects every eligible row passes too.

Timeline, in days and seconds from the clock's start (`storage_parity.T0`):

- 0: two nodes, the question operator, keys, and the messages every later
  case reads, all sent at exactly 0 so their expiry is exact.
- 0 + 59 s, 60 s, 61 s, 121 s: a 60-second question is pending, then timed
  out; the operator's late answer is recorded as late; an expired question no
  longer counts against the two-pending limit; an operator answer at exactly a
  timed-out question's deadline is recorded nowhere.
- 1, 2, 3: the recipient acknowledges two sealed messages (days 1 and 3), and
  both nodes reply (day 2).
- 9: acknowledged 8 days ago reads as its tombstone in `sent_lookup`, `sent`
  and `list`; acknowledged 6 days ago and unacknowledged stay sealed.
- 10 - 1 s, 10: the 7-day boundary is inclusive.
- 89: a resend inside the `client_id` window returns the first id, and a
  different payload is refused.
- 90, 90 + 1 s: the 90-day retention boundary is inclusive.
- 91: expired messages are gone from `list`, `count`, `sent`, `sent_lookup`,
  `ack` and `reply`; a plaintext `reply_to` to a purged parent reads null; a
  sealed reply keeps its parent id; the `client_id` is free again; the
  question row is gone; `enqueued_at` limits.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import hub_contract as hc
import storage_parity as sp
from hub_contract import NONE, Eq, NonEmpty

DAY = sp.DAY
RETENTION = 90 * DAY
SEALED_LIFETIME = 7 * DAY
QUESTION_TIMEOUT = 60
DEFAULT_CLOCK_HUB_CMD = sp.DEFAULT_HUB_CMD
NODE_A, NODE_B = "lifecycle-a", "lifecycle-b"
OPERATOR = sp.OPERATOR
# The sealed messages whose tombstones the golden records, and the case that reads each one.
CLOCK_TEMPLATE_REFUSAL = "--clock-hub-cmd must contain {port} and {clock_file} so the hub runs on the drive's clock"
TOMBSTONE_CASES = {
    "s-old": "lifecycle sent_lookup a sealed message acknowledged 8 days ago",
    "s-mid": "lifecycle sent_lookup when the 7-day lifetime ends",
}


@dataclass
class Lifecycle:
    """What the lifecycle run hands back to the CLI."""

    hub: hc.Hub
    result: dict
    tombstones: dict[str, str] = field(default_factory=dict)


def launch_clocked(template: str, *, brigade: list[str], state_dir: Path, startup_timeout: float) -> tuple[hc.Hub, Any]:
    """Start a hub on the drive's clock, set to the run's start."""
    if "{port}" not in template or "{clock_file}" not in template:
        raise hc.ContractError(CLOCK_TEMPLATE_REFUSAL)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    admin = "contract-admin-" + secrets.token_urlsafe(18)
    token_file = state_dir / "admin-token"
    token_file.write_text(admin + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    clock = sp.Clock(state_dir / "clock")
    clock.set(0)
    try:
        hub = sp.launch(
            sp.Engine("lifecycle", template),
            brigade=brigade,
            state_dir=state_dir,
            admin_token=admin,
            env_extra={},
            phase=0,
            startup_timeout=startup_timeout,
        )
    except sp.ParityError as exc:
        raise hc.ContractError(str(exc)) from exc
    return hub, clock


def would_launch(template: str, *, brigade: list[str], state_dir: str) -> list[str]:
    values = {
        "host": hc.LOOPBACK,
        "port": "<port>",
        "state_dir": state_dir,
        "db": f"{state_dir}/hub.db",
        "token_file": f"{state_dir}/admin-token",
        "clock_file": f"{state_dir}/clock",
        "python": sp.python_for(brigade),
        "launcher": str(sp.LAUNCHER),
    }
    if "{clock_file}" not in template:
        raise hc.ContractError(CLOCK_TEMPLATE_REFUSAL)
    return hc.expand_command(template, brigade=brigade, values=values)


def tombstone_of(text: str) -> str:
    """The rule the golden records: the stored envelope without `ct` and `recipients`, re-encoded."""
    value = json.loads(text)
    value.pop("ct", None)
    value.pop("recipients", None)
    return hc.canonical(value)


def _by_client(page: Any) -> dict[str, dict]:
    return hc._keyed(hc._items(page, "messages"), "client_id")


def _msg(page: Any) -> dict:
    return hc._obj(hc._obj(page).get("message"))


class Run:
    """The lifecycle sequence: one Drive, one clock, and the ids the cases share."""

    def __init__(self, hub: hc.Hub, clock: Any) -> None:
        self.d = hc.Drive(hub)
        self.clock = clock
        self.tok: dict[str, str] = {}
        self.ids: dict[str, Any] = {}
        self.envelopes: dict[str, str] = {}
        self.kids: dict[str, str] = {}
        self.tombstones: dict[str, str] = {}

    def at(self, offset: int) -> None:
        self.clock.set(offset)

    def msg(self, case: str, body: dict, token: str, *, status: int, shape: Any = None) -> dict:
        return self.d.call(case, "POST", "/messages", status=status, body=body, token=token, shape=shape)

    # -- setup at 0 -------------------------------------------------------------

    def setup(self) -> None:
        d, admin = self.d, self.d.hub.admin_token
        for node in (NODE_A, NODE_B):
            added = d.call(
                f"lifecycle nodes add {node}",
                "POST",
                "/nodes",
                status=200,
                body={"action": "add", "node_id": node},
                token=admin,
                shape={"added": Eq(True), "token": NonEmpty()},
            )
            self.tok[node] = str(added.get("token") or "contract-missing-token")
        enrolled = d.call(
            "lifecycle message principal add the question operator",
            "POST",
            "/message-principals",
            status=201,
            body={"action": "add", "name": OPERATOR.removeprefix("agent:")},
            token=admin,
            shape={"principal": {"principal_id": Eq(OPERATOR), "token": NonEmpty()}},
        )
        self.tok[OPERATOR] = str(hc._obj(enrolled.get("principal")).get("token") or "contract-missing-token")
        d.call(
            "lifecycle message principal grants for the question operator",
            "POST",
            "/message-principals",
            status=200,
            body={"action": "grants", "name": OPERATOR.removeprefix("agent:"), "may_send_to": ["node:*"]},
            token=admin,
            shape={"principal": {"may_send_to": Eq(["node:*"])}},
        )
        d.secrets.update(t for t in self.tok.values() if t != "contract-missing-token")
        for node in (NODE_A, NODE_B):
            record = sp.key_record(node, self.clock.mesh())
            self.kids[node] = record["kid"]
            self.msg(
                f"lifecycle keys_publish {node}",
                {"action": "keys_publish", "record": record},
                self.tok[node],
                status=201,
                shape={"key": {"kid": Eq(record["kid"])}},
            )
        a = self.tok[NODE_A]
        for cid, label in (("e1", "expiring"), ("m1", "parent"), ("w1", "window")):
            sent = self.msg(
                f"lifecycle send {label}",
                {"action": "send", "to": NODE_B, "subject": label, "body": label, "client_id": cid},
                a,
                status=201,
                shape={"message": {**hc.MESSAGE, "client_id": Eq(cid), "reply_to": NONE}},
            )
            self.ids[cid] = _msg(sent).get("id")
        for cid in ("p1", "s-old", "s-mid", "s-new"):
            text = hc.canonical(
                sp.envelope(
                    sender=NODE_A,
                    sender_kid=self.kids[NODE_A],
                    recipient=NODE_B,
                    recipient_kid=self.kids[NODE_B],
                    client_id=cid,
                    created_at=self.clock.mesh(),
                )
            )
            self.envelopes[cid] = text
            sent = self.msg(
                f"lifecycle sealed send {cid}",
                {"action": "send", "envelope": text},
                a,
                status=201,
                shape={"message": {**hc.SEALED_MESSAGE, "envelope": Eq(text), "content_state": Eq("sealed")}},
            )
            self.ids[cid] = _msg(sent).get("id")

    # -- questions, inside day 0 -----------------------------------------------

    def questions(self) -> None:
        a, op = self.tok[NODE_A], self.tok[OPERATOR]
        self.at(0)
        create = {
            "action": "question_create",
            "question": "Expire?",
            "timeout_secs": QUESTION_TIMEOUT,
            "request_id": "q1",
        }
        created = self.msg(
            "lifecycle questions create",
            create,
            a,
            status=201,
            shape={**hc.QUESTION, "status": Eq("pending"), "created": Eq(True)},
        )
        qid, asked = created.get("question_id"), created.get("message_id")
        self.ids["q1"] = qid
        status = {"action": "question_status", "question_id": qid}
        self.at(QUESTION_TIMEOUT - 1)
        self.msg(
            "lifecycle questions status one second before the deadline",
            status,
            a,
            status=200,
            shape={**hc.QUESTION, "status": Eq("pending"), "late_answer_message_id": NONE},
        )
        self.at(QUESTION_TIMEOUT)
        self.msg(
            "lifecycle questions status at the deadline",
            status,
            a,
            status=200,
            shape={**hc.QUESTION, "status": Eq("timeout"), "answer": NONE, "late_answer_message_id": NONE},
        )
        self.msg(
            "lifecycle questions desktop answer after the deadline",
            {"action": "question_desktop", "question_id": qid, "answer": "too late"},
            a,
            status=200,
            shape={**hc.QUESTION, "status": Eq("timeout"), "answer": NONE, "source": NONE},
        )
        # One second past the deadline: an answer at exactly the deadline, once a read has
        # timed the question out, matches neither branch of the hub's reply trigger.
        self.at(QUESTION_TIMEOUT + 1)
        late = self.msg(
            "lifecycle questions operator answer after the deadline",
            {"action": "reply", "id": int(asked) if isinstance(asked, str) and asked.isdigit() else 0, "body": "late"},
            op,
            status=201,
            shape={"message": {**hc.MESSAGE, "sender": Eq(OPERATOR), "recipient": Eq(NODE_A)}},
        )
        late_id = _msg(late).get("id")
        self.msg(
            "lifecycle questions status after a late answer",
            status,
            a,
            status=200,
            shape={
                **hc.QUESTION,
                "status": Eq("timeout"),
                "answer": NONE,
                "answer_id": NONE,
                "late_answer_message_id": Eq(str(late_id)) if isinstance(late_id, int) else hc.DIGITS,
                "late_answered_at": NonEmpty(),
            },
        )
        pending = {}
        for request_id in ("q2", "q3"):
            pending[request_id] = self.msg(
                f"lifecycle questions create {request_id} with q1 expired",
                {**create, "request_id": request_id, "question": f"Pending {request_id}?"},
                a,
                status=201,
                shape={**hc.QUESTION, "status": Eq("pending"), "created": Eq(True)},
            )
        self.msg(
            "lifecycle questions create over the pending limit",
            {**create, "request_id": "q4", "question": "Third pending?"},
            a,
            status=429,
            shape={"error": NonEmpty(), "code": Eq("question_station_busy")},
        )
        # q2's deadline is 121 s. Once a read times it out, an operator answer at exactly
        # the deadline matches neither branch of the reply trigger: not on time (the
        # question is no longer pending) and not late (it is not after the deadline).
        q2 = pending["q2"]
        status2 = {"action": "question_status", "question_id": q2.get("question_id")}
        self.at(QUESTION_TIMEOUT + 1 + QUESTION_TIMEOUT)
        self.msg(
            "lifecycle questions status at q2's deadline",
            status2,
            a,
            status=200,
            shape={**hc.QUESTION, "status": Eq("timeout")},
        )
        asked2 = q2.get("message_id")
        self.msg(
            "lifecycle questions operator answer at exactly the deadline",
            {
                "action": "reply",
                "id": int(asked2) if isinstance(asked2, str) and asked2.isdigit() else 0,
                "body": "now",
            },
            op,
            status=201,
            shape={"message": {**hc.MESSAGE, "sender": Eq(OPERATOR), "recipient": Eq(NODE_A)}},
        )
        self.msg(
            "lifecycle questions status after an answer at exactly the deadline",
            status2,
            a,
            status=200,
            shape={
                **hc.QUESTION,
                "status": Eq("timeout"),
                "answer": NONE,
                "answer_id": NONE,
                "late_answer_message_id": NONE,
                "late_answered_at": NONE,
            },
        )

    # -- acknowledgements and replies, days 1 to 3 -------------------------------

    def acks_and_replies(self) -> None:
        b = self.tok[NODE_B]
        self.at(1 * DAY)
        self.msg(
            "lifecycle ack sealed s-old",
            {"action": "ack", "id": self.ids["s-old"]},
            b,
            status=200,
            shape={"message": {**hc.SEALED_MESSAGE, "content_state": Eq("sealed"), "acked_at": NonEmpty()}},
        )
        self.at(2 * DAY)
        reply = self.msg(
            "lifecycle plaintext reply to the parent",
            {"action": "reply", "id": self.ids["m1"], "body": "plain reply", "client_id": "r1"},
            b,
            status=201,
            shape={"message": {**hc.MESSAGE, "reply_to": Eq(self.ids["m1"])}},
        )
        self.ids["r1"] = _msg(reply).get("id")
        text = hc.canonical(
            sp.envelope(
                sender=NODE_B,
                sender_kid=self.kids[NODE_B],
                recipient=NODE_A,
                recipient_kid=self.kids[NODE_A],
                client_id="p1-reply",
                created_at=self.clock.mesh(),
                in_reply_to={"hub": sp.HUB_ID, "from": NODE_A, "client_id": "p1"},
            )
        )
        self.envelopes["p1-reply"] = text
        sealed = self.msg(
            "lifecycle sealed reply to the sealed parent",
            {"action": "reply", "id": self.ids["p1"], "envelope": text},
            b,
            status=201,
            shape={"message": {**hc.SEALED_MESSAGE, "reply_to": Eq(self.ids["p1"]), "envelope": Eq(text)}},
        )
        self.ids["p1-reply"] = _msg(sealed).get("id")
        self.at(3 * DAY)
        self.msg(
            "lifecycle ack sealed s-mid",
            {"action": "ack", "id": self.ids["s-mid"]},
            b,
            status=200,
            shape={"message": {**hc.SEALED_MESSAGE, "content_state": Eq("sealed")}},
        )

    # -- sealed lifecycle, days 9 and 10 ------------------------------------------

    def _state(self, cid: str, expired: bool) -> dict:
        original = self.envelopes[cid]
        text = tombstone_of(original) if expired else original
        return {
            **hc.SEALED_MESSAGE,
            "client_id": Eq(cid),
            "content_state": Eq("content_expired" if expired else "sealed"),
            "envelope": Eq(text),
            "envelope_bytes": Eq(len(text)),
        }

    def sealed_lifecycle(self) -> None:
        a, b = self.tok[NODE_A], self.tok[NODE_B]
        self.at(9 * DAY)
        found = self.msg(
            TOMBSTONE_CASES["s-old"],
            {"action": "sent_lookup", "client_id": "s-old"},
            a,
            status=200,
            shape={"message": self._state("s-old", True)},
        )
        self.tombstones["s-old"] = str(_msg(found).get("envelope"))
        ids = [self.ids[cid] for cid in ("s-old", "s-mid", "s-new")]
        outbox = self.msg(
            "lifecycle sent by ids across the sealed lifecycle",
            {"action": "sent", "ids": ids},
            a,
            status=200,
            shape=hc.PAGE,
        )
        inbox = self.msg(
            "lifecycle list across the sealed lifecycle",
            {"action": "list", "after": 0, "unacked": False},
            b,
            status=200,
            shape=hc.PAGE,
        )
        for case, page in (
            ("lifecycle sent by ids across the sealed lifecycle", outbox),
            ("lifecycle list across the sealed lifecycle", inbox),
        ):
            got = _by_client(page)
            for cid, expired in (("s-old", True), ("s-mid", False), ("s-new", False)):
                problems = hc.match(self._state(cid, expired), got.get(cid, {}), f"$[{cid}]")
                self.d.invariant(
                    case,
                    not problems,
                    f"{cid} reads as {'its tombstone' if expired else 'sealed'} on day 9",
                    problems,
                )
        self.at(3 * DAY + SEALED_LIFETIME - 1)
        self.msg(
            "lifecycle sent_lookup one second before the 7-day lifetime ends",
            {"action": "sent_lookup", "client_id": "s-mid"},
            a,
            status=200,
            shape={"message": self._state("s-mid", False)},
        )
        self.at(3 * DAY + SEALED_LIFETIME)
        found = self.msg(
            TOMBSTONE_CASES["s-mid"],
            {"action": "sent_lookup", "client_id": "s-mid"},
            a,
            status=200,
            shape={"message": self._state("s-mid", True)},
        )
        self.tombstones["s-mid"] = str(_msg(found).get("envelope"))
        unacked = _by_client(
            self.msg(
                "lifecycle list keeps the unacknowledged sealed message sealed",
                {"action": "list", "after": 0, "unacked": True},
                b,
                status=200,
                shape=hc.PAGE,
            )
        )
        self.d.invariant(
            "lifecycle list keeps the unacknowledged sealed message sealed",
            not hc.match(self._state("s-new", False), unacked.get("s-new", {})),
            "an unacknowledged sealed message stays sealed on day 10",
            unacked.get("s-new", {}).get("content_state"),
        )

    # -- client_id window inside retention, day 89 ------------------------------

    def window_inside(self) -> None:
        a = self.tok[NODE_A]
        self.at(89 * DAY)
        body = {"action": "send", "to": NODE_B, "subject": "window", "body": "window", "client_id": "w1"}
        again = self.msg(
            "lifecycle send retry inside the client_id window",
            body,
            a,
            status=201,
            shape={"message": {**hc.MESSAGE, "client_id": Eq("w1")}},
        )
        self.d.invariant(
            "lifecycle send retry inside the client_id window",
            _msg(again).get("id") == self.ids["w1"],
            "a resend 89 days later returns the first id",
            [self.ids["w1"], _msg(again).get("id")],
        )
        self.msg(
            "lifecycle send conflict inside the client_id window",
            {**body, "body": "different"},
            a,
            status=409,
            shape={"error": NonEmpty(), "code": Eq("message_client_id_conflict"), "id": Eq(self.ids["w1"])},
        )

    # -- the 90-day retention boundary ------------------------------------------

    def retention_boundary(self) -> None:
        b = self.tok[NODE_B]
        self.at(RETENTION)
        self.msg(
            "lifecycle count at exactly 90 days",
            {"action": "count"},
            b,
            status=200,
            shape={"unread_count": Eq(5)},
        )
        self.at(RETENTION + 1)
        self.msg(
            "lifecycle count one second past 90 days",
            {"action": "count"},
            b,
            status=200,
            shape={"unread_count": Eq(0)},
        )

    # -- after expiry, day 91 -----------------------------------------------------

    def after_expiry(self) -> None:
        a, b = self.tok[NODE_A], self.tok[NODE_B]
        self.at(91 * DAY)
        self.msg(
            "lifecycle list after expiry",
            {"action": "list", "after": 0, "unacked": False},
            b,
            status=200,
            shape={"messages": Eq([]), "next_cursor": Eq(0)},
        )
        self.msg("lifecycle count after expiry", {"action": "count"}, b, status=200, shape={"unread_count": Eq(0)})
        self.msg(
            "lifecycle sent after expiry",
            {"action": "sent", "after": 0},
            a,
            status=200,
            shape={"messages": Eq([]), "next_cursor": Eq(0)},
        )
        self.msg(
            "lifecycle sent by ids after expiry",
            {"action": "sent", "ids": [self.ids["e1"], self.ids["p1"]]},
            a,
            status=200,
            shape={"messages": Eq([])},
        )
        for case, body, token, status in (
            ("lifecycle sent_lookup after expiry", {"action": "sent_lookup", "client_id": "e1"}, a, 404),
            ("lifecycle ack after expiry", {"action": "ack", "id": self.ids["e1"]}, b, 404),
            ("lifecycle reply to an expired message", {"action": "reply", "id": self.ids["m1"], "body": "x"}, b, 404),
            (
                "lifecycle questions status after retention",
                {"action": "question_status", "question_id": self.ids["q1"]},
                a,
                403,
            ),
        ):
            self.msg(case, body, token, status=status, shape=hc.error_body())
        replies = {
            "r1": {**hc.MESSAGE, "client_id": Eq("r1"), "reply_to": NONE},
            "p1-reply": {
                **hc.SEALED_MESSAGE,
                "client_id": Eq("p1-reply"),
                "reply_to": Eq(self.ids["p1"]),
                "content_state": Eq("sealed"),
                "envelope": Eq(self.envelopes["p1-reply"]),
            },
        }
        for case, body, token in (
            ("lifecycle list replies after their parents expire", {"action": "list", "after": 0, "unacked": False}, a),
            ("lifecycle sent replies after their parents expire", {"action": "sent", "after": 0}, b),
        ):
            page = self.msg(case, body, token, status=200, shape=hc.PAGE)
            got = _by_client(page)
            self.d.invariant(
                case,
                sorted(got) == sorted(replies),
                "only the two day-2 replies remain",
                sorted(got),
            )
            for cid, spec in replies.items():
                problems = hc.match(spec, got.get(cid, {}), f"$[{cid}]")
                self.d.invariant(
                    case,
                    not problems,
                    "a plaintext reply_to to a purged parent reads null; a sealed reply keeps its parent id",
                    problems,
                )
        body = {"action": "send", "to": NODE_B, "subject": "window", "body": "window", "client_id": "w1"}
        fresh = self.msg(
            "lifecycle send after the client_id window",
            body,
            a,
            status=201,
            shape={"message": {**hc.MESSAGE, "client_id": Eq("w1")}},
        )
        new_id = _msg(fresh).get("id")
        self.d.invariant(
            "lifecycle send after the client_id window",
            isinstance(new_id, int) and isinstance(self.ids["w1"], int) and new_id > self.ids["w1"],
            "once the first message expired, the same client_id makes a new message",
            [self.ids["w1"], new_id],
        )
        self.msg(
            "lifecycle sent_lookup after the client_id window",
            {"action": "sent_lookup", "client_id": "w1"},
            a,
            status=200,
            shape={"message": {"id": Eq(new_id) if isinstance(new_id, int) else int}},
        )
        now = self.clock.moment()
        edge, skew, second = timedelta(days=90), timedelta(minutes=5), timedelta(seconds=1)
        for case, stamp, status, cid in (
            ("lifecycle send enqueued past retention", now - edge - second, 400, "q-old"),
            ("lifecycle send enqueued too far in the future", now + skew + second, 400, "q-future"),
            ("lifecycle send enqueued exactly at the retention limit", now - edge, 201, "q-edge"),
            ("lifecycle send enqueued exactly at the future limit", now + skew, 201, "q-skew"),
        ):
            shape: Any = {"message": {**hc.MESSAGE, "client_id": Eq(cid)}} if status == 201 else hc.error_body()
            self.msg(
                case,
                {
                    "action": "send",
                    "to": NODE_B,
                    "subject": cid,
                    "body": cid,
                    "client_id": cid,
                    "enqueued_at": stamp.isoformat(),
                },
                a,
                status=status,
                shape=shape,
            )
        self.msg(
            "lifecycle count with a message at the retention limit",
            {"action": "count"},
            b,
            status=200,
            shape={"unread_count": Eq(3)},
        )
        self.at(91 * DAY + 1)
        self.msg(
            "lifecycle count one second later",
            {"action": "count"},
            b,
            status=200,
            shape={"unread_count": Eq(2)},
        )


def run(template: str, *, brigade: list[str], state_dir: Path, startup_timeout: float) -> Lifecycle:
    """Launch the clocked hub, drive the timeline, and always stop it."""
    hub, clock = launch_clocked(template, brigade=brigade, state_dir=state_dir, startup_timeout=startup_timeout)
    try:
        r = Run(hub, clock)
        r.setup()
        r.questions()
        r.acks_and_replies()
        r.sealed_lifecycle()
        r.window_inside()
        r.retention_boundary()
        r.after_expiry()
    finally:
        hc.stop_hub(hub)
    secrets_ = {s for s in r.d.secrets if len(s) >= 8}
    result = {
        "cases": r.d.cases,
        "mismatches": hc.scrub(r.d.mismatches, secrets_),
        "exchanges": hc.scrub(r.d.exchanges, secrets_),
        "covered_routes": sorted(r.d.routes),
        "covered_message_actions": sorted(r.d.actions),
    }
    return Lifecycle(hub=hub, result=result, tombstones=r.tombstones)


if __name__ == "__main__":  # pragma: no cover - the entry point is control-brigade.py hub-contract
    raise SystemExit("run this through control-brigade.py hub-contract")
