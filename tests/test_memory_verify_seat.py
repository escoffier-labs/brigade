"""Verify seats: connector refusal, evidence-based isolation, executable binding and cleanup.

Every seat here is scripted. No test calls a real model: LAUNCH is replaced by
a fake that writes the CLI's stream-json events, the Ollama daemon is a fake
reply, and the one real child process is a local script that never answers.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from brigade import cli, dirfd, proc
from brigade import dispatch_memory as audiences
from brigade import memory_verify_seat as seats
from brigade import memory_verify_seat_area as area_mod
from brigade import memory_verify_state as vstate

SAFE = pytest.mark.skipif(
    not (area_mod.available() and proc.held_exec_supported()),
    reason="needs safe directory descriptors and held-descriptor exec (Linux)",
)
POSIX = pytest.mark.skipif(
    not (dirfd.posix_available() and proc.held_exec_supported()),
    reason="needs no-follow executable opens and held-descriptor exec (Linux)",
)
PROMPT_SECRET = "Quillbramble-prompt-7731"
REPLY_SECRET = "Marrowfen-reply-4402"
ELF = b"\x7fELF" + b"\x00" * 60
DIGEST_A = "a" * 64
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

[agents.local]
cli = "ollama:llama3"
role = "reviewer"
"""


def write_roster(target: Path, extra: str = "", base: str = BASE_ROSTER) -> None:
    path = target / ".brigade" / "roster.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base + textwrap.dedent(extra), encoding="utf-8")


def write_care(target: Path, propose: str = "verify-propose", confirm: str = "verify-confirm") -> None:
    path = target / ".brigade" / "memory-care.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[verify]\npropose_seat = "{propose}"\nconfirm_seat = "{confirm}"\n', encoding="utf-8")


def ollama_daemon(*, remote: bool = False, digest: str = DIGEST_A, fmt: str = "gguf"):
    def api(path, body):
        entry = {"name": "llama3:latest", "model": "llama3:latest", "digest": digest, "size": 4_000_000}
        if path == "/api/tags":
            return {"models": [entry]}
        show = {"details": {"format": fmt}, "model_info": {"general.architecture": "llama"}}
        if remote:
            show["remote_host"] = "https://ollama.example"
            show["remote_model"] = "example-cloud"
        return show

    return api


class Workspace(type(Path())):  # type: ignore[misc]
    """The verify folder, carrying the fake executable and the temporary root the test uses."""

    executable: Path
    temp_root: Path


@pytest.fixture
def target(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "verify")
    workspace.mkdir()
    write_roster(workspace)
    executable = tmp_path / "bin" / "seat-cli"
    executable.parent.mkdir()
    executable.write_bytes(ELF)
    executable.chmod(0o700)
    temp_root = tmp_path / "temproot"
    temp_root.mkdir()

    def resolve(cli_ref: str) -> proc.ExecutableIdentity:
        return proc.ExecutableIdentity(command=cli_ref, path=str(executable), kind="native", runnable=True, detail="")

    monkeypatch.setattr(seats, "RESOLVE", resolve)
    monkeypatch.setattr(seats, "LAUNCH", forbidden_launch)
    monkeypatch.setattr(seats, "OLLAMA_API", ollama_daemon())
    monkeypatch.setattr(area_mod, "TEMP_ROOTS", lambda: [str(temp_root)])
    workspace.executable = executable
    workspace.temp_root = temp_root
    return workspace


def forbidden_launch(*args, **kwargs):
    raise AssertionError("no seat may be launched in this test")


def result(stdout: str = "", stderr: str = "", code: int = 0) -> proc.Result:
    return proc.Result(code=code, stdout=stdout, stderr=stderr)


def init_event(cwd, argv, /, **overrides):
    model = argv[argv.index("--model") + 1]
    event = {
        "type": "system",
        "subtype": "init",
        "cwd": str(cwd),
        "tools": [],
        "mcp_servers": [],
        "slash_commands": [],
        "skills": [],
        "plugins": [],
        "agents": ["claude", "Explore", "general-purpose", "Plan"],
        "additional_directories": [],
        "model": model,
    }
    event.update(overrides)
    return event


def stream(cwd, argv, text, *, init=None, middle=(), is_error=False):
    events = [init or init_event(cwd, argv), *middle, {"type": "result", "is_error": is_error, "result": text}]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def nonce_of(stdin: bytes) -> str:
    return stdin.decode().split()[2].rstrip(".")


