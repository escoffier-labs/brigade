"""Writing shipped memory-care runbooks never deletes an operator's runbooks.

Before this fix, ``brigade care install`` on a workspace that lacked one newly
shipped runbook replaced the whole ``.brigade/memory-care/runbooks`` directory
with the templates, deleting every other runbook (evidence crawl, closeout,
refresh) and overwriting customized shipped ones.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

from brigade import care_cmd, care_patrol, care_runbooks, care_schedules, extras, memory_cmd, runbook_cmd


def _runbook_dir(target: Path) -> Path:
    return memory_cmd._memory_care_runbooks_dir(target)


def _seed(target: Path) -> tuple[Path, Path, bytes]:
    assert memory_cmd.init(target=target, force=True, update_gitignore=False, with_runbooks=True) == 0
    runbooks = _runbook_dir(target)
    own = runbooks / "evidence-crawl.json"
    own.write_text('{"id": "evidence-crawl", "operator": "own runbook"}\n', encoding="utf-8")
    customized = runbooks / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[1]
    custom_bytes = customized.read_bytes() + b"\n"
    customized.write_bytes(custom_bytes)
    return own, customized, custom_bytes


def test_care_install_adds_a_missing_shipped_runbook_without_touching_the_rest(tmp_path, capsys):
    own, customized, custom_bytes = _seed(tmp_path)
    missing = _runbook_dir(tmp_path) / memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[0]
    missing.unlink()
    capsys.readouterr()

    assert care_cmd._ensure_memory_care_runbooks(tmp_path) == 0

    assert missing.is_file()
    assert own.read_text(encoding="utf-8") == '{"id": "evidence-crawl", "operator": "own runbook"}\n'
    assert customized.read_bytes() == custom_bytes


def test_memory_care_init_with_runbooks_refreshes_shipped_files_and_keeps_others(tmp_path, capsys):
    own, customized, custom_bytes = _seed(tmp_path)
    capsys.readouterr()

    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0

    # The explicit init refreshes the shipped templates but never deletes a runbook it does not ship.
    assert own.is_file()
    assert customized.read_bytes() != custom_bytes
    for name in memory_cmd.MEMORY_CARE_RUNBOOK_NAMES:
        assert (_runbook_dir(tmp_path) / name).is_file()


# --- review round 2: one presence predicate, no symlink writes, no partial publish ---

NAMES = memory_cmd.MEMORY_CARE_RUNBOOK_NAMES
CLOSEOUT = care_cmd.MEMORY_CLOSEOUT_RUNBOOK_NAME
DAILY_OBS = care_cmd.DAILY_OBSERVABILITY_RUNBOOK_NAME
BAK = re.compile(r"\.bak-\d{8}T\d{6}Z(-\d+)?$")
needs_symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlink fixtures are POSIX-only")
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A systemd care install target whose runbook writers are the real ones."""
    home, target = tmp_path / "home", tmp_path / "ws"
    target.mkdir()
    unit_dir = home / ".config" / "systemd" / "user"
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_schedules, "effective_systemd_calendar", lambda timer_name: (None, None))
    monkeypatch.setattr(extras, "enabled", lambda: True)
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda h=None: unit_dir)
    return home, target


