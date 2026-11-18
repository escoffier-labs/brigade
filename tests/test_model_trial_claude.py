"""Live paired adapter, driven by a fake `claude` that emits recorded-style stream-json."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from brigade import cli, model_trial_claude, model_trials
from brigade.memory_verify_seat import CLAUDE_SETTINGS
from brigade.model_trial_claude import (
    ADAPTER_ID,
    ENV_PASSTHROUGH,
    FIXED_ENV,
    LIVE_SCHEMA,
    PRELUDE,
    QUALIFIED,
    TOKEN_ENV,
    _Observer,
    _run_process,
    ancestor_instruction_file,
    build_argv,
    build_env,
    execute_live as _execute_live,
    module_hashes,
)
from brigade.model_trial_paired import TOOLS, digest
from tests.paired_live_fake import GOOD_STEPS, make_fake, records

pytestmark = pytest.mark.allow_agent_cli  # launches only the fake claude from tests/paired_live_fake.py

FIXTURES = Path(__file__).parents[1] / "src/brigade/fixtures/paired-brigade-work"
LIVE = FIXTURES / "live-claude.json"
SECRET_TOKEN = "sentinel-oauth-token-value"
LAUNCH = (sys.executable,)  # The fake is a script; a launcher prefix runs it the same way on every platform.
WINDOWS_BASICS = {
    k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ
}
ENVIRON = {
    **WINDOWS_BASICS,
    "PATH": "/usr/bin:/bin",
    "CLAUDE_CODE_OAUTH_TOKEN": SECRET_TOKEN,
    "ANTHROPIC_API_KEY": "sentinel-api-key",
    "ANTHROPIC_AUTH_TOKEN": "sentinel-auth",
    "UNRELATED_SECRET": "sentinel-unrelated",
    "HOME": "/parent-home",
}


def execute_live(*args, **kwargs):
    return _execute_live(*args, launcher=LAUNCH, **kwargs)


@pytest.fixture(scope="module", autouse=True)
def clean_temp_root():
    """Arm cwds are made under the temp dir, and the adapter refuses ancestors holding CLAUDE.md or AGENTS.md."""
    root = None
    for base in (Path(tempfile.gettempdir()), Path("/tmp")):
        if base.is_dir():
            candidate = Path(tempfile.mkdtemp(prefix="paired-live-", dir=base))
            if ancestor_instruction_file(candidate) is None:
                root = candidate
                break
            shutil.rmtree(candidate)
    if root is None:
        pytest.skip("no temp directory without CLAUDE.md or AGENTS.md in its ancestors")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(root))
        yield root
    shutil.rmtree(root, ignore_errors=True)


def _live(tmp_path, fake_dir="fake", output="evidence", **scenario):
    exe = make_fake(tmp_path / fake_dir, **scenario)
    result = execute_live(LIVE, tmp_path / output, claude_bin=str(exe), environ=ENVIRON, timeout=60)
    return result, tmp_path / fake_dir, tmp_path / output


def _failures(result):
    return sorted({a["failure"] for a in result["attempts"]})


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("good")
    return _live(tmp, steps=GOOD_STEPS, end_tool_listed=True, end_conversation=True) + (tmp,)


def test_good_pair_runs_through_real_server_and_validates(good):
    result, fake_dir, output, _ = good
    assert result["schema"] == LIVE_SCHEMA
    assert result["plan"]["adapter"]["id"] == ADAPTER_ID != "brigade.offline-replay.v1"
    assert (result["live"], result["model_executed"], result["promotion_eligible"]) == (True, True, False)
    assert result["plan"]["model_executed"] is True
    assert all(a["failure"] is None and a["grade"]["passed"] for a in result["attempts"])
    assert result["pairs"][0]["comparison"] == "both_pass"
    digest = (output / "evidence.sha256").read_text().strip()
    assert model_trials.paired_validate(output, digest)["live"] is True  # live-aware dispatch


def test_tool_use_maps_into_item_tool_call_envelope(good):
    result = good[0]
    attempt = result["attempts"][0]
    session = attempt["live"]["init"]["session_id"]
    for row, observed in zip(attempt["trace"], attempt["live"]["observed_calls"], strict=True):
        params = observed["params"]
        assert observed["method"] == "item/tool/call" and params["callId"] == observed["id"]
        assert params["threadId"] == session and params["turnId"]
        assert row["request_envelope"] == observed and row["tool"] == params["tool"]
        assert row["response_envelope"]["id"] == observed["id"]
    assert [r["tool"] for r in attempt["trace"]] == ["brief", "verify", "receipt", "scoreability", "capture"]


def test_end_conversation_is_an_observed_control_event_never_graded(good):
    attempt = good[0]["attempts"][0]
    assert [c["tool"] for c in attempt["live"]["control_events"]] == ["EndConversation"]
    assert "EndConversation" not in [r["tool"] for r in attempt["trace"]]
    assert "EndConversation" in attempt["live"]["init"]["tools"]
    assert attempt["grade"]["passed"] is True


def test_each_arm_is_fresh_and_the_child_env_is_allowlisted(good):
    _, fake_dir, _, _ = good
    runs = records(fake_dir)
    assert len(runs) == 2
    assert len({r["session_id"] for r in runs}) == 2
    assert len({r["cwd"] for r in runs}) == 2
    assert len({r["env"]["CLAUDE_CONFIG_DIR"] for r in runs}) == 2
    for run in runs:
        env = run["env"]
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
        assert "UNRELATED_SECRET" not in env and env["HOME"] != "/parent-home"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET_TOKEN
        assert env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1" and env["DISABLE_TELEMETRY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1" and env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        allowed = {*ENV_PASSTHROUGH, *FIXED_ENV, TOKEN_ENV, "HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "TMPDIR"}
        allowed |= {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
        assert set(env) <= allowed | {"PWD", "SHLVL", "_", "LC_CTYPE", "LANG"}, sorted(set(env) - allowed)
        assert not [key for key in env if key.startswith("ANTHROPIC_")]
        assert env["USERPROFILE"] == env["HOME"]
        assert list(run["config"]["mcpServers"]) == ["brigade-paired"]


def test_argv_has_the_exclusive_flags_and_none_of_the_forbidden_ones(good):
    argv = records(good[1])[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    for flag in (
        "-p",
        "--verbose",
        "--include-hook-events",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--no-chrome",
        "--restricted",
    ):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json" and argv[argv.index("--model") + 1] == "opus"
    assert "--safe-mode" not in argv and "--bare" not in argv
    assert argv[argv.index("--settings") + 1] == CLAUDE_SETTINGS  # every builtin plugin disabled
    assert argv[argv.index("--allowedTools") + 1].split(",") == list(QUALIFIED)
    assert sorted(QUALIFIED) == sorted(f"mcp__brigade-paired__{tool}" for tool in TOOLS) and len(QUALIFIED) == 5
    assert not any(SECRET_TOKEN in part for part in argv)
    assert argv[-3:] == ["--disallowedTools", "Task", "Agent"]  # variadic flag stays last
    assert "mcp__*" not in " ".join(argv)


def test_arm_system_prompts_differ_only_in_skill_bytes_and_hashes_are_in_the_cell(good):
    result, fake_dir, _, _ = good
    prompts = {r["session_id"]: r["system_prompt"] for r in records(fake_dir)}
    by_arm = {a["cell"]["arm"]: a for a in result["attempts"]}
    skills = {arm: (FIXTURES / arm / "SKILL.md").read_text() for arm in ("baseline", "candidate")}
    for arm, attempt in by_arm.items():
        prompt = prompts[attempt["live"]["init"]["session_id"]]
        assert prompt.startswith(PRELUDE) and prompt[len(PRELUDE) :] == skills[arm]
        identity = attempt["cell"]["adapter"]["arms"][arm]
        import hashlib

        assert identity["system_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        assert identity["skill_sha256"] == hashlib.sha256(skills[arm].encode()).hexdigest()
        assert attempt["cell"]["adapter"]["prelude_sha256"] == hashlib.sha256(PRELUDE.encode()).hexdigest()
    assert skills["baseline"] != skills["candidate"]
    assert records(fake_dir)[0]["stdin"] == records(fake_dir)[1]["stdin"]  # same user message in both arms


def test_the_oauth_token_never_reaches_evidence(good):
    output = good[2]
    for path in output.rglob("*"):
        if path.is_file():
            assert SECRET_TOKEN.encode() not in path.read_bytes(), path


def test_stale_cwd_and_state_do_not_survive_the_arm(good):
    run = records(good[1])[0]
    assert not Path(run["cwd"]).exists()


GATE_CASES = {
    "extra builtin tool": {"init": {"tools": ["Bash"]}},
    "extra mcp tool": {"init": {"tools": ["mcp__brigade-paired__brief", "mcp__other__x"]}},
    "missing paired tool": {"init": {"tools": ["mcp__brigade-paired__brief"]}},
    "extra server": {
        "init": {
            "mcp_servers": [
                {"name": "brigade-paired", "status": "connected"},
                {"name": "other", "status": "connected"},
            ]
        }
    },
    "server not connected": {"init": {"mcp_servers": [{"name": "brigade-paired", "status": "failed"}]}},
    "mcp_server_errors": {"init": {"mcp_server_errors": ["boom"]}},
    "wrong model": {"init": {"model": "claude-sonnet-5-5"}},
    "installed plugin": {"init": {"plugins": [{"name": "x", "path": "/users/x/.claude/plugins/x", "source": "x@m"}]}},
    "builtin plugin reported": {
        "init": {"plugins": [{"name": "cc-plugin-privacy", "path": "builtin", "source": "cc-plugin-privacy@builtin"}]}
    },
    "agents beyond built-ins": {"init": {"agents": ["general-purpose", "my-agent"]}},
    "agents missing": {"init": {"agents": None}},
    "additional directories": {"init": {"additional_directories": ["/somewhere"]}},
    "other working directory": {"init": {"cwd": "/"}},
    "permission mode": {"init": {"permissionMode": "bypassPermissions"}},
    "skills present": {"init": {"skills": ["brigade-work"]}},
    "slash commands present": {"init": {"slash_commands": ["review"]}},
    "api key auth": {"init": {"apiKeySource": "ANTHROPIC_API_KEY"}},
    "wrong session": {"init": {"session_id": "00000000-0000-4000-8000-000000000000"}},
    "cli version drift": {"init": {"claude_code_version": "9.9.9"}},
    "hook before init": {"pre_events": [{"type": "system", "subtype": "hook_started", "hook_name": "SessionStart"}]},
    "hook after init": {"post_init_events": [{"type": "system", "subtype": "hook_response"}]},
}


@pytest.mark.parametrize("name", sorted(GATE_CASES))
def test_init_gate_aborts_the_cell_before_grading(tmp_path, name):
    result, _, _ = _live(tmp_path, **GATE_CASES[name])
    assert all(a["failure"] and a["grade"]["passed"] is False for a in result["attempts"])
    assert result["pairs"][0]["comparison"] == "incomplete"
    expected = "hook_event" if "hook" in name else "init_gate:"
    assert all(f.startswith(expected) for f in _failures(result)), _failures(result)
    assert all(a["trace"] == [] and a["receipt"] is None for a in result["attempts"])


@pytest.mark.parametrize("tool", ["Bash", "Read", "Write"])
def test_hostile_transcript_requesting_builtin_tools_fails_the_cell(tmp_path, tool):
    steps = [{"call": "brief"}, {"tool_use": tool, "input": {"command": "touch escaped"}}, *GOOD_STEPS[1:]]
    result, _, _ = _live(tmp_path, steps=steps)
    assert _failures(result) == [f"forbidden_tool:{tool}"]
    assert all(a["grade"]["passed"] is False for a in result["attempts"])


def test_forbidden_mcp_tool_name_in_the_stream_fails_the_cell(tmp_path):
    result, _, _ = _live(tmp_path, steps=[{"call": "brief"}, {"emit_only": "canary"}])
    assert _failures(result) == ["forbidden_tool:mcp__brigade-paired__canary"]


def test_forbidden_mcp_call_is_rejected_logged_and_fails_the_cell(tmp_path):
    # The CLI hides the request from the stream; the server still logs and rejects it.
    result, _, output = _live(tmp_path, steps=[*GOOD_STEPS, {"server_only": "canary"}])
    assert _failures(result) == ["forbidden_mcp_call:canary"]
    attempt = result["attempts"][0]
    assert attempt["grade"]["passed"] is False
    logged = (output / "attempts" / attempt["attempt_id"] / "server-trace.jsonl").read_text()
    row = json.loads(logged.splitlines()[-1])
    assert (row["tool"], row["allowlisted"], row["response"]["error"]) == (
        "canary",
        False,
        "unsupported tool or arguments",
    )


@pytest.mark.parametrize(
    "steps, expected",
    [
        ([*GOOD_STEPS, {"server_only": "brief"}], "trace_count_mismatch:observed=5,server=6"),
        ([{"call": "brief"}, {"emit_only": "verify"}], "trace_count_mismatch:observed=2,server=1"),
        ([], None),
    ],
)
def test_server_and_adapter_counts_must_match(tmp_path, steps, expected):
    result, _, _ = _live(tmp_path, steps=steps, final={"run_id": "x", "status": "completed", "eligible": False})
    if expected is None:
        assert _failures(result) == [None]  # nothing observed, nothing logged: counts agree
        assert all(a["grade"]["passed"] is False for a in result["attempts"])  # but nothing was done
    else:
        assert _failures(result) == [expected]


def test_server_rejects_arguments_and_the_cell_does_not_pass(tmp_path):
    steps = [{"call": "brief", "input": {"x": 1}}]  # stream and server agree; the server rejects the arguments
    result, _, _ = _live(tmp_path, steps=steps)
    attempt = result["attempts"][0]
    assert attempt["trace"][0]["response"] == {"error": "unsupported tool or arguments"}
    assert attempt["grade"]["passed"] is False


@pytest.mark.parametrize(
    "step",
    [
        {"call": "brief", "stream_as": "verify"},  # stream names another tool than the server ran
        {"call": "brief", "stream_input": {"x": 1}},  # stream shows other arguments than the server got
    ],
)
def test_stream_disagreeing_with_the_server_row_is_trace_mismatch(tmp_path, step):
    result, _, _ = _live(tmp_path, steps=[step])
    assert _failures(result) == ["trace_mismatch"]
    assert all(a["trace"] == [] and a["grade"]["passed"] is False for a in result["attempts"])


def test_timeout_and_cli_errors_are_recorded_not_retried(tmp_path):
    exe = make_fake(tmp_path / "hang", hang=True)
    result = execute_live(LIVE, tmp_path / "out", claude_bin=str(exe), environ=ENVIRON, timeout=3)
    assert _failures(result) == ["model_timeout"]
    assert len(records(tmp_path / "hang")) == 2
    result, _, _ = _live(
        tmp_path,
        fake_dir="err",
        output="out2",
        is_error=True,
        result_subtype="error_during_execution",
        final="Not logged in",
    )
    assert _failures(result) == ["model_error:error_during_execution"]


def test_final_report_must_be_a_json_object_graded_against_the_receipt(tmp_path):
    result, _, _ = _live(
        tmp_path, steps=GOOD_STEPS, final={"run_id": "forged", "status": "completed", "eligible": True}
    )
    assert all(a["failure"] is None and a["grade"]["passed"] is False for a in result["attempts"])
    result, _, _ = _live(tmp_path, fake_dir="f2", output="o2", steps=GOOD_STEPS, final="all done")
    assert all(a["final"] is None and a["grade"]["passed"] is False for a in result["attempts"])


def test_resume_appends_and_refuses_any_identity_change(tmp_path):
    first, fake_dir, output = _live(tmp_path, init={"model": "wrong"})
    exe = fake_dir / "claude"
    again = execute_live(LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=60)
    assert len(again["attempts"]) == 4 and again["attempts"][:2] == first["attempts"]
    make_fake(fake_dir, init={"model": "wrong"}, version="2.1.400")
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=60)
    make_fake(fake_dir, init={"model": "wrong"})
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=61)
    copy = tmp_path / "pkg"
    shutil.copytree(FIXTURES, copy)
    first_copy = execute_live(
        copy / "live-claude.json", tmp_path / "out3", claude_bin=str(exe), environ=ENVIRON, timeout=60
    )
    assert len(first_copy["attempts"]) == 2
    skill = copy / "candidate/SKILL.md"
    skill.write_text(skill.read_text() + "\nChanged.\n")
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(copy / "live-claude.json", tmp_path / "out3", claude_bin=str(exe), environ=ENVIRON, timeout=60)


def test_tampered_live_evidence_fails_validation(good, tmp_path):
    _, _, output, _ = good
    copy = tmp_path / "copy"
    shutil.copytree(output, copy)
    evidence = copy / "evidence.json"
    payload = json.loads(evidence.read_text())
    payload["attempts"][0]["live"]["observed_calls"][0]["params"]["tool"] = "verify"
    evidence.write_text(json.dumps(payload))
    from brigade.model_trial_paired import digest

    with pytest.raises(ValueError, match="request trace"):
        model_trials.paired_validate(copy, digest(payload))
    payload = json.loads((output / "evidence.json").read_text())
    payload["promotion_eligible"] = True
    evidence.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="cannot authorize promotion"):
        model_trials.paired_validate(copy, digest(payload))


def test_live_refuses_before_launching_without_subscription_token_or_full_model(tmp_path):
    exe = make_fake(tmp_path / "never")
    env = {k: v for k, v in ENVIRON.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    with pytest.raises(ValueError, match="setup-token"):
        execute_live(LIVE, tmp_path / "o", claude_bin=str(exe), environ=env)
    with pytest.raises(ValueError, match="full model id"):
        execute_live(FIXTURES / "manifest.json", tmp_path / "o", claude_bin=str(exe), environ=ENVIRON)
    with pytest.raises(ValueError, match="not found"):
        execute_live(LIVE, tmp_path / "o", claude_bin=str(tmp_path / "missing"), environ=ENVIRON)
    assert records(tmp_path / "never") == [] and not (tmp_path / "o").exists()


def test_build_env_requires_no_parent_secrets_beyond_the_token():
    env = build_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "PATH": "/p"}, Path("/arm"))
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["PATH"] == "/p" and env["HOME"] == "/arm/home"
    assert env["USERPROFILE"] == env["HOME"]


def test_argv_builder_effort_is_optional():
    kwargs = dict(session_id="s", mcp_config=Path("m"), system_prompt_file=Path("p"), cli_model="opus")
    assert "--effort" not in build_argv("claude", effort=None, **kwargs)
    argv = build_argv("claude", effort="medium", **kwargs)
    assert argv[argv.index("--effort") + 1] == "medium" and argv[-3:] == ["--disallowedTools", "Task", "Agent"]


@pytest.fixture
def cli_launcher(monkeypatch):
    """The CLI has no launcher flag; route its live call through the script launcher."""
    real = model_trials.paired_live
    monkeypatch.setattr(model_trials, "paired_live", lambda *a, **k: real(*a, launcher=LAUNCH, **k))


def test_cli_paired_live_exits_2_without_a_token_and_runs_with_one(tmp_path, monkeypatch, capsys, cli_launcher):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    args = ["model", "trial", "paired-live", str(LIVE), "--output-dir", str(tmp_path / "o")]
    assert cli.main(args) == 2
    assert "No model was launched" in capsys.readouterr().err
    exe = make_fake(tmp_path / "cli", init={"model": "wrong"})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET_TOKEN)
    assert cli.main([*args, "--claude-bin", str(exe), "--case", "passing-verify", "--timeout", "30"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["live"] is True and printed["promotion_eligible"] is False
    assert cli.main([*args, "--claude-bin", str(exe), "--case", "audit-only"]) == 2  # case not in manifest


def test_cli_exits_2_when_claude_version_never_returns(tmp_path, monkeypatch, capsys, cli_launcher):
    monkeypatch.setattr(model_trial_claude, "VERSION_TIMEOUT", 1.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET_TOKEN)
    exe = make_fake(tmp_path / "vh", version_hang=True)
    args = ["model", "trial", "paired-live", str(LIVE), "--output-dir", str(tmp_path / "o"), "--claude-bin", str(exe)]
    assert cli.main(args) == 2
    assert "timed out" in capsys.readouterr().err
    assert records(tmp_path / "vh") == []


def test_offline_replay_is_unchanged_and_not_live(tmp_path):
    from brigade.model_trial_paired import replay_session

    script = json.loads((FIXTURES / "replay.json").read_text())
    result = model_trials.paired_execute(
        FIXTURES / "manifest.json", tmp_path, replay_session(script), cases=["audit-only"]
    )
    assert result["schema"] == "brigade.paired_trial.v2"
    assert result["plan"]["adapter"]["id"] == "brigade.offline-replay.v1"
    assert (result["live"], result["model_executed"], result["promotion_eligible"]) == (False, False, False)
    assert model_trials.paired_validate(tmp_path, result["evidence_sha256"])["live"] is False


GOOD_INIT = {
    "type": "system",
    "subtype": "init",
    "tools": list(QUALIFIED),
    "mcp_servers": [{"name": "brigade-paired", "status": "connected"}],
    "model": "claude-opus-5-5",
    "permissionMode": "dontAsk",
    "slash_commands": [],
    "skills": [],
    "plugins": [],
    "agents": ["Plan"],
    "apiKeySource": "none",
    "claude_code_version": "2.1.327",
    "session_id": "sid",
}


def _observer(tmp_path):
    observer = _Observer(expected_model="claude-opus-5-5", session_id="sid", version="2.1.327", cwd=tmp_path)
    assert observer.feed({**GOOD_INIT, "cwd": str(tmp_path)}) is False
    return observer


def _assistant(*blocks):
    return {"type": "assistant", "uuid": "turn-1", "session_id": "sid", "message": {"content": list(blocks)}}


def _block(kind="tool_use", name="mcp__brigade-paired__brief", call_id="toolu_1", **extra):
    return {"type": kind, "name": name, "id": call_id, "input": {}, **extra}


@pytest.mark.parametrize("kind", ["server_tool_use", "mcp_tool_use"])
def test_observer_refuses_server_and_mcp_tool_blocks_not_just_tool_use(tmp_path, kind):
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block(kind, name="web_search"))) is True
    assert observer.failure == "forbidden_tool:web_search"
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block(kind))) is True  # even with a paired name, the block type is wrong
    assert observer.failure == f"forbidden_tool_type:{kind}"


@pytest.mark.parametrize("bad_id", [None, 7, "", ["x"]])
def test_observer_aborts_on_a_missing_or_non_string_tool_id(tmp_path, bad_id):
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block(call_id=bad_id))) is True
    assert observer.failure == "malformed_tool_use" and observer.observed == []


def test_observer_aborts_on_a_reused_id_and_checks_the_name_before_dedupe(tmp_path):
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block())) is False
    assert observer.feed(_assistant(_block())) is True and observer.failure == "malformed_tool_use"
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block())) is False
    assert observer.feed(_assistant(_block(name="Bash"))) is True  # reused id must not hide a forbidden tool
    assert observer.failure == "forbidden_tool:Bash"
    observer = _observer(tmp_path)
    assert observer.feed(_assistant(_block(name="Bash", call_id=None))) is True
    assert observer.failure == "forbidden_tool:Bash"


def test_a_malformed_tool_id_in_the_stream_fails_the_cell(tmp_path):
    steps = [{"call": "brief"}, {"block": {"type": "tool_use", "name": "mcp__brigade-paired__verify", "input": {}}}]
    result, _, _ = _live(tmp_path, steps=steps)
    assert _failures(result) == ["malformed_tool_use"]


def test_ancestor_instruction_files_fail_the_cell_without_launching(tmp_path, monkeypatch):
    root = tmp_path / "temp-root"
    root.mkdir()
    (root / "AGENTS.md").write_text("Always do X.\n")
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    assert ancestor_instruction_file(root / "a" / "b") == root / "AGENTS.md"
    result, fake_dir, _ = _live(tmp_path, steps=GOOD_STEPS)
    assert _failures(result) == ["ancestor_instructions:AGENTS.md"]
    assert records(fake_dir) == []  # the CLI never started
    assert all(a["trace"] == [] and a["grade"]["passed"] is False for a in result["attempts"])
    (root / "AGENTS.md").unlink()
    (root / "CLAUDE.md").write_text("x")
    result, _, _ = _live(tmp_path, fake_dir="again", output="again-out", steps=GOOD_STEPS)
    assert _failures(result) == ["ancestor_instructions:CLAUDE.md"]


def test_a_cli_echoing_the_token_never_persists_it(tmp_path):
    result, _, output = _live(tmp_path, steps=GOOD_STEPS, leak=True)
    assert all(a["failure"] is None for a in result["attempts"])
    saved = 0
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert SECRET_TOKEN.encode() not in data, path
            saved += b"[redacted]" in data
    assert saved >= 4  # stream.jsonl and stderr.txt of both arms carried the marker
    digest_value = (output / "evidence.sha256").read_text().strip()
    assert model_trials.paired_validate(output, digest_value)["live"] is True  # hashes match redacted bytes


def test_module_hashes_cover_every_module_on_the_live_path():
    here = Path(model_trial_claude.__file__).parent
    hashes = module_hashes()
    assert {
        "model_trial_claude.py",
        "model_trial_paired.py",
        "paired_mcp.py",
        "mcp_server.py",
        "paired_output.py",
        "skills_cmd/registry.py",
    } <= set(hashes)
    assert all(hashlib.sha256((here / name).read_bytes()).hexdigest() == value for name, value in hashes.items())


def test_editing_a_live_module_refuses_validation(good, tmp_path, monkeypatch):
    output = good[2]
    edited = {**module_hashes(), "mcp_server.py": "0" * 64}
    monkeypatch.setattr(model_trial_claude, "module_hashes", lambda: edited)
    with pytest.raises(ValueError, match="implementation changed"):
        model_trials.paired_validate(output, (output / "evidence.sha256").read_text().strip())


def test_revalidation_uses_the_stored_requested_session_not_init(good, tmp_path):
    output = good[2]
    copy = tmp_path / "copy"
    shutil.copytree(output, copy)
    payload = json.loads((copy / "evidence.json").read_text())
    live = payload["attempts"][0]["live"]
    assert live["session_id"] == live["init"]["session_id"] and live["cwd"] == live["init"]["cwd"]
    live["session_id"] = "00000000-0000-4000-8000-000000000000"  # init still names the original session
    (copy / "evidence.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="not observed"):
        model_trials.paired_validate(copy, digest(payload))


def test_revalidation_checks_the_stored_cwd_against_init(good, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(good[2], copy)
    payload = json.loads((copy / "evidence.json").read_text())
    payload["attempts"][0]["live"]["cwd"] = "/elsewhere"
    (copy / "evidence.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="not observed"):
        model_trials.paired_validate(copy, digest(payload))


def test_expiry_cannot_flip_a_finished_cell_or_signal_a_recycled_group(monkeypatch, tmp_path):
    events, callbacks, kills = [], [], []

    class StubTimer:
        daemon = False

        def __init__(self, interval, function):
            callbacks.append(function)

        def start(self):
            events.append("start")

        def cancel(self):
            events.append("cancel")

    class SpyPopen(subprocess.Popen):
        def wait(self, *args, **kwargs):
            events.append("wait")
            return super().wait(*args, **kwargs)

    monkeypatch.setattr(model_trial_claude.threading, "Timer", StubTimer)
    monkeypatch.setattr(model_trial_claude.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(model_trial_claude, "_kill", lambda proc: kills.append(proc.pid))
    observer = _Observer(expected_model="m", session_id="s", version="v", cwd=tmp_path)
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    _, code, timed_out = _run_process(
        [sys.executable, "-c", "pass"], env, tmp_path, "", 30, observer, tmp_path / "err.txt", "tok"
    )
    assert (code, timed_out) == (0, False)
    assert events.index("cancel") < events.index("wait")  # the timer is gone before we reap
    before = list(kills)
    callbacks[0]()  # a late expiry for a process that already exited
    assert kills == before
