"""S18 reuses Dot's prompt-load boundary for local and hub memory."""

import json
from importlib.resources import files

from brigade.dot.prompt_scan import blocked_rule
from brigade.guard import Policy
from brigade.untrusted import _LINE_RULES


def test_s18_prompt_load_withholds_warnings_and_new_role_overrides():
    assert blocked_rule(["e\u0301 and a leading BOM", "\ufeffordinary text"]) is None
    for text, rule in [
        ("join\u200cer", "zero-width-joiner"),
        ("join\u200der", "zero-width-joiner"),
        ("person@example.com", "example-email-reserved"),
        ("new system prompt", "role-override"),
    ]:
        assert blocked_rule([text]) == rule, ascii(text)

    bundle = json.loads(files("brigade.guard").joinpath("memory-rules.json").read_text(encoding="utf-8"))
    policy = Policy()
    assert bundle["rules"] == [
        {"id": rule.id, "pattern": rule.pattern, "flags": int(rule.flags), "action": policy.action_for(rule)}
        for rule in policy.all_rules()
    ]
    assert bundle["injections"][: len(_LINE_RULES)] == [
        {"id": name, "pattern": pattern.pattern, "flags": int(pattern.flags | 8), "action": "warn"}
        for name, pattern in _LINE_RULES
    ]