def _bad_entry(kind: str, path: Path, tmp_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink-to-dir":
        (tmp_path / "elsewhere").mkdir(exist_ok=True)
        path.symlink_to(tmp_path / "elsewhere")
    elif kind == "symlink-to-file":
        real = tmp_path / "real.json"
        real.write_text("{}\n")
        path.symlink_to(real)
    else:
        path.symlink_to(tmp_path / "missing.json")


BAD_KINDS = [
    "directory",
    pytest.param("fifo", marks=needs_fifo),
    pytest.param("symlink-to-dir", marks=needs_symlinks),
    pytest.param("symlink-to-file", marks=needs_symlinks),
    pytest.param("dangling-symlink", marks=needs_symlinks),
]


def _is_untouched(path: Path, kind: str) -> bool:
    if kind == "directory":
        return path.is_dir() and not path.is_symlink() and not list(path.iterdir())
    if kind == "fifo":
        return path.exists() and not path.is_file() and not path.is_symlink()
    return path.is_symlink()


# 1. one presence predicate (lstat): only a regular file is present; anything else is refused


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_non_regular_runbook_entry_is_refused_naming_the_path_and_never_replaced(tmp_path, capsys, kind):
    target = tmp_path / "ws"
    target.mkdir()
    bad = _runbook_dir(target) / NAMES[0]
    _bad_entry(kind, bad, tmp_path)

    assert care_cmd._ensure_memory_care_runbooks(target) == 2

    assert str(bad) in capsys.readouterr().err
    assert _is_untouched(bad, kind)
    assert not any((_runbook_dir(target) / name).is_file() for name in NAMES[1:])


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_care_install_refuses_a_non_regular_runbook_entry(ws, capsys, kind):
    home, target = ws
    bad = _runbook_dir(target) / DAILY_OBS
    _bad_entry(kind, bad, target.parent)

    assert care_cmd.install(target=target, backend="systemd", home=home) == 2

    assert str(bad) in capsys.readouterr().err
    assert _is_untouched(bad, kind)


# 2. never write through symlinks; stage in an unpredictable directory


@needs_symlinks
@pytest.mark.parametrize("linked", [".brigade", ".brigade/memory-care", ".brigade/memory-care/runbooks"])
def test_symlinked_runbook_path_part_is_refused_and_nothing_is_written_through_it(tmp_path, capsys, linked):
    target = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = target / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    assert care_cmd._ensure_care_runbooks(target) == 2

    assert str(link) in capsys.readouterr().err
    assert list(outside.rglob("*")) == []


@needs_symlinks
def test_a_symlinked_runbook_dir_is_refused_by_memory_care_init_before_it_writes_config(tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    runbooks = _runbook_dir(tmp_path)
    runbooks.parent.mkdir(parents=True)
    runbooks.symlink_to(outside)

    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 2

    assert str(runbooks) in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert not (tmp_path / ".brigade" / "memory-care.toml").exists()


def test_staging_uses_an_unpredictable_directory_beside_the_runbooks_dir(tmp_path, monkeypatch):
    seen: list[tuple[object, object]] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        seen.append((kwargs.get("dir"), kwargs.get("prefix")))
        return real(*args, **kwargs)

    monkeypatch.setattr(care_runbooks.tempfile, "mkdtemp", spy)
    assert care_cmd._ensure_care_runbooks(tmp_path) == 0

    assert seen
    assert seen[0] == (_runbook_dir(tmp_path).parent, ".runbooks-")
    assert not [p for p in _runbook_dir(tmp_path).parent.iterdir() if p.name.startswith(".runbooks")]
    assert not list(tmp_path.rglob("*.init-tmp"))


# 3. no partial publish, no traceback


def test_a_blocked_destination_publishes_nothing_even_when_earlier_runbooks_are_missing(tmp_path, capsys):
    (_runbook_dir(tmp_path) / NAMES[-1]).mkdir(parents=True)

    assert care_cmd._ensure_care_runbooks(tmp_path) == 2

    assert str(_runbook_dir(tmp_path) / NAMES[-1]) in capsys.readouterr().err
    assert not any((_runbook_dir(tmp_path) / name).exists() for name in (*NAMES[:-1], DAILY_OBS))


def test_a_daily_observability_pin_failure_leaves_no_runbook_behind(tmp_path, monkeypatch, capsys):
    real = runbook_cmd._pin_payload_from_runbook

    def pin(target, payload):
        if payload.get("id") == "daily-observability":
            return None, "cannot pin daily-observability"
        return real(target, payload)

    monkeypatch.setattr(runbook_cmd, "_pin_payload_from_runbook", pin)

    assert care_cmd._ensure_care_runbooks(tmp_path) == 2

    assert "cannot pin daily-observability" in capsys.readouterr().err
    assert not list(_runbook_dir(tmp_path).glob("*.json")) if _runbook_dir(tmp_path).exists() else True
    assert not list(tmp_path.rglob("*.json"))


def test_an_os_failure_while_publishing_returns_2_names_the_file_and_lists_earlier_ones(tmp_path, monkeypatch, capsys):
    failing = NAMES[2]
    real_link, real_replace = os.link, os.replace

    def link(src, dst, *args, **kwargs):
        if Path(dst).name == failing and ".runbooks-" not in str(dst):
            raise OSError(errno.EIO, "disk on fire")
        return real_link(src, dst, *args, **kwargs)

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == failing and ".runbooks-" not in str(dst):
            raise OSError(errno.EIO, "disk on fire")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(care_runbooks.os, "link", link)
    monkeypatch.setattr(care_runbooks.os, "replace", replace)

    assert care_cmd._ensure_care_runbooks(tmp_path) == 2

    err = capsys.readouterr().err
    assert failing in err
    assert "disk on fire" in err
    assert "already published before the failure: " + ", ".join(NAMES[:2]) in err
    assert "Traceback" not in err
    assert not (_runbook_dir(tmp_path) / failing).exists()
    assert not [p for p in _runbook_dir(tmp_path).parent.iterdir() if p.name.startswith(".runbooks")]


# 4. every other care install writer keeps existing files and refuses links


def _writer_cases():
    patrol = care_patrol.RUNBOOK_REL
    closeout = f"{memory_cmd.MEMORY_CARE_RUNBOOKS_REL}/{CLOSEOUT}"
    daily = care_cmd.MEMORY_CARE_RUNBOOK_REL["daily-observability"]
    return [
        pytest.param(
            patrol, lambda t: care_patrol.write_runbook(t, patrol, care_patrol.RUNBOOK_PAYLOAD), id="run-patrol"
        ),
        pytest.param(closeout, care_cmd._write_memory_closeout_runbook, id="memory-closeout"),
        pytest.param(daily, care_cmd._write_daily_observability_runbook, id="daily-observability"),
    ]


@pytest.mark.parametrize(("rel", "write"), _writer_cases())
def test_each_runbook_writer_keeps_an_existing_regular_file(tmp_path, rel, write):
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"id": "operator", "steps": []}\n')

    assert write(tmp_path) == 0

    assert path.read_bytes() == b'{"id": "operator", "steps": []}\n'


@pytest.mark.parametrize(("rel", "write"), _writer_cases())
@pytest.mark.parametrize("kind", BAD_KINDS)
def test_each_runbook_writer_refuses_a_non_regular_entry(tmp_path, capsys, rel, write, kind):
    path = tmp_path / "ws" / rel
    (tmp_path / "ws").mkdir()
    _bad_entry(kind, path, tmp_path)

    assert write(tmp_path / "ws") == 2

    assert str(path) in capsys.readouterr().err
    assert _is_untouched(path, kind)


@needs_symlinks
@pytest.mark.parametrize(("rel", "write"), _writer_cases())
def test_each_runbook_writer_refuses_a_symlinked_runbook_dir(tmp_path, capsys, rel, write):
    target = tmp_path / "ws"
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / Path(rel).parent.parent).mkdir(parents=True, exist_ok=True)
    (target / Path(rel).parent).symlink_to(outside)

    assert write(target) == 2

    assert list(outside.iterdir()) == []


