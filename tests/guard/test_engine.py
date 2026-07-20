from __future__ import annotations

from importlib import resources
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brigade.guard.engine import scan_text
from brigade.guard.git_commits import _default_commit_policy
from brigade.guard.git_scan import _default_repo_policy
from brigade.guard.policy import Policy, default_policy, load_policy
from brigade.guard.types import Rule, ScanOptions


class EngineTests(unittest.TestCase):
    def test_blocks_infrastructure_but_warns_pii_by_default(self) -> None:
        # content-guard: allow all
        result = scan_text("Reach me at alice@solomonneas.dev via localhost:5204")

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("localhost-port", "block"), actions)
        self.assertIn(("email", "warn"), actions)
        self.assertTrue(result.blocked)

    def test_redacts_policy_redact_actions(self) -> None:
        policy = Policy(defaults={"infrastructure": "redact", "secret": "redact", "pii": "warn"})
        # content-guard: allow private-ipv4
        result = scan_text("Service is 192.168.1.25", policy=policy)

        self.assertFalse(result.blocked)
        self.assertEqual(result.redacted_text, "Service is [redacted-ip]")

    def test_allow_comment_applies_to_next_line(self) -> None:
        text = "<!-- content-guard: allow localhost-bare -->\nUse localhost as an example."
        result = scan_text(text)

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].action, "allow")
        self.assertFalse(result.blocked)

    def test_frontmatter_is_skipped_by_default(self) -> None:
        # content-guard: allow localhost-port
        text = "---\nsource: localhost:5204\n---\nBody is clean.\n"
        result = scan_text(text)

        self.assertEqual(result.findings, [])

    def test_can_skip_code_blocks(self) -> None:
        # content-guard: allow localhost-port
        text = "```\ncurl http://localhost:5204\n```\n"
        result = scan_text(text, options=ScanOptions(scan_code_blocks=False))

        self.assertEqual(result.findings, [])

    def test_custom_rule(self) -> None:
        policy = Policy(
            custom_rules=[
                Rule(
                    id="project-codename",
                    category="business",
                    pattern=r"\bProject Nightshade\b",
                    replacement="[redacted-project]",
                )
            ]
        )
        result = scan_text("Project Nightshade launches later.", policy=policy)

        self.assertEqual(result.findings[0].rule_id, "project-codename")
        self.assertEqual(result.findings[0].action, "warn")

    def test_pr_draft_policy_blocks_pii(self) -> None:
        policy_path = Path(__file__).resolve().parents[2] / "src" / "brigade" / "guard" / "policies" / "pr-draft.json"
        # content-guard: allow all
        result = scan_text("PR note with alice@solomonneas.dev and localhost:5204", policy=load_policy(policy_path))

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("email", "block"), actions)
        self.assertIn(("localhost-port", "block"), actions)
        self.assertTrue(result.blocked)

    def test_public_repo_policy_blocks_pii_and_secrets(self) -> None:
        policy_path = (
            Path(__file__).resolve().parents[2] / "src" / "brigade" / "guard" / "policies" / "public-repo.json"
        )
        # content-guard: allow all
        result = scan_text(
            "token = abcdefghijklmnopqrstuvwxyz123456 and alice@solomonneas.dev", policy=load_policy(policy_path)
        )

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("api-key-assignment", "block"), actions)
        self.assertIn(("email", "block"), actions)
        self.assertTrue(result.blocked)

    def test_packaged_policy_resource_loads(self) -> None:
        policy_path = resources.files("brigade.guard").joinpath("policies", "public-content.json")
        policy = load_policy(policy_path)

        self.assertEqual(policy.name, "public-content")

    def test_secret_assignment_preserves_sentence_punctuation(self) -> None:
        # content-guard: allow api-key-assignment
        result = scan_text("Temporary token=abc123abc123abc123abc123abc123.")

        # content-guard: allow api-key-assignment
        self.assertEqual(result.findings[0].match, "token=abc123abc123abc123abc123abc123")
        self.assertEqual(result.redacted_text, "Temporary [redacted-secret].")

    def test_coauthor_trailer_blocks_and_removes_line(self) -> None:
        # content-guard: allow email
        # content-guard: allow example-email-reserved
        result = scan_text("feat: change\n\nCo-authored-by: Example User <user@solomonneas.dev>\n")

        self.assertEqual(result.findings[0].rule_id, "coauthored-by-trailer")
        self.assertTrue(result.blocked)
        self.assertEqual(result.redacted_text, "feat: change\n\n")

    def test_inhouse_policy_allows_only_approved_agent_coauthor_trailers(self) -> None:
        rejected = "feat: change\n\nCo-authored-by: Example User <person@private.test>\n"
        inhouse_policy = load_policy(default_policy("in-house-repo.json"))

        rejected_result = scan_text(rejected, policy=inhouse_policy)

        for address in (
            "noreply@anthropic.com",
            "codex@openai.com",
            "cursoragent@cursor.com",
            "12345@users.noreply.github.com",
        ):
            allowed = f"feat: change\n\nCo-authored-by: Agent <{address}>\n"
            allowed_result = scan_text(allowed, policy=inhouse_policy)
            self.assertEqual(allowed_result.findings, [])
            self.assertEqual(allowed_result.redacted_text, allowed)

        outbound_result = scan_text(
            "feat: change\n\nCo-authored-by: Codex <codex@openai.com>\n",
            policy=load_policy(default_policy("pr-draft.json")),
        )
        self.assertTrue(rejected_result.blocked)
        self.assertEqual(rejected_result.redacted_text, "feat: change\n\n")
        self.assertTrue(outbound_result.blocked)
        self.assertEqual(outbound_result.redacted_text, "feat: change\n\n")

    def test_inhouse_policy_blocks_private_display_email_in_approved_agent_trailer(self) -> None:
        message = "feat: change\n\nCo-authored-by: secret.person@corp.internal <codex@openai.com>\n"
        inhouse_policy = load_policy(default_policy("in-house-repo.json"))

        result = scan_text(message, policy=inhouse_policy)

        self.assertEqual(
            [(finding.rule_id, finding.match) for finding in result.findings],
            [("email", "secret.person@corp.internal")],
        )
        self.assertTrue(result.blocked)
        self.assertEqual(
            result.redacted_text,
            "feat: change\n\nCo-authored-by: <PRIVATE_EMAIL> <codex@openai.com>\n",
        )

    def test_external_defaults_strip_approved_agent_coauthor_trailers(self) -> None:
        message = "feat: change\n\nCo-authored-by: Codex <codex@openai.com>\n"
        policies = (
            load_policy(default_policy("public-repo.json")),
            _default_commit_policy(),
            _default_repo_policy(),
        )

        for policy in policies:
            result = scan_text(message, policy=policy)
            self.assertTrue(result.blocked)
            self.assertEqual(result.redacted_text, "feat: change\n\n")

    def test_policy_can_enable_opf_backend(self) -> None:
        with TemporaryDirectory() as tmp:
            opf_bin = Path(tmp) / "opf"
            policy_path = Path(tmp) / "policy.json"
            opf_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "text = pathlib.Path(sys.argv[-1]).read_text()\n"
                "print(text.replace('Alice Example', '<PRIVATE_PERSON>'), end='')\n"
            )
            opf_bin.chmod(0o755)
            policy_path.write_text(
                '{"backends":{"opf":{"enabled":true,"action":"redact","bin":"' + str(opf_bin) + '"}}}'
            )

            result = scan_text("Alice Example wrote the draft.", policy=load_policy(policy_path))

        self.assertIn(("opf-pii", "redact"), {(f.rule_id, f.action) for f in result.findings})
        self.assertEqual(result.redacted_text, "<PRIVATE_PERSON> wrote the draft.")


