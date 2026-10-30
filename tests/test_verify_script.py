import ast
import os
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fake_tool_dir(tmp_path: Path) -> tuple[Path, Path]:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    log_path = tmp_path / "tools.log"
    stub = """#!/bin/sh
printf '%s' "$(basename "$0")" >> "$VERIFY_LOG"
for arg in "$@"; do printf '\\t%s' "$arg" >> "$VERIFY_LOG"; done
printf '\\n' >> "$VERIFY_LOG"
"""
    for name in ("ruff", "mypy", "python", "pytest"):
        path = tool_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    return tool_dir, log_path


def test_verify_script_runs_mypy_and_coverage_floor():
    text = (ROOT / "scripts/verify").read_text()

    assert '"$PY/mypy"' in text
    assert '"$PY/python" scripts/verify_parallel.py' in text


def test_verify_script_documents_full_coverage_gate():
    text = (ROOT / "scripts/verify").read_text()

    assert "ruff lint, ruff format, mypy, version sync, pytest with coverage" in text


def test_root_ruff_configuration_excludes_non_python_trees():
    text = (ROOT / "pyproject.toml").read_text()
    ruff_config = text.split("[tool.ruff]\n", maxsplit=1)[1].split("\n[tool.ruff.", maxsplit=1)[0]
    exclude_line = next(line for line in ruff_config.splitlines() if line.startswith("extend-exclude = "))
    exclusions = ast.literal_eval(exclude_line.partition("=")[2].strip())

    assert "engines" in exclusions
    assert "*.md" in exclusions
    assert "force-exclude = true" in ruff_config


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_requires_a_pytest_selector():
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "usage: ./scripts/verify-focused <pytest-selector>..." in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_focused_forwards_only_selected_tests(tmp_path, monkeypatch):
    tool_dir, log_path = _fake_tool_dir(tmp_path)
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    selectors = [
        "tests/test_verify_script.py",
        "tests/test_verify_script.py::test_root_ruff_configuration_excludes_non_python_trees",
    ]
    completed = subprocess.run(
        [str(ROOT / "scripts/verify-focused"), *selectors],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0
    assert log_path.read_text().splitlines()[-1] == f"pytest\t-q\t{selectors[0]}\t{selectors[1]}"


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
def test_verify_full_gate_fails_fast_when_another_gate_holds_the_lock(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.delenv("BRIGADE_VERIFY_LOCK_HELD", raising=False)
    tool_dir, log_path = _fake_tool_dir(tmp_path)
    lock_path = tmp_path / "full.lock"
    monkeypatch.setenv("BRIGADE_VERIFY_SLOTS", "1")
    monkeypatch.setenv("BRIGADE_VERIFY_WAIT", "0")
    # Slot 0 is the unsuffixed path that pre-slot gates lock.
    slot_path = lock_path
    slot_path.touch(mode=0o600)
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PYTHON", sys.executable)
    with slot_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = subprocess.run(
            [str(ROOT / "scripts/verify")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ.copy(),
            timeout=15,
        )
    assert completed.returncode == 75
    assert "full verification already running" in completed.stderr
    assert "./scripts/verify-focused" in completed.stderr
    assert not log_path.exists() or log_path.read_text() == ""


@pytest.mark.skipif(os.name == "nt", reason="Bash verification entrypoint")
def test_verify_full_gate_does_not_invoke_external_flock(tmp_path, monkeypatch):
    monkeypatch.delenv("BRIGADE_VERIFY_LOCK_HELD", raising=False)
    tool_dir, log_path = _fake_tool_dir(tmp_path)
    flock_dir = tmp_path / "flock-bin"
    flock_dir.mkdir()
    flock_log = tmp_path / "flock.log"
    flock_shim = flock_dir / "flock"
    flock_shim.write_text(f'#!/bin/sh\nprintf invoked >> "{flock_log}"\nexit 1\n')
    flock_shim.chmod(0o755)
    lock_path = tmp_path / "full.lock"
    monkeypatch.setenv("BRIGADE_VERIFY_SLOTS", "1")
    monkeypatch.setenv("BRIGADE_VERIFY_WAIT", "0")
    monkeypatch.setenv("PY", str(tool_dir))
    monkeypatch.setenv("VERIFY_LOG", str(log_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PATH", str(lock_path))
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PYTHON", sys.executable)
    env = os.environ.copy()
    env["PATH"] = f"{flock_dir}{os.pathsep}{env.get('PATH', '')}"
    completed = subprocess.run(
        [str(ROOT / "scripts/verify")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 0
    assert log_path.read_text().splitlines() == [
        "python\tscripts/verify_env.py",
        "ruff\tcheck\t.",
        "ruff\tformat\t--check\t.",
        "mypy",
        "python\tscripts/version_sync.py\t--check",
        "python\tscripts/managed_snapshot.py\t--check",
        "python\tscripts/template_profile_snapshot.py\t--check",
        "python\tscripts/verify_parallel.py",
    ]
    assert not flock_log.exists()


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
@pytest.mark.parametrize("topology,entrypoint", [("clone", "verify-fast"), ("worktree", "verify")])
def test_verify_full_gate_keeps_lock_after_execvpe_into_verify(tmp_path, monkeypatch, topology, entrypoint):
    # Stale inherited state must not bypass admission or split the host lock.
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_HELD", "1")
    monkeypatch.delenv("BRIGADE_VERIFY_LOCK_PATH", raising=False)
    monkeypatch.delenv("BRIGADE_VERIFY_SLOTS", raising=False)
    monkeypatch.setenv("BRIGADE_VERIFY_SHARDS", "1")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copytree(ROOT / "scripts", checkout / "scripts")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "scripts"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-qm",
            "test fixture",
        ],
        check=True,
    )
    other = tmp_path / "other"
    if topology == "clone":
        subprocess.run(["git", "clone", "-q", str(checkout), str(other)], check=True)
    else:
        subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", "--detach", str(other)], check=True)
    holder_root = tmp_path / "holder"
    second_root = tmp_path / "second"
    holder_root.mkdir()
    second_root.mkdir()
    holder_dir, holder_log = _fake_tool_dir(holder_root)
    second_dir, second_log = _fake_tool_dir(second_root)
    ready_marker = tmp_path / "ready"
    release_marker = tmp_path / "release"
    monkeypatch.setenv("BRIGADE_VERIFY_WAIT", "0")
    (holder_dir / "ruff").write_text(
        """#!/bin/sh
printf '%s' "$(basename "$0")" >> "$VERIFY_LOG"
for arg in "$@"; do printf '\\t%s' "$arg" >> "$VERIFY_LOG"; done
printf '\\n' >> "$VERIFY_LOG"
: > "$VERIFY_READY_MARKER"
while [ ! -f "$VERIFY_RELEASE_MARKER" ]; do
  sleep 0.05
done
"""
    )
    (holder_dir / "ruff").chmod(0o755)
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PYTHON", sys.executable)
    holder_env = os.environ.copy()
    holder_env["PY"] = str(holder_dir)
    holder_env["VERIFY_LOG"] = str(holder_log)
    holder_env["VERIFY_READY_MARKER"] = str(ready_marker)
    holder_env["VERIFY_RELEASE_MARKER"] = str(release_marker)
    second_env = os.environ.copy()
    second_env["PY"] = str(second_dir)
    second_env["VERIFY_LOG"] = str(second_log)
    second_env.update(TMPDIR=str(second_root), XDG_RUNTIME_DIR=str(second_root), HOME=str(second_root))

    # Resolve defaults in each checkout before isolating the admission exercise
    # from any outer full gate running these tests on the same user account.
    def default_prefix(repo, env):
        code = (
            "import runpy,sys; sys.path.insert(0, 'scripts'); "
            "print(runpy.run_path('scripts/verify_lock.py')['_lock_path']())"
        )
        return subprocess.check_output([sys.executable, "-c", code], cwd=repo, env=env, text=True).strip()

    assert default_prefix(checkout, holder_env) == default_prefix(other, second_env)
    holder_env["BRIGADE_VERIFY_LOCK_PATH"] = str(tmp_path / "full.lock")
    second_env["BRIGADE_VERIFY_LOCK_PATH"] = holder_env["BRIGADE_VERIFY_LOCK_PATH"]
    verify = checkout / "scripts/verify"
    contender = other / "scripts" / entrypoint
    holder = subprocess.Popen(
        [str(verify)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_env,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not ready_marker.exists():
            if holder.poll() is not None:
                _stdout, stderr = holder.communicate()
                raise AssertionError(f"holder exited {holder.returncode} before ready marker: {stderr}")
            if time.monotonic() >= deadline:
                holder.terminate()
                _stdout, stderr = holder.communicate(timeout=5)
                raise AssertionError(f"holder did not write ready marker: {stderr}")
            time.sleep(0.05)
        completed = subprocess.run(
            [str(contender)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=second_env,
            timeout=15,
        )
        assert completed.returncode == 75
        assert "full verification already running" in completed.stderr
        assert "./scripts/verify-focused" in completed.stderr
        assert not second_log.exists() or second_log.read_text() == ""
    finally:
        release_marker.touch()
        try:
            holder.wait(timeout=15)
        except subprocess.TimeoutExpired:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)
    assert holder.returncode == 0
    released = subprocess.run([str(contender)], env=second_env, capture_output=True, text=True, timeout=15)
    assert released.returncode == 0, released.stderr


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
@pytest.mark.parametrize("shards", ["1", "4", "8"])
def test_default_slot_limit_does_not_grow_when_workers_shrink(tmp_path, monkeypatch, shards):
    import fcntl

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setenv("BRIGADE_VERIFY_SHARDS", shards)
    monkeypatch.delenv("BRIGADE_VERIFY_SLOTS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 24)
    prefix = tmp_path / "full.lock"
    monkeypatch.setenv("BRIGADE_VERIFY_LOCK_PATH", str(prefix))
    monkeypatch.setenv("BRIGADE_VERIFY_WAIT", "0")
    runner = runpy.run_path(str(ROOT / "scripts/verify_lock.py"))
    prefix.touch(mode=0o600)
    with prefix.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as exit_status:
            runner["main"]()
    assert exit_status.value.code == 75


def _run_env_guard(tmp_path: Path, package_root: Path) -> subprocess.CompletedProcess:
    """Run the guard with `brigade` resolving to package_root."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package_root)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify_env.py")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )


def test_verify_env_guard_rejects_an_interpreter_from_another_checkout(tmp_path):
    foreign = tmp_path / "other-checkout" / "src"
    (foreign / "brigade").mkdir(parents=True)
    (foreign / "brigade" / "__init__.py").write_text("")

    result = _run_env_guard(tmp_path, foreign)

    assert result.returncode != 0
    assert str(foreign / "brigade") in result.stderr
    assert str(ROOT / "src" / "brigade") in result.stderr
    assert "pip install -e" in result.stderr


def test_verify_env_guard_accepts_this_checkout(tmp_path):
    result = _run_env_guard(tmp_path, ROOT / "src")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", ["verify", "verify-focused"])
def test_verify_scripts_run_the_env_guard_before_linting(script):
    text = (ROOT / "scripts" / script).read_text()

    guard = '"$PY/python" scripts/verify_env.py'
    assert guard in text
    assert text.index(guard) < text.index('"$PY/ruff"')


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
def test_verify_slots_allow_k_gates_then_reject_k_plus_one(tmp_path, monkeypatch):
    import fcntl

    tools, log = _fake_tool_dir(tmp_path)
    prefix = tmp_path / "slots" / "full.lock"
    prefix.parent.mkdir()
    env = dict(
        os.environ,
        PY=str(tools),
        VERIFY_LOG=str(log),
        BRIGADE_VERIFY_LOCK_PATH=str(prefix),
        BRIGADE_VERIFY_LOCK_PYTHON=sys.executable,
        BRIGADE_VERIFY_SLOTS="2",
        BRIGADE_VERIFY_WAIT="0",
    )
    env.pop("BRIGADE_VERIFY_LOCK_HELD", None)
    prefix.touch(mode=0o600)
    Path(f"{prefix}.1").touch(mode=0o600)
    with prefix.open("w") as first, Path(f"{prefix}.1").open("w") as second:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        available = subprocess.run([str(ROOT / "scripts/verify")], env=env, capture_output=True, text=True, timeout=15)
        assert available.returncode == 0, available.stderr
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = subprocess.run([str(ROOT / "scripts/verify")], env=env, capture_output=True, text=True, timeout=15)
        assert busy.returncode == 75
        assert "full verification already running" in busy.stderr


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
@pytest.mark.parametrize("wait,expected", [("5", 0), ("0.1", 75)])
def test_verify_slot_wait_acquires_after_release_or_expires(tmp_path, wait, expected):
    import fcntl

    tools, log = _fake_tool_dir(tmp_path)
    prefix = tmp_path / "full.lock"
    env = dict(
        os.environ,
        PY=str(tools),
        VERIFY_LOG=str(log),
        BRIGADE_VERIFY_LOCK_PATH=str(prefix),
        BRIGADE_VERIFY_LOCK_PYTHON=sys.executable,
        BRIGADE_VERIFY_SLOTS="1",
        BRIGADE_VERIFY_WAIT=wait,
    )
    env.pop("BRIGADE_VERIFY_LOCK_HELD", None)
    prefix.touch(mode=0o600)
    with prefix.open("w") as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen(
            [str(ROOT / "scripts/verify")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            time.sleep(0.3)
            assert not log.exists()
            if expected == 0:
                assert child.poll() is None
            fcntl.flock(slot, fcntl.LOCK_UN)
            _, stderr = child.communicate(timeout=15)
            assert child.returncode == expected, stderr
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_verify_rejects_unsafe_slot_files(tmp_path, monkeypatch, kind):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    runner = runpy.run_path(str(ROOT / "scripts/verify_lock.py"))
    target = tmp_path / "target"
    target.touch(mode=0o600)
    prefix = tmp_path / "full.lock"
    if kind == "symlink":
        prefix.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, prefix)
    else:
        prefix.touch(mode=0o666)
        prefix.chmod(0o666)
    with pytest.raises((OSError, ValueError)):
        runner["acquire_slot"](prefix, 1)


@pytest.mark.skipif(os.name != "posix", reason="fcntl is POSIX")
@pytest.mark.parametrize("entrypoint", ["verify", "verify-fast"])
@pytest.mark.parametrize(
    "name,value",
    [
        ("BRIGADE_VERIFY_SLOTS", "0"),
        ("BRIGADE_VERIFY_WAIT", "nan"),
        ("BRIGADE_VERIFY_WAIT", "-1"),
        ("BRIGADE_VERIFY_SHARDS", "0"),
    ],
)
def test_full_entrypoints_reject_invalid_settings_before_tools(tmp_path, name, value, entrypoint):
    tools, log = _fake_tool_dir(tmp_path)
    env = dict(
        os.environ,
        PY=str(tools),
        VERIFY_LOG=str(log),
        BRIGADE_VERIFY_LOCK_PYTHON=sys.executable,
        BRIGADE_VERIFY_LOCK_PATH=str(tmp_path / "full.lock"),
    )
    env.pop("BRIGADE_VERIFY_LOCK_HELD", None)
    env[name] = value
    result = subprocess.run([str(ROOT / "scripts" / entrypoint)], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stderr
    assert not log.exists()


@pytest.mark.skipif(os.name != "posix", reason="Bash verification entrypoint")
@pytest.mark.parametrize("jobs,shards,expected", [("2", None, 0), ("auto", None, 2), ("2", "3", 2)])
def test_verify_fast_jobs_compatibility_is_explicit(tmp_path, jobs, shards, expected):
    tools, log = _fake_tool_dir(tmp_path)
    env = dict(
        os.environ,
        PY=str(tools),
        VERIFY_LOG=str(log),
        JOBS=jobs,
        BRIGADE_VERIFY_LOCK_PATH=str(tmp_path / "full.lock"),
        BRIGADE_VERIFY_LOCK_PYTHON=sys.executable,
    )
    env.pop("BRIGADE_VERIFY_LOCK_HELD", None)
    env.pop("BRIGADE_VERIFY_SHARDS", None)
    if shards:
        env["BRIGADE_VERIFY_SHARDS"] = shards
    result = subprocess.run([str(ROOT / "scripts/verify-fast")], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stderr
    assert "BRIGADE_VERIFY_SHARDS" in result.stderr
    assert log.exists() == (expected == 0)
    if expected == 0:
        calls = log.read_text().splitlines()
        assert calls[0] == "python\tscripts/verify_env.py"
        assert calls[-1] == "python\tscripts/verify_parallel.py"