def echo_probe(argv, held_fd, *, timeout, env, cwd, stdin, **_):
    """A tool-free seat: its init event is clean and it answers with the nonce."""
    return result(stdout=stream(cwd, argv, f"{nonce_of(stdin)}\n"))


def all_bytes(*roots: Path) -> bytes:
    data = b""
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                data += path.read_bytes()
    return data


def leftovers(target: Path) -> list[str]:
    found = []
    for root in (target / area_mod.WORK_ROOT, *target.temp_root.glob("brigade-verify-*")):
        if root.exists():
            found.extend(os.listdir(root))
    return found


# --- resolution and refusal -------------------------------------------------


@POSIX
def test_claude_profile_disables_every_channel_and_launches_the_canonical_binary(target, tmp_path, monkeypatch):
    link = tmp_path / "bin" / "seat-link"
    link.symlink_to(target.executable)
    monkeypatch.setattr(
        seats,
        "RESOLVE",
        lambda ref: proc.ExecutableIdentity(command=ref, path=str(link), kind="native", runnable=True, detail=""),
    )
    seat = seats.resolve(target, "verify-propose", "propose")
    argv = list(seat.argv)
    assert argv[0] == os.path.realpath(target.executable)
    assert argv[1:3] == ["--model", "claude-sonnet-5-5"]
    for flag in ("--safe-mode", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "enabledPlugins": {f"{name}@builtin": False for name in seats.CLAUDE_BUILTIN_PLUGINS}
    }
    assert seat.identity[-1] == seats.hashlib.sha256(ELF).hexdigest()
    agent = seats.roster_mod.load_roster(target / ".brigade" / "roster.toml").agents["verify-propose"]
    assert seats.audience(agent) == audiences.LOCAL


@POSIX
@pytest.mark.parametrize(
    ("flag", "value"),
    [("--safe-mode", None), ("--tools", "default"), ("--setting-sources", "user"), ("--output-format", "text")],
)
def test_a_profile_missing_an_isolation_switch_is_refused(target, monkeypatch, flag, value):
    builder, required, valued = seats.PROFILES["claude"]

    def leaky(executable, model):
        argv = builder(executable, model)
        if value is None:
            argv.remove(flag)
        else:
            argv[argv.index(flag) + 1] = value
        return argv

    monkeypatch.setitem(seats.PROFILES, "claude", (leaky, required, valued))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "verify-propose", "propose")
    assert caught.value.code == "verify_seat_not_isolated"


@POSIX
def test_a_script_launcher_is_refused(target):
    target.executable.write_text('#!/bin/sh\nexec real-cli "$@"\n', encoding="utf-8")
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "verify-propose", "propose")
    assert caught.value.code == "verify_seat_not_isolated"


@POSIX
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
    assert not (target / area_mod.WORK_ROOT).exists()


@SAFE
@pytest.mark.parametrize("name", ["cloud", "remote-endpoint", "other-node"])
def test_doctor_refuses_cloud_and_remote_seats_without_writing_a_prompt(target, name, capsys):
    write_roster(target, CONNECTOR_SEATS)
    write_care(target, propose=name)
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["code"] == ("verify_seat_unknown" if name == "other-node" else "verify_seat_connector")
    assert not (target / area_mod.WORK_ROOT).exists()
    assert not (target / seats.PROBES).exists()
    assert leftovers(target) == []


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


# --- ollama ---------------------------------------------------------------------


@POSIX
def test_local_ollama_binds_the_manifest_digest(target, monkeypatch):
    seat = seats.resolve(target, "local", "propose")
    assert seat.model_key == ("ollama", "llama3:latest")
    assert list(seat.argv[1:]) == ["run", "--nowordwrap", "--hidethinking", "llama3"]
    assert seat.ollama_digest == DIGEST_A
    monkeypatch.setattr(seats, "OLLAMA_API", ollama_daemon(digest="b" * 64))
    assert seats.resolve(target, "local", "propose").fingerprint != seat.fingerprint


@POSIX
def test_an_ollama_alias_of_a_cloud_model_is_a_connector(target, monkeypatch):
    monkeypatch.setattr(seats, "OLLAMA_API", ollama_daemon(remote=True))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "local", "propose")
    assert caught.value.code == "verify_seat_connector"