class ApiKeyAssignmentFalsePositiveTests(unittest.TestCase):
    """The api-key-assignment rule should not match code that reads a secret
    from a safe source (env vars, config objects). Those are not leaks; the
    actual secret is in the env, not in this file."""

    def test_process_env_assignment_not_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text("const apiKey = process.env.IMMICH_API_KEY;")

        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_os_environ_assignment_not_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text('API_KEY = os.environ["IMMICH_API_KEY"]')

        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_os_getenv_assignment_not_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text('token = os.getenv("GH_TOKEN")')

        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_config_object_assignment_not_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text("apiKey = config.apiKey || settings.fallback")

        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_real_literal_token_still_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text('token = "abcdef1234567890abcdef1234567890"')

        self.assertIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_real_unquoted_token_still_flagged(self) -> None:
        # content-guard: allow all
        result = scan_text("token = abcdef1234567890abcdef1234567890")

        self.assertIn("api-key-assignment", {f.rule_id for f in result.findings})


class FileScopedAllowTests(unittest.TestCase):
    def test_allow_file_scope_exempts_entire_file(self) -> None:
        text = (
            "<!-- content-guard: allow private-ipv4 file -->\n"
            "\n"
            "Use 192.168.1.10 as the gateway.\n"
            "Also 192.168.1.20 for the backup.\n"
            "And one more: 192.168.1.30.\n"
        )
        result = scan_text(text)

        for finding in result.findings:
            if finding.rule_id == "private-ipv4":
                self.assertEqual(finding.action, "allow")
        self.assertFalse(result.blocked)

    def test_allow_all_file_scope_exempts_all_rules_in_file(self) -> None:
        text = "<!-- content-guard: allow all file -->\n\nEmail: alice@example.com\nService: 192.168.1.10:8080\n"
        result = scan_text(text)

        self.assertFalse(result.blocked)
        for finding in result.findings:
            self.assertEqual(finding.action, "allow")

    def test_allow_file_scope_does_not_affect_other_rules(self) -> None:
        text = (
            "<!-- content-guard: allow private-ipv4 file -->\n"
            "\n"
            "Use 192.168.1.10 here.\n"
            "But token=abcdef1234567890abcdef1234567890 is a real secret.\n"
        )
        result = scan_text(text)

        rule_actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("private-ipv4", "allow"), rule_actions)
        # api-key-assignment still blocks
        self.assertTrue(any(f.action == "block" for f in result.findings))


