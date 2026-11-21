"""Live paired adapter, driven by a fake `claude` that emits recorded-style stream-json."""

import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from brigade import cli, model_trial_claude, model_trials, proc, proc_group
from brigade.memory_verify_seat import CLAUDE_SETTINGS
from brigade.proc_group import drain_supported
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
    _StderrCapture,
    ancestor_instruction_file,
    build_argv,
    bind_executable,
    build_env,
    execute_live as _execute_live,
    module_hashes,
)
from brigade.model_trial_paired import TOOLS, _pairs, digest
from tests._child_publish import PUBLISH_SOURCE, process_is_running
from tests._posix import requires_process_groups
from tests.paired_live_fake import GOOD_STEPS, make_fake, records, render_script

pytestmark = [
    pytest.mark.allow_agent_cli,  # launches only the fake claude from tests/paired_live_fake.py
    pytest.mark.skipif(
        os.name == "nt",
        reason="paired-live call phase hangs in a pipe wait on Windows (issue #2720); predates this change",
    ),
]

FIXTURES = Path(__file__).parents[1] / "src/brigade/fixtures/paired-brigade-work"
LIVE = FIXTURES / "live-claude.json"
SECRET_TOKEN = "sentinel-oauth-token-value"
LAUNCH = (sys.executable,)  # The fake is a script; a launcher prefix runs it the same way on every platform.
WINDOWS_BASICS = {
    k: os.environ[k] for k in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT") if k in os.environ
}
ENVIRON = {
    **WINDOWS_BASICS,
    # The Boundary's `work brief` and `outcome capture` call git by name, so the arm PATH must reach it.
    "PATH": os.environ["PATH"] if os.name == "nt" else "/usr/bin:/bin",
    "CLAUDE_CODE_OAUTH_TOKEN": SECRET_TOKEN,
    "ANTHROPIC_API_KEY": "sentinel-api-key",
    "ANTHROPIC_AUTH_TOKEN": "sentinel-auth",
    "UNRELATED_SECRET": "sentinel-unrelated",
    "HOME": "/parent-home",
}


def execute_live(*args, **kwargs):
    return _execute_live(*args, launcher=LAUNCH, **kwargs)


def _remove_temp_root(root, *, remove=None, attempts=5, pause=0.05):
    """Remove a temp root, retrying briefly when a child is still flushing a write.

    The fake MCP server can release its state-dir handles a moment after the CLI exits,
    and on Windows a stopped process keeps them a little longer. Retry a bounded number
    of times on ENOTEMPTY and PermissionError, then give up rather than waiting forever.
    ``remove`` is a seam so a test can inject the late write deterministically.
    """
    remove = remove if remove is not None else shutil.rmtree
    for attempt in range(1, attempts + 1):
        try:
            remove(root)
            return
        except PermissionError:
            if attempt == attempts:
                raise
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt == attempts:
                raise
        time.sleep(pause * attempt)


def _track_work_dirs(monkeypatch):
    """Record each cell work dir so a test can assert on the exact directories it created."""
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(model_trial_claude.tempfile, "mkdtemp", tracking)
    return created


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
    _remove_temp_root(root)


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
    outcomes = [(a["failure"], a["grade"]["passed"], a["grade"].get("reasons")) for a in result["attempts"]]
    assert all(failure is None and passed for failure, passed, _ in outcomes), outcomes
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
    assert attempt["grade"]["passed"] is True, (attempt["failure"], attempt["grade"])


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
        # On Windows build_env also roots these inside the arm directory (never copied from the parent).
        allowed |= {"APPDATA", "LOCALAPPDATA", "TEMP", "TMP"}
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
    # The adapter hands the model the file's exact bytes, so read them without newline translation
    # (a Windows checkout holds SKILL.md with CRLF, and read_text would fold it to LF).
    skills = {arm: (FIXTURES / arm / "SKILL.md").read_bytes().decode("utf-8") for arm in ("baseline", "candidate")}
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


def test_resume_appends_only_with_the_reviewed_digest_and_refuses_any_identity_change(tmp_path):
    first, fake_dir, output = _live(tmp_path, init={"model": "wrong"})
    exe = fake_dir / "claude"
    again = execute_live(
        LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=60, resume_digest=first["evidence_sha256"]
    )
    assert len(again["attempts"]) == 4 and again["attempts"][:2] == first["attempts"]
    reviewed = again["evidence_sha256"]
    make_fake(fake_dir, init={"model": "wrong"}, version="2.1.400")
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=60, resume_digest=reviewed)
    make_fake(fake_dir, init={"model": "wrong"})
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=61, resume_digest=reviewed)
    copy = tmp_path / "pkg"
    shutil.copytree(FIXTURES, copy)
    first_copy = execute_live(
        copy / "live-claude.json", tmp_path / "out3", claude_bin=str(exe), environ=ENVIRON, timeout=60
    )
    assert len(first_copy["attempts"]) == 2
    skill = copy / "candidate/SKILL.md"
    skill.write_text(skill.read_text() + "\nChanged.\n")
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(
            copy / "live-claude.json",
            tmp_path / "out3",
            claude_bin=str(exe),
            environ=ENVIRON,
            timeout=60,
            resume_digest=first_copy["evidence_sha256"],
        )


