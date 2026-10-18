"""Bridge behavior ported to isolated Miller tools."""

from dataclasses import replace
import json
import time
from unittest.mock import patch

from brigade.miller import hub_tools as hub_module
from brigade.miller.contracts import MillerError, TOOL_SCHEMAS
from brigade.miller.process import CommandResult, child_path
from brigade.miller.state import BridgeState
from tests.test_miller_adapter import BRIGADE, T3, OCTOPOOL, GH, FLEET_OK, NOW, EMAIL, OTHER, Fixture


class OverviewChecks(Fixture):
    def test_overview_runs_two_bounded_commands_and_summarizes(self):
        runs = [
            {
                "run_id": f"r{i}",
                "state": "run.completed" if i % 2 else "run.failed",
                "received_at": f"2026-10-0{1 + i % 9}T00:00:00Z",
                "repo": "brigade",
                "extra": "x" * 50,
            }
            for i in range(40)
        ]
        claims = [{"target": "example/brigade#1", "harness": "codex", "holder": "never-listed", "expired": False}]

        def handler(argv, stdin):
            if argv[1:3] == ["fleet", "status"]:
                return CommandResult(0, json.dumps({"preference": {"impl": "sol61"}, "runs": runs}).encode())
            return CommandResult(0, json.dumps({"claims": claims}).encode())

        self.runner.handler = handler
        result = self.call("fleet_overview")
        self.assertEqual(
            self.runner.argvs(), [[BRIGADE, "fleet", "status", "--json"], [BRIGADE, "fleet", "claims", "--json"]]
        )
        self.assertEqual(result["status"]["run_count"], 40)
        self.assertEqual(result["status"]["by_state"], {"run.completed": 20, "run.failed": 20})
        self.assertEqual(len(result["status"]["latest_runs"]), 25)
        self.assertNotIn("extra", result["status"]["latest_runs"][0])
        self.assertEqual(result["status"]["routing"], {"impl": "sol61"})
        self.assertEqual(result["claims"]["count"], 1)
        self.assertNotIn("holder", result["claims"]["claims"][0])
        self.runner.calls.clear()
        self.call("fleet_overview", include_terminal=True)
        self.assertEqual(self.runner.argvs()[0], [BRIGADE, "fleet", "status", "--json", "--all"])
        self.assertEqual(self.runner.calls[0]["max_output"], hub_module.MAX_STATUS_ALL_CAPTURE)

    def test_overview_partial_failure(self):
        def handler(argv, stdin):
            if argv[1:3] == ["fleet", "status"]:
                return CommandResult(1, b"hub offline")
            return CommandResult(0, b'{"claims": []}')

        self.runner.handler = handler
        result = self.call("fleet_overview")
        self.assertEqual(result["status"], {"error": "brigade_unavailable"})
        self.assertEqual(result["claims"], {"count": 0, "claims": []})


V4_TOOLS = (
    "fleet_sessions",
    "fleet_work_list",
    "fleet_work_show",
    "fleet_work_next",
    "fleet_work_burn",
    "fleet_models",
    "fleet_cloud",
    "fleet_hub_policy",
)
# Hub subcommands that write; none may ever appear in a v4 argv.
WRITE_VERBS = {
    "create",
    "patch",
    "set",
    "admit",
    "retire",
    "default",
    "save",
    "preview",
    "rollback",
    "apply",
    "route",
    "reservation",
    "observe",
    "quota",
    "session",
    "delegation",
    "release",
    "flush",
    "export",
    "serve",
    "nodes",
    "preference",
}

WORK_ITEM = {
    "work_id": "wl-936295469f26989ce5ec329e",
    "title": "Fix it",
    "status": "ready",
    "kind": "repo",
    "priority": "normal",
    "burn_eligible": True,
    "burn_rank": 10,
    "description": "long text",
    "links": [{"display_ref": "example/brigade#1", "link_id": "lnk-1"}],
}


def reply(value, code=0):
    return lambda argv, stdin: CommandResult(code, json.dumps(value).encode())


