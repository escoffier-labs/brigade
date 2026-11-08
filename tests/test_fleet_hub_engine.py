"""`brigade fleet serve --engine rust` (S10): binary checks, refusals and the watchdog."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import brigade
from brigade import component_install, component_paths, component_state, fleet_hub, fleet_hub_engine
from brigade.cli import fleet as fleet_cli

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="brigade-hub ships for Linux only; these run or check the binary (O_NOFOLLOW, owner and mode bits, /proc)",
)


# A stand-in brigade-hub. Like the real one it unblocks the stop signals it
# inherits blocked, and it exits 0 on SIGTERM.
_PRELUDE = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})\n"
    # brigade-hub reports its bound address on stderr; the watchdog waits for it.
    "sys.stderr.write('listening on 127.0.0.1:3774\\n'); sys.stderr.flush()\n"
)
SERVE = "while True:\n    time.sleep(0.05)\n"


def _fake_hub(tmp_path: Path, body: str) -> tuple[Path, str]:
    path = tmp_path / "brigade-hub"
    path.write_text(f"#!{sys.executable}\n" + _PRELUDE + body)
    path.chmod(0o755)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _short_stop_grace(monkeypatch):
    monkeypatch.setattr(fleet_hub_engine, "STOP_GRACE", 5.0)


def _run(tmp_path: Path, binary: Path, digest: str, *, env: dict[str, str] | None = None, **kwargs) -> int:
    return fleet_hub_engine.run(
        host="127.0.0.1",
        port=3774,
        db_path=tmp_path / "hub.db",
        token_file=None,
        allow_admin_writes=False,
        require_sealed=False,
        deck_config_path=None,
        trust_tailscale_identity=False,
        trust_forwarded_proto=False,
        hub_binary=binary,
        hub_sha256=digest,
        env=env if env is not None else {},
        **kwargs,
    )


def test_hub_argv_keeps_every_serve_flag_and_route_modes(tmp_path):
    argv = fleet_hub_engine.hub_argv(
        Path("/opt/brigade-hub"),
        host="127.0.0.1",
        port=4000,
        db_path=tmp_path / "hub.db",
        token_file=tmp_path / "token",
        allow_admin_writes=True,
        require_sealed=True,
        deck_config_path=tmp_path / "deck.json",
        trust_tailscale_identity=True,
        trust_forwarded_proto=True,
        route_modes=("claims=native", "memory=native"),
    )
    assert argv == [
        str(Path("/opt/brigade-hub")),
        "--host",
        "127.0.0.1",
        "--port",
        "4000",
        "--db",
        str(tmp_path / "hub.db"),
        "--token-file",
        str(tmp_path / "token"),
        "--deck-config",
        str(tmp_path / "deck.json"),
        "--allow-admin-writes",
        "--require-sealed",
        "--trust-tailscale-identity",
        "--trust-forwarded-proto",
        "--route-mode",
        "claims=native",
        "--route-mode",
        "memory=native",
    ]


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("127.0.0.1", "http://127.0.0.1:3774/health"),
        ("0.0.0.0", "http://127.0.0.1:3774/health"),
        ("::", "http://[::1]:3774/health"),
        ("::1", "http://[::1]:3774/health"),
        ("100.64.0.7", "http://100.64.0.7:3774/health"),
    ],
)
def test_health_url_probes_loopback_for_wildcard_binds(host, url):
    assert fleet_hub_engine.health_url(host, 3774) == url


def test_resolve_binary_requires_binary_and_checksum_together(tmp_path):
    with pytest.raises(fleet_hub_engine.EngineError, match="needs --hub-sha256 or --hub-sha256-file"):
        fleet_hub_engine.resolve_binary(tmp_path / "hub", None)
    with pytest.raises(fleet_hub_engine.EngineError, match="needs --hub-sha256 or --hub-sha256-file"):
        fleet_hub_engine.resolve_binary(None, "a" * 64)
    with pytest.raises(fleet_hub_engine.EngineError, match="cannot be used together"):
        fleet_hub_engine.resolve_binary(tmp_path / "hub", "a" * 64, hub_sha256_file=tmp_path / "sum")
    with pytest.raises(fleet_hub_engine.EngineError, match="64 hexadecimal"):
        fleet_hub_engine.resolve_binary(tmp_path / "hub", "xyz")


def _install_state(tmp_path: Path, *, version: str, components: dict[str, Path]) -> dict[str, str]:
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }
    # This host's roots, as `brigade setup` and resolve_binary use them.
    roots = component_install.resolve_roots(env=env)
    records = {
        cid: component_state.InstalledComponentRecord(
            component_revision="a" * 40,
            asset_name=f"{cid}-linux-amd64",
            byte_size=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            download_url=f"https://example.invalid/{cid}-linux-amd64",
            executable=str(path),
        )
        for cid, path in components.items()
    }
    state = component_state.InstalledState(
        schema_version=component_state.SCHEMA_VERSION,
        brigade_version=version,
        manifest_revision="fixture",
        platform="linux-amd64",
        installed_at="2026-11-08T00:00:00+00:00",
        components=records,
    )
    path = Path(component_paths.installed_state_path(roots.data_root))
    path.parent.mkdir(parents=True, exist_ok=True)
    component_state.write_installed_state(path, state)
    return env


def test_resolve_binary_reads_a_checksum_file_like_sha256sum_writes(tmp_path):
    checksum = tmp_path / "brigade-hub.sha256"
    checksum.write_text("A" * 64 + "  brigade-hub\n")
    binary = fleet_hub_engine.resolve_binary(tmp_path / "hub", None, hub_sha256_file=checksum)
    assert binary.sha256 == "a" * 64
    with pytest.raises(fleet_hub_engine.EngineError, match="cannot read a SHA-256"):
        fleet_hub_engine.resolve_binary(tmp_path / "hub", None, hub_sha256_file=tmp_path / "absent")
    checksum.write_text("")
    with pytest.raises(fleet_hub_engine.EngineError, match="cannot read a SHA-256"):
        fleet_hub_engine.resolve_binary(tmp_path / "hub", None, hub_sha256_file=checksum)


def test_resolve_binary_reads_the_setup_record(tmp_path):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    env = _install_state(tmp_path, version=brigade.__version__, components={"brigade-hub": hub})
    binary = fleet_hub_engine.resolve_binary(None, None, env=env)
    assert (binary.path, binary.sha256) == (hub, digest)


def test_resolve_binary_names_setup_when_the_hub_is_not_installed(tmp_path):
    other, _ = _fake_hub(tmp_path, "sys.exit(0)\n")
    env = _install_state(tmp_path, version=brigade.__version__, components={"sessionfind": other})
    with pytest.raises(fleet_hub_engine.EngineError, match="brigade setup --component brigade-hub"):
        fleet_hub_engine.resolve_binary(None, None, env=env)


def test_resolve_binary_refuses_a_hub_from_another_release(tmp_path):
    hub, _ = _fake_hub(tmp_path, "sys.exit(0)\n")
    env = _install_state(tmp_path, version="0.0.1", components={"brigade-hub": hub})
    with pytest.raises(fleet_hub_engine.EngineError, match="from Brigade 0.0.1"):
        fleet_hub_engine.resolve_binary(None, None, env=env)


def _refused(binary: fleet_hub_engine.Binary, match: str) -> None:
    with pytest.raises(fleet_hub_engine.EngineError, match=match):
        os.close(fleet_hub_engine.open_binary(binary))


@linux_only
def test_open_binary_checks_the_descriptor_it_returns(tmp_path):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    fd = fleet_hub_engine.open_binary(fleet_hub_engine.Binary(hub, digest, "test"))
    try:
        assert os.path.samestat(os.fstat(fd), os.stat(hub))
    finally:
        os.close(fd)
    _refused(fleet_hub_engine.Binary(hub, "0" * 64, "test"), "does not match the checksum")
    _refused(fleet_hub_engine.Binary(tmp_path / "absent", digest, "test"), "missing")
    hub.chmod(0o644)
    _refused(fleet_hub_engine.Binary(hub, digest, "test"), "not executable")


@linux_only
@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group-writable", "world-writable"])
def test_open_binary_refuses_a_binary_others_can_write(tmp_path, mode):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    hub.chmod(mode)
    _refused(fleet_hub_engine.Binary(hub, digest, "test"), "writable by its group or others")


@linux_only
def test_open_binary_refuses_a_symlink_and_a_directory(tmp_path):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    link = tmp_path / "link"
    link.symlink_to(hub)
    _refused(fleet_hub_engine.Binary(link, digest, "test"), "cannot open brigade-hub binary")
    _refused(fleet_hub_engine.Binary(tmp_path, digest, "test"), "not a regular file")


@linux_only
def test_open_binary_refuses_a_binary_owned_by_another_user(tmp_path, monkeypatch):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    monkeypatch.setattr(fleet_hub_engine.os, "geteuid", lambda: os.stat(hub).st_uid + 1)
    if os.stat(hub).st_uid == 0:
        pytest.skip("root owns the fixture, which is always allowed")
    _refused(fleet_hub_engine.Binary(hub, digest, "test"), "not root or this user")


@linux_only
def test_a_binary_swapped_after_the_check_never_runs(tmp_path, monkeypatch):
    ran = tmp_path / "ran"
    original, digest = _fake_hub(tmp_path, f"open({str(ran)!r}, 'w').write('original')\n")
    impostor = tmp_path / "impostor"
    impostor.write_text(f"#!{sys.executable}\nopen({str(ran)!r}, 'w').write('impostor')\n")
    impostor.chmod(0o755)
    real_init = fleet_hub.init_db

    def init_then_swap(path, *args, **kwargs):
        # Between the digest check and the exec, replace the directory entry.
        conn = real_init(path, *args, **kwargs)
        os.replace(impostor, original)
        return conn

    monkeypatch.setattr(fleet_hub, "init_db", init_then_swap)
    _run(tmp_path, original, digest)
    assert ran.read_text() == "original"


@linux_only
def test_port_zero_never_probes_port_zero(tmp_path, capsys):
    script = tmp_path / "brigade-hub"
    script.write_text(
        f"#!{sys.executable}\nimport sys, time\n"
        "sys.stderr.write('starting\\nlistening on [::1]:43123\\n'); sys.stderr.flush()\ntime.sleep(1)\n"
    )
    script.chmod(0o755)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    probed: list[str] = []
    code = fleet_hub_engine.run(
        host="::1",
        port=0,
        db_path=tmp_path / "hub.db",
        token_file=None,
        allow_admin_writes=False,
        require_sealed=False,
        deck_config_path=None,
        trust_tailscale_identity=False,
        trust_forwarded_proto=False,
        hub_binary=script,
        hub_sha256=digest,
        env=_watchdog_env(),
        healthy=lambda url: probed.append(url) or True,
        only_loopback=lambda: False,
    )
    assert code == 0
    assert probed and set(probed) == {"http://[::1]:43123/health"}
    captured = capsys.readouterr()
    assert "listening on [::1]:43123 (" in captured.out
    # brigade-hub's own stderr still reaches ours.
    assert "starting" in captured.err


@linux_only
def test_a_child_reporting_a_foreign_host_is_never_probed_and_the_start_is_refused(tmp_path, capsys):
    hub, digest = _fake_hub(
        tmp_path,
        # Replaces the prelude's line: this child claims a host on the network.
        "sys.stderr.write('listening on 10.0.0.5:8080\\n'); sys.stderr.flush()\n" + SERVE,
    )
    hub.write_text(hub.read_text().replace("listening on 127.0.0.1:3774", "starting"))
    digest = hashlib.sha256(hub.read_bytes()).hexdigest()
    probed: list[str] = []
    code = _run(
        tmp_path,
        hub,
        digest,
        env=_watchdog_env(),
        healthy=lambda url: probed.append(url) or True,
        only_loopback=lambda: False,
    )
    assert code == 1
    assert probed == []
    captured = capsys.readouterr()
    assert "reported a non-loopback address '10.0.0.5'" in captured.err
    assert "listening on 10.0.0.5" not in captured.out


@pytest.mark.parametrize(
    ("configured", "reported", "url"),
    [
        ("127.0.0.1", ("127.0.0.1", 4000), "http://127.0.0.1:4000/health"),
        ("0.0.0.0", ("0.0.0.0", 4000), "http://127.0.0.1:4000/health"),
        ("::", ("::", 4000), "http://[::1]:4000/health"),
        ("::1", ("::1", 4000), "http://[::1]:4000/health"),
        # The probe host comes from the configuration, never from the child.
        ("127.0.0.1", ("127.0.0.2", 4000), "http://127.0.0.1:4000/health"),
        ("100.64.0.7", ("100.64.0.7", 4000), "http://100.64.0.7:4000/health"),
        ("127.0.0.1", ("10.0.0.5", 8080), None),
        ("127.0.0.1", ("evil.example", 8080), None),
        ("100.64.0.7", ("100.64.0.8", 4000), None),
    ],
)
def test_probe_url_takes_only_the_port_from_the_child(configured, reported, url):
    assert fleet_hub_engine.probe_url(configured, reported) == url


def test_bound_address_reads_brigade_hub_listening_lines():
    assert fleet_hub_engine.bound_address(b"listening on 127.0.0.1:3774\n") == ("127.0.0.1", 3774)
    assert fleet_hub_engine.bound_address(b"listening on [::1]:40000\n") == ("::1", 40000)
    assert fleet_hub_engine.bound_address(b"listening on nowhere\n") is None
    assert fleet_hub_engine.bound_address(b"error: listening on 1.2.3.4:5\n") is None


@linux_only
def test_altered_binary_never_runs_and_never_touches_the_database(tmp_path, capsys):
    marker = tmp_path / "ran"
    hub, _ = _fake_hub(tmp_path, f"open({str(marker)!r}, 'w').close()\n")
    assert _run(tmp_path, hub, "0" * 64) == 2
    assert "does not match the checksum" in capsys.readouterr().err
    assert not marker.exists()
    assert not (tmp_path / "hub.db").exists()


@linux_only
def test_newer_schema_is_refused_before_the_hub_starts(tmp_path, capsys):
    marker = tmp_path / "ran"
    hub, digest = _fake_hub(tmp_path, f"open({str(marker)!r}, 'w').close()\n")
    fleet_hub.init_db(tmp_path / "hub.db").close()
    with sqlite3.connect(tmp_path / "hub.db") as conn:
        conn.execute(f"PRAGMA user_version={fleet_hub.SCHEMA_VERSION + 1}")
    assert _run(tmp_path, hub, digest) == 2
    assert f"has schema version {fleet_hub.SCHEMA_VERSION + 1}" in capsys.readouterr().err
    assert not marker.exists()


@linux_only
def test_migrates_then_runs_the_hub_with_this_interpreter(tmp_path, capsys):
    seen = tmp_path / "seen"
    hub, digest = _fake_hub(
        tmp_path,
        f"open({str(seen)!r}, 'w').write('\\n'.join([os.environ['BRIGADE_HUB_PYTHON'], *sys.argv[1:]]))\nsys.exit(3)\n",
    )
    assert _run(tmp_path, hub, digest, route_modes=("claims=native",)) == 3
    lines = seen.read_text().splitlines()
    assert lines[0] == (os.environ.get("BRIGADE_HUB_PYTHON") or sys.executable)
    assert lines[1:3] == ["--host", "127.0.0.1"]
    assert lines[-2:] == ["--route-mode", "claims=native"]
    with sqlite3.connect(tmp_path / "hub.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert "brigade-hub exited with 3" in capsys.readouterr().err


@linux_only
def test_a_restored_rollback_journal_file_is_switched_to_wal_before_the_hub_starts(tmp_path):
    # `fleet restore` publishes its file with journal_mode=DELETE, and init_db
    # leaves a current schema alone. brigade-hub's zero-wait readers need WAL.
    fleet_hub.init_db(tmp_path / "hub.db").close()
    with sqlite3.connect(tmp_path / "hub.db") as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    seen = tmp_path / "journal"
    hub, digest = _fake_hub(
        tmp_path,
        "import sqlite3\n"
        f"db = sqlite3.connect({str(tmp_path / 'hub.db')!r})\n"
        f"open({str(seen)!r}, 'w').write(db.execute('PRAGMA journal_mode').fetchone()[0])\n",
    )
    assert _run(tmp_path, hub, digest) == 0
    assert seen.read_text() == "wal"


def _watchdog_env(**extra: str) -> dict[str, str]:
    return {"BRIGADE_HUB_WATCHDOG_INTERVAL": "0.05", "BRIGADE_HUB_WATCHDOG_FAILURES": "3", **extra}


@linux_only
def test_watchdog_stops_a_hub_whose_health_fails(tmp_path, capsys):
    hub, digest = _fake_hub(tmp_path, SERVE)
    answers = iter([True, True])
    started = time.monotonic()
    code = _run(
        tmp_path,
        hub,
        digest,
        env=_watchdog_env(),
        healthy=lambda _url: next(answers, False),
        only_loopback=lambda: False,
    )
    assert code == 1
    assert time.monotonic() - started < 10
    captured = capsys.readouterr()
    assert "brigade fleet hub listening on 127.0.0.1:3774" in captured.out
    assert "/health failed 3 checks in a row" in captured.err


@linux_only
def test_watchdog_stops_a_hub_left_with_only_loopback(tmp_path, capsys):
    hub, digest = _fake_hub(tmp_path, SERVE)
    code = _run(
        tmp_path,
        hub,
        digest,
        env=_watchdog_env(BRIGADE_HUB_WATCHDOG_NETNS="1"),
        healthy=lambda _url: True,
        only_loopback=lambda: True,
    )
    assert code == 1
    assert "no network interface but loopback in 3 checks" in capsys.readouterr().err


@linux_only
def test_negative_namespace_watch_is_off_unless_asked(tmp_path):
    # Without BRIGADE_HUB_WATCHDOG_NETNS the same lonely namespace changes nothing.
    hub, digest = _fake_hub(tmp_path, "time.sleep(0.5)\n")
    assert _run(tmp_path, hub, digest, env=_watchdog_env(), healthy=lambda _url: True, only_loopback=lambda: True) == 0


@linux_only
def test_watchdog_gives_up_on_a_hub_that_never_answers(tmp_path, capsys):
    hub, digest = _fake_hub(tmp_path, SERVE)
    code = _run(
        tmp_path,
        hub,
        digest,
        env=_watchdog_env(BRIGADE_HUB_WATCHDOG_STARTUP="0.3"),
        healthy=lambda _url: False,
        only_loopback=lambda: False,
    )
    assert code == 1
    assert "did not answer within 0.3 s of startup" in capsys.readouterr().err


@linux_only
def test_stop_signal_is_forwarded_and_a_clean_exit_is_reported(tmp_path, capsys):
    # The fake hub sends this process SIGTERM, then exits 0 when it is forwarded.
    hub, digest = _fake_hub(tmp_path, "os.kill(os.getppid(), signal.SIGTERM)\n" + SERVE)
    code = _run(tmp_path, hub, digest, env=_watchdog_env(), healthy=lambda _url: True, only_loopback=lambda: False)
    assert code == 0
    assert "brigade fleet hub stopped cleanly" in capsys.readouterr().out


def test_watchdog_settings_must_be_positive_numbers(tmp_path, capsys):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    if not sys.platform.startswith("linux"):
        assert _run(tmp_path, hub, digest) == 2
        return
    assert _run(tmp_path, hub, digest, env={"BRIGADE_HUB_WATCHDOG_INTERVAL": "zero"}) == 2
    assert "must be a number" in capsys.readouterr().err
    assert _run(tmp_path, hub, digest, env={"BRIGADE_HUB_WATCHDOG_FAILURES": "-1"}) == 2
    assert "must be positive" in capsys.readouterr().err


def _serve_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = dict(
        host="127.0.0.1",
        port=3774,
        db=tmp_path / "hub.db",
        token_file=None,
        allow_admin_writes=False,
        require_sealed=False,
        deck_config=None,
        trust_tailscale_identity=False,
        trust_forwarded_proto=False,
        engine="python",
        route_mode=[],
        hub_binary=None,
        hub_sha256=None,
        hub_sha256_file=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("route_mode", ["claims=native"]),
        ("hub_binary", Path("/opt/brigade-hub")),
        ("hub_sha256", "a" * 64),
        ("hub_sha256_file", Path("/opt/brigade-hub.sha256")),
    ],
)
def test_rust_only_flags_are_refused_with_the_python_engine(tmp_path, monkeypatch, capsys, flag, value):
    monkeypatch.setattr(fleet_hub, "run", lambda **_kwargs: pytest.fail("the Python hub started"))
    assert fleet_cli._dispatch_serve(_serve_args(tmp_path, **{flag: value}), environ={}) == 2
    assert "requires --engine rust" in capsys.readouterr().err


def test_engine_rust_dispatches_every_flag(tmp_path, monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(fleet_hub_engine, "run", lambda **kwargs: captured.update(kwargs) or 0)
    monkeypatch.setattr(fleet_hub, "run", lambda **_kwargs: pytest.fail("the Python hub started"))
    args = _serve_args(
        tmp_path,
        engine="rust",
        route_mode=["claims=native"],
        hub_binary=tmp_path / "hub",
        hub_sha256="a" * 64,
        require_sealed=True,
    )
    assert fleet_cli._dispatch_serve(args, environ={}) == 0
    assert captured["route_modes"] == ("claims=native",)
    assert captured["hub_binary"] == tmp_path / "hub"
    assert captured["hub_sha256"] == "a" * 64
    assert captured["require_sealed"] is True
    assert captured["db_path"] == tmp_path / "hub.db"


def test_default_engine_is_python(tmp_path, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(fleet_hub, "run", lambda **kwargs: seen.update(kwargs) or 0)
    assert fleet_cli._dispatch_serve(_serve_args(tmp_path), environ={}) == 0
    assert seen["host"] == "127.0.0.1"


def _supervise(argv):
    return fleet_hub_engine._supervise(
        argv,
        detail="test",
        interval=1.0,
        failures=3,
        startup=30.0,
        watch_namespace=False,
        healthy=lambda _url: False,
        only_loopback=lambda: False,
    )


@linux_only
def test_a_stop_that_lands_while_the_hub_is_spawning_is_forwarded(monkeypatch, capsys):
    sent = []

    class Child:
        stderr = None

        def __init__(self, argv, **_kwargs):
            # The stop lands mid-spawn: the handler runs before `child` is bound.
            os.kill(os.getpid(), signal.SIGTERM)

        def send_signal(self, signum):
            sent.append(signum)

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(fleet_hub_engine.subprocess, "Popen", Child)
    try:
        code = _supervise(["brigade-hub"])
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert sent == [signal.SIGTERM]
    assert code == 0
    assert capsys.readouterr().out.strip() == fleet_hub_engine.STOPPED


MASK_HUB = """
import signal, sys, time
from pathlib import Path
with open("/proc/self/status", encoding="utf-8") as status:
    blocked = next(line.split()[1] for line in status if line.startswith("SigBlk:"))
