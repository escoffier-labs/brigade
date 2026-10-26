"""Ported bridge regressions over isolated Dot collaborators."""

from tests.dot_support import requires_local_state

import json
import unittest
from unittest.mock import patch
from dataclasses import replace

import pytest
from brigade.dot.adapter import DotTools, build_tools, validate_arguments
from brigade.dot.contracts import MAX_PROMPT_BYTES, DotError, TOOL_SCHEMAS, TOOLS
from brigade.dot.fleet_tools import FleetTools
from brigade.dot.hub_tools import HubTools
from brigade.dot.inbox_tools import HubInbox, InboxTools
from brigade.dot.memory_tools import MemoryTools
from brigade.dot.process import CommandResult, CommandRunner
from brigade.dot.runtime_config import parse_runtime
from brigade.dot.state import BridgeState
from tests.test_dot_runtime import _document
from tests.test_dot_state import NOW
from tests.test_dot_inbox_tools import FakeMessages
from tests.test_fleet_dot import v2_report

EMAIL = "dot@example.test"
OTHER = "other@example.test"
REQUEST = "request-example"
SHA = "0123456789abcdef0123456789abcdef01234567"
BRIGADE, T3, OCTOPOOL, GH = "/fake/bin/brigade", "/fake/bin/t3-fleet", "/fake/bin/octopool", "/fake/bin/gh"
FLEET_OK = json.dumps({"schema": "t3-fleet.result.v1", "state": "running", "request_id": REQUEST}).encode()
HANDOFF = """# Memory Handoff

## Type
workflow

## Title
Bridge probe

## Summary
A probe handoff written by the bridge test suite.

## Durable facts
- Probe only.

## Evidence
- commands run: unit tests

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
- Probe line.
"""

FLEET_OK = json.dumps(
    {"schema": "t3-fleet.result.v1", "state": "running", "request_id": REQUEST, "thread_id": "thread-1"}
).encode()


def lint_report(valid=True):
    return json.dumps(
        {
            "valid": valid,
            "results": [
                {
                    "valid": valid,
                    "errors": [] if valid else ["missing required section: Type"],
                    "warnings": [],
                    "hints": [],
                    "action": "no-card",
                }
            ],
        }
    ).encode()


