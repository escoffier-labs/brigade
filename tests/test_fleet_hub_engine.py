"""`brigade fleet serve --engine rust` (S10): binary checks, refusals and the watchdog."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import brigade
from brigade import component_install, component_paths, component_state, fleet_hub, fleet_hub_engine
from brigade.cli import fleet as fleet_cli

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="--engine rust runs on Linux only")


# A stand-in brigade-hub. Like the real one it unblocks the stop signals it
# inherits blocked, and it exits 0 on SIGTERM.
_PRELUDE = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})\n"
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
        "/opt/brigade-hub",
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
    env = {"HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "data")}
    roots = component_install.resolve_roots(env=env, system="linux")
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


def test_verify_binary_refuses_missing_unexecutable_and_altered_files(tmp_path):
    hub, digest = _fake_hub(tmp_path, "sys.exit(0)\n")
    fleet_hub_engine.verify_binary(fleet_hub_engine.Binary(hub, digest, "test"))
    with pytest.raises(fleet_hub_engine.EngineError, match="does not match the checksum"):
        fleet_hub_engine.verify_binary(fleet_hub_engine.Binary(hub, "0" * 64, "test"))
    hub.chmod(0o644)
    with pytest.raises(fleet_hub_engine.EngineError, match="not executable"):
        fleet_hub_engine.verify_binary(fleet_hub_engine.Binary(hub, digest, "test"))
    with pytest.raises(fleet_hub_engine.EngineError, match="missing"):
        fleet_hub_engine.verify_binary(fleet_hub_engine.Binary(tmp_path / "absent", digest, "test"))


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