@POSIX
def test_ollama_without_provable_local_weights_is_refused(target, monkeypatch):
    monkeypatch.setattr(seats, "OLLAMA_API", ollama_daemon(fmt=""))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "local", "propose")
    assert caught.value.code == "verify_seat_not_isolated"

    def down(path, body):
        raise OSError("connection refused")

    monkeypatch.setattr(seats, "OLLAMA_API", down)
    with pytest.raises(seats.SeatRefused) as caught:
        seats.resolve(target, "local", "propose")
    assert caught.value.code == "verify_seat_unavailable"


@SAFE
def test_an_ollama_model_swapped_after_resolution_is_refused_before_launch(target, monkeypatch):
    seat = seats.resolve(target, "local", "propose")
    monkeypatch.setattr(seats, "OLLAMA_API", ollama_daemon(digest="c" * 64))
    with seats.work_area(target) as area:
        with pytest.raises(seats.SeatRefused) as caught:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert caught.value.code == "verify_seat_not_isolated"


@SAFE
@pytest.mark.parametrize(
    ("stdout", "stderr", "status", "fatal"),
    [
        ("", "Error: 429 Too Many Requests", "seat_quota", False),
        ("", "Error: 401 unauthorized", "seat_auth", True),
        ("", "something else broke", "seat_failed", False),
    ],
)
def test_plain_text_failures_map_to_fixed_codes(target, monkeypatch, stdout, stderr, status, fatal):
    seat = seats.resolve(target, "local", "propose")
    monkeypatch.setattr(seats, "LAUNCH", lambda *a, **k: result(stdout=stdout, stderr=stderr, code=1))
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.fatal) == (status, fatal)


# --- evidence from the init event -------------------------------------------------


@SAFE
@pytest.mark.parametrize(
    "overrides",
    [
        {"tools": ["Read"]},
        {"mcp_servers": [{"name": "memory", "status": "connected"}]},
        {"plugins": [{"name": "cc-plugin-agents-md"}]},
        {"slash_commands": ["review"]},
        {"skills": ["notes"]},
        {"agents": ["claude", "project-agent"]},
        {"additional_directories": ["/srv/example"]},
        {"cwd": "/srv/elsewhere"},
        {"model": "claude-haiku-5-5"},
        {"tools": None},
    ],
)
def test_a_call_is_refused_unless_its_init_event_proves_isolation(target, monkeypatch, overrides):
    seat = seats.resolve(target, "verify-propose", "propose")

    def launch(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        init = init_event(cwd, argv, **overrides)
        if overrides.get("tools", []) is None:
            del init["tools"]
        return result(stdout=stream(cwd, argv, '{"ok": true}', init=init))

    monkeypatch.setattr(seats, "LAUNCH", launch)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.fatal, reply.text) == ("verify_seat_not_isolated", True, "")


@SAFE
@pytest.mark.parametrize(
    "middle",
    [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        {"type": "system", "subtype": "hook_response"},
        {"type": "system", "subtype": "init"},
    ],
)
def test_a_tool_use_or_hook_event_in_the_stream_is_refused(target, monkeypatch, middle):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(
        seats, "LAUNCH", lambda argv, held_fd, **k: result(stdout=stream(k["cwd"], argv, "{}", middle=[middle]))
    )
    with seats.work_area(target) as area:
        assert seats.call(area, seat, PROMPT_SECRET, timeout=30).status == "verify_seat_not_isolated"


