# content-guard: allow jwt-token file
from __future__ import annotations

import unittest
from pathlib import Path

from brigade.guard.engine import scan_text
from brigade.guard.policy import Policy, load_policy
from brigade.guard.types import Rule


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


class MissingLeakShapeTests(unittest.TestCase):
    def test_windows_spaced_paths_are_completely_redacted(self) -> None:
        for separator in ("\\", "\\\\"):
            for components in (
                ("Example User", "Private Folder", "secret.txt"),
                ("example-user", "Private Folder", "secret file.txt"),
                ("Example User",),
                ("Example User", "Private Folder"),
            ):
                path = "C:" + separator + separator.join(("Users", *components))
                for quote in ('"', "'", "`"):
                    text = f"Config at {quote}{path}{quote}, then continue."
                    with self.subTest(separator=separator, components=components, quote=quote):
                        result = scan_text(text, policy=Policy(rules={"windows-home-path": "redact"}))
                        self.assertEqual([finding.match for finding in result.findings], [path])
                        self.assertEqual(
                            result.redacted_text,
                            f"Config at {quote}<PRIVATE_HOME_PATH>{quote}, then continue.",
                        )

    def test_unquoted_windows_spaced_paths_preserve_surrounding_text(self) -> None:
        for separator in ("\\", "\\\\"):
            path = "C:" + separator + separator.join(("Users", "Example User", "Private Folder", "secret file.txt"))
            for before, after in (
                ("Config at ", " for logs."),
                ("Config at ", ", then continue."),
                ("Config at (", ") for logs."),
                ("Config at [", "]; then continue."),
                ("Config at ", " then consult docs.md."),
                ("Config at ", "\nNext line."),
            ):
                with self.subTest(separator=separator, before=before, after=after):
                    result = scan_text(before + path + after, policy=Policy(rules={"windows-home-path": "redact"}))
                    self.assertEqual([finding.match for finding in result.findings], [path])
                    self.assertEqual(result.redacted_text, before + "<PRIVATE_HOME_PATH>" + after)

    def test_compound_hostname_config_keys_match_without_broadening_code_context(self) -> None:
        host = "fixture-db" + ".internal"
        for key in (
            "DATABASE_HOST",
            "INTERNAL_API_HOST",
            "database-host",
            "database_hostname",
            "internal-api-endpoint",
        ):
            for assignment in ("=", ": "):
                with self.subTest(key=key, assignment=assignment):
                    text = f"{key}{assignment}{host}"
                    result = scan_text(text)
                    self.assertEqual([finding.rule_id for finding in result.findings], ["private-hostname"])
                    self.assertEqual(result.redacted_text, f"{key}{assignment}[redacted-host]")

        for before, after in (
            ("arbitrary_value=", ""),
            ("DATABASE_HOSTNAME_SUFFIX=", ""),
            ("DATABASE_GHOST=", ""),
            ("DATABASEHOST=", ""),
            ("obj.DATABASE_HOST=", ""),
            ("const DATABASE_HOST=", ";"),
            ("callback(", ")"),
            ("DATABASE_HOST=", ".example.com"),
        ):
            with self.subTest(before=before, after=after):
                self.assertNotIn(
                    "private-hostname",
                    {finding.rule_id for finding in scan_text(before + host + after).findings},
                )

    def test_equals_form_hostname_options_match_and_redact(self) -> None:
        host = "fixture-db" + ".internal"
        for key in ("host", "hostname", "server", "endpoint", "domain", "url", "address", "database-host"):
            for prefix in ("--", "tool --", "tool -", "tool --verbose --output=json --"):
                with self.subTest(key=key, prefix=prefix):
                    before = f"{prefix}{key}="
                    text = before + host + " --verbose"
                    result = scan_text(text)
                    self.assertEqual([finding.rule_id for finding in result.findings], ["private-hostname"])
                    self.assertEqual([finding.match for finding in result.findings], [host])
                    redacting = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
                    self.assertEqual(redacting.redacted_text, before + "[redacted-host] --verbose")

    def test_equals_form_hostname_option_skips_remain(self) -> None:
        host = "fixture-db" + ".internal"
        for before, after in (
            ("tool --arbitrary-value=", ""),
            ("tool --hostname-suffix=", ""),
            ("tool --ghost=", ""),
            ("tool --databasehost=", ""),
            ("arbitrary_value=", ""),
            ("obj.host=", ""),
            ("const value = tool --host=", ";"),
            ("callback(--host=", ")"),
            ("callback(host=", ")"),
            ("tool --host=", "()"),
            ("tool --host=", " ? yes : no"),
            ("tool --host=", ".example.com"),
        ):
            with self.subTest(before=before, after=after):
                text = before + host + after
                result = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
                self.assertNotIn("private-hostname", {finding.rule_id for finding in result.findings})
                self.assertEqual(result.redacted_text, text)

    def test_windows_home_path_matches(self) -> None:
        result = scan_text(
            r"Config at C:\Users\example-user\.config\tool.json"  # content-guard: allow windows-home-path
        )
        self.assertIn("windows-home-path", {finding.rule_id for finding in result.findings})

    def test_root_path_matches(self) -> None:
        result = scan_text("Config at /root/.config/tool.json")  # content-guard: allow root-path
        self.assertIn("root-path", {finding.rule_id for finding in result.findings})

    def test_quoted_root_spaced_paths_are_completely_matched_and_redacted(self) -> None:
        public_policy = load_policy(Path(__file__).parents[2] / "src/brigade/guard/policies/public-repo.json")
        for components in (
            ("Private Folder",),
            ("Private Folder", "secret.txt"),
            (".config", "Private Folder", "secret file.txt"),
            (".config", "secret file.txt"),
        ):
            path = "/".join(("", "root", *components))
            for quote in ('"', "'", "`"):
                text = f"Config at {quote}{path}{quote}, then continue."
                with self.subTest(components=components, quote=quote, check="match"):
                    result = scan_text(text, policy=public_policy)
                    self.assertEqual([finding.match for finding in result.findings], [path])
                    self.assertTrue(result.blocked)
                with self.subTest(components=components, quote=quote, check="redaction"):
                    result = scan_text(text, policy=Policy(rules={"root-path": "redact"}))
                    self.assertEqual(
                        result.redacted_text,
                        f"Config at {quote}[redacted-root-path]{quote}, then continue.",
                    )

    def test_unquoted_root_paths_preserve_surrounding_prose(self) -> None:
        path = "/".join(("", "root", ".config", "tool.json"))
        text = f"Config at {path} then consult docs.md."
        result = scan_text(text, policy=Policy(rules={"root-path": "redact"}))
        self.assertEqual([finding.match for finding in result.findings], [path])
        self.assertEqual(result.redacted_text, "Config at [redacted-root-path] then consult docs.md.")

    def test_bare_provider_token_matches(self) -> None:
        token = "ghp_" + "A" * 36
        result = scan_text(f"Leaked credential: {token}")
        self.assertIn("bare-token-prefix", {finding.rule_id for finding in result.findings})

    def test_provider_tokens_preserve_context_detector_precedence(self) -> None:
        for text, expected in (
            (
                'api_key="ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"',  # content-guard: allow api-key-assignment
                "api-key-assignment",
            ),
            (
                "token='ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'",  # content-guard: allow api-key-assignment
                "api-key-assignment",
            ),
            (
                "Credential: ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # content-guard: allow bare-token-prefix
                "bare-token-prefix",
            ),
            (
                "Bearer ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # content-guard: allow bearer-token
                "bearer-token",
            ),
        ):
            with self.subTest(expected=expected, text=text):
                result = scan_text(text)
                self.assertEqual([finding.rule_id for finding in result.findings], [expected])

    def test_private_hostname_matches(self) -> None:
        result = scan_text("Connect to build-agent.internal for the artifact.")  # content-guard: allow private-hostname
        self.assertIn("private-hostname", {finding.rule_id for finding in result.findings})

    def test_public_and_reserved_example_hostnames_do_not_match(self) -> None:
        result = scan_text("See docs.example.com and api.github.com.")
        self.assertNotIn("private-hostname", {finding.rule_id for finding in result.findings})

    def test_windows_home_path_negatives(self) -> None:
        for text in (r"C:\Projects\demo", r"C:\Users", r"XC:\Users\demo"):
            with self.subTest(text=text):
                self.assertNotIn("windows-home-path", {f.rule_id for f in scan_text(text).findings})

        for separator in ("\\", "\\\\", "\\\\\\\\"):
            text = '"C:' + separator + "Users" + separator + '"'
            with self.subTest(separator=separator):
                self.assertNotIn("windows-home-path", {f.rule_id for f in scan_text(text).findings})

    def test_root_path_negatives(self) -> None:
        for text in ("/rooted/project", "/srv/root/project", "relative/root/project"):
            with self.subTest(text=text):
                self.assertNotIn("root-path", {f.rule_id for f in scan_text(text).findings})

        for path in ("/rooted/Private Folder", "/srv/root/Private Folder", "relative/root/Private Folder", "/root"):
            for quote in ('"', "'", "`"):
                text = f"Config at {quote}{path}{quote}, then continue."
                with self.subTest(path=path, quote=quote):
                    result = scan_text(text, policy=Policy(rules={"root-path": "redact"}))
                    self.assertEqual(result.findings, [])
                    self.assertEqual(result.redacted_text, text)

    def test_provider_prefix_families_match_without_assignment(self) -> None:
        for prefix, body in (
            ("github_pat_", "a" * 30),
            ("gho_", "a" * 36),
            ("xoxb-", "a" * 24),
            ("sk-live-", "a" * 24),
            ("sk-proj-", "a" * 24),
            ("sk-ant-", "a" * 24),
            ("AKIA", "A" * 16),
        ):
            with self.subTest(prefix=prefix):
                result = scan_text(f"Credential: {prefix}{body}")
                self.assertIn("bare-token-prefix", {f.rule_id for f in result.findings})

    def test_provider_prefix_negatives(self) -> None:
        for text in ("ghp_short", "github_pat_example", "xoxb-short", "sk-live-demo", "AKIAEXAMPLE"):
            with self.subTest(text=text):
                self.assertNotIn("bare-token-prefix", {f.rule_id for f in scan_text(text).findings})

    def test_worklore_provider_families_preserve_context_and_redact_whole_tokens(self) -> None:
        for prefix, body in (
            ("ASIA", "A" * 16),
            ("AIza", "a" * 30),
            ("AIza", "a" * 28 + "_-"),
            ("glpat-", "a" * 16),
            ("glpat-", "a" * 14 + "-_"),
        ):
            credential = prefix + body
            for before, after, expected in (
                ("Credential: ", ".", "bare-token-prefix"),
                ("(", ")", "bare-token-prefix"),
                ('api_key="', '"', "api-key-assignment"),
                ("token='", "'", "api-key-assignment"),
                ("Bearer ", "", "bearer-token"),
            ):
                with self.subTest(prefix=prefix, body=body, expected=expected, before=before):
                    result = scan_text(before + credential + after)
                    self.assertEqual([finding.rule_id for finding in result.findings], [expected])
                    self.assertNotIn(credential, result.redacted_text)
                    if expected == "bare-token-prefix":
                        self.assertEqual(result.findings[0].match, credential)
                        self.assertEqual(result.redacted_text, before + "[redacted-secret]" + after)
                    elif expected == "bearer-token":
                        self.assertEqual(result.findings[0].match, before + credential)
                        self.assertEqual(result.redacted_text, before + "[redacted-secret]" + after)

    def test_bearer_provider_tokens_redact_terminal_hyphens_completely(self) -> None:
        for prefix, minimum in (("AIza", 30), ("glpat-", 16)):
            for length in (minimum, minimum + 1, minimum + 16):
                credential = prefix + "a" * (length - 1) + "-"
                for before, after in (("Bearer ", ""), ("Bearer ", "."), ("(Bearer ", ")")):
                    with self.subTest(prefix=prefix, length=length, before=before, after=after):
                        result = scan_text(before + credential + after)
                        self.assertEqual([finding.rule_id for finding in result.findings], ["bearer-token"])
                        self.assertEqual(result.findings[0].match, "Bearer " + credential)
                        self.assertEqual(result.redacted_text, before + "[redacted-secret]" + after)

    def test_bearer_token_minimum_length_with_terminal_hyphen(self) -> None:
        for length in (19, 20, 21):
            credential = "a" * (length - 1) + "-"
            with self.subTest(length=length):
                result = scan_text("Bearer " + credential)
                if length < 20:
                    self.assertEqual(result.findings, [])
                    self.assertEqual(result.redacted_text, "Bearer " + credential)
                else:
                    self.assertEqual([finding.rule_id for finding in result.findings], ["bearer-token"])
                    self.assertEqual(result.findings[0].match, "Bearer " + credential)
                    self.assertEqual(result.redacted_text, "Bearer [redacted-secret]")

    def test_bearer_ascii_token_boundaries_preserve_unicode_context(self) -> None:
        for terminal in ("+", "/", "=", "~", "-"):
            for length in (19, 20, 21):
                credential = "a" * (length - 1) + terminal
                for following in ("é", "λ", "中", ".", ""):
                    with self.subTest(terminal=terminal, length=length, following=following):
                        text = "Bearer " + credential + following
                        result = scan_text(text)
                        if length < 20:
                            self.assertEqual(result.findings, [])
                            self.assertEqual(result.redacted_text, text)
                        else:
                            self.assertEqual([finding.rule_id for finding in result.findings], ["bearer-token"])
                            self.assertEqual(result.findings[0].match, "Bearer " + credential)
                            self.assertEqual(result.redacted_text, "Bearer [redacted-secret]" + following)

    def test_worklore_provider_short_prefixes_and_embedded_tokens_do_not_match(self) -> None:
        for text in (
            "AsiaPacificCustomers",
            "ASIA" + "a" * 16,
            "asia" + "A" * 16,
            "aiza" + "a" * 30,
            "GLPAT-" + "a" * 16,
            "ASIAEXAMPLE",
            "ASIA" + "A" * 15,
            "ASIA" + "A" * 17,
            "ASIA" + "A" * 16 + "_",
            "AIza" + "a" * 29,
            "glpat-demo",
            "glpat-" + "a" * 15,
            "xASIA" + "A" * 16,
            "_ASIA" + "A" * 16,
            "xAIza" + "a" * 30,
            "_AIza" + "a" * 30,
            "xglpat-" + "a" * 16,
            "_glpat-" + "a" * 16,
        ):
            with self.subTest(text=text):
                self.assertNotIn("bare-token-prefix", {f.rule_id for f in scan_text(text).findings})

    def test_private_hostname_without_hyphen_and_nested_labels(self) -> None:
        for host in (
            "db.internal",  # content-guard: allow private-hostname
            "nas.local",  # content-guard: allow private-hostname
            "ci.lan",  # content-guard: allow private-hostname
            "db.corp",  # content-guard: allow private-hostname
            "nas.home",  # content-guard: allow private-hostname
            "wiki.intranet",  # content-guard: allow private-hostname
            "db.localdomain",  # content-guard: allow private-hostname
            "DB.INTERNAL",  # content-guard: allow private-hostname
            "build.agent.internal",  # content-guard: allow private-hostname
        ):
            with self.subTest(host=host):
                result = scan_text(f"Connect to {host}.")
                self.assertIn("private-hostname", {f.rule_id for f in result.findings})

    def test_hostname_boundaries_and_python_calls_do_not_match(self) -> None:
        for text in (
            "build-agent.internal.example.com",
            "build-agent.localhost",
            "build-agent.internality",
            "build-agent.internal_module",
            "build-agent.internal.py",
            "-build-agent.internal",
            "build_agent.internal",
            "threading.local()",
            "Path.home()",
        ):
            with self.subTest(text=text):
                self.assertNotIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_json_encoded_windows_home_path_matches(self) -> None:
        result = scan_text(r'"Config at C:\\Users\\demo\\.config\\tool.json"')  # content-guard: allow windows-home-path
        self.assertIn("windows-home-path", {f.rule_id for f in result.findings})

    def test_custom_hostname_rule_keeps_code_shaped_matches(self) -> None:
        text = "obj.local"  # content-guard: allow private-hostname
        self.assertEqual(scan_text(text).findings, [])
        policy = Policy(
            custom_rules=[
                Rule(
                    id="private-hostname",
                    category="infrastructure",
                    pattern=r"\bobj\.local\b",
                    replacement="[redacted-host]",
                )
            ]
        )
        result = scan_text(text, policy=policy)
        self.assertEqual([finding.rule_id for finding in result.findings], ["private-hostname"])
        self.assertTrue(result.blocked)
        self.assertEqual(result.redacted_text, "[redacted-host]")

    def test_hostname_code_contexts_are_skipped(self) -> None:
        for text in (
            "if not obj.local:",  # content-guard: allow private-hostname
            "return obj.local",  # content-guard: allow private-hostname
            "obj.local",  # content-guard: allow private-hostname
            "self.home = target",  # content-guard: allow private-hostname
            "callback(obj.local)",  # content-guard: allow private-hostname
            "call(tc.home, other)",  # content-guard: allow private-hostname
            "const value = opts.local ? yes : no;",  # content-guard: allow private-hostname
            "fs.mkdirSync(fresh.home);",  # content-guard: allow private-hostname
            "[seats.home]",
            "f'path = {self.home}'",
        ):
            with self.subTest(text=text):
                self.assertNotIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_hostname_text_url_and_config_contexts_match(self) -> None:
        for text in (
            '"db.local"',  # content-guard: allow private-hostname
            "'db.internal'",  # content-guard: allow private-hostname
            "https://db.home/path",  # content-guard: allow private-hostname
            "server=db.lan",  # content-guard: allow private-hostname
            "server: db.corp",  # content-guard: allow private-hostname
            "Reach db.internal for logs.",  # content-guard: allow private-hostname
            "db.local is reachable.",  # content-guard: allow private-hostname
        ):
            with self.subTest(text=text):
                self.assertIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_hostname_prose_punctuation_remains_detectable(self) -> None:
        for text in (
            "Database: db.local, then continue.",  # content-guard: allow private-hostname
            "Connect to db.local, then continue.",  # content-guard: allow private-hostname
            "Use (db.local) for logs.",  # content-guard: allow private-hostname
        ):
            with self.subTest(text=text):
                self.assertIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_spaced_interpolation_remains_code_shaped(self) -> None:
        result = scan_text("f'{ self.home }'")
        self.assertNotIn("private-hostname", {f.rule_id for f in result.findings})

    def test_shell_prefixed_hostname_assignments_match(self) -> None:
        """#1920: export/readonly and batch set still declare host config."""
        host = "deploy" + ".internal"
        for before in (
            "export HOSTNAME=",
            "readonly server=",
            "set HOST=",
            "set HOSTNAME=",
        ):
            with self.subTest(before=before):
                text = before + host
                self.assertEqual([f.rule_id for f in scan_text(text).findings], ["private-hostname"])
                redacting = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
                self.assertEqual(redacting.redacted_text, before + "[redacted-host]")

    def test_shell_prefixed_arbitrary_and_code_assignments_stay_skipped(self) -> None:
        host = "deploy" + ".internal"
        for before, after in (
            ("export ARBITRARY_VALUE=", ""),
            ("export DATABASE_HOSTNAME_SUFFIX=", ""),
            ("obj.DATABASE_HOST=", ";"),
            ("callback(", ")"),
            ("const value = ", " ? yes : no;"),
            ("offset host=", ""),
        ):
            with self.subTest(before=before, after=after):
                text = before + host + after
                self.assertNotIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_url_interpolated_member_expressions_stay_code_shaped(self) -> None:
        """#1918: the URL-context acceptance must not run before interpolation
        rejection, or a formatted-URL member expression becomes a finding."""
        for text in (
            'f"https://{db.local}/path"',  # content-guard: allow private-hostname
            'f"https://{cfg.db.local}/path"',  # content-guard: allow private-hostname
            "f'https://{db.local}/path'",  # content-guard: allow private-hostname
            'url = f"https://{db.local}/api"',  # content-guard: allow private-hostname
            'rf"https://{db.local}/path"',  # content-guard: allow private-hostname
            'fr"https://{db.local}/path"',  # content-guard: allow private-hostname
            # A triple-quoted formatted string, and an earlier field.
            'f"""https://{cfg.db.local}/path"""',  # content-guard: allow private-hostname
            'f"https://{d[1:2]}/{cfg.db.local}"',  # content-guard: allow private-hostname
            # Whitespace, '=', conversions, format specs, and an escaped brace before the field.
            'f"https://{ cfg.db.local }/p"',  # content-guard: allow private-hostname
            'f"https://{cfg.db.local=}/p"',  # content-guard: allow private-hostname
            'f"https://{cfg.db.local!r}/p"',  # content-guard: allow private-hostname
            'f"https://{cfg.db.local:>{width}}/p"',  # content-guard: allow private-hostname
            'f"https://{{{cfg.db.local}}}/p"',  # content-guard: allow private-hostname
            # Common expression positions for the f-string itself.
            'requests.get(f"https://{cfg.db.local}/x")',  # content-guard: allow private-hostname
            'urls = [f"https://{cfg.db.local}/x"]',  # content-guard: allow private-hostname
            'return f"https://{cfg.db.local}/x"',  # content-guard: allow private-hostname
            'get(url=f"https://{cfg.db.local}/x")',  # content-guard: allow private-hostname
            'x = 1; return f"https://{cfg.db.local}/x"',  # content-guard: allow private-hostname
        ):
            with self.subTest(text=text):
                result = scan_text(text)
                self.assertNotIn("private-hostname", {f.rule_id for f in result.findings})
                redacting = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
                self.assertEqual(redacting.redacted_text, text)

    def test_quoted_hosts_inside_url_interpolation_still_match(self) -> None:
        for text in (
            "f\"{fetch('https://db.home/path')}\"",  # content-guard: allow private-hostname
            "f\"https://{lookup('db.home')}/path\"",  # content-guard: allow private-hostname
            # A literal brace or an escaped quote earlier in the expression.
            "f\"https://{lookup('{','db.home')}/path\"",  # content-guard: allow private-hostname
            "f\"https://{lookup('can\\'t','db.home')}/path\"",  # content-guard: allow private-hostname
            # Doubled braces are literal text, not an expression (#1962).
            'print(f"{{")  # see https://db.home/x',  # content-guard: allow private-hostname
            # An f-prefix inside an ordinary string does not open a field.
            'hint = "start with f\'{name"; url = "https://db.home/x"',  # content-guard: allow private-hostname
            # Comment text is not lexed, and a format spec is literal text.
            "# start with f'{name; see https://db.home/x",  # content-guard: allow private-hostname
            'f"result={value:https://db.home/path}"',  # content-guard: allow private-hostname
            # A comment inside a multi-line field, and a backslash before a doubled brace.
            'x = f"""{(\n  a # see https://db.home/x\n)}"""',  # content-guard: allow private-hostname
            'rf"https://\\{{db.home}}/path"',  # content-guard: allow private-hostname
            # A line inside an earlier line's triple-quoted ordinary string.
            'hint = """\nf"{(use https://db.home/x\n"""',  # content-guard: allow private-hostname
            # Incomplete f-string examples in other comment forms, backtick spans,
            # and backslash-continued strings never close a field.
            '// start with f"{(use https://db.home/x',  # content-guard: allow private-hostname
            'Use `f"{(use` then https://db.home/x',  # content-guard: allow private-hostname
            's = "abc \\\nf"{(use https://db.home/x"',  # content-guard: allow private-hostname
            # A complete-looking example in a comment is still comment text.
            '// f"{(use https://db.home/x)}"',  # content-guard: allow private-hostname
            # Only a field holding the host alone is skipped; anything else in a
            # formatted URL keeps the URL detection main already had.
            "f\"https://{resolve('https://fallback.invalid',db.home)}/path\"",  # content-guard: allow private-hostname
            'rf"\\"https://{db.home}/path\\""',  # content-guard: allow private-hostname
            's = f"""\nhttps://{db.home}/p\n"""',  # content-guard: allow private-hostname
            'f"https://{db.home[0]}/p"',  # content-guard: allow private-hostname
            'f"https://{db.home or x}/p"',  # content-guard: allow private-hostname
            '"https://{db.home}/p"',  # content-guard: allow private-hostname
            "`https://${db.home}/p`",  # content-guard: allow private-hostname
            # f-string text inside an ordinary string or a comment is not an f-string.
            "hint = 'f\"https://{db.home}/path\"'",  # content-guard: allow private-hostname
            '# f"https://{db.home}/path"',  # content-guard: allow private-hostname
            '// f"https://{db.home}/path"',  # content-guard: allow private-hostname
            # A nested f-string inside another literal keeps main's detection.
            "f\"https://{f'{db.home}'}/\"",  # content-guard: allow private-hostname
            # A complete f-string example in prose or after a line-start comment.
            'REM f"https://{db.home}/x"',  # content-guard: allow private-hostname
            'Use f"https://{db.home}/x" for that.',  # content-guard: allow private-hostname
            '; f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '; url = f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '* f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '-- f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '<!-- f"https://{db.home}/x" -->',  # content-guard: allow private-hostname
            'For example, f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '(see f"https://{db.home}/x")',  # content-guard: allow private-hostname
            'Note: f"https://{db.home}/x"',  # content-guard: allow private-hostname
            '[f"https://{db.home}/x"](link)',  # content-guard: allow private-hostname
            '{"url": f"https://{db.home}/x"}',  # content-guard: allow private-hostname
            'base + f"https://{db.home}/x"',  # content-guard: allow private-hostname
            'Please return f"https://{db.home}/x"',  # content-guard: allow private-hostname
            'const x = 1; /* sample = f"https://{db.home}/x" */',  # content-guard: allow private-hostname
        ):
            with self.subTest(text=text):
                self.assertIn("private-hostname", {f.rule_id for f in scan_text(text).findings})
                redacting = scan_text(text, policy=Policy(rules={"private-hostname": "redact"}))
                self.assertNotIn("db.home", redacting.redacted_text)  # content-guard: allow private-hostname

    def test_literal_url_hostname_context_still_matches(self) -> None:
        # Controls from the existing detection surface, next to the new skips.
        for text in (
            "https://db.home/path",  # content-guard: allow private-hostname
            '"https://db.home/path"',  # content-guard: allow private-hostname
            # An f-string or brace elsewhere on the line must not hide the URL host.
            'print(f"x"); cfg = {"url": "https://db.home/x"}',  # content-guard: allow private-hostname
            'requests.get(f"{base}/x", json={"cb": "https://db.home/cb"})',  # content-guard: allow private-hostname
            '{"f": 1, "cfg": {"url": "https://db.home/x"}}',  # content-guard: allow private-hostname
        ):
            with self.subTest(text=text):
                self.assertIn("private-hostname", {f.rule_id for f in scan_text(text).findings})

    def test_nested_interpolation_remains_code_shaped(self) -> None:
        result = scan_text(
            'lines.append(f\'token_file = "{(self.home / "node.token").as_posix()}"\')'  # content-guard: allow private-hostname
        )
        self.assertNotIn("private-hostname", {f.rule_id for f in result.findings})
