"""Oracle browser adapter: exact argv assertions, no CLI execution.

Oracle is not installed on CI machines, so these argv shapes are confirmed
against oracle's published docs/cli-reference.md and docs/gemini.md rather than
a local `oracle --help`. That is why they live here and not in
tests/test_agents_model_pin.py, whose stated invariant is that every adapter it
covers has a confirmed model flag on an installed CLI.
"""

import json
import shutil
import sys

from brigade import agents


def _stub_proc(monkeypatch, code, stdout, stderr):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", lambda argv, **kw: agents.proc.Result(code, stdout, stderr))


def test_oracle_argv_pins_the_browser_engine():
    assert agents.build_argv("oracle", "P") == [
        "oracle",
        "--engine",
        "browser",
        "-p",
        "P",
    ]


def test_oracle_argv_is_identical_under_read_only():
    # Oracle has no filesystem write path, so read-only needs no flag and no
    # prompt instruction. The argv must not change at all.
    assert agents.build_argv("oracle", "P", read_only=True) == agents.build_argv("oracle", "P")
    assert agents.build_argv("oracle", "P", sandbox="read-only") == agents.build_argv("oracle", "P")


def test_oracle_argv_pins_model_after_the_command():
    assert agents.build_argv("oracle", "P", model="gemini-3.1-pro") == [
        "oracle",
        "--model",
        "gemini-3.1-pro",
        "--engine",
        "browser",
        "-p",
        "P",
    ]


def test_oracle_argv_never_emits_heartbeat_or_an_api_path():
    # stdout is the answer channel: --heartbeat would interleave progress lines
    # into it, and dropping --engine browser would fall back to an API key.
    for argv in (
        agents.build_argv("oracle", "P"),
        agents.build_argv("oracle", "P", read_only=True),
        agents.build_argv("oracle", "P", model="gemini-3.1-pro"),
    ):
        assert "--heartbeat" not in argv
        assert argv[argv.index("--engine") + 1] == "browser"


def test_oracle_read_only_enforcement_is_hard():
    assert agents.read_only_enforcement("oracle") == "hard"
    assert agents.read_only_enforcement("oracle", sandbox="read-only") == "hard"


def test_oracle_supports_model_pinning_but_not_reasoning():
    assert agents.supports_model_pinning("oracle") is True
    # --browser-thinking-time is deferred to the ChatGPT Pro phase.
    assert agents.supports_reasoning("oracle") is False


def test_oracle_auth_detail_points_at_pantry():
    detail = agents._oracle_auth_detail("oracle", "", "Error: not logged in to gemini.google.com")
    assert detail is not None
    assert "brigade pantry expiry-alert" in detail


def test_oracle_auth_detail_matches_expired_cookies():
    detail = agents._oracle_auth_detail("oracle", "browser session expired", "")
    assert detail is not None


def test_oracle_auth_detail_ignores_other_clis():
    # A claude seat saying "sign in" is not a pantry problem.
    assert agents._oracle_auth_detail("claude", "", "please sign in") is None


def test_oracle_auth_detail_ignores_unrelated_oracle_failures():
    assert agents._oracle_auth_detail("oracle", "", "TypeError: bad flag") is None


def test_run_agent_surfaces_oracle_auth_failure_as_browser_auth(monkeypatch):
    # Call site one: nonzero exit. A stale cookie jar is not workspace trust,
    # and mislabelling it as such poisons outcome capture and the scorecard.
    _stub_proc(monkeypatch, 1, "", "Error: not logged in to gemini.google.com")
    res = agents.run_agent("oracle", "hello")
    assert res.ok is False
    assert res.failure_phase == "provider-preflight"
    assert res.failure_kind == "browser-auth"
    assert "brigade pantry expiry-alert" in res.detail


def test_run_agent_surfaces_oracle_auth_failure_on_empty_output(monkeypatch):
    # Call site two is a separate branch: exit 0, no answer, login on stderr.
    _stub_proc(monkeypatch, 0, "", "browser session expired")
    res = agents.run_agent("oracle", "hello")
    assert res.ok is False
    assert res.failure_kind == "browser-auth"
    assert "brigade pantry expiry-alert" in res.detail


def test_run_agent_prefers_auth_diagnosis_over_workspace_trust(monkeypatch):
    # Both patterns present: auth is the more specific diagnosis and wins.
    _stub_proc(monkeypatch, 1, "", "not logged in; also workspace is not trusted")
    res = agents.run_agent("oracle", "hello")
    assert res.failure_kind == "browser-auth"


def test_run_agent_keeps_workspace_trust_for_non_oracle_seats(monkeypatch):
    # Regression guard: the oracle branch must not swallow the preflight path.
    _stub_proc(monkeypatch, 1, "", "error: workspace is not trusted; run from a Git repository")
    res = agents.run_agent("codex", "hello")
    assert res.ok is False
    assert res.failure_kind == "workspace-trust"


def test_run_agent_returns_oracle_markdown_from_stdout(monkeypatch):
    # The settled decision: stdout IS the answer channel, no extraction step.
    _stub_proc(monkeypatch, 0, "## Answer\n\nPlants convert light.\n", "")
    res = agents.run_agent("oracle", "hello")
    assert res.ok is True
    assert res.text == "## Answer\n\nPlants convert light."


def test_oracle_argv_survives_a_real_exec(tmp_path, monkeypatch):
    # Mocked proc.run cannot prove the argv reaches a real process. A stub
    # binary on PATH records what it actually received. The shebang uses an
    # absolute interpreter because PATH is narrowed to tmp_path.
    argv_log = tmp_path / "argv.json"
    stub = tmp_path / "oracle"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(argv_log)!r}, 'w'))\n"
        "print('## Answer')\n"
        "print()\n"
        "print('real exec ok')\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    # conftest's _no_managed_tools_on_path pins proc.which to None so the suite
    # behaves like a bare host. Restore real resolution, confined to tmp_path,
    # so no host binary can leak in either.
    monkeypatch.setattr(agents.proc, "which", lambda cmd, path=None: shutil.which(cmd, path=str(tmp_path)))

    res = agents.run_agent("oracle", "hello", model="gemini-3.1-pro")

    assert res.ok is True
    assert res.text == "## Answer\n\nreal exec ok"
    assert json.loads(argv_log.read_text()) == [
        "--model",
        "gemini-3.1-pro",
        "--engine",
        "browser",
        "-p",
        "hello",
    ]