class V4Schema(Fixture):
    def test_all_v4_tools_are_read_only_and_ungated(self):
        for name in V4_TOOLS:
            self.assertTrue(TOOL_SCHEMAS[name].read_only, name)
            self.assertFalse(TOOL_SCHEMAS[name].gated, name)

    def test_invalid_arguments_never_spawn(self):
        cases = [
            ("fleet_sessions", {"limit": 101}),
            ("fleet_sessions", {"include_ended": "yes"}),
            ("fleet_work_list", {"source": "gitlab"}),
            ("fleet_work_list", {"status": "--all"}),
            ("fleet_work_list", {"limit": 51}),
            ("fleet_work_show", {}),
            ("fleet_work_show", {"work_id": "--json"}),
            ("fleet_work_show", {"work_id": "wl-../x"}),
            ("fleet_work_show", {"work_id": "wl-ABCDEF0123"}),
            ("fleet_work_next", {"consumer": "anything"}),
            ("fleet_work_next", {"workload": "-x"}),
            ("fleet_work_next", {"reserve": True}),
            ("fleet_work_burn", {"cursor": "--limit=100"}),
            ("fleet_work_burn", {"limit": 0}),
            ("fleet_models", {"consumer": "admin"}),
            ("fleet_cloud", {"limit": 1000}),
            ("fleet_hub_policy", {"apply": True}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaisesRegex(MillerError, "^invalid_request$"):
                    self.adapter.call(EMAIL, name, arguments)
        self.assertEqual(self.runner.calls, [])

    def test_works_with_dispatch_disabled(self):
        adapter = self.make_adapter(False)
        self.runner.handler = reply({"consumer": "brigade-run", "roster_revision": 1, "seats": []})
        self.assertEqual(adapter.call(EMAIL, "fleet_models", {})["roster_revision"], 1)


class V4Behavior(Fixture):
    def test_sessions_argv_sorting_projection_and_bounds(self):
        sessions = [
            {
                "session_id": f"s{i}",
                "node_id": "alpha" if i % 2 else "beta",
                "harness": "codex",
                "state": "active",
                "heartbeat_at": f"2026-10-10T00:{i:02d}:00Z",
                "dirty_paths": ["a"] * i,
                "dirty_truncated": False,
            }
            for i in range(60)
        ]
        self.runner.handler = reply(sessions)
        result = self.call("fleet_sessions")
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "sessions", "--json"]])
        self.assertEqual(result["session_count"], 60)
        self.assertEqual(len(result["sessions"]), 50)
        self.assertEqual(result["sessions"][0]["session_id"], "s59")
        self.assertEqual(result["sessions"][0]["dirty_count"], 59)
        self.assertNotIn("dirty_paths", result["sessions"][0])
        self.assertEqual(result["by_node"], {"alpha": 30, "beta": 30})
        self.runner.calls.clear()
        self.call("fleet_sessions", include_ended=True, limit=3)
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "sessions", "--all", "--json"]])
        self.assertEqual(self.runner.calls[0]["max_output"], hub_module.MAX_FLEET_CAPTURE)

    def test_work_list_filters_in_process(self):
        items = [dict(WORK_ITEM, work_id=f"wl-{i:024x}", status="ready" if i < 3 else "captured") for i in range(10)]
        self.runner.handler = reply({"items": items, "next_cursor": "abc"})
        result = self.call("fleet_work_list", source="github", status="ready", limit=2)
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "work", "list", "--source", "github", "--json"]])
        self.assertEqual((result["page_count"], result["match_count"], len(result["items"])), (10, 3, 2))
        self.assertEqual(result["by_status"], {"captured": 7, "ready": 3})
        self.assertTrue(result["more_on_hub"])
        self.assertEqual(result["items"][0]["links"], ["example/brigade#1"])
        self.assertNotIn("description", result["items"][0])

    def test_work_show_and_hub_error_envelope(self):
        self.runner.handler = reply({"item": WORK_ITEM})
        result = self.call("fleet_work_show", work_id=WORK_ITEM["work_id"])
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "work", "show", WORK_ITEM["work_id"], "--json"]])
        self.assertEqual(result["item"]["description"], "long text")
        self.runner.handler = reply({"code": "not-found", "error": "fleet hub work failed: HTTP 404"}, code=1)
        result = self.call("fleet_work_show", work_id="wl-000000")
        self.assertEqual(result["error"], "hub_error")
        self.assertEqual(result["code"], "not-found")

    def test_work_next_is_dry_run_argv(self):
        considered = [{"bucket": "not-ready", "work_id": f"wl-{i:06x}"} for i in range(30)]
        self.runner.handler = reply(
            {
                "schema": "brigade.fleet_work_next.v1",
                "work_id": "wl-abcdef",
                "item": {"work_id": "wl-abcdef"},
                "route": {"reason": "selected"},
                "considered": considered,
            }
        )
        result = self.call("fleet_work_next")
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "work", "next", "--json"]])
        self.assertEqual(result["considered_count"], 30)
        self.assertEqual(len(result["considered"]), 20)
        self.assertEqual(result["considered_by_bucket"], {"not-ready": 30})
        self.assertFalse(result["reserved"])
        self.runner.calls.clear()
        self.call("fleet_work_next", consumer="t3-fleet", workload="review")
        self.assertEqual(
            self.runner.argvs(),
            [[BRIGADE, "fleet", "work", "next", "--consumer", "t3-fleet", "--workload", "review", "--json"]],
        )

    def test_work_burn_pages_with_cursor(self):
        self.runner.handler = reply({"exclusions": {"not-ready": 4}, "items": [WORK_ITEM], "next_cursor": "eyJr"})
        result = self.call("fleet_work_burn", cursor="eyJraW5k", limit=5)
        self.assertEqual(
            self.runner.argvs(), [[BRIGADE, "fleet", "work", "burn", "--limit", "5", "--cursor", "eyJraW5k", "--json"]]
        )
        self.assertEqual(result["next_cursor"], "eyJr")
        self.assertEqual(result["exclusions"], {"not-ready": 4})

    def test_models_uses_list_subcommand_only(self):
        self.runner.handler = reply(
            {
                "consumer": "t3-fleet",
                "roster_revision": 53,
                "seats": [{"seat": "chef", "model": "opus", "secret_extra": "x"}],
            }
        )
        result = self.call("fleet_models", consumer="t3-fleet")
        self.assertEqual(
            self.runner.argvs(), [[BRIGADE, "fleet", "models", "list", "--consumer", "t3-fleet", "--json"]]
        )
        self.assertEqual(result["seats"][0]["model"], "opus")
        self.assertNotIn("secret_extra", result["seats"][0])

    def test_cloud_sorts_and_limits_leases(self):
        leases = [
            {"lease_id": f"l{i}", "state": "pending", "admitted_at": f"2026-10-0{i + 1}T00:00:00Z"} for i in range(5)
        ]
        self.runner.handler = reply({"policy": {"global_limit": 4}, "leases": leases})
        result = self.call("fleet_cloud", include_released=True, limit=2)
        self.assertEqual(self.runner.argvs(), [[BRIGADE, "fleet", "cloud", "--all", "--json"]])
        self.assertEqual([lease["lease_id"] for lease in result["leases"]], ["l4", "l3"])
        self.assertEqual(result["lease_count"], 5)

    def test_hub_policy_reads_show_and_status_only(self):
        def handler(argv, stdin):
            if argv[3] == "show":
                return CommandResult(0, json.dumps({"version": 7, "digest": "sha256:x", "document": {}}).encode())
            return CommandResult(1, b"not json")

        self.runner.handler = handler
        result = self.call("fleet_hub_policy")
        self.assertEqual(
            self.runner.argvs(),
            [[BRIGADE, "fleet", "policy", "show", "--json"], [BRIGADE, "fleet", "policy", "status", "--json"]],
        )
        self.assertEqual(result["policy"]["version"], 7)
        self.assertEqual(result["status"], {"error": "brigade_unavailable"})

    def test_failures_map_to_fixed_codes(self):
        for result, code in (
            (CommandResult(0, b"", timed_out=True), "brigade_timeout"),
            (CommandResult(0, b"", overflow=True), "output_too_large"),
            (CommandResult(0, b"[]"), "brigade_unavailable"),
        ):
            self.runner.handler = lambda argv, stdin, result=result: result
            self.assertEqual(self.call("fleet_cloud"), {"error": code})

    def test_credentials_are_redacted(self):
        self.runner.handler = reply({"item": dict(WORK_ITEM, description="token=abcdefghijklmnopqrstuv")})
        result = self.call("fleet_work_show", work_id=WORK_ITEM["work_id"])
        self.assertNotIn("abcdefghijklmnopqrstuv", json.dumps(result))

    def test_no_v4_call_uses_a_write_verb(self):
        self.runner.handler = reply({})
        for name in V4_TOOLS:
            arguments = {"work_id": WORK_ITEM["work_id"]} if name == "fleet_work_show" else {}
            self.call(name, **arguments)
        for argv in self.runner.argvs():
            self.assertEqual(argv[:2], [BRIGADE, "fleet"])
            self.assertEqual(argv[-1], "--json")
            self.assertFalse(WRITE_VERBS & set(argv[2:4]), argv)

    def test_capabilities_point_miller_at_fleet_awareness(self):
        capabilities = self.call("dot_t3_capabilities")
        self.assertEqual(capabilities["bridge_version"], "6")  # v5 bumped it
        self.assertIn("fleet_work_next", capabilities["operating_instructions"])
        self.assertIn("fleet_overview", capabilities["operating_instructions"])
        self.assertTrue(set(V4_TOOLS) <= set(capabilities["tools"]))
        self.assertTrue(set(V4_TOOLS) <= set(capabilities["fleet_hub"]["tools"]))
        self.assertTrue(capabilities["fleet_hub"]["read_only"])


