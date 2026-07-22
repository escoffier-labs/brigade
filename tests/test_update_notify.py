"""Tests for the passive update notice."""

from __future__ import annotations

import io
import json
from pathlib import Path

from brigade import __version__, update_notify


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    env.update(extra)
    return env


def _write_state(tmp_path: Path, **state) -> Path:
    path = tmp_path / "cache" / "brigade" / "update-notify.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return path


def _no_spawn() -> None:
    raise AssertionError("spawn should not have been called")


def test_is_newer_table():
    assert update_notify.is_newer("0.26.0", "0.25.1")
    assert update_notify.is_newer("1.0.0", "0.99.9")
    assert not update_notify.is_newer("0.25.1", "0.25.1")
    assert not update_notify.is_newer("0.25.0", "0.25.1")
    assert not update_notify.is_newer("0.26.0rc1", "0.25.1")  # fail closed
    assert not update_notify.is_newer("garbage", "0.25.1")
    assert not update_notify.is_newer("0.26.0", "garbage")


def test_gates_skip_everything(tmp_path):
    err = _Tty()
    calls: list[str] = []

    def spawn() -> None:
        calls.append("spawned")

    base = dict(env=_env(tmp_path), now=1000.0, stderr=err, spawn=spawn)

    update_notify.maybe_notify(
        ["work"], 0, **{**base, "env": _env(tmp_path, BRIGADE_NO_UPDATE_CHECK="1")}
    )
    update_notify.maybe_notify(["work"], 0, **{**base, "env": _env(tmp_path, CI="true")})
    update_notify.maybe_notify(["work"], 1, **base)
    update_notify.maybe_notify(["update"], 0, **base)
    update_notify.maybe_notify(["completions"], 0, **base)
    update_notify.maybe_notify(["work"], 0, **{**base, "stderr": io.StringIO()})  # not a tty

    assert calls == []
    assert err.getvalue() == ""


def test_notifies_from_cache_and_throttles(tmp_path):
    _write_state(tmp_path, checked_at=990.0, latest="99.0.0")
    err = _Tty()
    env = _env(tmp_path)

    update_notify.maybe_notify(["work"], 0, env=env, now=1000.0, stderr=err, spawn=_no_spawn)
    line = err.getvalue()
    assert "99.0.0" in line and __version__ in line and "brigade update" in line
    assert line.count("\n") == 1

    # within 24h: silent
    err2 = _Tty()
    update_notify.maybe_notify(["work"], 0, env=env, now=2000.0, stderr=err2, spawn=_no_spawn)
    assert err2.getvalue() == ""

    # after 24h: notifies again
    err3 = _Tty()
    update_notify.maybe_notify(
        ["work"], 0, env=env, now=1000.0 + 86401.0, stderr=err3, spawn=_no_spawn
    )
    assert "99.0.0" in err3.getvalue()


def test_stale_cache_spawns_refresh_and_older_latest_is_silent(tmp_path):
    _write_state(tmp_path, checked_at=0.0, latest="0.0.1")
    err = _Tty()
    calls: list[str] = []
    update_notify.maybe_notify(
        ["work"],
        0,
        env=_env(tmp_path),
        now=1_000_000.0,
        stderr=err,
        spawn=lambda: calls.append("spawned"),
    )
    assert err.getvalue() == ""
    assert calls == ["spawned"]


def test_fresh_cache_does_not_spawn(tmp_path):
    _write_state(tmp_path, checked_at=999_999.0, latest="0.0.1")
    update_notify.maybe_notify(
        ["work"], 0, env=_env(tmp_path), now=1_000_000.0, stderr=_Tty(), spawn=_no_spawn
    )


def test_missing_and_malformed_cache_spawns(tmp_path):
    calls: list[str] = []

    def spawn() -> None:
        calls.append("x")

    update_notify.maybe_notify(
        ["work"], 0, env=_env(tmp_path), now=1.0, stderr=_Tty(), spawn=spawn
    )
    path = _write_state(tmp_path, checked_at="soon")
    path.write_text("{not json")
    update_notify.maybe_notify(
        ["work"], 0, env=_env(tmp_path), now=1.0, stderr=_Tty(), spawn=spawn
    )
    assert calls == ["x", "x"]


def test_maybe_notify_swallows_exceptions(tmp_path, monkeypatch):
    def boom(_env):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(update_notify, "cache_path", boom)
    update_notify.maybe_notify(
        ["work"], 0, env=_env(tmp_path), now=1.0, stderr=_Tty(), spawn=_no_spawn
    )


def test_run_refresh_success_writes_latest(tmp_path, monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"latest": "0.30.0"}'

    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["ua"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(update_notify.urllib.request, "urlopen", fake_urlopen)
    rc = update_notify.run_refresh(env=_env(tmp_path), now=555.0)
    assert rc == 0
    state = json.loads((tmp_path / "cache" / "brigade" / "update-notify.json").read_text())
    assert state == {"checked_at": 555.0, "latest": "0.30.0"}
    assert captured["url"] == update_notify.CHECK_URL
    assert captured["timeout"] == 5
    assert str(captured["ua"]).startswith(f"brigade-cli/{__version__}")


def test_run_refresh_failure_still_stamps_checked_at(tmp_path, monkeypatch):
    def fake_urlopen(request, timeout):
        raise OSError("endpoint down")

    monkeypatch.setattr(update_notify.urllib.request, "urlopen", fake_urlopen)
    _write_state(tmp_path, checked_at=1.0, latest="0.26.0")
    rc = update_notify.run_refresh(env=_env(tmp_path), now=777.0)
    assert rc == 0
    state = json.loads((tmp_path / "cache" / "brigade" / "update-notify.json").read_text())
    assert state == {"checked_at": 777.0, "latest": "0.26.0"}  # old latest kept


def test_run_refresh_rejects_garbage_latest(tmp_path, monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"latest": ["not", "a", "version"]}'

    monkeypatch.setattr(update_notify.urllib.request, "urlopen", lambda r, timeout: _Resp())
    update_notify.run_refresh(env=_env(tmp_path), now=9.0)
    state = json.loads((tmp_path / "cache" / "brigade" / "update-notify.json").read_text())
    assert state == {"checked_at": 9.0}


def test_run_refresh_honors_optout(tmp_path, monkeypatch):
    def boom(request, timeout):
        raise AssertionError("network call despite opt-out")

    monkeypatch.setattr(update_notify.urllib.request, "urlopen", boom)
    rc = update_notify.run_refresh(env=_env(tmp_path, BRIGADE_NO_UPDATE_CHECK="1"), now=9.0)
    assert rc == 0
    assert not (tmp_path / "cache" / "brigade" / "update-notify.json").exists()