# 5. memory care init --force --with-runbooks reports and backs up what it replaces


def test_init_force_backs_up_a_customized_shipped_runbook_and_names_it(tmp_path, capsys):
    own, customized, custom_bytes = _seed(tmp_path)
    capsys.readouterr()

    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0

    out = capsys.readouterr().out
    backups = sorted(_runbook_dir(tmp_path).glob(f"{customized.name}.bak-*"))
    assert len(backups) == 1 and BAK.search(backups[0].name)
    assert backups[0].read_bytes() == custom_bytes
    assert customized.read_bytes() != custom_bytes
    assert f"replaced shipped runbook: {customized} (backup: {backups[0]})" in out
    # Unmodified shipped files are not rewritten, so they get no backup and no replaced line.
    for name in (n for n in NAMES if n != customized.name):
        assert not list(_runbook_dir(tmp_path).glob(f"{name}.bak-*"))
        assert f"replaced shipped runbook: {_runbook_dir(tmp_path) / name}" not in out
    assert own.read_text(encoding="utf-8") == '{"id": "evidence-crawl", "operator": "own runbook"}\n'


def test_init_force_twice_in_one_second_keeps_every_backup(tmp_path):
    _, customized, custom_bytes = _seed(tmp_path)
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0
    customized.write_bytes(custom_bytes + b"\n")
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0

    backups = list(_runbook_dir(tmp_path).glob(f"{customized.name}.bak-*"))
    assert sorted(path.read_bytes() for path in backups) == sorted([custom_bytes, custom_bytes + b"\n"])


def test_the_refresh_docstring_does_not_claim_customized_files_are_kept():
    doc = memory_cmd._write_memory_care_runbooks.__doc__ or ""
    assert "only_missing" in doc and "<name>.bak-<UTC timestamp>" in doc


# 6. brigade care install itself keeps every customized runbook byte for byte


def test_care_install_keeps_customized_runbooks_and_writes_the_missing_shipped_one(ws, capsys):
    home, target = ws
    ids = ["daily-care", "daily-observability", "memory-closeout", "run-patrol", "evidence-crawl"]
    _runbook_dir(target).mkdir(parents=True)
    (_runbook_dir(target) / "evidence-crawl.json").write_bytes(b'{"id": "evidence-crawl", "operator": "own"}\n')
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=ids) == 0
    runbooks = _runbook_dir(target)
    custom = {
        runbooks / DAILY_OBS: b'{"id": "daily-observability", "operator": "customized"}\n',
        runbooks / CLOSEOUT: b'{"id": "memory-closeout", "operator": "customized"}\n',
        target / care_patrol.RUNBOOK_REL: b'{"id": "run-patrol", "operator": "customized"}\n',
        runbooks / "evidence-crawl.json": b'{"id": "evidence-crawl", "operator": "own"}\n',
        runbooks / NAMES[1]: (runbooks / NAMES[1]).read_bytes() + b"\n",
    }
    for path, data in custom.items():
        path.write_bytes(data)
    missing = runbooks / NAMES[0]
    missing.unlink()
    capsys.readouterr()

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=ids) == 0

    for path, data in custom.items():
        assert path.read_bytes() == data, path.name
    payload, error = runbook_cmd._read_runbook(missing, require_pin_hashes=True)
    assert error is None and payload is not None
    assert payload["pins"] and all(pin["sha256"] for pin in payload["pins"])
    assert not list(target.rglob("*.bak-*"))