STATUS = {
    "runs": [
        {
            "run_id": f"r{i}",
            "node_id": "alpha",
            "repo": "brigade",
            "seat": "sol61",
            "state": "done" if i % 2 else "running",
            "received_at": f"2026-10-10T00:{i:02d}:00Z",
            "harness": "codex",
            "repo_identity": "x",
        }
        for i in range(30)
    ],
    "preference": {"impl": "sol61"},
}
CLAIMS = {
    "claims": [
        {
            "target": f"repo{i}",
            "harness": "codex",
            "role": "impl",
            "job": "j",
            "session": "s",
            "owner_node": "alpha",
            "expires_at": "2026-10-11T00:00:00Z",
            "expired": False,
        }
        for i in range(25)
    ]
}
NEXT = {
    "schema": "brigade.fleet.work.next.v1",
    "work_id": "wl-05a63cf2eeaf241617321cbe",
    "item": {
        "work_id": "wl-05a63cf2eeaf241617321cbe",
        "title": "Burn item",
        "status": "ready",
        "description": "long body that must not reach the digest",
        "priority": "high",
    },
    "route": {"seat": "sol61", "host": "alpha"},
    "considered": [{"work_id": f"wl-{i:024x}", "bucket": "eligible" if i < 2 else "blocked"} for i in range(6)],
}
PRS = [
    {
        "number": 1670,
        "title": "feat: green",
        "headRefOid": "a" * 40,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SKIPPED"},
        ],
    },
    {
        "number": 1671,
        "title": "fix: red",
        "headRefOid": "b" * 40,
        "isDraft": True,
        "mergeable": "CONFLICTING",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
        ],
    },
    {
        "number": 1672,
        "title": "chore: running",
        "headRefOid": "c" * 40,
        "isDraft": False,
        "mergeable": "UNKNOWN",
        "statusCheckRollup": [
            {"__typename": "StatusContext", "state": "PENDING"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    },
    {
        "number": 1673,
        "title": "docs: none",
        "headRefOid": "d" * 40,
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
    },
]


def ok(value):
    return CommandResult(0, json.dumps(value).encode())


class Hub:
    """Routes fake argv to canned results; any entry may be overridden per test."""

    def __init__(self, **overrides):
        self.routes = {
            "status": ok(STATUS),
            "claims": ok(CLAIMS),
            "next": ok(NEXT),
            "whoami": CommandResult(0, b"login: x\n"),
            "octopool-pr": ok(PRS),
            "gh-pr": ok(PRS),
        }
        self.routes.update(overrides)

    def __call__(self, argv, stdin):
        if argv[0] == BRIGADE and argv[1:3] == ["fleet", "status"]:
            key = "status"
        elif argv[0] == BRIGADE and argv[1:3] == ["fleet", "claims"]:
            key = "claims"
        elif argv[0] == BRIGADE and argv[1:4] == ["fleet", "work", "next"]:
            key = "next"
        elif argv == [OCTOPOOL, "whoami"]:
            key = "whoami"
        elif argv[:3] == [OCTOPOOL, "gh", "pr"]:
            key = "octopool-pr"
        elif argv[:2] == [GH, "pr"]:
            key = "gh-pr"
        elif argv[0] == T3:
            return CommandResult(0, FLEET_OK)
        else:
            raise AssertionError("unexpected argv " + repr(argv))
        route = self.routes[key]
        return route(argv, stdin) if callable(route) else route


def subscribe(state, owner, expires=None, ident="sub-1"):
    with state.transaction() as connection:
        connection.execute(
            """INSERT INTO subscriptions(id, owner, name, arguments, url, secret, expires, created)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                ident,
                owner,
                "fleet.inbox.message",
                "{}",
                "https://connectors.api.openai.com/x",
                "s",
                expires,
                time.time(),
            ),
        )


class V5Digest(Fixture):
    def digest(self):
        return self.adapter.call(EMAIL, "fleet_digest", {})

    def test_happy_path_is_compact_and_uses_octopool(self):
        self.runner.handler = Hub()
        for index in range(7):
            self.messages.add(body=f"BODY-{index}", subject=f"subject {index}")
        self.messages.rows[0]["acked_at"] = NOW.isoformat()
        subscribe(self.state, EMAIL, ident="mine")
        subscribe(self.state, EMAIL, expires=time.time() - 10, ident="expired")
        subscribe(self.state, OTHER, ident="theirs")
        digest = self.digest()
        self.assertEqual(digest["section_errors"], [])
        overview = digest["fleet_overview"]
        self.assertEqual(overview["status"]["run_count"], 30)
        self.assertEqual(len(overview["status"]["latest_runs"]), 10)
        self.assertNotIn("harness", overview["status"]["latest_runs"][0])
        self.assertEqual(overview["claims"]["count"], 25)
        self.assertEqual(len(overview["claims"]["claims"]), 20)
        work = digest["fleet_work_next"]
        self.assertEqual(work["work_id"], NEXT["work_id"])
        self.assertEqual(work["item"]["title"], "Burn item")
        self.assertNotIn("description", json.dumps(work))
        self.assertEqual(work["considered_count"], 6)
        self.assertFalse(work["reserved"])
        self.assertEqual(digest["inbox"]["unacked"], 6)
        self.assertEqual(
            [m["subject"] for m in digest["inbox"]["oldest"]],
            ["subject 1", "subject 2", "subject 3", "subject 4", "subject 5"],
        )
        self.assertNotIn("BODY-", json.dumps(digest))
        self.assertEqual(digest["subscriptions"], {"count": 1})
        prs = digest["brigade_prs"]
        self.assertEqual((prs["via"], prs["count"]), ("octopool", 4))
        self.assertEqual(
            [(pr["number"], pr["head_sha8"], pr["checks"]) for pr in prs["prs"]],
            [
                (1670, "a" * 8, "success"),
                (1671, "b" * 8, "failure"),
                (1672, "c" * 8, "pending"),
                (1673, "d" * 8, "none"),
            ],
        )
        self.assertEqual(prs["prs"][1]["mergeable"], "CONFLICTING")
        self.assertTrue(prs["prs"][1]["draft"])
        # Exact read-only argv, digest timeouts, and OCTOPOOL_NO_FALLBACK on the pooled read.
        calls = {tuple(call["argv"][:4]): call for call in self.runner.calls}
        pr_call = next(call for call in self.runner.calls if call["argv"][:2] == [OCTOPOOL, "gh"])
        self.assertEqual(
            pr_call["argv"],
            [
                OCTOPOOL,
                "gh",
                "pr",
                "list",
                "--repo",
                self.runtime.digest_pr_repo,
                "--state",
                "open",
                "--limit",
                "30",
                "--json",
                "number,title,headRefOid,isDraft,mergeable,statusCheckRollup",
            ],
        )
        self.assertEqual(pr_call["timeout"], hub_module.DIGEST_GH_TIMEOUT)
        self.assertTrue(calls)
        brigade_calls = [call for call in self.runner.calls if call["argv"][0] == BRIGADE]
        self.assertEqual(
            sorted(call["argv"][1:] for call in brigade_calls),
            [["fleet", "claims", "--json"], ["fleet", "status", "--json"], ["fleet", "work", "next", "--json"]],
        )
        self.assertTrue(all(call["timeout"] == hub_module.DIGEST_BRIGADE_TIMEOUT for call in brigade_calls))

    def test_octopool_env_sets_no_fallback(self):
        seen = {}

        class Runner:
            def run(inner, argv, *, stdin=b"", timeout, max_output, env=None):
                if argv[:2] == [OCTOPOOL, "gh"]:
                    seen["env"] = env
                return Hub()(argv, stdin)

        self.adapter.hub.commands = Runner()
        self.digest()
        self.assertEqual(seen["env"]["OCTOPOOL_NO_FALLBACK"], "1")
        self.assertEqual(seen["env"]["PATH"], child_path(self.runtime.paths.bin))

    def test_whoami_failure_uses_plain_gh_and_is_cached(self):
        self.runner.handler = Hub(whoami=CommandResult(1, b""))
        first, second = self.digest(), self.digest()
        self.assertEqual(first["brigade_prs"]["via"], "gh")
        self.assertEqual(second["brigade_prs"]["count"], 4)
        self.assertEqual(sum(call["argv"] == [OCTOPOOL, "whoami"] for call in self.runner.calls), 1)
        self.assertFalse(any(call["argv"][:2] == [OCTOPOOL, "gh"] for call in self.runner.calls))

    def test_octopool_read_failure_falls_back_to_gh(self):
        self.runner.handler = Hub(**{"octopool-pr": CommandResult(3, b"refused")})
        prs = self.digest()["brigade_prs"]
        self.assertEqual((prs["via"], prs["octopool_error"], prs["count"]), ("gh", "gh_unavailable", 4))

    def test_failing_sections_do_not_fail_the_call(self):
        self.runner.handler = Hub(
            status=CommandResult(None, b"", timed_out=True),
            next=ok({"code": "unavailable", "error": "hub down"}),
            **{
                "octopool-pr": CommandResult(None, b"", timed_out=True),
                "gh-pr": CommandResult(None, b"", timed_out=True),
            },
        )
        digest = self.digest()
        self.assertEqual(digest["fleet_overview"]["status"], {"error": "brigade_timeout"})
        self.assertEqual(digest["fleet_overview"]["claims"]["count"], 25)
        self.assertEqual(digest["fleet_work_next"]["error"], "hub_error")
        self.assertEqual(digest["brigade_prs"], {"error": "gh_timeout", "via": "gh"})
        self.assertEqual(digest["inbox"]["unacked"], 0)
        self.assertEqual(digest["section_errors"], ["brigade_prs", "fleet_work_next"])

    def test_exceptions_and_deadline_are_per_section(self):
        def boom(argv, stdin):
            raise RuntimeError("unexpected")

        def slow(argv, stdin):
            time.sleep(1.5)
            return ok(PRS)

        self.runner.handler = Hub(next=boom, whoami=slow)
        with patch.object(hub_module, "DIGEST_DEADLINE", 0.5):
            started = time.monotonic()
            digest = self.digest()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.4)
        self.assertEqual(digest["fleet_work_next"], {"error": "section_unavailable"})
        self.assertEqual(digest["brigade_prs"], {"error": "section_timeout"})
        self.assertEqual(digest["inbox"]["unacked"], 0)
        time.sleep(1.2)  # let the slow fake finish before tearDown removes the state dir

    def test_state_failure_is_reported_in_section(self):
        self.runner.handler = Hub()
        with patch.object(BridgeState, "subscription_count", side_effect=MillerError("state_unavailable")):
            digest = self.digest()
        self.assertEqual(digest["subscriptions"], {"error": "state_unavailable"})
        self.assertIn("subscriptions", digest["section_errors"])

    def test_works_with_dispatch_disabled_and_is_bounded(self):
        adapter = self.make_adapter(False)
        self.runner.handler = Hub()
        digest = adapter.call(EMAIL, "fleet_digest", {})
        self.assertLessEqual(len(json.dumps(digest).encode()), 65536)
        self.assertEqual(digest["bridge_version"], "6")


class DigestRoutes(Fixture):
    def test_optional_octopool_skips_probe(self):
        runtime = replace(
            self.runtime, paths=replace(self.runtime.paths, bin=replace(self.runtime.paths.bin, octopool=None))
        )
        self.adapter.hub.runtime = runtime
        self.runner.handler = Hub()
        self.assertEqual(self.call("fleet_digest")["brigade_prs"]["via"], "gh")
        self.assertFalse(any(call["argv"][0] == OCTOPOOL for call in self.runner.calls))

    def test_route_cache_expires_after_ten_minutes(self):
        self.runner.handler = Hub()
        with patch.object(hub_module.time, "monotonic", return_value=1000):
            self.adapter.hub._github_cli()
        with patch.object(hub_module.time, "monotonic", return_value=1599):
            self.adapter.hub._github_cli()
        with patch.object(hub_module.time, "monotonic", return_value=1601):
            self.adapter.hub._github_cli()
        self.assertEqual(self.runner.argvs(), [[OCTOPOOL, "whoami"], [OCTOPOOL, "whoami"]])

    def test_digest_rejects_arguments(self):
        with self.assertRaisesRegex(MillerError, "invalid_request"):
            self.call("fleet_digest", repo="other")
        self.assertEqual(self.runner.calls, [])
