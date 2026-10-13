"""Public journal-export contracts: original bytes, validation and custody."""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import cli, dirfd, run_checkpoint, run_dirfd, run_events, run_journal
from tests.support import PRIVATE_FILE_MODE, assert_private_mode

RUN_ID = "20260727-153045-a1b2c3d4"
TIMES = (
    "2026-07-26T23:59:59.000000Z",
    "2026-07-27T00:00:00.000000Z",
    "2026-07-27T01:00:00.000000Z",
    "2026-07-27T00:30:00.000000Z",
)


def _journal(target: Path, *, run_id: str = RUN_ID, times=TIMES):
    path = target / ".brigade" / "runs" / run_id / "events" / "lifecycle.jsonl"
    path.parent.mkdir(parents=True)
    events = [
        run_journal.append_event(
            path,
            run_id=run_id,
            event_type="run.planning.started",
            payload={"detail": f"private detail {index}: café"},
            idempotency_key=f"planning-{index}",
            expected_previous_sequence=index,
            recorded_at=recorded_at,
        )
        for index, recorded_at in enumerate(times)
    ]
    if not times:
        path.touch()
    return path, events


def _export(target: Path, *options: str, run_id: str = RUN_ID) -> int:
    try:
        return cli.main(["receipts", "export", "journal", "--target", str(target), "--run-id", run_id, *options])
    except SystemExit as exc:
        return int(exc.code)


def test_canonical_roundtrip_preserves_original_digests_and_source(tmp_path, capsys):
    source, events = _journal(tmp_path)
    original = source.read_bytes()
    assert _export(tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.encode("utf-8") == original
    output = tmp_path / "export.jsonl"
    output.write_bytes(captured.out.encode("utf-8"))
    report = run_journal.read_journal_bounded(output)
    assert not report.chain_errors and report.partial_tail is None
    assert [event.event_digest for event in report.events] == [event.event_digest for event in events]
    assert source.read_bytes() == original


@pytest.mark.parametrize("target", ["../workspace", "../sibling/../workspace"])
def test_target_parent_navigation_exports_without_mutating_source(tmp_path, capsys, monkeypatch, target):
    source, _ = _journal(tmp_path / "workspace")
    original = source.read_bytes()
    metadata = source.stat()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    monkeypatch.chdir(sibling)

    assert _export(Path(target)) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out.encode("utf-8") == original
    after = source.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode) == (
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_mode,
    )
    assert source.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="colon and backslash are legal POSIX directory names")
@pytest.mark.parametrize("ancestor", ["build:2026", r"build\2026"])
def test_posix_ancestor_names_export_original_journal(tmp_path, capsys, ancestor):
    target = tmp_path / ancestor / "workspace"
    source, _ = _journal(target)
    original = source.read_bytes()

    assert _export(target) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out.encode("utf-8") == original
    assert source.read_bytes() == original


def test_symlink_before_parent_navigation_is_rejected(tmp_path, capsys):
    source, _ = _journal(tmp_path / "workspace")
    original = source.read_bytes()
    outside = tmp_path / "outside" / "nested"
    outside.mkdir(parents=True)
    linked = tmp_path / "link"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create symlinks")
    output = tmp_path / "export.jsonl"

    assert _export(linked / ".." / "workspace", "--out", str(output)) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "error: journal export:" in captured.err
    assert not output.exists() and source.read_bytes() == original


def test_real_cli_stdout_is_original_utf8_and_lf_bytes(tmp_path):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brigade",
            "receipts",
            "export",
            "journal",
            "--target",
            str(tmp_path),
            "--run-id",
            RUN_ID,
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "BRIGADE_NO_UPDATE_CHECK": "1"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == original
    assert b"caf\xc3\xa9" in result.stdout
    assert b"\r\n" not in result.stdout


def test_stdout_bypasses_text_newline_translation(tmp_path, monkeypatch):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="utf-8", newline="\r\n")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stream)
        status = _export(tmp_path)
        stream.flush()
    assert status == 0
    assert buffer.getvalue() == original


@pytest.mark.parametrize("restore_before_return", [False, True])
def test_disappeared_journal_is_rejected_instead_of_exporting_empty(
    tmp_path, capsys, monkeypatch, restore_before_return
):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    real_read = run_journal.read_journal_bounded

    def remove_before_read(path):
        source.unlink()
        report = real_read(path)
        if restore_before_return:
            source.write_bytes(original)
        return report

    monkeypatch.setattr(run_journal, "read_journal_bounded", remove_before_read)
    output = tmp_path / "export.jsonl"
    assert _export(tmp_path, "--out", str(output)) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "error:" in captured.err
    assert not output.exists()


