"""Verify seats: connector refusal, the isolation probe, owner-only work areas and cleanup.

Every seat here is scripted. No test calls a real model: LAUNCH is replaced by
a fake, or the resolved executable is a local script that ignores its flags.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from brigade import cli, proc
from brigade import dispatch_memory as audiences
from brigade import memory_verify_seat as seats
from brigade import memory_verify_state as vstate

SAFE = pytest.mark.skipif(not seats._safe_dirs(), reason="safe directory descriptors unavailable")
PROMPT_SECRET = "Quillbramble-prompt-7731"
REPLY_SECRET = "Marrowfen-reply-4402"
BASE_ROSTER = """
orchestrator = "chef"

[agents.chef]
cli = "claude"
role = "orchestrator"

[agents.verify-propose]
cli = "claude"
role = "reviewer"
model = "claude-sonnet-5-5"

[agents.verify-confirm]
cli = "claude"
role = "reviewer"
model = "claude-opus-5-5"
"""


def write_roster(target: Path, extra: str = "") -> None:
    path = target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BASE_ROSTER + textwrap.dedent(extra), encoding="utf-8")


def write_care(target: Path, propose: str = "verify-propose", confirm: str = "verify-confirm") -> None:
    path = target / ".brigade" / "memory-care.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[verify]\npropose_seat = "{propose}"\nconfirm_seat = "{confirm}"\n', encoding="utf-8")


@pytest.fixture
def target(tmp_path, monkeypatch):
    workspace = tmp_path / "verify"
    workspace.mkdir()
    write_roster(workspace)
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    def resolve(cli_ref: str) -> proc.ExecutableIdentity:
        return proc.ExecutableIdentity(command=cli_ref, path=str(executable), kind="native", runnable=True, detail="")

    monkeypatch.setattr(seats, "RESOLVE", resolve)
    monkeypatch.setattr(seats, "LAUNCH", forbidden_launch)
    return workspace


def forbidden_launch(*args, **kwargs):
    raise AssertionError("no seat may be launched in this test")


def result(stdout: str = "", stderr: str = "", code: int = 0) -> proc.Result:
    return proc.Result(code=code, stdout=stdout, stderr=stderr)


def echo_probe(argv, *, timeout, env, cwd, stdin):
    """A tool-free seat: it answers with the nonce and sees nothing else."""
    nonce = stdin.decode().split()[2].rstrip(".")
    return result(stdout=f"{nonce}\n")


def all_bytes(root: Path) -> bytes:
    data = b""
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            data += path.read_bytes()
    return data


# --- resolution and refusal -------------------------------------------------


def test_claude_profile_is_tool_free_recall_free_and_pins_the_model(target):
    seat = seats.resolve(target, "verify-propose", "propose")
    argv = list(seat.argv)
    assert argv[1:3] == ["--model", "claude-sonnet-5-5"]
    for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert seat.model_key == ("anthropic", "claude-sonnet-5-5")
    assert seats.audience(
        seats.roster_mod.load_roster(target / ".brigade" / "roster.toml").agents["verify-propose"]
    ) == (audiences.LOCAL)


def test_a_profile_missing_an_isolation_flag_is_refused(target, monkeypatch):
    builder, required = seats.PROFILES["claude"]

    def leaky(executable, model):
        argv = builder(executable, model)
        argv.remove("--safe-mode")
        return argv

    monkeypatch.setitem(seats.PROFILES, "claude", (leaky, required))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "verify-propose", "propose")
    assert caught.value.code == "verify_seat_not_isolated"

    def tools(executable, model):
        argv = builder(executable, model)
        argv[argv.index("--tools") + 1] = "default"
        return argv

    monkeypatch.setitem(seats.PROFILES, "claude", (tools, required))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "verify-propose", "propose")
    assert caught.value.code == "verify_seat_not_isolated"


def test_two_seats_pinned_to_one_model_are_refused(target):
    write_roster(
        target,
        """
        [agents.long-context]
        cli = "claude"
        role = "reviewer"
        model = "claude-sonnet-5-5[1m]"
        """,
    )
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve_pair(target, "verify-propose", "long-context")
    assert caught.value.code == "verify_seat_same_model"


def test_an_alias_or_missing_model_is_not_a_pin(target):
    write_roster(
        target,
        """
        [agents.alias]
        cli = "claude"
        role = "reviewer"
        model = "opus"

        [agents.bare]
        cli = "claude"
        role = "reviewer"
        """,
    )
    for name in ("alias", "bare"):
        with pytest.raises(seats.SeatRefused) as caught:
            seats.resolve(target, name, "propose")
        assert caught.value.code == "verify_seat_unpinned"


CONNECTOR_SEATS = """
[agents.cloud]
cli = "codex-cloud:env-example"
role = "reviewer"

