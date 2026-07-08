# content-guard: allow jwt-token file
from __future__ import annotations

import unittest
from pathlib import Path

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy, load_policy


class LoopbackSplitTests(unittest.TestCase):
    def test_loopback_address_matches_loopback_rule_only(self) -> None:
        # content-guard: allow all
        result = scan_text("Service is 127.0.0.1 today.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("loopback-ipv4", rule_ids)
        self.assertNotIn("private-ipv4", rule_ids)

    def test_rfc1918_address_matches_private_rule_only(self) -> None:
        # content-guard: allow all
        result = scan_text("Service is 192.168.1.10 today.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("private-ipv4", rule_ids)
        self.assertNotIn("loopback-ipv4", rule_ids)

    def test_loopback_uses_redacted_ip_replacement(self) -> None:
        policy = Policy(defaults={"infrastructure": "redact"})
        # content-guard: allow all
        result = scan_text("Bind to 127.0.0.1 here.", policy=policy)

        self.assertEqual(result.redacted_text, "Bind to [redacted-ip] here.")


class UsPhoneTighteningTests(unittest.TestCase):
    def test_bare_ten_digit_number_is_not_a_phone(self) -> None:
        # content-guard: allow all
        result = scan_text("Timestamp 1710672000 was logged.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("us-phone", rule_ids)

    def test_parentheses_area_code_matches(self) -> None:
        # content-guard: allow all
        result = scan_text("Call (415) 555-1234 today.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("us-phone", rule_ids)

    def test_dashed_phone_matches(self) -> None:
        # content-guard: allow all
        result = scan_text("Reach 415-555-1234 anytime.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("us-phone", rule_ids)

    def test_plus_one_country_code_matches(self) -> None:
        # content-guard: allow all
        result = scan_text("Dial +1 415 555 1234 from abroad.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("us-phone", rule_ids)

    def test_bare_ten_digit_timestamp_is_not_a_phone(self) -> None:
        # content-guard: allow all
        result = scan_text("Recorded at 1710672000 in the log.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("us-phone", rule_ids)

    def test_phone_cue_word_promotes_bare_digits(self) -> None:
        # content-guard: allow all
        result = scan_text("phone: 4155551234 (primary)")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("us-phone", rule_ids)

    def test_cue_word_substring_does_not_promote_digits(self) -> None:
        # content-guard: allow all
        result = scan_text("recall: 4155551234 was the order id.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("us-phone", rule_ids)

    def test_phone_shape_inside_alphanumeric_token_does_not_match(self) -> None:
        # content-guard: allow all
        result = scan_text("transaction id x415-555-1234 logged.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("us-phone", rule_ids)


class PackagedPolicyParityTests(unittest.TestCase):
    def test_packaged_public_repo_warns_docs_friendly_rules(self) -> None:
        from importlib import resources

        from brigade.guard.policy import load_policy

        policy_path = resources.files("brigade.guard").joinpath("policies", "public-repo.json")
        policy = load_policy(policy_path)
        # content-guard: allow all
        result = scan_text("Bind to 127.0.0.1 and use localhost:11434 with port 18789.", policy=policy)

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("loopback-ipv4", "warn"), actions)
        self.assertIn(("localhost-port", "warn"), actions)
        self.assertIn(("port-reference", "warn"), actions)
        self.assertFalse(result.blocked)

    def test_git_scan_default_policy_warns_docs_friendly_rules(self) -> None:
        from brigade.guard.git_scan import _default_repo_policy

        policy = _default_repo_policy()
        # content-guard: allow all
        result = scan_text("Bind to 127.0.0.1 and use localhost:11434 with port 18789.", policy=policy)

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("loopback-ipv4", "warn"), actions)
        self.assertIn(("localhost-port", "warn"), actions)
        self.assertIn(("port-reference", "warn"), actions)
        self.assertFalse(result.blocked)


class PublicRepoPolicyTests(unittest.TestCase):
    def test_docs_friendly_findings_warn_only_rfc1918_blocks(self) -> None:
        policy_path = (
            Path(__file__).resolve().parents[2] / "src" / "brigade" / "guard" / "policies" / "public-repo.json"
        )
        text = (
            "Bind to 127.0.0.1 for local dev.\n"
            "Use localhost:11434 for the model.\n"
            "Open port 18789 on the host.\n"  # content-guard: allow private-ipv4
            "But 192.168.1.10 is the actual office subnet.\n"
        )
        # content-guard: allow all
        result = scan_text(text, policy=load_policy(policy_path))

        actions = {(f.rule_id, f.action) for f in result.findings}

        self.assertIn(("loopback-ipv4", "warn"), actions)
        self.assertIn(("localhost-port", "warn"), actions)
        self.assertIn(("port-reference", "warn"), actions)
        self.assertIn(("private-ipv4", "block"), actions)

        warn_count = sum(1 for f in result.findings if f.action == "warn")
        block_count = sum(1 for f in result.findings if f.action == "block")
        self.assertEqual(warn_count, 3)
        self.assertEqual(block_count, 1)

    def test_localhost_bare_warns_in_public_repo_policy(self) -> None:
        policy_path = (
            Path(__file__).resolve().parents[2] / "src" / "brigade" / "guard" / "policies" / "public-repo.json"
        )
        # content-guard: allow all
        result = scan_text("Visit localhost in your browser.", policy=load_policy(policy_path))

        actions = {(f.rule_id, f.action) for f in result.findings}
        self.assertIn(("localhost-bare", "warn"), actions)


if __name__ == "__main__":
    unittest.main()


class EmailSshRemoteTests(unittest.TestCase):
    def test_ssh_remote_url_is_not_an_email(self) -> None:
        # content-guard: allow all
        result = scan_text("Push to git@github.com:owner/repo.git when ready.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("email", rule_ids)

    def test_scp_style_path_is_not_an_email(self) -> None:
        # content-guard: allow all
        result = scan_text("Copy with scp backup@host.example.com:/srv/data .")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertNotIn("email", rule_ids)

    def test_email_followed_by_colon_and_space_still_matches(self) -> None:
        # content-guard: allow all
        result = scan_text("Contact alice@gmail.com: she has the keys.")
        rule_ids = {f.rule_id for f in result.findings}

        self.assertIn("email", rule_ids)


class ApiKeyAssignmentRhsTests(unittest.TestCase):
    def test_dotted_identifier_chain_is_not_a_secret(self) -> None:
        # content-guard: allow all
        result = scan_text("const apiKey = apiKeys.anthropicApiKey;")
        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_camelcase_identifier_is_not_a_secret(self) -> None:
        # content-guard: allow all
        result = scan_text("token: daemonConfigPrimaryToken,")
        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_member_env_access_is_not_a_secret(self) -> None:
        # content-guard: allow all
        result = scan_text("apiKey: ctx.env.FIRECRAWL_API_KEY,")
        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_unquoted_literal_with_digits_still_matches(self) -> None:
        # content-guard: allow all
        result = scan_text("Temporary token=abc123abc123abc123abc123abc123.")
        self.assertIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_quoted_literal_still_matches(self) -> None:
        # content-guard: allow all
        result = scan_text('api_key = "sk-fixture0123456789abcdefgh"')
        self.assertIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_quoted_hex_still_matches(self) -> None:
        # content-guard: allow all
        result = scan_text('secret: "0123456789abcdef0123456789abcdef"')
        self.assertIn("api-key-assignment", {f.rule_id for f in result.findings})


class ApiKeyAssignmentDigitChainTests(unittest.TestCase):
    def test_versioned_property_chain_is_not_a_secret(self) -> None:
        # content-guard: allow all
        result = scan_text("const apiKey = apiKeys.v2.anthropicApiKey;")
        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})

    def test_env_member_with_digit_is_not_a_secret(self) -> None:
        # content-guard: allow all
        result = scan_text("token: ctx.env.S3_SECRET_ACCESS_KEY,")
        self.assertNotIn("api-key-assignment", {f.rule_id for f in result.findings})


class JwtTokenRuleTests(unittest.TestCase):
    def test_bare_jwt_assignment_is_caught(self) -> None:
        # content-guard: allow all
        result = scan_text(
            "token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N1XgL0n3I9PlFUP0THsR8U"
        )
        self.assertIn("jwt-token", {f.rule_id for f in result.findings})

    def test_jwt_outside_assignment_is_caught(self) -> None:
        # content-guard: allow all
        result = scan_text(
            "Use eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N1XgL0n3I9PlFUP0THsR8U here."
        )
        self.assertIn("jwt-token", {f.rule_id for f in result.findings})
