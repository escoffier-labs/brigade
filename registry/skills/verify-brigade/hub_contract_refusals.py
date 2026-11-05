"""Fleet Hub wire contract: the refusal sweep (v2 slice S1b).

Request-validation refusals on the routes the contract covers, beyond the ones
the core and hot-set cases already send. Each case pins only its status here;
the exact `error`, `code` and `reason` text of every refusal the drive sees is
checked against `hub-contract-golden.json`, which `hub-contract --write-golden`
captures from the in-repo Python hub (`hub_contract_golden.py`). Nothing in
this module, or in the golden, is derived from the hub's constants.

None of these requests changes hub state, so the sweep can run anywhere in the
drive. Refusals that need a full quota (1,000 unread messages, 1,000 sends a
day), a non-default flag (`--require-sealed`, storage limits), or a race
(a credential rotated mid-request) are not swept; `features/hub-contract.md`
lists them.
"""

from __future__ import annotations

import secrets
from typing import Any

import hub_contract as hc
from hub_contract import Ctx

# Most refusals carry a string; a claim target over the byte limit carries an object.
REFUSAL = {"error": (str, dict)}


def _send(x: Ctx, **fields: Any) -> dict:
    body: dict[str, Any] = {"action": "send", "to": x.node_b, "subject": "refusal", "body": "refusal"}
    body.update(fields)
    return {key: value for key, value in body.items() if value is not None}


def message_refusals(x: Ctx) -> list[tuple[str, dict, str, int]]:
    """(case, body, token, status) for POST /messages."""
    hub_id = "h_" + hc.b64(secrets.token_bytes(16))
    bare = hc.sealed_envelope(
        hub_id=hub_id,
        sender=x.node_a,
        sender_kid=x.kid_a,
        recipient=x.node_b,
        recipient_kid=x.kid_b,
        client_id=f"r-bare-{x.tag}",
    )
    bare.pop("ct")
    bare.pop("recipients")
    wrong_rcpt = hc.sealed_envelope(
        hub_id=hub_id,
        sender=x.node_a,
        sender_kid=x.kid_a,
        recipient=x.node_b,
        recipient_kid="k1_" + hc.b64(secrets.token_bytes(16)),
        client_id=f"r-rcpt-{x.tag}",
    )
    record = hc.key_record(x.node_a, hub_id)
    a, p = x.a, x.principal_token
    return [
        ("refusal count with an argument", {"action": "count", "after": 0}, a, 400),
        ("refusal send without a recipient", _send(x, to=None), a, 400),
        ("refusal send with an empty body", _send(x, body=""), a, 400),
        ("refusal send with a long subject", _send(x, subject="s" * 201), a, 400),
        ("refusal send with a long ref", _send(x, ref="r" * 257), a, 400),
        ("refusal send with a long client_id", _send(x, client_id="c" * 65), a, 400),
        ("refusal send with a bad priority", _send(x, priority="loud"), a, 400),
        ("refusal send with a bad kind", _send(x, kind="memo"), a, 400),
        ("refusal send with the reserved kind", _send(x, kind="operator_question"), a, 400),
        ("refusal send with a bad thread", _send(x, thread="bad thread!"), a, 400),
        ("refusal send with a bad sender_label", _send(x, sender_label="-bad"), a, 400),
        ("refusal send with a sender_label from a message principal", _send(x, sender_label="ok"), p, 400),
        ("refusal send with enqueued_at that is not a timestamp", _send(x, enqueued_at="yesterday"), a, 400),
        ("refusal send with enqueued_at without an offset", _send(x, enqueued_at="2026-01-01T00:00:00"), a, 400),
        (
            "refusal send with enqueued_at outside the timestamp range",
            _send(x, enqueued_at="0001-01-01T00:00:00+01:00"),
            a,
            400,
        ),
        ("refusal send with expired enqueued_at", _send(x, enqueued_at="2000-01-01T00:00:00+00:00"), a, 400),
        ("refusal send with future enqueued_at", _send(x, enqueued_at="2999-01-01T00:00:00+00:00"), a, 400),
        ("refusal send with a reply_to that is not an id", _send(x, reply_to="one"), a, 400),
        ("refusal list with a non-boolean unacked", {"action": "list", "after": 0, "unacked": "yes"}, x.b, 400),
        ("refusal sent with a negative cursor", {"action": "sent", "after": -1}, a, 400),
        ("refusal sent with no ids", {"action": "sent", "ids": []}, a, 400),
        ("refusal ack with an id that is not a number", {"action": "ack", "id": "one"}, x.b, 400),
        (
            "refusal sealed reply with an id that is not a number",
            {"action": "reply", "id": "one", "envelope": "{}"},
            a,
            400,
        ),
        ("refusal sent_lookup with an empty client_id", {"action": "sent_lookup", "client_id": ""}, a, 400),
        ("refusal keys_publish without a record", {"action": "keys_publish"}, a, 400),
        ("refusal keys_publish with a record that is not an object", {"action": "keys_publish", "record": "k"}, a, 400),
        (
            "refusal keys_publish with an unsupported suite",
            {"action": "keys_publish", "record": {**record, "suites": ["bm2"]}},
            a,
            400,
        ),
        (
            "refusal keys_publish with a bad hub id",
            {"action": "keys_publish", "record": {**record, "hub": "hub-1"}},
            a,
            400,
        ),
        (
            "refusal keys_publish with a bad binary field",
            {"action": "keys_publish", "record": {**record, "self_sig": "short"}},
            a,
            400,
        ),
        (
            "refusal keys_publish with a bad timestamp",
            {"action": "keys_publish", "record": {**record, "created_at": "yesterday"}},
            a,
            400,
        ),
        (
            "refusal keys_publish with a bad previous key id",
            {"action": "keys_publish", "record": {**record, "prev_kid": "kid-1"}},
            a,
            400,
        ),
        (
            "refusal keys_publish with a bad transport",
            {"action": "keys_publish", "record": {**record, "transport": {"iroh": "x"}}},
            a,
            400,
        ),
        ("refusal sealed send without ct and recipients", {"action": "send", "envelope": hc.canonical(bare)}, a, 400),
        (
            "refusal sealed send to a stale recipient key",
            {"action": "send", "envelope": hc.canonical(wrong_rcpt)},
            a,
            409,
        ),
        (
            "refusal questions create without a request_id",
            {"action": "question_create", "question": "Why?", "timeout_secs": 30},
            a,
            400,
        ),
        (
            "refusal questions create with an empty question",
            {"action": "question_create", "question": "", "timeout_secs": 30, "request_id": f"rq-{x.tag}"},
            a,
            400,
        ),
        (
            "refusal questions desktop with an empty answer",
            {"action": "question_desktop", "question_id": "0" * 32, "answer": ""},
            a,
            400,
        ),
        (
            "refusal questions status with an extra field",
            {"action": "question_status", "question_id": "0" * 32, "answer": "x"},
            a,
            400,
        ),
    ]