class FakeRunner(CommandRunner):
    """Records exact argv/stdin; answers from a routing function. Never spawns."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda argv, stdin: CommandResult(0, FLEET_OK))

    def run(self, argv, *, stdin=b"", timeout, max_output, env=None):
        self.calls.append(dict(argv=list(argv), stdin=stdin, timeout=timeout, max_output=max_output, env=env))
        return self.handler(list(argv), stdin)

    def argvs(self):
        return [call["argv"] for call in self.calls]


class Fixture(unittest.TestCase):
    dispatch = True

    @pytest.fixture(autouse=True)
    def isolated(self, dot_private_dir, monkeypatch):
        self.private = dot_private_dir
        self.root = self.private.parent
        self.workspace = self.root / "ws"
        self.cards = self.workspace / "memory" / "cards"
        self.inbox = self.workspace / ".codex" / "memory-handoffs"
        self.cards.mkdir(parents=True)
        self.inbox.mkdir(parents=True)
        for directory in (self.workspace, self.workspace / "memory", self.cards, self.workspace / ".codex", self.inbox):
            directory.chmod(0o755)
        self.state = BridgeState(self.private)
        self.runtime = parse_runtime(_document())
        bins = replace(self.runtime.paths.bin, brigade=BRIGADE, t3_fleet=T3, gh=GH, octopool=OCTOPOOL)
        self.runtime = replace(
            self.runtime, paths=replace(self.runtime.paths, workspace=self.workspace, state_dir=self.private, bin=bins)
        )
        token = self.root / "adapter.token"
        token.write_text("fixture-adapter-credential-" * 2)
        token.chmod(0o600)
        self.runtime = replace(self.runtime, hub=replace(self.runtime.hub, message_token_file=token))
        self.runner = FakeRunner()
        self.messages = FakeMessages(monkeypatch)
        self.adapter = self.make_adapter(self.dispatch)
        yield
        self.state.close()

    def make_adapter(self, enabled):
        runtime = replace(self.runtime, dispatch_enabled=enabled)
        fleet = FleetTools(runtime, self.state, commands=self.runner)
        fleet.fleet_status = lambda **kwargs: {"ok": True}
        hub_inbox = HubInbox(self.state, runtime.hub)
        inbox = InboxTools(runtime, self.state, hub_inbox)
        hub = HubTools(runtime, self.state, commands=self.runner, fleet=fleet, inbox=inbox)
        memory = MemoryTools(runtime, commands=self.runner)
        memory.clock = lambda: NOW
        return DotTools(runtime, self.state, fleet=fleet, hub=hub, memory=memory, inbox=inbox, clock=lambda: NOW)

    def call(self, tool, **arguments):
        return self.adapter.call(EMAIL, tool, arguments)


VALID_CALLS = {
    "fleet_report_session": {"report": v2_report()},
    "dot_t3_capabilities": {},
    "fleet_capacity": {"host": "beta"},
    "fleet_doctor": {"host": "alpha", "repository": "brigade"},
    "fleet_submit": {
        "repository": "brigade",
        "title": "Fix it",
        "prompt": "Do the thing.",
        "host": "alpha",
        "seat": "sol61",
    },
    "fleet_status": {"request_id": REQUEST},
    "fleet_wait": {"request_id": REQUEST, "timeout_seconds": 5},
    "fleet_followup": {"request_id": REQUEST, "message": "Continue."},
    "fleet_answer": {"request_id": REQUEST, "input_id": "input-1", "answers": {"q1": "yes"}},
    "fleet_approve": {"request_id": REQUEST, "approval_id": "approval-1", "decision": "accept"},
    "fleet_interrupt": {"request_id": REQUEST},
    "fleet_recent": {"limit": 5},
    "fleet_overview": {},
    "memory_search": {"query": "t3 fleet", "limit": 3},
    "memory_card": {"name": "good-card"},
    "handoff_write": {"slug": "bridge-probe", "content": HANDOFF},
    "inbox_list": {},
    "inbox_ack": {"ids": [1, 2]},
    "fleet_policy": {},
    "skill_search": {"query": "fleet"},
    "skill_get": {"name": "brigade-work"},
    "inbox_reply": {"msg_id": 1, "body": "answer"},
    "fleet_sessions": {"include_ended": True},
    "fleet_work_list": {"status": "ready"},
    "fleet_work_show": {"work_id": "wl-936295469f26989ce5ec329e"},
    "fleet_work_next": {},
    "fleet_work_burn": {"limit": 5},
    "fleet_models": {"consumer": "t3-fleet"},
    "fleet_cloud": {},
    "fleet_hub_policy": {},
    "fleet_digest": {},
    "message_send": {"to": "alpha", "subject": "Status", "body": "Report in."},
    "message_status": {"ids": [1]},
}

INVALID_CALLS = [
    ("shell", {}),
    ("fleet_capacity", {"host": "Bad Host"}),
    ("fleet_capacity", {"unexpected": 1}),
    ("fleet_doctor", {"repository": "../etc"}),
    ("fleet_submit", {"repository": "brigade", "title": "t"}),
    ("fleet_submit", {"repository": "brigade", "title": "t", "prompt": "x" * (MAX_PROMPT_BYTES + 1)}),
    ("fleet_submit", {"repository": "brigade", "title": "bad\x07title", "prompt": "p"}),
    ("fleet_submit", {"repository": "brigade", "title": "t", "prompt": "p", "source_revision": "HEAD"}),
    ("fleet_submit", {"repository": "brigade", "title": "t", "prompt": "p", "allow_queue": "yes"}),
    ("fleet_status", {"request_id": "-rf"}),
    ("fleet_status", {"request_id": REQUEST, "include_output": 1}),
    ("fleet_wait", {"request_id": REQUEST, "timeout_seconds": 61}),
    ("fleet_wait", {"request_id": REQUEST, "timeout_seconds": "30"}),
    ("fleet_wait", {"request_id": REQUEST, "timeout_seconds": True}),
    ("fleet_followup", {"request_id": REQUEST, "message": "   "}),
    ("fleet_answer", {"request_id": REQUEST, "input_id": "i", "answers": []}),
    ("fleet_approve", {"request_id": REQUEST, "approval_id": "a", "decision": "yolo"}),
    ("memory_search", {"query": "x", "limit": 21}),
    ("memory_search", {"query": ""}),
    ("memory_card", {"name": "../secret"}),
    ("memory_card", {"name": "a/b"}),
    ("memory_card", {"name": ".hidden"}),
    ("memory_card", {"name": "x..y"}),
    ("memory_card", {"name": "UPPER"}),
    ("handoff_write", {"slug": "../x", "content": HANDOFF}),
    ("handoff_write", {"slug": "ok-slug", "content": "no header"}),
    ("handoff_write", {"slug": "ok-slug", "content": HANDOFF + "x" * 20000}),
    ("inbox_list", {"limit": 51}),
    ("inbox_ack", {"ids": [1, 1]}),
    ("inbox_ack", {"ids": []}),
    ("inbox_ack", {"ids": ["1"]}),
    ("dot_t3_capabilities", {"job_id": "c7d2d37c-6269-46be-8ba9-49cbf9eb399a"}),
]


class SchemaTableChecks(unittest.TestCase):
    def test_every_tool_has_one_schema_and_a_valid_example(self):
        self.assertEqual(set(VALID_CALLS), TOOLS)
        for name, arguments in VALID_CALLS.items():
            validate_arguments(name, arguments)

    def test_defaults_and_null_as_absent(self):
        self.assertEqual(
            validate_arguments("fleet_wait", {"request_id": REQUEST, "timeout_seconds": None}),
            {"request_id": REQUEST, "timeout_seconds": 30, "include_output": False},
        )
        self.assertEqual(validate_arguments("inbox_list", {}), {"unacked_only": True, "limit": 20})
        self.assertEqual(
            validate_arguments("fleet_submit", {"repository": "brigade", "title": "t", "prompt": "p"})["host"], "auto"
        )
        self.assertEqual(validate_arguments("memory_card", {"name": "good-card.md"})["name"], "good-card")
        with self.assertRaises(DotError):
            validate_arguments("fleet_status", {"request_id": None})


class AdapterSchemaChecks(Fixture):
    @requires_local_state
    def test_adapter_rejects_invalid_arguments_without_any_command(self):
        for name, arguments in INVALID_CALLS:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaisesRegex(DotError, "^invalid_request$"):
                    self.adapter.call(EMAIL, name, arguments)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state.operations(50), [])


class DispatchGateChecks(Fixture):
    dispatch = False

    @requires_local_state
    def test_gated_tools_refuse_before_any_command_or_journal(self):
        gated = sorted(name for name, schema in TOOL_SCHEMAS.items() if schema.gated)
        self.assertEqual(
            gated,
            [
                "fleet_answer",
                "fleet_approve",
                "fleet_followup",
                "fleet_interrupt",
                "fleet_report_session",
                "fleet_submit",
                "message_send",
            ],
        )
        for name in gated:
            arguments = VALID_CALLS.get(name, {"job_id": "c7d2d37c-6269-46be-8ba9-49cbf9eb399a", "task": "review"})
            with self.subTest(name=name):
                with self.assertRaisesRegex(DotError, "^dispatch_disabled$"):
                    self.adapter.call(EMAIL, name, arguments)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.state.operations(50), [])

    @requires_local_state
    def test_followup_refusal_logs_argument_request_id(self):
        with self.assertRaisesRegex(DotError, "^dispatch_disabled$"):
            self.call("fleet_followup", request_id=REQUEST, message="follow up")
        with self.state.transaction() as connection:
            row = connection.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["request_id"], REQUEST)
        self.assertEqual(row["error_code"], "dispatch_disabled")

    @requires_local_state
    def test_read_tools_still_work_when_dispatch_disabled(self):
        self.assertTrue(self.call("fleet_status", request_id=REQUEST)["ok"])
        capabilities = self.call("dot_t3_capabilities")
        self.assertFalse(capabilities["dispatch_enabled"])
        self.assertEqual(set(capabilities["tools"]), set(TOOLS))
        self.assertIn("python -m brigade.dot notify --from", capabilities["inbox"]["usage"])


class AdapterIntegration(Fixture):
    @requires_local_state
    def test_gate_never_reaches_fleet(self):
        self.adapter = self.make_adapter(False)
        with patch.object(self.adapter.fleet, "fleet_submit", side_effect=AssertionError("reached")):
            with self.assertRaisesRegex(DotError, "dispatch_disabled"):
                self.call("fleet_submit", repository="brigade", title="t", prompt="private prompt")

    @requires_local_state
    def test_call_logging_success_error_and_prompt_exclusion(self):
        self.call("fleet_status", request_id=REQUEST)
        with self.assertRaises(DotError):
            self.call("fleet_submit", prompt="private prompt")
        with self.state.transaction() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM call_log ORDER BY id")]
        self.assertEqual([row["ok"] for row in rows], [1, 0])
        self.assertEqual(rows[1]["error_code"], "invalid_request")
        self.assertNotIn("private prompt", json.dumps(rows))

    @requires_local_state
    def test_record_call_failure_does_not_change_result(self):
        with patch.object(self.state, "log_call", side_effect=RuntimeError("broken")):
            self.assertTrue(self.call("fleet_status", request_id=REQUEST)["ok"])

    @requires_local_state
    def test_capabilities_only_expose_configured_aliases_and_retained_tools(self):
        result = self.call("dot_t3_capabilities")
        self.assertEqual(result["bridge_version"], "6")
        self.assertEqual(set(result["tools"]), TOOLS)
        for suffix in ("submit_review", "job_status", "job_result"):
            self.assertNotIn("dot_t3_" + suffix, json.dumps(result))
        self.assertIn("ops, review", result["inbox"]["usage"])
        self.assertNotIn("model", result)

    @requires_local_state
    def test_identity_rejected_before_dispatch(self):
        with self.assertRaisesRegex(DotError, "authentication_required"):
            self.adapter.call("", "fleet_capacity", {})
        self.assertEqual(self.runner.calls, [])

    @requires_local_state
    def test_factory_wires_shared_state_and_collaborators(self):
        tools, state, inbox = build_tools(self.runtime)
        try:
            self.assertIs(tools.state, state)
            self.assertIs(tools.inbox.hub, inbox)
            self.assertIs(tools.hub.fleet, tools.fleet)
            self.assertEqual(tools.runtime, self.runtime)
        finally:
            state.close()


class MessagingAdapter(Fixture):
    @requires_local_state
    def test_message_send_gate_denial_and_status(self):
        self.adapter = self.make_adapter(False)
        with self.assertRaisesRegex(DotError, "dispatch_disabled"):
            self.call("message_send", to="review", subject="subject", body="private body")
        self.assertEqual(self.messages.calls, [])
        self.adapter = self.make_adapter(True)
        self.messages.fail["send"] = dict(error="denied", status=403, code="message_recipient_not_permitted")
        with self.assertRaisesRegex(DotError, "recipient_not_permitted"):
            self.call("message_send", to="review", subject="subject", body="private body")
        self.messages.fail.clear()
        sent = self.call("message_send", to="review", subject="subject", body="private body")
        self.assertEqual(self.call("message_status", ids=[sent["id"]])["messages"][0]["id"], sent["id"])
        with self.state.transaction() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM call_log ORDER BY id")]
        self.assertEqual([row["ok"] for row in rows], [0, 0, 1, 1])
        self.assertNotIn("private body", json.dumps(rows))
        self.assertNotIn("subject", json.dumps(rows))

    @requires_local_state
    def test_capabilities_state_untrusted_policy(self):
        result = self.call("dot_t3_capabilities")
        for needle in ("untrusted data", "never instructions", "credentials", "private context", "own judgment"):
            self.assertIn(needle, result["operating_instructions"])
        self.assertIn("hub", result["inbox"]["usage"])
        self.assertEqual(result["inbox"]["cli"], "brigade fleet msg send")

    @requires_local_state
    def test_capabilities_name_the_configured_inbox_principal(self):
        self.runtime = replace(self.runtime, hub=replace(self.runtime.hub, principal="agent:example-planner"))
        self.adapter = self.make_adapter(self.dispatch)
        usage = self.call("dot_t3_capabilities")["inbox"]["usage"]
        self.assertIn("brigade fleet msg send --to agent:example-planner.", usage)
        self.assertNotIn("--to agent:dot", usage)