@SAFE
def test_a_reply_without_an_init_event_is_not_isolated(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(seats, "LAUNCH", lambda *a, **k: result(stdout='{"answer": 1}\n'))
    with seats.work_area(target) as area:
        assert seats.call(area, seat, PROMPT_SECRET, timeout=30).status == "verify_seat_not_isolated"


@SAFE
@pytest.mark.parametrize(
    ("error", "text", "status", "fatal"),
    [
        ("rate_limit", "Claude AI usage limit reached", "seat_quota", False),
        ("authentication_failed", "Not logged in. Please run /login", "seat_auth", True),
        (None, "Internal failure", "seat_failed", False),
    ],
)
def test_stream_errors_map_to_fixed_codes(target, monkeypatch, error, text, status, fatal):
    seat = seats.resolve(target, "verify-propose", "propose")
    middle = [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    if error:
        middle[0]["error"] = error

    def launch(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        return result(stdout=stream(cwd, argv, text, middle=middle, is_error=True), code=1)

    monkeypatch.setattr(seats, "LAUNCH", launch)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.fatal) == (status, fatal)


@SAFE
def test_a_reply_that_mentions_a_quota_is_still_a_reply(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    monkeypatch.setattr(
        seats, "LAUNCH", lambda argv, held_fd, **k: result(stdout=stream(k["cwd"], argv, '{"note": "rate limit 429"}'))
    )
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.text) == ("ok", '{"note": "rate limit 429"}')


# --- executable binding -----------------------------------------------------------


@SAFE
def test_an_executable_changed_after_resolution_is_refused_before_launch(target):
    seat = seats.resolve(target, "verify-propose", "propose")
    target.executable.write_bytes(ELF + b"patched")
    with seats.work_area(target) as area:
        with pytest.raises(seats.SeatRefused) as caught:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
        assert dirfd.child_names(area.files_fd, 8) == []
    assert caught.value.code == "verify_seat_not_isolated"


@SAFE
def test_a_probe_record_binds_the_executable_content(target, monkeypatch, capsys):
    write_care(target)
    monkeypatch.setattr(seats, "LAUNCH", echo_probe)
    assert cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    seats.ready(target, "verify-propose", "verify-confirm")
    stat_before = os.stat(target.executable)
    target.executable.write_bytes(ELF[:-1] + b"\x01")
    os.utime(target.executable, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.ready(target, "verify-propose", "verify-confirm")
    assert caught.value.code == "verify_seat_not_isolated"


def test_platforms_without_held_descriptor_exec_refuse_every_seat(target, monkeypatch):
    monkeypatch.setattr(seats.proc, "held_exec_supported", lambda: False)
    for name in ("verify-propose", "local"):
        with pytest.raises(seats.SeatRefused) as caught:
            seats.resolve(target, name, "propose")
        assert caught.value.code == "verify_seat_not_isolated"


def _native_echo() -> str | None:
    found = shutil.which("echo")
    if found is None:
        return None
    real = os.path.realpath(found)
    with open(real, "rb") as handle:
        return real if handle.read(4) == b"\x7fELF" else None


@SAFE
def test_a_binary_swapped_after_verification_never_receives_the_prompt(target, tmp_path, monkeypatch):
    echo = _native_echo()
    if echo is None:
        pytest.skip("needs a native echo binary")
    # The verified seat binary is a real native program that prints its arguments.
    shutil.copyfile(echo, target.executable)
    target.executable.chmod(0o700)
    marker = tmp_path / "swapped-binary-ran"
    seat = seats.resolve(target, "verify-propose", "propose")
    seen = {}

    def swap_then_spawn(argv, held_fd, **kwargs):
        # After verification, before the spawn: put a different program at the canonical path.
        replacement = tmp_path / "replacement"
        replacement.write_text(f"#!/bin/sh\ncat > /dev/null\ntouch {marker}\n", encoding="utf-8")
        replacement.chmod(0o700)
        os.replace(replacement, target.executable)
        seen["fds"] = (held_fd,)
        seen["result"] = proc.run_held(argv, held_fd, **kwargs)
        return seen["result"]

    monkeypatch.setattr(seats, "LAUNCH", swap_then_spawn)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
        for fd in seen["fds"]:
            with pytest.raises(OSError):
                os.fstat(fd)
    assert not marker.exists()
    # The held image ran: echo printed the profile flags. That is not stream-json, so the call fails.
    assert seen["result"].code == 0 and "--safe-mode" in seen["result"].stdout
    assert reply.status == "seat_failed"
    assert leftovers(target) == []


# --- the work area ------------------------------------------------------------------


def test_the_work_area_fails_closed_without_safe_directories(target, monkeypatch):
    monkeypatch.setattr(area_mod, "available", lambda: False)
    with pytest.raises(seats.SeatRefused) as caught:
        with seats.work_area(target):
            pass
    assert caught.value.code == "verify_workdir_unavailable"
    assert not (target / area_mod.WORK_ROOT).exists()


@SAFE
def test_the_working_directory_has_no_instruction_files_above_it(target, tmp_path, monkeypatch):
    dirty = tmp_path / "dirty" / "nested"
    dirty.mkdir(parents=True)
    (tmp_path / "dirty" / "AGENTS.md").write_text("instructions", encoding="utf-8")
    monkeypatch.setattr(area_mod, "TEMP_ROOTS", lambda: [str(dirty), str(target.temp_root)])
    with seats.work_area(target) as area:
        assert area.cwd.is_relative_to(os.path.realpath(target.temp_root))
        assert not area.cwd.is_relative_to(target)
        for parent in area.cwd.parents:
            assert not any((parent / name).exists() for name in area_mod.INSTRUCTION_NAMES)
    monkeypatch.setattr(area_mod, "TEMP_ROOTS", lambda: [str(dirty)])
    with pytest.raises(seats.SeatRefused) as caught:
        with seats.work_area(target):
            pass
    assert caught.value.code == "verify_seat_not_isolated"


@SAFE
def test_work_area_and_files_are_owner_only_and_gone_after_the_call(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")
    seen = {}

    def launch(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        run_dir = Path(cwd).parent
        files = next((target / area_mod.WORK_ROOT).iterdir())
        seen["modes"] = {
            str(path): stat.S_IMODE(os.lstat(path).st_mode)
            for path in (run_dir.parent, run_dir, Path(cwd), run_dir / "tmp", files, *files.glob("*.prompt"))
        }
        seen["prompt"] = next(files.glob("*.prompt")).read_bytes()
        seen["env"] = env
        seen["cwd_entries"] = os.listdir(cwd)
        return result(stdout=stream(cwd, argv, json.dumps({"answer": REPLY_SECRET})))

    monkeypatch.setattr(seats, "LAUNCH", launch)
    monkeypatch.setenv("BRIGADE_MEMORY_HUB_TOKEN_FILE", "/nonexistent/token")
    monkeypatch.setenv("OLLAMA_HOST", "203.0.113.20:11434")
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
        files = next((target / area_mod.WORK_ROOT).iterdir())
        replies = list(files.glob("*.reply"))
        assert len(replies) == 1 and REPLY_SECRET in replies[0].read_text()
        assert stat.S_IMODE(os.lstat(replies[0]).st_mode) == 0o600
    assert reply.status == "ok" and REPLY_SECRET in reply.text
    assert seen["prompt"] == PROMPT_SECRET.encode()
    assert seen["cwd_entries"] == []
    for path, mode in seen["modes"].items():
        assert mode == (0o600 if path.endswith(".prompt") else 0o700), path
    assert "BRIGADE_MEMORY_HUB_TOKEN_FILE" not in seen["env"]
    assert "OLLAMA_HOST" not in seen["env"]
    assert seen["env"]["TMPDIR"].endswith("/tmp")
    assert leftovers(target) == []


@SAFE
def test_files_are_removed_after_a_timeout_and_after_an_interrupt(target, monkeypatch, tmp_path):
    # A local script stands in for the seat process. It never answers.
    script = tmp_path / "bin" / "scripted-seat"
    script.write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
    script.chmod(0o700)
    monkeypatch.setattr(
        seats,
        "LAUNCH",
        lambda argv, held_fd, **kwargs: proc.run([str(script), *argv[1:]], **kwargs),
    )
    seat = seats.resolve(target, "verify-propose", "propose")
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=1)
    assert reply.status == "seat_timeout" and not reply.fatal
    assert leftovers(target) == []

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(seats, "LAUNCH", interrupted)
    with pytest.raises(KeyboardInterrupt):
        with seats.work_area(target) as area:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert leftovers(target) == []
    assert PROMPT_SECRET.encode() not in all_bytes(target, target.temp_root)


@SAFE
def test_an_error_from_the_callers_block_passes_through_and_still_cleans_up(target):
    with pytest.raises(FileNotFoundError):
        with seats.work_area(target):
            raise FileNotFoundError("caller")
    assert leftovers(target) == []


@SAFE
def test_a_cleanup_failure_is_recorded_and_blocks_new_areas_until_removed(target, monkeypatch):
    real = dirfd.remove_tree_child
    monkeypatch.setattr(area_mod.dirfd, "remove_tree_child", lambda parent, name: (_ for _ in ()).throw(OSError()))
    with pytest.raises(RuntimeError) as caught:
        with seats.work_area(target):
            raise RuntimeError("primary")
    assert str(caught.value) == "primary"
    assert area_mod.CLEANUP_NOTE in caught.value.__notes__
    with pytest.raises(seats.SeatRefused) as cleanup:
        with seats.work_area(target):
            pass
    assert cleanup.value.code == "verify_workdir_stale"
    monkeypatch.setattr(area_mod.dirfd, "remove_tree_child", real)
    with seats.work_area(target):
        pass
    assert leftovers(target) == []


@SAFE
def test_a_normal_exit_whose_cleanup_fails_is_refused(target, monkeypatch):
    calls = []

    def fail_once(parent, name):
        calls.append(name)
        raise OSError("busy")

    with pytest.raises(seats.SeatRefused) as caught:
        with seats.work_area(target):
            monkeypatch.setattr(area_mod.dirfd, "remove_tree_child", fail_once)
    assert caught.value.code == "verify_workdir_cleanup"
    assert len(calls) == 2


@SAFE
def test_leftover_call_directories_are_swept_in_batches(target, monkeypatch):
    work = target / area_mod.WORK_ROOT
    for index in range(area_mod.SWEEP_BATCH + 6):
        leftover = work / f"call-{index:032x}"
        (leftover / "cwd").mkdir(parents=True)
        (leftover / "0001-propose.prompt").write_text(PROMPT_SECRET, encoding="utf-8")
    with seats.work_area(target) as area:
        assert len(os.listdir(work)) == 1
        assert os.listdir(area.cwd) == []
    assert leftovers(target) == []
    monkeypatch.setattr(area_mod, "SWEEP_MAX", 3)
    for index in range(5):
        (work / f"call-{index:032x}").mkdir()
    with pytest.raises(seats.SeatRefused) as caught:
        with seats.work_area(target):
            pass
    assert caught.value.code == "verify_workdir_stale"


@SAFE
def test_a_seat_that_writes_in_its_working_directory_is_not_isolated(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")

    def writer(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        (Path(cwd) / "notes.txt").write_text("written", encoding="utf-8")
        return result(stdout=stream(cwd, argv, "{}"))

    monkeypatch.setattr(seats, "LAUNCH", writer)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert reply.status == "verify_seat_not_isolated" and reply.fatal
    assert leftovers(target) == []


@SAFE
def test_a_working_directory_replaced_during_the_call_is_not_isolated(target, monkeypatch):
    seat = seats.resolve(target, "verify-propose", "propose")

    def swapper(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        os.rename(cwd, f"{cwd}-moved")
        os.mkdir(cwd, 0o700)
        return result(stdout=stream(cwd, argv, "{}"))

    monkeypatch.setattr(seats, "LAUNCH", swapper)
    with seats.work_area(target) as area:
        reply = seats.call(area, seat, PROMPT_SECRET, timeout=30)
    assert (reply.status, reply.fatal) == ("verify_seat_not_isolated", True)
    assert leftovers(target) == []


@SAFE
def test_an_oversized_prompt_is_refused_before_a_file_is_written(target):
    seat = seats.resolve(target, "verify-propose", "propose")
    with seats.work_area(target) as area:
        with pytest.raises(seats.SeatRefused) as caught:
            seats.call(area, seat, "x" * (seats.MAX_PROMPT_BYTES + 1), timeout=30)
        assert caught.value.code == "verify_prompt_too_large"
        assert dirfd.child_names(area.files_fd, 8) == []


# --- the isolation probe and the doctor ---------------------------------------


@SAFE
def test_doctor_records_passing_probes_and_ready_accepts_only_that_fingerprint(target, monkeypatch, capsys):
    write_care(target)
    prompts = []

    def launch(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        prompts.append(stdin)
        assert {"AGENTS.md", "CLAUDE.md", "canary.txt", "cwd", "tmp"} <= set(os.listdir(Path(cwd).parent))
        assert os.listdir(cwd) == []
        return echo_probe(argv, held_fd, timeout=timeout, env=env, cwd=cwd, stdin=stdin)

    monkeypatch.setattr(seats, "LAUNCH", launch)
    code = cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["status"] == "ok"
    assert [row["status"] for row in payload["seats"]] == ["isolated", "isolated"]
    assert len(prompts) == 2
    assert stat.S_IMODE(os.lstat(target / seats.PROBES).st_mode) == 0o600
    propose, confirm = seats.ready(target, "verify-propose", "verify-confirm")
    assert (propose.role, confirm.role) == ("propose", "confirm")

    write_roster(target, base=BASE_ROSTER.replace("claude-sonnet-5-5", "claude-haiku-5-5"))
    with pytest.raises(seats.SeatRefused) as caught:
        seats.ready(target, "verify-propose", "verify-confirm")
    assert caught.value.code == "verify_seat_not_isolated"


@SAFE
def test_a_probe_with_tools_in_its_init_event_fails_whatever_the_model_says(target, monkeypatch, capsys):
    write_care(target)

    def honest_words_wrong_init(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        return result(stdout=stream(cwd, argv, nonce_of(stdin), init=init_event(cwd, argv, tools=["Bash"])))

    monkeypatch.setattr(seats, "LAUNCH", honest_words_wrong_init)
    assert cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "verify_seat_not_isolated"
    assert seats.load_probes(target) == {}


@SAFE
@pytest.mark.parametrize("leak", ["canary", "word"])
def test_a_seat_that_reads_the_canary_or_sees_project_instructions_fails_the_probe(target, monkeypatch, leak, capsys):
    write_care(target)

    def leaky(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        parent = Path(cwd).parent
        source = parent / ("canary.txt" if leak == "canary" else "CLAUDE.md")
        return result(stdout=stream(cwd, argv, f"{nonce_of(stdin)} {source.read_text()}"))

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
    monkeypatch.setattr(
        seats, "LAUNCH", lambda argv, held_fd, **k: result(stdout=stream(k["cwd"], argv, "I cannot help with that."))
    )
    assert seats.probe(target, seat) == "verify_seat_probe_inconclusive"


@SAFE
def test_no_journal_report_or_output_holds_prompt_or_reply_text(target, monkeypatch, capsys):
    write_care(target)

    def launch(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        if stdin.startswith(b"Isolation probe"):
            return result(stdout=stream(cwd, argv, nonce_of(stdin) + REPLY_SECRET), stderr=PROMPT_SECRET)
        return result(stdout=stream(cwd, argv, json.dumps({"reply": REPLY_SECRET})), stderr=PROMPT_SECRET)

    monkeypatch.setattr(seats, "LAUNCH", launch)
    assert cli.main(["memory", "verify", "doctor", "--target", str(target)]) == 0
    out = capsys.readouterr()
    seat = seats.resolve(target, "verify-propose", "propose")
    with vstate.locked(target):
        with seats.work_area(target) as area:
            seats.call(area, seat, PROMPT_SECRET, timeout=30)
    stored = all_bytes(target, target.temp_root)
    for secret in (PROMPT_SECRET, REPLY_SECRET, "Isolation probe"):
        assert secret.encode() not in stored
        assert secret not in out.out + out.err
    assert not list(target.rglob("*journal*"))
    assert leftovers(target) == []


@SAFE
def test_a_failed_probe_is_invalidated_on_disk_before_the_next_seat_runs(target, monkeypatch, capsys):
    write_care(target)
    monkeypatch.setattr(seats, "LAUNCH", echo_probe)
    assert cli.main(["memory", "verify", "doctor", "--target", str(target), "--json"]) == 0
    capsys.readouterr()
    seats.ready(target, "verify-propose", "verify-confirm")
    calls = []

    def first_fails_second_interrupted(argv, held_fd, *, timeout, env, cwd, stdin, **_):
        calls.append(argv)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return result(stdout=stream(cwd, argv, nonce_of(stdin), init=init_event(cwd, argv, tools=["Bash"])))

    monkeypatch.setattr(seats, "LAUNCH", first_fails_second_interrupted)
    propose, confirm = seats.resolve_pair(target, "verify-propose", "verify-confirm")
    with pytest.raises(KeyboardInterrupt):
        seats.doctor_payload(target, "verify-propose", "verify-confirm", timeout=30)
    assert len(calls) == 2
    assert propose.name not in seats.load_probes(target)
    assert confirm.name not in seats.load_probes(target)
    with pytest.raises(seats.SeatRefused) as caught:
        seats.ready(target, "verify-propose", "verify-confirm")
    assert caught.value.code == "verify_seat_not_isolated" and "propose_seat" in str(caught.value)
    assert leftovers(target) == []