def _forge_one_attempt_away(output, destination):
    """What a local process can do: drop an attempt and rewrite evidence.json and its stored digest to match."""
    shutil.copytree(output, destination)
    payload = json.loads((destination / "evidence.json").read_text())
    payload["attempts"].pop()
    payload["pairs"] = _pairs(payload["plan"], payload["attempts"])
    (destination / "evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    (destination / "evidence.sha256").write_text(digest(payload) + "\n")
    return digest(payload)


def test_a_stored_digest_cannot_vouch_for_rewritten_attempts(tmp_path):
    first, fake_dir, output = _live(tmp_path, init={"model": "wrong"})
    exe = fake_dir / "claude"
    forged = tmp_path / "forged"
    forged_digest = _forge_one_attempt_away(output, forged)
    # The file beside the evidence says the forged tree is intact. Without the operator's digest nothing is reused.
    fresh = execute_live(LIVE, forged, claude_bin=str(exe), environ=ENVIRON, timeout=60)
    assert len(fresh["attempts"]) == 2 and all(a["attempt_id"].endswith("-1") for a in fresh["attempts"])
    moved = sorted(path.name for path in forged.glob("superseded-*"))
    assert len(moved) == 1
    held = json.loads((forged / moved[0] / "evidence.json").read_text())
    assert len(held["attempts"]) == 1  # the earlier evidence is kept aside, never overwritten
    # The reviewed digest of the genuine run does not match the forged tree.
    forged_again = tmp_path / "forged2"
    _forge_one_attempt_away(output, forged_again)
    with pytest.raises(ValueError, match="digest mismatch"):
        execute_live(
            LIVE,
            forged_again,
            claude_bin=str(exe),
            environ=ENVIRON,
            timeout=60,
            resume_digest=first["evidence_sha256"],
        )
    assert len(json.loads((forged_again / "evidence.json").read_text())["attempts"]) == 1
    assert forged_digest != first["evidence_sha256"]


def test_resume_digest_without_evidence_is_refused(tmp_path):
    exe = make_fake(tmp_path / "f")
    with pytest.raises(ValueError, match="no evidence to resume"):
        execute_live(LIVE, tmp_path / "empty", claude_bin=str(exe), environ=ENVIRON, timeout=60, resume_digest="0" * 64)
    assert records(tmp_path / "f") == []


def test_cli_resume_digest_flag_resumes_only_matching_evidence(tmp_path, monkeypatch, capsys, cli_launcher):
    exe = make_fake(tmp_path / "cli", init={"model": "wrong"})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET_TOKEN)
    args = ["model", "trial", "paired-live", str(LIVE), "--output-dir", str(tmp_path / "o"), "--claude-bin", str(exe)]
    assert cli.main([*args, "--case", "passing-verify", "--timeout", "30"]) == 0
    reviewed = json.loads(capsys.readouterr().out)["evidence_sha256"]
    assert cli.main([*args, "--case", "passing-verify", "--timeout", "30", "--resume-digest", "0" * 64]) == 2
    assert "digest mismatch" in capsys.readouterr().err
    assert cli.main([*args, "--case", "passing-verify", "--timeout", "30", "--resume-digest", reviewed]) == 0
    assert len(json.loads(capsys.readouterr().out)["attempts"]) == 4


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
    arm = Path("/arm")
    env = build_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "PATH": "/p"}, arm)
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["PATH"] == "/p"
    assert env["HOME"] == str(arm / "home")  # str(Path) so the separator is the platform's
    assert env["USERPROFILE"] == env["HOME"]


