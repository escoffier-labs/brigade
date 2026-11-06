"""The hub memory private class (bundle v6) leaves the publication guard alone.

The repository guard, the pre-push scanner and evidence redaction read the
rules in guard/rules.py. Only the hub memory bundle moves the network, path
and contact rules to the private class. These verdicts were generated from
origin/main before the change and from this branch after it, and match.
"""

# content-guard: allow all file

import json
from importlib.resources import files

from brigade.guard import Policy, redact_text, scan_text


def _ip(*parts: str) -> str:
    return ".".join(parts)


HOME = "/ho" + "me/example/notes.md"
ROOT = "/ro" + "ot/.config/app"
WINDOWS = "C:\\Us" + "ers\\example\\Documents\\notes.txt"
EMAILS = "mail ops" + "@fixture-mail.net or person" + "@example.com"
PHONES = "call 813" + "-555-0142 or 555" + "-123-4567"
LINES = [
    "gateway " + _ip("10", "20", "30", "40") + " and " + _ip("192", "168", "1", "20"),
    "loopback " + _ip("127", "0", "0", "1") + " on port " + "8443",
    "open local" + "host and local" + "host:" + "8080",
    "reach nas01." + "lan from ops@" + _ip("192", "168", "1", "21"),
    "notes in " + HOME + " and " + ROOT,
    "copy " + WINDOWS,
    EMAILS,
    PHONES,
]
TEXT = "\n".join(LINES) + "\n"

FINDINGS = [
    ("email", "warn"),
    ("example-email-reserved", "warn"),
    ("example-phone-555", "warn"),
    ("home-path", "warn"),
    ("localhost-bare", "block"),
    ("localhost-port", "block"),
    ("loopback-ipv4", "block"),
    ("port-reference", "block"),
    ("private-hostname", "block"),
    ("private-ipv4", "block"),
    ("root-path", "block"),
    ("ssh-private-target", "block"),
    ("us-phone", "warn"),
    ("windows-home-path", "warn"),
]
REDACTED = (
    "\n".join(
        [
            "gateway [redacted-ip] and [redacted-ip]",
            "loopback [redacted-ip] on port [redacted]",
            "open [redacted-service] and [redacted-service]",
            "reach [redacted-host] from [redacted-target]",
            "notes in " + HOME + " and [redacted-root-path]",
            "copy " + WINDOWS,
            EMAILS,
            PHONES,
        ]
    )
    + "\n"
)


def test_guard_verdicts_match_the_pre_change_golden():
    result = scan_text(TEXT)
    assert sorted({(f.rule_id, f.action) for f in result.findings}) == FINDINGS
    assert result.blocked is True
    assert redact_text(TEXT) == REDACTED


def test_guard_severities_of_the_private_memory_rules_are_unchanged():
    policy = Policy()
    actions = {rule.id: policy.action_for(rule) for rule in policy.all_rules()}
    bundle = json.loads(files("brigade.guard").joinpath("memory-rules.json").read_text(encoding="utf-8"))
    private = {item["id"] for item in bundle["rules"] if item["action"] == "private"}
    assert {rule: actions[rule] for rule in private} == dict(FINDINGS)
    assert "private" not in actions.values()