class ExamplePatternDowngradeTests(unittest.TestCase):
    def test_example_phone_555_warns_not_blocks(self) -> None:
        # content-guard: allow all
        result = scan_text("Reach the test line at +1 (555) 123-4567 anytime.")

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("example-phone-555", "warn"), actions)
        # The general us-phone rule should NOT also fire (first match wins)
        self.assertNotIn("us-phone", {f.rule_id for f in result.findings})

    def test_example_phone_555_dashed(self) -> None:
        # content-guard: allow all
        result = scan_text("Call 555-123-4567 for the demo.")

        rule_ids = {f.rule_id for f in result.findings}
        self.assertIn("example-phone-555", rule_ids)

    def test_real_area_code_phone_still_matches_us_phone(self) -> None:
        # content-guard: allow all
        result = scan_text("Call 415-555-9999 for service.")

        rule_ids = {f.rule_id for f in result.findings}
        # 415 area code is real, so us-phone fires, NOT example-phone-555
        # (the 555 is just the prefix here, not the area code)
        self.assertIn("us-phone", rule_ids)

    def test_example_email_reserved_warns_not_blocks(self) -> None:
        # content-guard: allow all
        result = scan_text("Contact alice@example.com for details.")

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("example-email-reserved", "warn"), actions)
        self.assertNotIn("email", {f.rule_id for f in result.findings})

    def test_example_email_covers_example_org_and_net(self) -> None:
        # content-guard: allow all
        text = "Try bob@example.org or carol@example.net for testing."
        result = scan_text(text)

        rule_ids = {f.rule_id for f in result.findings}
        self.assertEqual(rule_ids, {"example-email-reserved"})

    def test_example_email_covers_test_tld(self) -> None:
        # RFC 2606 reserves .test for non-real use.
        # content-guard: allow all
        result = scan_text("admin@admin.test is the default fixture user.")

        rule_ids = {f.rule_id for f in result.findings}
        self.assertEqual(rule_ids, {"example-email-reserved"})

    def test_example_email_covers_local_tld(self) -> None:
        # mDNS / RFC 6762 reserves .local for link-local hosts (not real domains).
        # content-guard: allow all
        text = "Default MISP login: admin@misp.local and admin@thehive.local"
        result = scan_text(text)

        rule_ids = {f.rule_id for f in result.findings}
        self.assertEqual(rule_ids, {"example-email-reserved"})

    def test_example_email_covers_invalid_tld(self) -> None:
        # RFC 2606 reserves .invalid.
        # content-guard: allow all
        result = scan_text("Use noreply@nowhere.invalid for unverified entries.")

        rule_ids = {f.rule_id for f in result.findings}
        self.assertEqual(rule_ids, {"example-email-reserved"})

    def test_real_email_still_blocks(self) -> None:
        policy = Policy(defaults={"pii": "block"})
        # content-guard: allow all
        result = scan_text("Contact alice@solomonneas.dev directly.", policy=policy)

        rule_ids = {f.rule_id for f in result.findings}
        self.assertIn("email", rule_ids)
        self.assertNotIn("example-email-reserved", rule_ids)

    def test_example_rules_stay_warn_under_pii_block_policy(self) -> None:
        # Even when the policy aggressively blocks the PII category, the
        # example-* rules should still WARN (not BLOCK). This is the whole
        # point of the rule-default-override mechanism.
        policy = Policy(defaults={"pii": "block"})
        # content-guard: allow all
        result = scan_text("Test mail: bob@example.com. Test phone: 555-867-5309.", policy=policy)

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("example-email-reserved", "warn"), actions)
        self.assertIn(("example-phone-555", "warn"), actions)
        self.assertFalse(result.blocked)

    def test_example_rule_action_can_still_be_overridden_in_policy_rules(self) -> None:
        # Users who want example-* rules to actually block can still opt in.
        policy = Policy(rules={"example-email-reserved": "block"})
        # content-guard: allow all
        result = scan_text("alice@example.com", policy=policy)

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("example-email-reserved", "block"), actions)
        self.assertTrue(result.blocked)