[agents.hosted]
cli = "oracle"
role = "reviewer"

[agents.remote-endpoint]
role = "reviewer"
endpoint = "https://203.0.113.10/v1"
model = "example-model"

[agents.ollama-cloud]
cli = "ollama:gpt-oss:120b-cloud"
role = "reviewer"
"""


@pytest.mark.parametrize("name", ["cloud", "hosted", "remote-endpoint", "ollama-cloud"])
def test_cloud_and_connector_seats_are_refused_before_any_prompt(target, name):
    write_roster(target, CONNECTOR_SEATS)
    agent = seats.roster_mod.load_roster(target / ".brigade" / "roster.toml").agents[name]
    assert seats.audience(agent) == audiences.CONNECTOR
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve_pair(target, name, "verify-confirm")
    assert caught.value.code == "verify_seat_connector"
    assert not (target / seats.WORK_ROOT).exists()


@SAFE
@pytest.mark.parametrize("name", ["cloud", "remote-endpoint", "other-node"])
def test_doctor_refuses_cloud_and_remote_seats_without_writing_a_prompt(target, name, capsys):
    write_roster(target, CONNECTOR_SEATS)
    write_care(target, propose=name)
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["code"] == ("verify_seat_unknown" if name == "other-node" else "verify_seat_connector")
    assert not (target / seats.WORK_ROOT).exists()
    assert not (target / seats.PROBES).exists()


def test_a_seat_only_the_hub_knows_is_refused(target):
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "remote-node-seat", "propose")
    assert caught.value.code == "verify_seat_unknown"


def test_seats_the_profile_cannot_vouch_for_are_not_isolated(target):
    write_roster(
        target,
        """
        [agents.codex]
        cli = "codex"
        role = "reviewer"
        model = "gpt-example"

        [agents.wrapped]
        cli = "claude"
        role = "reviewer"
        model = "claude-opus-5-5"
        command = ["claude-wrapper"]

        [agents.env]
        cli = "claude"
        role = "reviewer"
        model = "claude-opus-5-5"
        env = { EXAMPLE_FLAG = "1" }
        """,
    )
    for name in ("codex", "wrapped", "env"):
        with pytest.raises(seats.SeatRefused) as caught:
            seats.resolve(target, name, "propose")
        assert caught.value.code == "verify_seat_not_isolated"


def test_local_ollama_is_pinned_by_its_reference_and_runs_without_a_host_override(target, monkeypatch):
    write_roster(
        target,
        """
        [agents.local]
        cli = "ollama:llama3"
        role = "reviewer"
        """,
    )
    seat = seats.resolve(target, "local", "propose")
    assert seat.model_key == ("ollama", "llama3:latest")
    assert list(seat.argv[1:]) == ["run", "--nowordwrap", "--hidethinking", "llama3"]


# --- the work area ------------------------------------------------------------


def test_the_work_area_fails_closed_without_safe_directories(target, monkeypatch):
    monkeypatch.setattr(seats, "_safe_dirs", lambda: False)
    with pytest.raises(seats.SeatRefused) as caught:
        with seats.work_area(target):
            pass
    assert caught.value.code == "verify_workdir_unavailable"
    assert not (target / seats.WORK_ROOT).exists()


@SAFE
def test_work_area_and_files_are_owner_only_and_gone_after_the_call(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    seen = {}

    def launch(argv, *, timeout, env, cwd, stdin):
        call_dir = Path(cwd).parent
        seen["modes"] = {
            path.name: stat.S_IMODE(os.lstat(path).st_mode)
            for path in (call_dir.parent, call_dir, Path(cwd), call_dir / "tmp", *call_dir.glob("*.prompt"))
        }
        seen["prompt"] = next(call_dir.glob("*.prompt")).read_bytes()
        seen["env"] = env
        seen["cwd_entries"] = os.listdir(cwd)
        return result(stdout=json.dumps({"answer": REPLY_SECRET}))

    monkeypatch.setattr(seats, "LAUNCH", launch)
    monkeypatch.setenv("BRIGADE_MEMORY_HUB_TOKEN_FILE", "/nonexistent/token")
    monkeypatch.setenv("OLLAMA_HOST", "203.0.113.20:11434")
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
        replies = list(area.path.glob("*.reply"))
        assert [path.read_text() for path in replies] == [json.dumps({"answer": REPLY_SECRET})]
        assert stat.S_IMODE(os.lstat(replies[0]).st_mode) == 0o600
    assert reply.status == "ok" and REPLY_SECRET in reply.text
    assert seen["prompt"] == PROMPT_SECRET.encode()
    assert seen["cwd_entries"] == []
    for name, mode in seen["modes"].items():
        assert mode == (0o600 if name.endswith(".prompt") else 0o700), name
    assert "BRIGADE_MEMORY_HUB_TOKEN_FILE" not in seen["env"]
    assert "OLLAMA_HOST" not in seen["env"]
    assert seen["env"]["TMPDIR"].endswith("/tmp")
    assert os.listdir(target / seats.WORK_ROOT) == []


@SAFE
def test_files_are_removed_after_a_timeout_and_after_an_interrupt(target, monkeypatch, tmp_path):
    # A local script stands in for the seat CLI. It ignores the profile flags and never answers.
    script = tmp_path / "bin" / "scripted-seat"
    script.write_text(
        f"#!{sys.executable}\nimport sys, time\nsys.stdin.read()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    monkeypatch.setattr(
        seats,
        "RESOLVE",
        lambda ref: proc.ExecutableIdentity(command=ref, path=str(script), kind="native", runnable=True, detail=""),
    )
    monkeypatch.setattr(seats, "LAUNCH", proc.run)
    seat = seats.resolve(target, "verify-propose", "propose")
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=1)
    assert reply.status == "seat_timeout" and not reply.fatal
    assert os.listdir(target / seats.WORK_ROOT) == []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(seats, "LAUNCH", interrupted)
    with pytest.raises(KeyboardInterrupt):
        with seats.work_area(target) as area:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert os.listdir(target / seats.WORK_ROOT) == []
    assert PROMPT_SECRET.encode() not in all_bytes(target)


@SAFE
def test_an_error_from_the_callers_block_passes_through_and_still_cleans_up(target):
    with pytest.raises(FileNotFoundError):
        with seats.work_area(target):
            raise FileNotFoundError("caller")
    assert os.listdir(target / seats.WORK_ROOT) == []


@SAFE
def test_a_killed_runs_leftover_call_directory_is_swept(target):
    leftover = target / seats.WORK_ROOT / f"call-{'a' * 32}"
    (leftover / "cwd").mkdir(parents=True)
    (leftover / "0001-propose.prompt").write_text(PROMPT_SECRET, encoding="utf-8")
    with seats.work_area(target) as area:
        assert sorted(os.listdir(area.path.parent)) == [area.path.name]
    assert not leftover.exists()


@SAFE
def test_a_seat_that_writes_in_its_working_directory_is_not_isolated(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")

    def writer(argv, *, timeout, env, cwd, stdin):
        (Path(cwd) / "notes.txt").write_text("written", encoding="utf-8")
        return result(stdout="{}")

    monkeypatch.setattr(seats, "LAUNCH", writer)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert reply.status == "verify_seat_not_isolated" and reply.fatal
    assert os.listdir(target / seats.WORK_ROOT) == []


@SAFE
@pytest.mark.parametrize(
    ("stdout", "stderr", "status", "fatal"),
    [
        ("Claude AI usage limit reached", "", "seat_quota", False),
        ("", "Error: 429 Too Many Requests", "seat_quota", False),
        ("Invalid API key. Please run /login", "", "seat_auth", True),
        ("", "something else broke", "seat_failed", False),
    ],
)
def test_rate_limit_and_auth_replies_map_to_fixed_codes(target, monkeypatch, stdout, stderr, status, fatal):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(seats, "LAUNCH", lambda *a, **k: result(stdout=stdout, stderr=stderr, code=1))
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.fatal) == (status, fatal)


@SAFE
def test_a_reply_that_mentions_a_quota_is_still_a_reply(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(seats, "LAUNCH", lambda *a, **k: result(stdout='{"note": "rate limit 429"}'))
    with seats.work_area(target) as area:
        assert seats.call(area, seat, PROMPT_SECRET, timeout=30).status == "ok"


@SAFE
def test_an_oversized_prompt_is_refused_before_a_file_is_written(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    with seats.work_area(target) as area:
        with pytest.raises(seats.SeatRefused) as caught:
            seats.call(area, seat, "x" * (seats.MAX_PROMPT_BYTES + 1), timeout=30)
        assert caught.value.code == "verify_prompt_too_large"
        assert sorted(os.listdir(area.path)) == ["cwd", "tmp"]


# --- the isolation probe and the doctor ---------------------------------------


@SAFE
def test_doctor_records_passing_probes_and_ready_accepts_only_that_fingerprint(target, monkeypatch, capsys):
    write_care(target)
    prompts = []

    def launch(argv, *, timeout, env, cwd, stdin):
        prompts.append(stdin)
        assert {"AGENTS.md", "CLAUDE.md", "canary.txt", "cwd", "tmp"} <= set(os.listdir(Path(cwd).parent))
        assert os.listdir(cwd) == []
        return echo_probe(argv, timeout=timeout, env=env, cwd=cwd, stdin=stdin)

    monkeypatch.setattr(seats, "LAUNCH", launch)
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "ok"
    assert [row["status"] for row in payload["seats"]] == ["isolated", "isolated"]
    assert len(prompts) == 2
    assert stat.S_IMODE(os.lstat(target / seats.PROBES).st_mode) == 0o600
    propose, confirm = seats.ready(target, "verify-propose", "verify-confirm")
    assert (propose.role, confirm.role) == ("propose", "confirm")

    (target / ".brigade" / "roster.toml").write_text(
        BASE_ROSTER.replace("claude-sonnet-5-5", "claude-haiku-5-5"), encoding="utf-8"
    )
    with pytest.raises(seats.SeatRefused) as caught:
        seats.ready(target, "verify-propose", "verify-confirm")
    assert caught.value.code == "verify_seat_not_isolated"


@SAFE
@pytest.mark.parametrize("leak", ["canary", "word"])
def test_a_seat_that_reads_the_canary_or_sees_project_instructions_fails_the_probe(target, monkeypatch, leak, capsys):
    write_care(target)

    def leaky(argv, *, timeout, env, cwd, stdin):
        nonce = stdin.decode().split()[2].rstrip(".")
        parent = Path(cwd).parent
        if leak == "canary":
            extra = (parent / "canary.txt").read_text().strip()
        else:
            extra = re.search(r"word-[0-9a-f]+", (parent / "CLAUDE.md").read_text()).group(0)
        return result(stdout=f"{nonce} {extra}")

    monkeypatch.setattr(seats, "LAUNCH", leaky)
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1 and payload["code"] == "verify_seat_not_isolated"
    assert seats.load_probes(target) == {}
    with pytest.raises(seats.SeatRefused):
        seats.ready(target, "verify-propose", "verify-confirm")


@SAFE
def test_a_probe_reply_without_the_nonce_is_inconclusive(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(seats, "LAUNCH", lambda *a, **k: result(stdout="I cannot help with that."))
    assert seats.probe(target, seat) == "verify_seat_probe_inconclusive"


@SAFE
def test_no_journal_report_or_output_holds_prompt_or_reply_text(target, monkeypatch, capsys):
    write_care(target)

    def launch(argv, *, timeout, env, cwd, stdin):
        if stdin.startswith(b"Isolation probe"):
            reply = echo_probe(argv, timeout=timeout, env=env, cwd=cwd, stdin=stdin)
            return result(stdout=reply.stdout + REPLY_SECRET, stderr=PROMPT_SECRET)
        return result(stdout=json.dumps({"reply": REPLY_SECRET}), stderr=PROMPT_SECRET)

    monkeypatch.setattr(seats, "LAUNCH", launch)
    assert cli.main(["memory", "verify", "doctor", "--target", str(target)]) == 0
    out = capsys.readouterr()
    seat = seats.resolve(target, "verify-propose", "propose")
    with vstate.locked(target):
        with seats.work_area(target) as area:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
    stored = all_bytes(target)
    for secret in (PROMPT_SECRET, REPLY_SECRET, "Isolation probe"):
        assert secret.encode() not in stored
        assert secret not in out.out + out.err
    assert not list(target.rglob("*journal*"))
    assert os.listdir(target / seats.WORK_ROOT) == []