def principal_refusals(x: Ctx) -> list[tuple[str, dict, int]]:
    """(case, body, status) for POST /message-principals, all with the admin token."""
    name = f"contract-agent-{x.tag}"
    return [
        ("refusal principal add with an unknown field", {"action": "add", "name": f"r1-{x.tag}", "color": "red"}, 400),
        ("refusal principal add with a bad name", {"action": "add", "name": "Bad Name"}, 400),
        ("refusal principal add an existing name", {"action": "add", "name": name}, 400),
        (
            "refusal principal add with a request_id and no token",
            {"action": "add", "name": f"r2-{x.tag}", "request_id": "r" * 24},
            400,
        ),
        (
            "refusal principal add with a bad expected_enrollment_id",
            {"action": "add", "name": f"r3-{x.tag}", "expected_enrollment_id": "?"},
            400,
        ),
        ("refusal principal get without a name", {"action": "get"}, 400),
        ("refusal principal revoke without a generation", {"action": "revoke", "name": name}, 400),
        ("refusal principal bad action", {"action": "rename", "name": name}, 400),
    ]


def other_refusals(x: Ctx) -> list[tuple[str, str, Any, str, int]]:
    """(case, path, body, token, status) for /nodes, /sessions, /claims and /events."""
    session = {
        "action": "upsert",
        "harness": "codex",
        "session_id": f"refusal-{x.tag}",
        "repo_identity": "example-org/example-repo",
        "identity_scope": "fleet",
        "repo_label": "example-repo",
        "checkout_path": "/srv/example/repo",
        "branch": "main",
        "ttl_seconds": 60,
    }
    return [
        ("refusal nodes add without a node_id", "/nodes", {"action": "add"}, x.admin, 400),
        ("refusal nodes add with a bad node_id", "/nodes", {"action": "add", "node_id": "bad node"}, x.admin, 400),
        ("refusal nodes revoke without a node_id", "/nodes", {"action": "revoke"}, x.admin, 400),
        ("refusal sessions body not an object", "/sessions", ["upsert"], x.a, 400),
        ("refusal sessions with an unknown field", "/sessions", {**session, "color": "red"}, x.a, 400),
        ("refusal sessions without a harness", "/sessions", {**session, "harness": ""}, x.a, 400),
        ("refusal sessions with a bad identity_scope", "/sessions", {**session, "identity_scope": "world"}, x.a, 400),
        ("refusal sessions with dirty_paths not a list", "/sessions", {**session, "dirty_paths": "a.py"}, x.a, 400),
        (
            "refusal sessions with a non-boolean dirty_truncated",
            "/sessions",
            {**session, "dirty_paths": [], "dirty_truncated": 1},
            x.a,
            400,
        ),
        ("refusal claims without a target", "/claims", {"action": "acquire", "node_id": x.node_a}, x.a, 400),
        (
            "refusal claims with a target over the byte limit",
            "/claims",
            {"action": "acquire", "node_id": x.node_a, "target": "repo:" + "x" * 4096, "holder": "h1"},
            x.a,
            409,
        ),
        ("refusal claims body not an object", "/claims", ["acquire"], x.a, 400),
        ("refusal events object without a node_id", "/events", {"run_id": "r1"}, x.a, 400),
        ("refusal events entry not an object", "/events", ["r1"], x.a, 400),
    ]


def drive_refusals(d: hc.Drive, x: Ctx) -> None:
    for case, body, token, status in message_refusals(x):
        d.call(case, "POST", "/messages", status=status, body=body, token=token, shape=REFUSAL)
    for case, body, status in principal_refusals(x):
        d.call(case, "POST", "/message-principals", status=status, body=body, token=x.admin, shape=REFUSAL)
    for case, path, body, token, status in other_refusals(x):
        d.call(case, "POST", path, status=status, body=body, token=token, shape=REFUSAL)
