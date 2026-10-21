"""Update refresh security through the installed console entry points."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sysconfig
import venv
import zipfile
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


@pytest.mark.parametrize("installation", ["external", "pip-user"])
def test_refresh_loads_running_installation_outside_interpreter_sites(tmp_path, monkeypatch, installation):
    """A user-site/PYTHONPATH install must still refresh under isolated Python."""
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, system_site_packages=installation == "pip-user").create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    staged = tmp_path / "staged"
    installed = staged
    shutil.copytree(
        Path(update_notify.__file__).resolve().parent,
        staged / "brigade",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    probe_output = tmp_path / "child-installation.json"
    (staged / "optional_dependency.py").write_text("VALUE = 'installed dependency'\n", encoding="utf-8")
    # Only this wheel's __main__ can produce this receipt, even on hosts that
    # also have a global editable Brigade installation.
    probe = staged / "brigade" / "_installation_probe.py"
    probe.write_text(
        "import json, optional_dependency, site, sys\nfrom pathlib import Path\n"
        f"Path({str(probe_output)!r}).write_text(json.dumps({{'file': __file__, "
        "'isolated': sys.flags.isolated, 'no_user_site': sys.flags.no_user_site, "
        "'user_site': site.ENABLE_USER_SITE, 'path': sys.path, 'dependency': optional_dependency.__file__}))\n",
        encoding="utf-8",
    )
    main = staged / "brigade" / "__main__.py"
    main.write_text("from . import _installation_probe\n" + main.read_text(encoding="utf-8"), encoding="utf-8")
    if installation == "pip-user":
        # Build a local wheel from this exact package with no network or build dependencies.
        wheel = tmp_path / "brigade_cli-0.0.0-py3-none-any.whl"
        info = "brigade_cli-0.0.0.dist-info"
        with zipfile.ZipFile(wheel, "w") as archive:
            names = []
            for path in staged.rglob("*"):
                if path.is_file():
                    name = path.relative_to(staged).as_posix()
                    archive.write(path, name)
                    names.append(name)
            for name, content in {
                f"{info}/METADATA": "Metadata-Version: 2.1\nName: brigade-cli\nVersion: 0.0.0\n",
                f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }.items():
                archive.writestr(name, content)
                names.append(name)
            archive.writestr(f"{info}/RECORD", "".join(f"{name},,\n" for name in [*names, f"{info}/RECORD"]))
        install_env = dict(os.environ, PYTHONUSERBASE=str(tmp_path / "userbase"), HOME=str(tmp_path / "pip-home"))
        for key in ("PYTHONPATH", "PYTHONNOUSERSITE"):
            install_env.pop(key, None)
        install = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--user",
                "--no-index",
                "--no-deps",
                "--ignore-installed",
                str(wheel),
            ],
            env=install_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert install.returncode == 0, install.stderr
        lookup = subprocess.run(
            [str(python), "-c", "import site; print(site.getusersitepackages())"],
            env=install_env,
            capture_output=True,
            text=True,
            check=True,
        )
        installed = Path(lookup.stdout.strip())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executed = tmp_path / "workspace-code-executed"
    (workspace / "brigade.py").write_text(f"from pathlib import Path\nPath({str(executed)!r}).touch()\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("BRIGADE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    environment = dict(os.environ)
    environment.pop("PYTHONNOUSERSITE", None)
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LOCALAPPDATA=str(home / "local"),
        XDG_CACHE_HOME=str(home / "cache"),
        PYTHONPATH=str(installed) if installation == "external" else "",
        PYTHONUSERBASE=str(tmp_path / "userbase"),
        HTTPS_PROXY="http://127.0.0.1:1",
        https_proxy="http://127.0.0.1:1",
        NO_PROXY="",
        no_proxy="",
    )
    # Import the legitimate running installation, then poison the inherited
    # import path. Wait for the actual child instead of observing only argv.
    launcher = tmp_path / "refresh.py"
    sentinel_source = f"from pathlib import Path\nPath({str(executed)!r}).touch()\n"
    launcher.write_text(
        "import os, subprocess\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from brigade import update_notify\n"
        "import site\n"
        + ("assert site.ENABLE_USER_SITE is True\n" if installation == "pip-user" else "")
        + f"assert Path(update_notify.__file__).resolve().parent == Path({str(installed / 'brigade')!r})\n"
        f"os.environ['PYTHONPATH'] = {str(workspace)!r}\n"
        # Loading this package must not enable unrelated user-site modules.
        f"Path({str(installed / 'platform.py')!r}).write_text({sentinel_source!r})\n"
        "children = []\n"
        "def spawn(*args, **kwargs):\n"
        "    process = subprocess.Popen(*args, **kwargs)\n"
        "    children.append(process)\n"
        "    return process\n"
        "update_notify.subprocess = SimpleNamespace(**vars(subprocess))\n"
        "update_notify.subprocess.Popen = spawn\n"
        "update_notify.maybe_notify(['extras', 'status'], 0)\n"
        "assert len(children) == 1\n"
        "assert children[0].wait(timeout=15) == 0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(python), str(launcher)], cwd=workspace, env=environment, capture_output=True, text=True, timeout=25
    )
    assert not executed.exists(), "refresh executed workspace code instead of the running installation"
    assert result.returncode == 0, result.stderr
    assert json.loads(update_notify.cache_path(environment).read_text())["checked_at"] > 0
    child = json.loads(probe_output.read_text(encoding="utf-8"))
    assert Path(child["file"]).resolve() == (installed / "brigade" / "_installation_probe.py").resolve()
    assert child["isolated"] == child["no_user_site"] == 1
    assert child["user_site"] is False
    assert child["path"][-1] == str(installed)
    assert Path(child["dependency"]).resolve() == (installed / "optional_dependency.py").resolve()
    assert str(workspace) not in child["path"]


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
