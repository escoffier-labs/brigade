"""Bridge behavior ported to isolated Dot tools."""

from tests.dot_support import requires_local_state

import hashlib
import json
import os
from pathlib import Path

from brigade.dot.contracts import DotError
from brigade.dot.process import CommandResult
from tests.test_dot_adapter import BRIGADE, HANDOFF, NOW, SHA, Fixture, lint_report


class MemoryChecks(Fixture):
    @requires_local_state
    def test_memory_search_argv_and_shape(self):
        reply = {
            "match_count": 2,
            "matches": [
                {
                    "card_id": "c",
                    "title": "t",
                    "path": "memory/cards/c.md",
                    "score": 3,
                    "tags": ["a"],
                    "summary": "",
                    "card_aliases": ["c"],
                    "body": "never returned",
                }
            ],
        }
        self.runner.handler = lambda argv, stdin: CommandResult(0, json.dumps(reply).encode())
        result = self.call("memory_search", query="-dash query", limit=4)
        self.assertEqual(
            self.runner.argvs(),
            [
                [
                    BRIGADE,
                    "memory",
                    "search",
                    "--target",
                    str(self.workspace),
                    "--limit",
                    "4",
                    "--json",
                    "--",
                    "-dash query",
                ]
            ],
        )
        self.assertEqual(result["matches"][0]["path"], "memory/cards/c.md")
        self.assertNotIn("body", result["matches"][0])

    @requires_local_state
    def test_memory_card_reads_chunks_and_rejects_links(self):
        text = "---\ntitle: big\n---\n" + "abcdefghij" * 7000
        (self.cards / "big-card.md").write_text(text)
        (self.cards / "good-card.md").write_text("hello card /srv/repos/example " + SHA)
        outside = self.root / "outside.md"
        outside.write_text("secret outside")
        (self.cards / "linked.md").symlink_to(outside)
        first = self.call("memory_card", name="big-card")
        self.assertEqual(first["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(first["next_offset"], 30000)
        self.assertEqual(first["content"], text[:30000])
        last = self.call("memory_card", name="big-card.md", offset=60000)
        self.assertIsNone(last["next_offset"])
        self.assertEqual(last["content"], text[60000:])
        good = self.call("memory_card", name="good-card")
        self.assertIn("/srv/repos/example", good["content"])
        self.assertIn(SHA, good["content"])
        self.assertEqual(good["path"], str(self.cards / "good-card.md"))
        self.assertEqual(self.call("memory_card", name="linked")["error"], "card_unavailable")
        self.assertEqual(self.call("memory_card", name="missing")["error"], "card_not_found")
        os.chmod(self.cards, 0o777)
        with self.assertRaisesRegex(DotError, "^memory_unavailable$"):
            self.call("memory_card", name="good-card")
        self.assertEqual(self.runner.calls, [])


class HandoffChecks(Fixture):
    def expected_name(self, slug):
        return f"{NOW.astimezone():%Y-%m-%d-%H%M}-dot-{slug}.md"

    @requires_local_state
    def test_write_lints_private_copy_then_never_overwrites(self):
        seen = []

        def handler(argv, stdin):
            seen.append(Path(argv[-1]).read_text())
            self.assertEqual(list(self.inbox.iterdir()), [])  # Nothing published before lint passes.
            return CommandResult(0, lint_report(True))

        self.runner.handler = handler
        result = self.call("handoff_write", slug="bridge-probe", content=HANDOFF)
        name = self.expected_name("bridge-probe")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(self.inbox / name))
        self.assertEqual(seen, [HANDOFF])
        argv = self.runner.argvs()[0]
        self.assertEqual(argv[:6], [BRIGADE, "handoff", "lint", "--json", "--target", str(self.workspace)])
        self.assertTrue(argv[6].startswith(str(self.private) + "/handoff-lint-"))
        self.assertFalse(Path(argv[6]).exists())
        self.assertEqual([path.name for path in self.inbox.iterdir()], [name])
        self.assertEqual((self.inbox / name).read_text(), HANDOFF)
        again = self.call("handoff_write", slug="bridge-probe", content=HANDOFF.replace("probe", "other"))
        self.assertEqual(again["error"], "handoff_exists")
        self.assertEqual((self.inbox / name).read_text(), HANDOFF)
        self.assertEqual(len(self.runner.calls), 1)

    @requires_local_state
    def test_link_race_and_symlink_destination_never_overwrite(self):
        self.runner.handler = lambda argv, stdin: (
            (self.inbox / self.expected_name("raced")).symlink_to(self.root / "target"),
            CommandResult(0, lint_report(True)),
        )[1]
        result = self.call("handoff_write", slug="raced", content=HANDOFF)
        self.assertEqual(result["error"], "handoff_exists")
        self.assertFalse((self.root / "target").exists())
        self.assertEqual(sorted(path.name for path in self.inbox.iterdir()), [self.expected_name("raced")])

    @requires_local_state
    def test_invalid_lint_and_credentials_publish_nothing(self):
        self.runner.handler = lambda argv, stdin: CommandResult(1, lint_report(False))
        result = self.call("handoff_write", slug="bad-one", content=HANDOFF)
        self.assertEqual(result["error"], "handoff_invalid")
        self.assertEqual(result["lint_errors"], ["missing required section: Type"])
        leaked = HANDOFF + "\n- token: ghp_" + "a" * 36 + "\n"
        self.assertEqual(self.call("handoff_write", slug="leaky", content=leaked)["error"], "content_rejected")
        self.runner.handler = lambda argv, stdin: CommandResult(None, b"", timed_out=True)
        self.assertEqual(self.call("handoff_write", slug="slow-lint", content=HANDOFF)["error"], "handoff_unavailable")
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertEqual([path.name for path in self.private.iterdir() if "handoff-lint" in path.name], [])

    @requires_local_state
    def test_untrusted_inbox_refused(self):
        os.chmod(self.inbox, 0o777)
        with self.assertRaisesRegex(DotError, "^handoff_unavailable$"):
            self.call("handoff_write", slug="bridge-probe", content=HANDOFF)
        self.assertEqual(self.runner.calls, [])


class PolicyAndSkills(Fixture):
    def policy_files(self):
        paths = [self.workspace / relative for relative in (".codex/AGENTS.md", "AGENTS.md", "ai-tools/AGENTS.md")]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o755)
            path.write_text(
                '## Writing and formatting\nUse plain words.\n\n<!-- SLOP-BANLIST:START -->\n- "filler"\n<!-- SLOP-BANLIST:END -->\n'
            )
        return paths

    @requires_local_state
    def test_policy_is_workspace_scoped_hashed_and_compacted(self):
        paths = self.policy_files()
        result = self.call("fleet_policy")
        self.assertEqual([source["path"] for source in result["sources"]], list(map(str, paths)))
        self.assertEqual(result["banned_phrases"], ["filler"])
        self.assertIn("Use plain words.", result["digest"]["writing_rules"])
        self.assertEqual(
            self.call("fleet_policy", since_hash=result["hash"]), {"changed": False, "hash": result["hash"]}
        )
        paths[0].write_text("updated")
        self.assertTrue(self.call("fleet_policy", since_hash=result["hash"])["changed"])

    @requires_local_state
    def test_policy_symlink_cannot_read_outside_workspace(self):
        paths = self.policy_files()
        outside = self.root / "outside-policy.md"
        outside.write_text("private outside text")
        paths[0].unlink()
        paths[0].symlink_to(outside)
        with self.assertRaises((DotError, OSError)):
            self.call("fleet_policy")

    @requires_local_state
    def test_skill_search_and_anchored_resource_paging(self):
        self.runner.handler = lambda argv, stdin: CommandResult(0, b'{"skills":[{"id":"example"}],"count":1}')
        self.assertEqual(self.call("skill_search", query="fleet", limit=1), {"skills": [{"id": "example"}], "count": 1})
        self.assertEqual(
            self.runner.argvs(), [[BRIGADE, "skills", "search", "fleet", "--target", str(self.workspace), "--json"]]
        )
        directory = self.workspace / ".brigade" / "skills" / "registry" / "example"
        directory.mkdir(parents=True)
        text = "a" * 24001
        (directory / "SKILL.md").write_text(text)
        result = self.call("skill_get", name="example")
        self.assertEqual(result["content"], text[:24000])
        self.assertEqual(result["next_offset"], 24000)
        self.assertEqual(self.call("skill_get", name="example", offset=24000)["content"], "a")
        self.assertEqual(self.call("skill_get", name="missing"), {"error": "skill_not_found"})
        with self.assertRaises(DotError):
            self.call("skill_get", name="example", resource="../outside")