@pytest.mark.parametrize("phase", ["after-stat", "during-validation", "during-validation-windows-sharing"])
def test_same_run_journal_replacement_is_rejected(tmp_path, capsys, monkeypatch, phase):
    source, _ = _journal(tmp_path / "workspace")
    replacement, _ = _journal(tmp_path / "replacement", times=TIMES[:1])
    output = tmp_path / "export.jsonl"
    swapped = False
    source_descriptor = None
    source_info = source.stat()
    real_open = run_dirfd.BoundRunDir.open_file
    real_stat = run_dirfd.BoundRunDir.stat
    real_read = run_journal.read_journal_bounded

    def capture_source_open(bound, components, name, flags, mode=0o600):
        nonlocal source_descriptor
        descriptor = real_open(bound, components, name, flags, mode)
        if components == ("events",) and name == "lifecycle.jsonl":
            source_descriptor = descriptor
        return descriptor

    def replace_source():
        nonlocal swapped
        if phase == "during-validation-windows-sharing":
            assert source_descriptor is not None
            try:
                held = os.fstat(source_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
            else:
                # Windows can refuse replacing an existing open target even
                # with delete sharing. Ignore reuse of the fd for another file.
                if (held.st_dev, held.st_ino) == (source_info.st_dev, source_info.st_ino):
                    raise PermissionError("Windows replacement refused: journal target is still open")
        replacement.replace(source)
        swapped = True

    def swap_after_stat(bound, components, name):
        info = real_stat(bound, components, name)
        if not swapped and components == ("events",) and name == "lifecycle.jsonl":
            replace_source()
        return info

    def swap_during_validation(path):
        replace_source()
        return real_read(path)

    if phase == "after-stat":
        monkeypatch.setattr(run_dirfd.BoundRunDir, "stat", swap_after_stat)
    else:
        monkeypatch.setattr(run_journal, "read_journal_bounded", swap_during_validation)
        if phase == "during-validation-windows-sharing":
            monkeypatch.setattr(run_dirfd.BoundRunDir, "open_file", capture_source_open)
    status = _export(tmp_path / "workspace", "--out", str(output))
    captured = capsys.readouterr()
    assert swapped, captured.err
    assert status != 0 and captured.out == "" and "error: journal export:" in captured.err
    assert not output.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory rename race")
@pytest.mark.parametrize("phase", ["after-stat", "during-validation"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_events_directory_replacement_is_rejected(tmp_path, capsys, monkeypatch, phase, replacement):
    target = tmp_path / "workspace"
    source, _ = _journal(target)
    original = source.read_bytes()
    other, _ = _journal(tmp_path / "replacement", times=TIMES[:1])
    replacement_bytes = other.read_bytes()
    held = source.parent.with_name("held-events")
    output = tmp_path / "export.jsonl"
    swapped = False
    real_stat = run_dirfd.BoundRunDir.stat
    real_read = run_journal.read_journal_bounded

    def replace_events():
        nonlocal swapped
        source.parent.rename(held)
        if replacement == "directory":
            shutil.copytree(other.parent, source.parent)
        else:
            source.parent.symlink_to(other.parent, target_is_directory=True)
        swapped = True

    def swap_after_stat(bound, components, name):
        info = real_stat(bound, components, name)
        if not swapped and components == ("events",) and name == "lifecycle.jsonl":
            replace_events()
        return info

    def swap_during_validation(path):
        replace_events()
        return real_read(path)

    if phase == "after-stat":
        monkeypatch.setattr(run_dirfd.BoundRunDir, "stat", swap_after_stat)
    else:
        monkeypatch.setattr(run_journal, "read_journal_bounded", swap_during_validation)
    status = _export(target, "--out", str(output))
    captured = capsys.readouterr()
    assert swapped, captured.err
    assert status != 0 and captured.out == "" and "error: journal export:" in captured.err
    if replacement == "directory":
        assert "journal events directory changed during export" in captured.err
    assert not output.exists()
    assert (held / "lifecycle.jsonl").read_bytes() == original
    assert source.read_bytes() == replacement_bytes != original


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO race")
def test_raced_fifo_is_rejected_without_blocking(tmp_path):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "export.jsonl"
    script = """
import os
import sys
from pathlib import Path
from brigade import cli, run_dirfd

target, run_id, output = sys.argv[1:]
source = Path(target) / '.brigade' / 'runs' / run_id / 'events' / 'lifecycle.jsonl'
real_open = run_dirfd.BoundRunDir.open_file
def swap_before_open(bound, components, name, flags, mode=0o600):
    if components == ('events',) and name == 'lifecycle.jsonl':
        source.rename(source.with_name('held.jsonl'))
        os.mkfifo(source, 0o600)
        print('FIFO_SWAP_OBSERVED', file=sys.stderr, flush=True)
    return real_open(bound, components, name, flags, mode)
run_dirfd.BoundRunDir.open_file = swap_before_open
raise SystemExit(cli.main(['receipts', 'export', 'journal', '--target', target,
                         '--run-id', run_id, '--out', output]))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), RUN_ID, str(output)],
            capture_output=True,
            check=False,
            timeout=10,
            env={**os.environ, "BRIGADE_NO_UPDATE_CHECK": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        assert b"FIFO_SWAP_OBSERVED" in (exc.stderr or b"")
        pytest.fail("export blocked after observed FIFO replacement")
    assert b"FIFO_SWAP_OBSERVED" in result.stderr
    assert result.returncode != 0 and result.stdout == b""
    assert b"error: journal export:" in result.stderr and not output.exists()
    assert source.with_name("held.jsonl").read_bytes() == original


@pytest.mark.parametrize("parser_failure", [None, "read-error", "missing-copy"])
def test_private_validation_copy_is_cleaned_up(tmp_path, capsys, monkeypatch, parser_failure):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    copies = []
    real_read = run_journal.read_journal_bounded

    def inspect_copy(path):
        path = Path(path)
        copies.append(path)
        assert path != source and path.read_bytes() == original
        assert_private_mode(path, PRIVATE_FILE_MODE)
        assert_private_mode(path.parent, 0o700)
        if parser_failure == "read-error":
            raise OSError("validation copy read failed")
        if parser_failure == "missing-copy":
            path.unlink()
        return real_read(path)

    monkeypatch.setattr(run_journal, "read_journal_bounded", inspect_copy)
    status = _export(tmp_path)
    captured = capsys.readouterr()
    assert copies and all(not path.parent.exists() for path in copies)
    if parser_failure:
        assert status != 0 and captured.out == "" and "error: journal export:" in captured.err
        if parser_failure == "read-error":
            assert "validation copy read failed" in captured.err
    else:
        assert status == 0 and captured.err == "" and captured.out.encode("utf-8") == original
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    ("since", "sequences"),
    [
        ("2026-07-27", [2, 3, 4]),
        ("2026-07-27T00:30:00Z", [3, 4]),
        ("2026-07-26T20:30:00-04:00", [3, 4]),
        ("2026-07-27T00:45:00Z", [3]),
        ("2026-07-28", []),
    ],
)
def test_since_is_inclusive_utc_and_keeps_sequence_order(tmp_path, capsys, since, sequences):
    source, events = _journal(tmp_path)
    original = source.read_bytes()
    assert _export(tmp_path, "--since", since) == 0
    captured = capsys.readouterr()
    expected = [event for event in events if event.sequence in sequences]
    assert captured.out.encode("utf-8") == b"".join(
        run_events.canonical_bytes(event.to_dict()) + b"\n" for event in expected
    )
    assert [json.loads(line)["event_digest"] for line in captured.out.splitlines()] == [
        event.event_digest for event in expected
    ]
    assert source.read_bytes() == original


@pytest.mark.parametrize("damage", ["partial", "corrupt", "oversized", "wrong-run", "mixed-run"])
@pytest.mark.parametrize("file_output", [False, True])
def test_invalid_full_journal_is_rejected_before_filter_or_output(tmp_path, capsys, damage, file_output):
    source, _ = _journal(tmp_path)
    if damage == "partial":
        with source.open("ab") as handle:
            handle.write(b'{"partial":')
    elif damage == "corrupt":
        source.write_bytes(source.read_bytes().replace(b"private detail 0", b"altered detail 0"))
    elif damage == "oversized":
        with source.open("ab") as handle:
            handle.truncate(run_checkpoint.MAX_JOURNAL_BYTES + 1)
    elif damage == "wrong-run":
        other, _ = _journal(tmp_path, run_id="another-run", times=TIMES[:1])
        source.write_bytes(other.read_bytes())
    else:
        run_journal.append_event(
            source,
            run_id="another-run",
            event_type="run.completed",
            payload={"status": "ok"},
            idempotency_key="other-run-completed",
            expected_previous_sequence=len(TIMES),
            recorded_at="2026-07-28T00:00:00.000000Z",
        )
    original = source.read_bytes()
    output = tmp_path / "export.jsonl"
    options = ["--since", "2099-01-01"]
    if file_output:
        options += ["--out", str(output)]
    assert _export(tmp_path, *options) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err and "invalid choice" not in captured.err
    assert not output.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("missing", ["target", "run", "journal", "prefix"])
def test_missing_source_does_not_create_directories(tmp_path, capsys, missing):
    target = tmp_path / "workspace"
    selected_id = RUN_ID
    if missing == "prefix":
        _journal(target)
        selected_id = RUN_ID[:8]
    elif missing == "journal":
        (target / ".brigade" / "runs" / RUN_ID).mkdir(parents=True)
    elif missing == "run":
        target.mkdir()
    before = set(tmp_path.rglob("*"))
    assert _export(target, run_id=selected_id) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err and "invalid choice" not in captured.err
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("run_id", ["latest", ".", "..", "../outside", "nested/run", "nested\\run", "C:run", ""])
def test_run_id_must_be_an_exact_bare_name(tmp_path, capsys, run_id):
    _journal(tmp_path, run_id="latest" if run_id == "latest" else RUN_ID)
    assert _export(tmp_path, run_id=run_id) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "run id" in captured.err.lower()


@pytest.mark.parametrize(
    "since", ["2026-02-30", "20260727", "2026-07-27T00:00:00", "yesterday", "", "0001-01-01T00:00:00+14:00"]
)
def test_invalid_since_fails_without_output(tmp_path, capsys, since):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "export.jsonl"
    assert _export(tmp_path, "--since", since, "--out", str(output)) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "since" in captured.err
    assert not output.exists() and source.read_bytes() == original


def test_format_and_required_run_id_are_parser_contracts(tmp_path, capsys):
    _journal(tmp_path)
    assert _export(tmp_path, "--format", "csv") != 0
    assert "csv" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["receipts", "export", "journal", "--target", str(tmp_path)])
    assert exc.value.code != 0
    assert "--run-id" in capsys.readouterr().err


@pytest.mark.parametrize("component", ["target", ".brigade", "runs", "run", "events", "journal"])
def test_symlinked_source_components_are_rejected(tmp_path, capsys, component):
    target = tmp_path / "workspace"
    source, _ = _journal(target)
    original = source.read_bytes()
    linked = {
        "target": target,
        ".brigade": target / ".brigade",
        "runs": target / ".brigade" / "runs",
        "run": source.parent.parent,
        "events": source.parent,
        "journal": source,
    }[component]
    relocated = linked.with_name(linked.name + "-real")
    linked.rename(relocated)
    try:
        linked.symlink_to(relocated, target_is_directory=component != "journal")
    except OSError:
        pytest.skip("host cannot create symlinks")
    output = tmp_path / "export.jsonl"
    assert _export(target, "--out", str(output)) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err and "invalid choice" not in captured.err
    assert source.read_bytes() == original and not output.exists()


def test_file_output_is_private_canonical_and_never_overwrites(tmp_path, capsys):
    source, _ = _journal(tmp_path)
    output = tmp_path / "export.jsonl"
    assert _export(tmp_path, "--format", "jsonl", "--out", str(output)) == 0
    assert capsys.readouterr().out == ""
    assert output.read_bytes() == source.read_bytes()
    assert_private_mode(output, PRIVATE_FILE_MODE)
    output.write_bytes(b"existing output\n")
    assert _export(tmp_path, "--out", str(output)) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "error:" in captured.err and "invalid choice" not in captured.err
    assert output.read_bytes() == b"existing output\n"


@pytest.mark.parametrize("option", ["target", "out"])
@pytest.mark.parametrize("expansion", ["native", "controlled"])
def test_unresolvable_user_path_is_refused(tmp_path, capsys, monkeypatch, option, expansion):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    unknown = Path("~brigade-nonexistent-user-7f9c2e/export.jsonl")
    output = tmp_path / "export.jsonl"
    if expansion == "native":
        if os.name != "posix":
            pytest.skip("POSIX unknown-user expansion")
        with pytest.raises(RuntimeError):
            unknown.expanduser()
    else:
        real_expand = Path.expanduser
        attempted = False

        def fail_unknown(path):
            nonlocal attempted
            if path == unknown:
                attempted = True
                raise RuntimeError("Could not determine home directory.")
            return real_expand(path)

        monkeypatch.setattr(Path, "expanduser", fail_unknown)
    target = unknown if option == "target" else tmp_path
    destination = str(unknown) if option == "out" else str(output)
    status = _export(target, "--out", destination)
    captured = capsys.readouterr()
    if expansion == "controlled":
        assert attempted
    assert status != 0 and captured.out == ""
    assert captured.err.startswith("error: journal export:")
    assert "home directory" in captured.err and "Traceback" not in captured.err
    assert not output.exists() and not unknown.exists()
    assert source.read_bytes() == original


def test_validation_runtime_error_is_not_translated_to_path_refusal(tmp_path, capsys, monkeypatch):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "export.jsonl"

    def fail_validation(path):
        raise RuntimeError("internal validation failure")

    monkeypatch.setattr(run_journal, "read_journal_bounded", fail_validation)
    with pytest.raises(RuntimeError, match="internal validation failure"):
        _export(tmp_path, "--out", str(output))
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert not output.exists() and source.read_bytes() == original


def test_output_errors_do_not_mutate_source(tmp_path, capsys):
    source, _ = _journal(tmp_path)
    original = source.read_bytes()
    assert _export(tmp_path, "--out", str(tmp_path / "missing" / "export.jsonl")) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and "error:" in captured.err and "invalid choice" not in captured.err
    assert source.read_bytes() == original
    assert not (tmp_path / "missing").exists()
    assert _export(tmp_path, "--out", str(source)) != 0
    assert source.read_bytes() == original


@pytest.mark.parametrize("file_output", [False, True])
def test_existing_empty_journal_exports_zero_rows(tmp_path, capsys, file_output):
    source, _ = _journal(tmp_path, times=())
    output = tmp_path / "export.jsonl"
    options = ["--out", str(output)] if file_output else []
    assert _export(tmp_path, *options) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert source.read_bytes() == b""
    if file_output:
        assert output.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory rename race")
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_run_directory_swap_cannot_redirect_export(tmp_path, capsys, monkeypatch, replacement):
    target = tmp_path / "workspace"
    source, _ = _journal(target)
    original = source.read_bytes()
    outside, _ = _journal(tmp_path / "outside", times=TIMES[:1])
    real_open = dirfd.open_child_directory
    swapped = False

    def swap_after_open(parent, name, **kwargs):
        nonlocal swapped
        descriptor = real_open(parent, name, **kwargs)
        if name == RUN_ID:
            source.parent.parent.rename(source.parent.parent.with_name("held-run"))
            if replacement == "symlink":
                source.parent.parent.symlink_to(outside.parent.parent, target_is_directory=True)
            else:
                shutil.copytree(outside.parent.parent, source.parent.parent)
            swapped = True
        return descriptor

    monkeypatch.setattr(dirfd, "open_child_directory", swap_after_open)
    status = _export(target)
    captured = capsys.readouterr()
    assert swapped
    assert status != 0 and captured.out == ""
    if replacement == "directory":
        assert "run directory changed during source traversal" in captured.err
    assert outside.read_bytes() != original


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory rename race")
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
@pytest.mark.parametrize("file_output", [False, True])
def test_run_directory_swap_during_validation_is_rejected(tmp_path, capsys, monkeypatch, replacement, file_output):
    target = tmp_path / "workspace"
    source, _ = _journal(target)
    original = source.read_bytes()
    other, _ = _journal(tmp_path / "replacement", times=TIMES[:1])
    replacement_bytes = other.read_bytes()
    run_dir = source.parent.parent
    held = run_dir.with_name("held-run")
    output = tmp_path / "export.jsonl"
    real_read = run_journal.read_journal_bounded
    swapped = False

    def swap_during_validation(path):
        nonlocal swapped
        assert path != source and path.read_bytes() == original
        run_dir.rename(held)
        if replacement == "directory":
            other.parent.parent.rename(run_dir)
        else:
            run_dir.symlink_to(other.parent.parent, target_is_directory=True)
        swapped = True
        return real_read(path)

    monkeypatch.setattr(run_journal, "read_journal_bounded", swap_during_validation)
    options = ["--out", str(output)] if file_output else []
    status = _export(target, *options)
    captured = capsys.readouterr()
    assert swapped, captured.err
    assert status != 0 and captured.out == "" and "error: journal export:" in captured.err
    assert "run directory changed during export" in captured.err
    assert not output.exists()
    assert (held / "events" / "lifecycle.jsonl").read_bytes() == original
    assert source.read_bytes() == replacement_bytes != original
