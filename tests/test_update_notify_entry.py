"""Update refresh security through the installed console entry points."""

from __future__ import annotations

import json
import os
import subprocess
import sysconfig
from pathlib import Path

import pytest

from brigade import update_notify
from brigade.claude_hooks.package import PACKAGE_REF


@pytest.fixture
def notify_entry(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    startup = tmp_path / "startup"
    startup.mkdir()
    launches = tmp_path / "refresh-launches.jsonl"
    notified = tmp_path / "notifier-called"
    # Observe the real console process and wait for its real refresh child, so
    # absence of a sentinel is checked after completion rather than a sleep.
    # Preload the installed package before passing hostile PYTHONPATH to children.
    (startup / "sitecustomize.py").write_text(
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from brigade import update_notify\n"
        "if os.environ.get('TEST_HOSTILE_PYTHONPATH'):\n"
        "    os.environ['PYTHONPATH'] = os.environ['TEST_WORKSPACE']\n"
        "original_notify = update_notify.maybe_notify\n"
        "def observed_notify(*args, **kwargs):\n"
        "    Path(os.environ['TEST_NOTIFIER_CALLED']).touch()\n"
        "    return original_notify(*args, **kwargs)\n"
        "update_notify.maybe_notify = observed_notify\n"
        "original_popen = subprocess.Popen\n"
        "def observed_popen(argv, *args, **kwargs):\n"
        "    refresh = '--notify-refresh' in argv\n"
        "    if refresh:\n"
        "        with open(os.environ['TEST_REFRESH_LAUNCHES'], 'a') as stream:\n"
        "            stream.write(json.dumps({'argv': argv, 'cwd': str(kwargs.get('cwd'))}) + '\\n')\n"
        "    process = original_popen(argv, *args, **kwargs)\n"
        "    if refresh:\n"
        "        process.wait(timeout=15)\n"
        "    return process\n"
        # Windows asyncio subclasses subprocess.Popen. Only instrument the
        # notifier's module reference, preserving the real stdlib class.
        "update_notify.subprocess = SimpleNamespace(**vars(subprocess))\n"
        "update_notify.subprocess.Popen = observed_popen\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BRIGADE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    env = dict(os.environ)
    for key in ("BRIGADE_HOME", "BRIGADE_FLEET_HUB_URL", "BRIGADE_FLEET_TOKEN", "BRIGADE_FLEET_NODE_TOKEN"):
        env.pop(key, None)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LOCALAPPDATA=str(home / "local"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_CONFIG_HOME=str(home / "config"),
        PYTHONPATH=os.pathsep.join([str(Path(update_notify.__file__).resolve().parents[1]), str(startup)]),
        TEST_WORKSPACE=str(workspace),
        TEST_REFRESH_LAUNCHES=str(launches),
        TEST_NOTIFIER_CALLED=str(notified),
        # A failed loopback proxy keeps the enabled refresh off the live service.
        HTTPS_PROXY="http://127.0.0.1:1",
        https_proxy="http://127.0.0.1:1",
        NO_PROXY="",
        no_proxy="",
    )
    cache = update_notify.cache_path(env)

    def run(argv, *, entry="brigade"):
        executable = Path(sysconfig.get_path("scripts")) / (entry + (".exe" if os.name == "nt" else ""))
        assert executable.is_file(), f"console entry point is not installed: {executable}"
        environment = dict(env)
        if argv == ["extras", "status"]:
            environment["TEST_HOSTILE_PYTHONPATH"] = "1"
        return subprocess.run(
            [str(executable), *argv],
            cwd=workspace,
            env=environment,
            input="{}",
            text=True,
            capture_output=True,
            timeout=25,
            check=True,
        )

    return workspace, cache, launches, notified, run


@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py"])
@pytest.mark.parametrize("cache_state", ["missing", "stale"])
@pytest.mark.parametrize("command", ["normal", "presence-stdin", "presence-publisher", "claude-hook", "direct-hook"])
def test_entry_never_executes_workspace_update_module(notify_entry, shadow, cache_state, command):
    workspace, cache, launches, notified, run = notify_entry
    executed = workspace / "workspace-code-executed"
    shadow_path = workspace / shadow
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(f"from pathlib import Path\nPath({str(executed)!r}).touch()\n", encoding="utf-8")
    if cache_state == "stale":
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"checked_at": 0.0, "latest": "0.0.1"}), encoding="utf-8")
    hook_args = ["--event", "Stop", "--package", PACKAGE_REF]
    argv = {
        "normal": ["extras", "status"],
        "presence-stdin": ["work", "presence-hook", "--harness", "antigravity", "--stdin", "--event", "start"],
        "presence-publisher": [
            "work",
            "presence-hook",
            "--harness",
            "antigravity",
            "--target",
            str(workspace),
            "--session",
            "example-session",
            "--event",
            "heartbeat",
        ],
        "claude-hook": ["work", "hook-run", *hook_args],
        "direct-hook": hook_args,
    }[command]
    result = run(argv, entry="brigade-hook" if command == "direct-hook" else "brigade")
    assert not executed.exists(), "refresh executed the workspace's Brigade copy"
    if command == "normal":
        assert "extras:" in result.stdout
        assert launches.exists(), "enabled update checks never launched a refresh"
        # A successful cache write also proves -I can find the installed package.
        assert json.loads(cache.read_text(encoding="utf-8"))["checked_at"] > 0
        (launch,) = [json.loads(line) for line in launches.read_text(encoding="utf-8").splitlines()]
        assert "-I" in launch["argv"]
        assert Path(launch["cwd"]).is_absolute()
        assert Path(launch["cwd"]).resolve() != workspace.resolve()
    else:
        assert not notified.exists(), "hook entered the update notifier"
        assert not launches.exists(), "hook spawned an update refresh"
        assert result.stderr == ""
        if command in {"claude-hook", "direct-hook"}:
            assert json.loads(result.stdout) == {}
        else:
            assert result.stdout == ""
        assert cache.exists() == (cache_state == "stale")
        if cache.exists():
            assert json.loads(cache.read_text(encoding="utf-8"))["checked_at"] == 0.0