Path(sys.argv[-1]).write_text(blocked, encoding="utf-8")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
# As brigade-hub does once its handlers exist: a held stop arrives now.
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
time.sleep(20)
sys.exit(4)
"""


@linux_only
def test_the_hub_starts_with_stop_signals_blocked_so_a_pending_stop_waits(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "brigade-hub"
    fake.write_text(f"#!{sys.executable}\n{MASK_HUB}", encoding="utf-8")
    fake.chmod(0o755)
    real = subprocess.Popen

    def popen(argv, **kwargs):
        # The stop lands during the spawn; it is held, then forwarded.
        os.kill(os.getpid(), signal.SIGTERM)
        return real(argv, **kwargs)

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    monkeypatch.setattr(fleet_hub_engine.subprocess, "Popen", popen)
    try:
        code = _supervise([str(fake), str(tmp_path / "mask")])
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    blocked = int((tmp_path / "mask").read_text(encoding="utf-8"), 16)
    assert blocked & (1 << (signal.SIGTERM - 1)) and blocked & (1 << (signal.SIGINT - 1))
    # The forwarded stop reached the hub's handler, not its default action.
    assert code == 0
    assert capsys.readouterr().out.strip() == fleet_hub_engine.STOPPED
    # This process's own mask is back as it was.
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == mask