def test_build_env_copies_only_named_variables_and_roots_windows_dirs_in_the_arm():
    parent = {name: f"parent-{name}" for name in ENV_PASSTHROUGH}
    secrets = ("AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
    parent.update({name: "parent-secret" for name in secrets} | {TOKEN_ENV: "t"})
    arm = Path("/arm")
    for windows in (False, True):
        env = build_env(parent, arm, windows=windows)
        assert all(value != "parent-secret" for value in env.values())  # no unlisted parent variable survives
        assert {k for k, v in env.items() if v.startswith("parent-")} == set(ENV_PASSTHROUGH)
        assert ("LOCALAPPDATA" in env) is windows and ("TEMP" in env) is windows
        if windows:
            home = arm / "home"
            assert env["LOCALAPPDATA"] == str(home / "AppData" / "Local")
            assert env["APPDATA"] == str(home / "AppData" / "Roaming")
            assert env["TEMP"] == env["TMP"] == str(arm / "tmp")


def test_build_env_disables_the_update_refresh_in_every_arm(tmp_path):
    """A cell needs no network, so its server must not spawn a detached refresh into the arm home."""
    for windows in (False, True):
        env = build_env({TOKEN_ENV: "t"}, tmp_path / "arm", windows=windows)
        assert env["BRIGADE_NO_UPDATE_CHECK"] == "1"


def test_an_arm_mcp_server_leaves_no_update_refresh_in_its_home(tmp_path):
    arm = tmp_path / "arm"
    (arm / "home").mkdir(parents=True)
    (arm / "state").mkdir()
    target = tmp_path / "target"
    target.mkdir()
    env = build_env(ENVIRON, arm)
    config = model_trial_claude.mcp_config("passing-verify", target, FIXTURES / "baseline", arm / "state")
    server = config["mcpServers"]["brigade-paired"]
    # A dead proxy makes any refresh child fail its request at once, so a leak cannot linger.
    dead_proxy = {name: "http://127.0.0.1:9" for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}
    subprocess.run(
        [server["command"], *server["args"]],
        env={**env, **server["env"], **dead_proxy},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )
    cache = arm / "home" / ".cache" / "brigade"
    assert not (cache / "update-notify.claim").exists(), "the arm's MCP server claimed an update refresh"
    assert not (cache / "update-notify.json").exists(), "the arm's MCP server spawned an update refresh"


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
    _, _, code, timed_out, drain_failed = _run_process(
        [sys.executable, "-c", "pass"], env, tmp_path, "", 30, observer, "tok"
    )
    assert (code, timed_out, drain_failed) == (0, False, False)
    assert events.index("cancel") < events.index("wait")  # the timer is gone before we reap
    before = list(kills)
    callbacks[0]()  # a late expiry for a process that already exited
    assert kills == before


# ---- Daybreak review: executable binding ------------------------------------------------------


def _elf(name):
    """A native program for a held-descriptor launch, resolved through its symlinks.

    Pass a shell, never ``echo``: some runners ship ``echo`` as a single-utility build that refuses to
    start unless the path it was executed by names echo, and a held exec goes through a descriptor
    (the same limit tests/test_memory_verify_seat.py documents). Both the file and its copy named
    ``claude`` must therefore be a program that does not look at its own name.
    """
    found = shutil.which(name)
    if found is None or not proc.held_exec_supported():
        pytest.skip(f"needs a native {name} and held-descriptor launch")
    real = Path(os.path.realpath(found))
    if real.read_bytes()[:4] != b"\x7fELF":
        pytest.skip(f"{name} is not a native ELF binary here")
    return real


def test_adapter_identity_records_the_executable_hash_and_a_changed_file_refuses_resume(tmp_path):
    first, fake_dir, output = _live(tmp_path, init={"model": "wrong"})
    exe = fake_dir / "claude"
    recorded = first["plan"]["adapter"]["executable"]
    assert recorded["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert recorded["size_bytes"] == exe.stat().st_size and recorded["launch"] == "launcher"
    assert all(cell["adapter"]["executable"] == recorded for cell in first["plan"]["cells"])
    with exe.open("a") as handle:  # Same version string, different bytes.
        handle.write("\n# replaced\n")
    with pytest.raises(ValueError, match="identity changed"):
        execute_live(
            LIVE, output, claude_bin=str(exe), environ=ENVIRON, timeout=60, resume_digest=first["evidence_sha256"]
        )
    stripped = json.loads((output / "evidence.json").read_text())
    stripped["plan"]["adapter"].pop("executable")
    (output / "evidence.json").write_text(json.dumps(stripped))
    with pytest.raises(ValueError, match="executable hash"):
        model_trials.paired_validate(output, digest(stripped))


def test_the_bound_executable_is_rechecked_before_every_launch(tmp_path, monkeypatch):
    exe = make_fake(tmp_path / "fake")
    with bind_executable(str(exe), LAUNCH) as bound:
        bound.verify()
        with exe.open("a") as handle:
            handle.write("# swapped after the hash\n")
        with pytest.raises(ValueError, match="changed after it was hashed"):
            bound.verify()
    fake_dir = tmp_path / "fake2"
    swapped = make_fake(fake_dir, steps=GOOD_STEPS)
    real = model_trial_claude.run_cell
    calls = []

    def run_after_a_swap(**kwargs):
        calls.append(1)
        with swapped.open("a") as handle:
            handle.write("# swapped between cells\n")
        return real(**kwargs)

    monkeypatch.setattr(model_trial_claude, "run_cell", run_after_a_swap)
    with pytest.raises(ValueError, match="changed after it was hashed"):
        execute_live(LIVE, tmp_path / "out", claude_bin=str(swapped), environ=ENVIRON, timeout=60)
    assert calls == [1] and records(fake_dir) == []  # the swapped file never received the token


def test_a_replaced_path_cannot_change_what_runs_after_the_hash(tmp_path):
    shell, true = _elf("sh"), _elf("true")
    claude = tmp_path / "claude"
    shutil.copy(shell, claude)
    other = tmp_path / "other"
    shutil.copy(true, other)
    with bind_executable(str(claude)) as bound:
        assert bound.identity()["launch"] == "held-fexecve"
        assert bound.sha256 == hashlib.sha256(shell.read_bytes()).hexdigest()
        os.replace(other, claude)  # the path now names a different binary
        script = ["-c", "echo held"]
        assert subprocess.run([str(claude), *script], capture_output=True).stdout == b""  # by path: the replacement
        ran = subprocess.run(bound.command(["claude", *script]), capture_output=True, pass_fds=bound.pass_fds)
        assert ran.stdout == b"held\n", ran.stderr  # by descriptor: the file that was hashed
        bound.verify()  # a replaced path leaves the held file intact
    rewritten = tmp_path / "claude2"
    shutil.copy(shell, rewritten)
    with bind_executable(str(rewritten)) as bound:
        with rewritten.open("ab") as handle:  # an in-place rewrite of the held inode
            handle.write(b"\0")
        with pytest.raises(ValueError, match="changed after it was hashed"):
            bound.verify()


def test_a_live_run_with_a_native_executable_records_its_hash_and_launches_by_descriptor(tmp_path):
    bash = _elf("bash")  # `bash --version` prints a version; a dash shell has no such flag
    claude = tmp_path / "claude"
    shutil.copy(bash, claude)
    result = _execute_live(LIVE, tmp_path / "out", claude_bin=str(claude), environ=ENVIRON, timeout=30)
    recorded = result["plan"]["adapter"]["executable"]
    assert recorded["sha256"] == hashlib.sha256(claude.read_bytes()).hexdigest()
    assert recorded["launch"] == "held-fexecve"
    assert result["plan"]["adapter"]["claude_code_version"]  # read through the held descriptor
    assert all(a["failure"] for a in result["attempts"])  # bash is no Claude, and no cell passes


def test_live_runs_refuse_a_platform_without_held_launch_before_any_process(tmp_path, monkeypatch):
    exe = make_fake(tmp_path / "never")
    monkeypatch.setattr(proc, "held_exec_supported", lambda: False)
    with pytest.raises(ValueError, match="held descriptor"):
        _execute_live(LIVE, tmp_path / "o", claude_bin=str(exe), environ=ENVIRON)  # no launcher: no path launch
    assert records(tmp_path / "never") == [] and not (tmp_path / "o").exists()


def test_scripts_and_launchers_are_not_accepted_as_the_executable(tmp_path):
    if not proc.held_exec_supported():
        pytest.skip("needs held-descriptor launch")
    exe = make_fake(tmp_path / "script")
    with pytest.raises(ValueError, match="script or launcher"):
        with bind_executable(str(exe)):
            pass


def test_the_fake_claude_source_compiles_with_a_windows_style_scenario_path():
    # Backslash separators broke the unescaped literal on Windows runners: the fake died on a syntax
    # error and the version probe read nothing.
    windows_path = "\\".join(["D:", "runner", "work", "temp", "pytest-0", "scenario.json"])
    source = render_script("python", windows_path)
    compile(source, "claude", "exec")
    assert json.dumps(windows_path) in source


def test_the_version_probe_runs_through_the_same_launcher_as_the_arms(tmp_path):
    exe = make_fake(tmp_path / "fake", version="2.1.999")
    with bind_executable(str(exe), LAUNCH) as bound:
        assert model_trial_claude._probe_version(bound, ENVIRON) == "2.1.999"
        assert bound.command(["claude", "--version"]) == [*LAUNCH, "claude", "--version"]


# ---- Daybreak review: token never reaches disk, MCP server never holds it ---------------------


def test_the_mcp_server_starts_without_the_token(good):
    for run in records(good[1]):
        assert run["config"]["mcpServers"]["brigade-paired"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert run["server_token"] == ""  # what the server process inherits once its config env applies
        assert run["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == SECRET_TOKEN  # only the CLI holds it


def test_child_output_is_redacted_in_memory_before_any_byte_reaches_disk(tmp_path, monkeypatch):
    """Keep the arm work directories as a crash before cleanup would, then look for the token in them."""
    kept = []
    monkeypatch.setattr(model_trial_claude, "remove_work_dir", kept.append)
    result, _, output = _live(tmp_path, steps=GOOD_STEPS, leak=True)
    assert all(a["failure"] is None for a in result["attempts"]) and len(kept) == 2
    leaked = [p for root in kept for p in root.rglob("*") if p.is_file() and SECRET_TOKEN.encode() in p.read_bytes()]
    assert leaked == []
    assert not list(kept[0].rglob("stderr.txt"))  # stderr is never staged on disk
    for root in kept:
        shutil.rmtree(root, ignore_errors=True)


class _Pipe:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read1(self, size):
        return self.chunks.pop(0) if self.chunks else b""


def test_stderr_redaction_catches_a_token_split_across_reads():
    token = SECRET_TOKEN
    raw = f"before {token} after".encode()
    for cut in range(1, len(raw)):
        capture = _StderrCapture(token)
        capture.drain(_Pipe([raw[:cut], raw[cut:]]), lambda: None)
        assert token.encode() not in bytes(capture.tail), cut
        assert bytes(capture.tail) == b"before [redacted] after"


def test_stderr_keeps_only_a_bounded_redacted_tail_and_flags_overflow(monkeypatch):
    monkeypatch.setattr(model_trial_claude, "MAX_STDERR_BYTES", 1000)
    monkeypatch.setattr(model_trial_claude, "STDERR_TAIL", 100)
    flagged = []
    capture = _StderrCapture("tok")
    capture.drain(_Pipe([b"a" * 600, b"b" * 600]), lambda: flagged.append(1))
    assert capture.overflow and flagged == [1] and len(capture.tail) == 100 and capture.tail.endswith(b"b" * 100)


# ---- Daybreak review: size caps before buffering ----------------------------------------------


def test_a_stream_line_past_the_cap_fails_the_cell_without_being_buffered(tmp_path, monkeypatch):
    monkeypatch.setattr(model_trial_claude, "MAX_LINE_BYTES", 4096)
    result, _, output = _live(tmp_path, steps=GOOD_STEPS, big_line=500_000)
    assert _failures(result) == ["stream_line_too_large"]
    stream = (output / "attempts" / result["attempts"][0]["attempt_id"] / "stream.jsonl").read_bytes()
    assert len(stream) < 100_000  # the oversized line was never kept


def test_stderr_past_the_cap_fails_the_cell_and_the_saved_tail_stays_small(tmp_path, monkeypatch):
    monkeypatch.setattr(model_trial_claude, "MAX_STDERR_BYTES", 20_000)
    monkeypatch.setattr(model_trial_claude, "STDERR_TAIL", 1000)
    result, _, output = _live(tmp_path, steps=GOOD_STEPS, stderr_flood=400_000)
    assert _failures(result) == ["stderr_too_large"]
    saved = (output / "attempts" / result["attempts"][0]["attempt_id"] / "stderr.txt").read_bytes()
    assert len(saved) <= 1000


def test_an_oversized_server_trace_fails_the_cell(tmp_path, monkeypatch):
    monkeypatch.setattr(model_trial_claude, "MAX_TRACE_BYTES", 200)
    result, _, _ = _live(tmp_path, steps=GOOD_STEPS)
    assert _failures(result) == ["server_trace_too_large"]
    assert all(a["trace"] == [] and a["grade"]["passed"] is False for a in result["attempts"])


def test_oversized_packages_are_refused_before_any_launch(tmp_path):
    exe = make_fake(tmp_path / "never")
    big = tmp_path / "big"
    shutil.copytree(FIXTURES, big)
    (big / "candidate/SKILL.md").write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="larger than its cap"):
        execute_live(big / "live-claude.json", tmp_path / "o", claude_bin=str(exe), environ=ENVIRON)
    flood = tmp_path / "flood"
    shutil.copytree(FIXTURES, flood)
    for index in range(600):
        (flood / "candidate" / f"extra-{index}.txt").write_text("x")
    with pytest.raises(ValueError, match="too many entries"):
        execute_live(flood / "live-claude.json", tmp_path / "o2", claude_bin=str(exe), environ=ENVIRON)
    total = tmp_path / "total"
    shutil.copytree(FIXTURES, total)
    for index in range(5):
        (total / "candidate" / f"blob-{index}.bin").write_bytes(b"y" * (1024 * 1024))
    with pytest.raises(ValueError, match="larger than its cap"):
        execute_live(total / "live-claude.json", tmp_path / "o3", claude_bin=str(exe), environ=ENVIRON)
    assert records(tmp_path / "never") == []


@pytest.mark.parametrize("key", ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"])
def test_a_proxy_url_with_credentials_refuses_the_run_without_naming_the_secret(tmp_path, key):
    exe = make_fake(tmp_path / "px")
    environ = {**ENVIRON, key: "http://proxyuser:hunter2-proxy-secret@proxy.example:3128"}
    with pytest.raises(ValueError, match=key) as caught:
        execute_live(LIVE, tmp_path / "out", claude_bin=str(exe), environ=environ, timeout=60)
    assert "hunter2-proxy-secret" not in str(caught.value) and "proxyuser" not in str(caught.value)
    assert not (tmp_path / "px" / "record.jsonl").exists()  # No model was launched.


def test_a_username_only_proxy_url_is_refused_and_a_plain_one_is_allowed():
    model_trial_claude.refuse_proxy_credentials({"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "a@b"})
    model_trial_claude.refuse_proxy_credentials({"http_proxy": "proxy.example:3128"})
    with pytest.raises(ValueError, match="http_proxy"):
        model_trial_claude.refuse_proxy_credentials({"http_proxy": "http://user@proxy.example:3128"})


@pytest.mark.parametrize(
    "value",
    [
        "http:/user:pw@proxy.example:3128",  # Node reads these as an authenticated URL, urlsplit does not.
        "http:////user:pw@proxy.example:3128",
        "http://user%40x:pw@proxy.example:3128",
        "http:/user%3Apw%40proxy.example:3128",
    ],
)
def test_malformed_proxy_urls_that_node_reads_as_authenticated_are_refused(value):
    with pytest.raises(ValueError, match="HTTPS_PROXY") as caught:
        model_trial_claude.refuse_proxy_credentials({"HTTPS_PROXY": value})
    assert "pw" not in str(caught.value).replace("password", "")


def test_the_mcp_server_config_blanks_every_proxy_variable(tmp_path):
    env = model_trial_claude.mcp_config("passing-verify", tmp_path, tmp_path, tmp_path)["mcpServers"][
        model_trial_claude.SERVER_NAME
    ]["env"]
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy"):
        assert env[key] == ""


def test_paired_validate_refuses_oversize_evidence_without_reading_it(tmp_path, monkeypatch):
    (tmp_path / "evidence.json").write_bytes(b" " * 64)
    monkeypatch.setattr(model_trial_claude, "MAX_EVIDENCE_BYTES", 16)
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("unbounded read_text"))
    with pytest.raises(ValueError):
        model_trials.paired_validate(tmp_path, "0" * 64)


def test_the_version_probe_is_bounded_and_kills_a_flood(tmp_path):
    exe = make_fake(tmp_path / "vf", version_flood=model_trial_claude.VERSION_MAX_BYTES * 4)
    with bind_executable(str(exe), LAUNCH) as bound:
        with pytest.raises(ValueError, match="too large"):
            model_trial_claude._probe_version(bound, ENVIRON)


def test_temp_root_removal_retries_a_late_child_write(tmp_path, monkeypatch):
    """A child flushing a trace as cleanup starts must not leave the temp root behind."""
    root = tmp_path / "root"
    (root / "work").mkdir(parents=True)
    real_rmtree = shutil.rmtree
    calls = []

    def late_write(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        real_rmtree(path, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", late_write)
    # A single removal cannot tolerate the late write; the temp root survives it.
    with pytest.raises(OSError) as exc:
        shutil.rmtree(root)
    assert exc.value.errno == errno.ENOTEMPTY
    # The bounded retry tolerates the same late write and finishes the removal.
    _remove_temp_root(root, pause=0.0)
    assert not root.exists()
    assert len(calls) == 2


def test_remove_work_dir_retries_only_on_windows_and_only_a_few_times(tmp_path, monkeypatch):
    monkeypatch.setattr(model_trial_claude, "WINDOWS_REMOVE_PAUSE", 0.0)
    real = shutil.rmtree
    calls = []

    def busy(path, **kwargs):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError(32, "in use")
        real(path, **kwargs)

    arm = tmp_path / "arm"
    (arm / "tmp").mkdir(parents=True)
    monkeypatch.setattr(shutil, "rmtree", busy)
    model_trial_claude.remove_work_dir(arm, windows=True)
    assert not arm.exists() and len(calls) == 3
    arm.mkdir()
    calls.clear()
    with pytest.raises(PermissionError):  # Off Windows a failed removal is raised at once.
        model_trial_claude.remove_work_dir(arm, windows=False)
    assert len(calls) == 1
    calls.clear()

    def denied(path, **kwargs):
        calls.append(path)
        raise PermissionError(5, "denied")

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(PermissionError):
        model_trial_claude.remove_work_dir(arm, windows=True)
    assert len(calls) == model_trial_claude.WINDOWS_REMOVE_ATTEMPTS


@pytest.mark.skipif(not drain_supported(), reason="process-group draining needs /proc")
def test_a_finished_cell_stops_a_descendant_that_left_its_process_group(tmp_path, monkeypatch):
    """The work dir is only removed after every process the arm started is gone.

    The writer leaves the CLI's process group with setpgid but keeps the session, and keeps writing
    under the arm's cwd. A group kill alone misses it, so the adapter drains and stops the session.
    """
    pid_log = tmp_path / "writers.pid"
    ready_base = tmp_path / "writer.ready"
    work_dirs = _track_work_dirs(monkeypatch)
    writer = PUBLISH_SOURCE + (
        "import os, time\n"
        "os.setpgid(0, 0)\n"
        "target = os.path.join(os.getcwd(), 'escaped-writer.log')\n"
        f"pid_log = {str(pid_log)!r}\n"
        f"ready = {str(ready_base)!r} + '.' + str(os.getppid())\n"
        "with open(target, 'w') as handle:\n"
        "    for index in range(2000):\n"
        "        handle.write('line %d\\n' % index)\n"
        "        handle.flush()\n"
        "        if index == 2:\n"
        "            with open(pid_log, 'a') as pids:\n"
        "                pids.write(str(os.getpid()) + '\\n')\n"
        "        if index == 5:\n"
        "            publish(ready, 'ready')\n"
        "        time.sleep(0.05)\n"
    )
    exe = make_fake(tmp_path / "fake", steps=GOOD_STEPS, escaped_writer={"code": writer, "ready": str(ready_base)})
    leaked = []
    try:
        result = execute_live(LIVE, tmp_path / "output", claude_bin=str(exe), environ=ENVIRON, timeout=60)
        assert all(a["failure"] is None for a in result["attempts"])
        pids = [int(line) for line in pid_log.read_text().splitlines() if line.strip()]
        assert pids, "the escaped writer never published its pid"
        leaked = [pid for pid in pids if process_is_running(pid)]
        assert leaked == [], f"arm descendants survived the cell: {leaked}"
        assert work_dirs and not any(path.exists() for path in work_dirs)
    finally:
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for path in work_dirs:
            shutil.rmtree(path, ignore_errors=True)


def test_a_drain_failure_keeps_the_original_error_and_reaps_the_leader(monkeypatch, tmp_path):
    threads, events, children = [], [], []
    real_thread = threading.Thread

    class SpyThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    class ThreadingProxy:
        Thread = SpyThread

        def __getattr__(self, name):
            return getattr(threading, name)

    class BoomObserver:
        failure = None
        result = None
        init_event = None
        observed = []
        controls = []

        def feed(self, event):
            raise ValueError("observer boom")

    class SpyPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    def failing_drain(process):
        events.append("drain")
        raise model_trial_claude.ArmDrainError("drain failed")

    monkeypatch.setattr(model_trial_claude, "threading", ThreadingProxy())
    monkeypatch.setattr(model_trial_claude.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(model_trial_claude, "_drain_arm_group", failing_drain)
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    with pytest.raises(ValueError, match="observer boom") as raised:
        _run_process(
            [sys.executable, "-c", "import json; print(json.dumps({'type': 'system'}))"],
            env,
            tmp_path,
            "",
            30,
            BoomObserver(),
            "tok",
        )
    child = children[0]
    assert events == ["drain"]  # drained once, and the drain failure never replaced the primary error
    assert child.returncode is not None  # the leader was reaped
    assert child.stdout is not None and child.stdout.closed and child.stderr is not None and child.stderr.closed
    assert threads and not threads[0].is_alive()  # the stderr drainer was joined
    if hasattr(raised.value, "add_note"):
        assert any("drain" in note for note in raised.value.__notes__)


def test_a_drain_failure_fails_only_its_cell_and_keeps_its_work_dir(tmp_path, monkeypatch):
    exe = make_fake(tmp_path / "fake", steps=GOOD_STEPS)
    work_dirs = _track_work_dirs(monkeypatch)

    def failing_drain(process):
        raise model_trial_claude.ArmDrainError("drain failed")

    monkeypatch.setattr(model_trial_claude, "_drain_arm_group", failing_drain)
    try:
        result = execute_live(LIVE, tmp_path / "output", claude_bin=str(exe), environ=ENVIRON, timeout=60)
        assert _failures(result) == ["process_group_drain_failed"]
        assert len(result["attempts"]) == 2  # the trial kept going after the first failed cell
        assert len(work_dirs) == 2 and all(path.is_dir() for path in work_dirs)
    finally:
        for path in work_dirs:
            shutil.rmtree(path, ignore_errors=True)


def test_the_bounded_drain_re_signals_members_on_every_poll(monkeypatch):
    calls = []
    alive = [True, True, False]

    monkeypatch.setattr(model_trial_claude, "drain_supported", lambda: True, raising=False)
    monkeypatch.setattr(model_trial_claude, "kill_group_members", lambda pgid, sig: calls.append(pgid), raising=False)
    monkeypatch.setattr(
        model_trial_claude, "group_has_live_members", lambda pgid: alive.pop(0) if alive else False, raising=False
    )
    model_trial_claude._drain_arm_group(types.SimpleNamespace(pid=4242))
    assert len(calls) == 3 and alive == []


def test_an_unreadable_process_table_fails_the_drain_at_once(monkeypatch):
    real_listdir = os.listdir

    def listdir(path):
        if str(path) == "/proc":
            raise PermissionError(errno.EACCES, "denied")
        return real_listdir(path)

    monkeypatch.setattr(proc_group.os, "listdir", listdir)
    monkeypatch.setattr(model_trial_claude, "GROUP_DRAIN_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(model_trial_claude.ArmDrainError):
        model_trial_claude._drain_arm_group(types.SimpleNamespace(pid=4242))
    assert time.monotonic() - started < 0.3


def test_kill_sends_nothing_for_a_reaped_child(monkeypatch):
    sent = []
    monkeypatch.setattr(model_trial_claude.os, "killpg", lambda *args: sent.append(args), raising=False)

    class Reaped:
        pid = 4242
        returncode = 0

        def kill(self):
            sent.append("kill")

    model_trial_claude._kill(Reaped())  # type: ignore[arg-type]
    assert sent == []
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    model_trial_claude._kill(child)
    assert sent == []


def test_kill_signals_an_unreaped_child_group_on_posix_and_the_child_on_windows(monkeypatch):
    sent = []
    monkeypatch.setattr(model_trial_claude.os, "killpg", lambda *args: sent.append(("killpg", *args)), raising=False)

    class Live:
        pid = 4242
        returncode = None

        def kill(self):
            sent.append(("kill",))

    model_trial_claude._kill(Live())  # type: ignore[arg-type]
    expected = [("killpg", 4242, model_trial_claude.signal.SIGKILL)] if os.name == "posix" else [("kill",)]
    assert sent == expected


def test_the_version_probe_timer_firing_after_completion_does_not_kill(monkeypatch, tmp_path):
    callbacks, kills = [], []

    class StubTimer:
        daemon = False

        def __init__(self, interval, function):
            callbacks.append(function)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr(model_trial_claude.threading, "Timer", StubTimer)
    monkeypatch.setattr(model_trial_claude, "_kill", lambda proc: kills.append(proc.pid))
    exe = make_fake(tmp_path / "late", version="2.1.999")
    with bind_executable(str(exe), LAUNCH) as bound:
        assert model_trial_claude._probe_version(bound, ENVIRON) == "2.1.999"
        before = list(kills)
        callbacks[0]()  # The timer fires after the child was reaped.
    assert kills == before


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX permission bits and unprivileged symlinks")
def test_cleanup_never_chmods_through_a_link(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(0o444)
    arm = tmp_path / "arm"
    arm.mkdir()
    link = arm / "link"
    link.symlink_to(outside)

    def refuse(func, target, **kwargs):
        raise PermissionError(13, "denied")

    handler_error = PermissionError(13, "denied")
    with pytest.raises(PermissionError) as caught:
        model_trial_claude._make_writable_and_retry(arm, refuse, str(link), handler_error)
    assert caught.value is handler_error
    assert outside.stat().st_mode & 0o777 == 0o444
    # A real file inside the directory is still made writable and retried.
    inside = arm / "ro.txt"
    inside.write_text("x")
    inside.chmod(0o400)
    retried = []
    model_trial_claude._make_writable_and_retry(arm, retried.append, str(inside), PermissionError())
    assert retried == [str(inside)] and inside.stat().st_mode & 0o777 == 0o700


def _gone(pid, wait=5.0):
    """True once the process no longer exists. A zombie awaiting init counts as gone."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            state = ""
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


@requires_process_groups
@pytest.mark.parametrize(
    "extra, timeout",
    [({}, 60), ({"hang": True}, 3), ({"big_line": model_trial_claude.MAX_LINE_BYTES * 2}, 60)],
    ids=["normal-exit", "timeout", "cap-overflow"],
)
def test_a_group_member_that_outlives_the_cli_is_killed_before_the_cell_returns(tmp_path, extra, timeout):
    pids_file = tmp_path / "grandchildren.txt"
    exe = make_fake(tmp_path / "fake", grandchild=str(pids_file), **extra)
    execute_live(LIVE, tmp_path / "evidence", claude_bin=str(exe), environ=ENVIRON, timeout=timeout)
    pids = [int(line) for line in pids_file.read_text().split()]
    assert pids
    assert all(_gone(pid) for pid in pids)


@requires_process_groups
def test_the_group_is_killed_while_the_leader_is_still_unreaped(monkeypatch):
    order = []
    real_killpg, real_wait = os.killpg, subprocess.Popen.wait

    def killpg(pid, sig):
        order.append(("killpg", child.returncode))
        real_killpg(pid, sig)

    def wait(self, *args, **kwargs):
        order.append(("wait",))
        return real_wait(self, *args, **kwargs)

    child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    monkeypatch.setattr(model_trial_claude.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    assert model_trial_claude._reap(child, 10.0) is not None
    assert order == [("killpg", None), ("wait",)]  # Signalled while returncode was still unset.


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX permission bits and unprivileged symlinks")
def test_posix_cleanup_removes_a_read_only_file_and_leaves_a_link_target_alone(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    outside.chmod(0o444)
    arm = tmp_path / "arm"
    (arm / "sub").mkdir(parents=True)
    (arm / "sub" / "link").symlink_to(outside)
    readonly = arm / "ro.txt"
    readonly.write_text("x")
    readonly.chmod(0o400)
    model_trial_claude.remove_work_dir(arm, windows=False)
    assert not arm.exists()
    assert outside.read_text() == "keep" and outside.stat().st_mode & 0o777 == 0o444


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX shutil.rmtree semantics")
def test_posix_cleanup_installs_no_error_handler(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(shutil, "rmtree", lambda path, **kwargs: seen.append(kwargs))
    model_trial_claude.remove_work_dir(tmp_path / "arm", windows=False)
    assert seen == [{}]