class KnownHostsTests(unittest.TestCase):
    def test_known_host_ip_blocks_in_warn_policy(self) -> None:
        # Policy WARNS on private-ipv4 (lenient) but a known-hosts entry should
        # still upgrade matches to BLOCK.
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                '{"rules": {"private-ipv4": "warn"},"known_hosts": ["192.168.99.56", "192.168.99.91"]}'
            )
            # content-guard: allow all
            result = scan_text("Real host is 192.168.99.56 here.", policy=load_policy(policy_path))

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("known-host", "block"), actions)
        # The private-ipv4 rule should NOT also fire (known-host claimed it first)
        self.assertNotIn("private-ipv4", {f.rule_id for f in result.findings})

    def test_non_known_host_ip_falls_back_to_normal_rule(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text('{"rules": {"private-ipv4": "warn"},"known_hosts": ["192.168.99.56"]}')
            # content-guard: allow all
            result = scan_text("Some other IP is 10.0.0.5 in the docs.", policy=load_policy(policy_path))

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("private-ipv4", "warn"), actions)
        self.assertNotIn("known-host", {f.rule_id for f in result.findings})

    def test_known_hosts_hostname_match(self) -> None:
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text('{"known_hosts": ["host-a.local", "host-c.local"]}')
            # content-guard: allow all
            result = scan_text("SSH into host-a.local to debug.", policy=load_policy(policy_path))

        rule_ids = {f.rule_id for f in result.findings}
        self.assertIn("known-host", rule_ids)

    def test_known_hosts_does_not_match_partial_ip(self) -> None:
        # 192.168.99.56 should not match if the text has 192.168.99.560 or similar
        with TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text('{"known_hosts": ["192.168.99.56"]}')
            # content-guard: allow all
            result = scan_text("Version is 1.92.168.4.561 actually.", policy=load_policy(policy_path))

        self.assertNotIn("known-host", {f.rule_id for f in result.findings})


if __name__ == "__main__":
    unittest.main()


class AllowCommentDisplayFormTests(unittest.TestCase):
    """Findings print as `category/rule-id`; copying that id into an allow
    comment must work, not silently fail (the pre-2026-07 behavior)."""

    def test_line_scoped_allow_accepts_displayed_category_id_form(self) -> None:
        text = "// content-guard: allow secret/bearer-token\nconst header = 'Bearer super-secret-token-value-123456';\n"
        result = scan_text(text)
        bearer = [f for f in result.findings if f.rule_id == "bearer-token"]
        self.assertEqual(len(bearer), 1)
        self.assertEqual(bearer[0].action, "allow")
        self.assertFalse(result.blocked)

    def test_file_scoped_allow_accepts_displayed_category_id_form(self) -> None:
        text = (
            "// content-guard: allow secret/bearer-token file\n"
            "line two\n"
            "line three\n"
            "const header = 'Bearer super-secret-token-value-123456';\n"
        )
        result = scan_text(text)
        bearer = [f for f in result.findings if f.rule_id == "bearer-token"]
        self.assertEqual(len(bearer), 1)
        self.assertEqual(bearer[0].action, "allow")
        self.assertFalse(result.blocked)

    def test_bare_rule_id_still_works(self) -> None:
        text = "// content-guard: allow bearer-token file\nconst header = 'Bearer super-secret-token-value-123456';\n"
        result = scan_text(text)
        bearer = [f for f in result.findings if f.rule_id == "bearer-token"]
        self.assertEqual(len(bearer), 1)
        self.assertEqual(bearer[0].action, "allow")

    def test_wrong_category_prefix_does_not_allow(self) -> None:
        text = (
            "// content-guard: allow pii/bearer-token file\nconst header = 'Bearer super-secret-token-value-123456';\n"
        )
        result = scan_text(text)
        bearer = [f for f in result.findings if f.rule_id == "bearer-token"]
        self.assertEqual(len(bearer), 1)
        self.assertNotEqual(bearer[0].action, "allow")
