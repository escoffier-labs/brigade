from pathlib import Path

from brigade.center_cmd.dashboard.views import handoff_inbox


def test_fetch_uses_handoff_lint_json(monkeypatch, tmp_path):
    expected = {"count": 1, "valid": True, "results": []}
    calls: list[tuple[Path, list[str]]] = []

    def fake_run_json(target: Path, args: list[str]) -> dict:
        calls.append((target, args))
        return expected

    monkeypatch.setattr("brigade.center_cmd.dashboard.data.run_json", fake_run_json)

    assert handoff_inbox.fetch(tmp_path) == expected
    assert calls == [(tmp_path, ["handoff", "lint"])]


def test_render_displays_queue_and_injection_count():
    payload = {
        "count": 2,
        "valid": False,
        "injection_flagged_count": 1,
        "results": [
            {
                "path": "/tmp/example-workspace/.claude/memory-handoffs/alpha.md",
                "valid": True,
                "action": "no-card",
                "errors": [],
                "warnings": [],
                "injection_signals": 0,
            },
            {
                "path": "/tmp/example-workspace/.claude/memory-handoffs/beta.md",
                "valid": False,
                "action": "create-card",
                "errors": ["missing required section: Summary"],
                "warnings": ["line 4: warning: [rule-x] suspicious text"],
                "injection_signals": 2,
            },
        ],
    }

    fragment = handoff_inbox.render(payload, "unused")

    assert "Injection-flagged handoffs" in fragment
    assert ">1<" in fragment
    assert "prompt-injection signals" in fragment
    assert "Queue summary" in fragment
    assert "Pending files" in fragment
    assert "All valid" in fragment
    assert ">no<" in fragment
    assert "alpha.md" in fragment
    assert "beta.md" in fragment
    assert "missing required section: Summary" in fragment
    assert "injection signals: 2" in fragment
    assert "create-card" in fragment


def test_render_escapes_untrusted_path_and_messages():
    payload = {
        "count": 1,
        "valid": False,
        "injection_flagged_count": 0,
        "results": [
            {
                "path": "/tmp/<script>alert(1)</script>.md",
                "valid": False,
                "action": "no-card",
                "errors": ["bad <tag>"],
                "warnings": [],
                "injection_signals": 0,
            },
        ],
    }

    fragment = handoff_inbox.render(payload, "unused")

    assert "<script>" not in fragment
    assert "&lt;script&gt;" in fragment
    assert "bad &lt;tag&gt;" in fragment


def test_render_error_and_empty_payloads_degrade_safely():
    assert "boom" in handoff_inbox.render({"error": "boom"}, "unused")

    assert "Nothing pending." in handoff_inbox.render({}, "unused")
    assert "Nothing pending." in handoff_inbox.render({"results": []}, "unused")
