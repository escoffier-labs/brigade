"""Contract checks for stations/notify packaging and pre-push hook behavior."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "stations" / "notify"


def _makefile() -> str:
    return (NOTIFY / "Makefile").read_text()


def _pre_push() -> str:
    return (NOTIFY / "hooks" / "pre-push").read_text()


def _logical_shell_lines(recipe: str) -> list[str]:
    """Join backslash-continued physical lines into single shell logical lines.

    Make runs each logical recipe line in its own shell, so fail-fast flags
    only protect commands on the same continued line.
    """
    logical: list[str] = []
    buf = ""
    for line in recipe.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            logical.append(buf + stripped)
            buf = ""
    if buf:
        logical.append(buf)
    return logical


def test_notify_pre_push_captures_scanner_exit_code():
    text = _pre_push()
    assert "|| rc=$?" in text
    assert "if ! PYTHONPATH=" not in text


def test_notify_pre_push_blocks_findings_with_exit_1():
    text = _pre_push()
    assert '"$rc" -eq 1' in text
    assert "BLOCKED. content-guard found violations." in text
    # The exit-1 assertion is scoped to the findings branch; the pre-existing
    # guard clauses at the top of the hook must not satisfy it.
    branch_start = text.index('if [[ "$rc" -eq 1 ]]')
    branch_end = text.index('exit "$rc"')
    branch = text[branch_start:branch_end]
    assert "exit 1" in branch


def test_notify_pre_push_reports_scanner_errors_and_preserves_status():
    text = _pre_push()
    assert "failed to run" in text
    assert "not a leak verdict" in text
    assert 'exit "$rc"' in text


def test_notify_makefile_install_creates_home_bin():
    text = _makefile()
    install = text[text.index("install:") : text.index("clean-dist:")]
    assert "mkdir -p $(HOME)/bin" in install
    assert "install -m 0755 $(BINARY) $(HOME)/bin/$(BINARY)" in install
    assert install.index("mkdir -p $(HOME)/bin") < install.index("install -m 0755")


def test_notify_makefile_install_fails_closed_without_home():
    # An unset/empty HOME must abort the recipe before mkdir -p /bin and
    # install into /bin can run.
    text = _makefile()
    install = text[text.index("install:") : text.index("clean-dist:")]
    assert "$(error" in install, "install target has no $(error ...) guard"
    guard = next(line for line in install.splitlines() if "$(error" in line)
    assert "HOME" in guard
    assert install.index("$(error") < install.index("mkdir -p $(HOME)/bin")


def test_notify_makefile_install_empty_home_aborts():
    if shutil.which("make") is None:
        pytest.skip("GNU make not available")
    env = {**os.environ, "HOME": ""}
    r = subprocess.run(
        ["make", "-n", "install"],
        cwd=NOTIFY,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, f"expected make to abort, got stdout={r.stdout!r}"
    assert "HOME is not set; refusing to install into /bin" in r.stderr


def test_notify_makefile_install_nonempty_home_dry_run_succeeds(tmp_path):
    if shutil.which("make") is None:
        pytest.skip("GNU make not available")
    home = str(tmp_path / "home")
    env = {**os.environ, "HOME": home}
    r = subprocess.run(
        ["make", "-n", "install"],
        cwd=NOTIFY,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"make failed: stderr={r.stderr!r}"
    assert "mkdir -p" in r.stdout
    assert "install -m 0755" in r.stdout


def test_notify_makefile_package_fails_fast_in_platform_loop():
    text = _makefile()
    package = text[text.index("package:") :]
    for step in (
        "go build",
        "cp README.md LICENSE",
        "chmod 755",
        "tar -C dist/tmp",
    ):
        assert step in package
    # set -eu must share one backslash-continued shell with the platform
    # loop; moved onto its own recipe line it runs in a separate shell and
    # the loop no longer fails fast.
    loop_shells = [line for line in _logical_shell_lines(package) if "for platform" in line]
    assert loop_shells, "platform loop not found in package recipe"
    for shell in loop_shells:
        assert "set -eu" in shell
        assert shell.index("set -eu") < shell.index("for platform")
