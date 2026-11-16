"""Fail closed across recorder processes and supported reference sources."""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import memory_card_reads as reads, memory_lifecycle as life, memory_cmd
from tests.memory_read_fixtures import signed_memory_authority as signed_memory_authority
from tests.memory_read_fixtures import automatic_archive_test_mode as automatic_archive_test_mode
from tests.memory_read_fixtures import windows_scheduler as windows_scheduler
from tests.test_memory_lifecycle_security import ID, TODAY, old_card, cover


@pytest.mark.skipif(os.name == "nt", reason="workspace signing authority requires POSIX confinement")
@pytest.mark.parametrize("failure", ["main-writes", "coverage-directory", "brigade-directory", "no-markers"])
def test_failed_explicit_read_blocks_archiving_in_another_process(tmp_path, failure):
    card = old_card(tmp_path)
    cover(tmp_path, end=TODAY - timedelta(days=5))
    child = """
import sys
from pathlib import Path
from brigade import memory_card_reads as reads, memory_mcp_core
root = Path(sys.argv[1])
if sys.argv[2] in {'main-writes', 'brigade-directory', 'no-markers'}:
    def denied(*args, **kwargs):
        raise PermissionError('coverage unavailable')
    reads._write_json = denied
    if sys.argv[2] == 'brigade-directory':
        (root / '.brigade').chmod(0o500)
    if sys.argv[2] == 'no-markers':
        from brigade.run_dirfd import BoundRunDir
        BoundRunDir.write_text_atomic = denied
else:
    state = root / reads.COVERAGE_REL
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o500)
content, failed = memory_mcp_core.MemoryTools(target=root).call('get_card', {'path': 'memory/cards/notes.md'})
assert not failed and 'widget' in content
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    try:
        subprocess.run([sys.executable, "-c", child, str(tmp_path), failure], env=env, check=True)
    finally:
        state = tmp_path / reads.COVERAGE_REL
        if state.exists():
            state.chmod(0o700)
        (tmp_path / ".brigade").chmod(0o700)
    assert reads.load(tmp_path).recorder_error
    result = life.run(tmp_path, today=TODAY)
    assert card.exists(), result
    assert not result["transitions"]
    if failure == "brigade-directory":
        assert reads.load(tmp_path).recorder_error
        assert (tmp_path / ".brigade-read-recorder-disabled").exists()
    else:
        assert reads.load(tmp_path).healthy_since == TODAY


def test_repeated_keyless_reads_write_nothing(tmp_path):
    for _ in range(20):
        reads.record_content(tmp_path, "guidance", "memory/cards/notes.md")
    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("command", [memory_cmd.status, memory_cmd.doctor])
@pytest.mark.parametrize("json_output", [False, True])
def test_keyless_status_and_doctor_explain_disabled_transitions(tmp_path, capsys, command, json_output):
    command(target=tmp_path, json_output=json_output)
    output = capsys.readouterr().out.lower()
    assert "lifecycle and expiry disabled" in output
    assert "workspace" in output and "key" in output


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"state":"pending"}', '{"state":"pending","day":"bad"}', '{"state":"unknown"}']
)
def test_malformed_intent_recovers_without_preserving_body(tmp_path, body):
    old_card(tmp_path)
    cover(tmp_path)
    intent = tmp_path / reads.COVERAGE_REL / "malformed.json"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(body)
    assert reads.load(tmp_path).recorder_error
    assert reads.record(tmp_path, [], today=TODAY)
    assert not reads.load(tmp_path).recorder_error
    assert reads.load(tmp_path).healthy_since == TODAY
    assert not intent.exists()
    quarantine = tmp_path / ".brigade/memory/read-coverage-quarantine"
    reasons = [json.loads(path.read_text()) for path in quarantine.glob("*.json")]
    assert reasons and all(value["reason"] == "malformed-intent" for value in reasons)
    assert all(set(value) == {"reason", "day"} for value in reasons)
    assert not life.run(tmp_path, today=TODAY)["transitions"]


@pytest.mark.parametrize(
    "bootstrap",
    [
        "TOOLS.md",
        "USER.md",
        "SOUL.md",
        "IDENTITY.md",
        "HEARTBEAT.md",
        "SAFETY_RULES.md",
        "INSTALL_FOR_AGENTS.md",
        "AGENTS.override.md",
    ],
)
def test_supported_bootstrap_reference_protects_card(tmp_path, bootstrap):
    card = old_card(tmp_path)
    cover(tmp_path)
    (tmp_path / bootstrap).write_text("[details][dep]\n\n[dep]: memory/cards/notes.md#details\n")
    result = life.run(tmp_path, today=TODAY)
    assert card.exists(), result
    assert any(row["path"] == "memory/cards/notes.md" and row["reason"] == "referenced" for row in result["skipped"])


@pytest.mark.parametrize(
    "rel",
    [
        ".brigade/work/plans/active.md",
        ".brigade/workflows/nested/active.md",
        ".brigade/memory-care/runbooks/sub/active.md",
        ".claude/commands/sub/active.md",
        "runbooks/build/release.md",
        "docs/runbooks/coverage/active.md",
    ],
)
def test_explicit_reference_roots_ignore_managed_exclusions(tmp_path, rel):
    card = old_card(tmp_path)
    cover(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".brigade/*\n.claude/commands/\n")
    reference = tmp_path / rel
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(f"[details][dep]\n\n[dep]: memory/cards/notes.md\n{ID}\n")
    result = life.run(tmp_path, today=TODAY)
    assert card.exists(), result
    assert any(row["reason"] == "referenced" for row in result["skipped"])


@pytest.mark.parametrize(
    "definition",
    [
        "[dep]:\n  notes.md",
        pytest.param("[dep]:\r\n  notes.md", id="crlf"),
        '[dep]:\n  <notes.md#details>\n  "Title"',
        "[dep]: /memory/cards/notes.md",
        "[dep]:\n  /memory/cards/notes.md",
    ],
)
def test_multiline_and_workspace_absolute_reference_destinations(tmp_path, definition):
    card = old_card(tmp_path)
    cover(tmp_path)
    card.with_name("active.md").write_text(f"[details][dep]\n\n{definition}\n", newline="")
    result = life.run(tmp_path, today=TODAY)
    assert card.exists(), result


def test_reference_scan_batches_ignore_queries(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for n in range(30):
        (tmp_path / f"source/{n}/sub").mkdir(parents=True)
    run = subprocess.run
    queries = []

    def observed(args, *a, **kw):
        if args[0] == "git":
            queries.append(args)
        return run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", observed)
    life._references(tmp_path, {})
    assert len(queries) <= 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_unreadable_reference_does_not_authorize_archiving(tmp_path):
    card = old_card(tmp_path)
    cover(tmp_path)
    reference = tmp_path / "TOOLS.md"
    reference.write_text("[details](memory/cards/notes.md)")
    reference.chmod(0)
    try:
        with pytest.raises(PermissionError):
            life.run(tmp_path, today=TODAY)
        assert card.exists()
    finally:
        reference.chmod(0o600)


@pytest.mark.skipif(os.name == "nt", reason="POSIX target symlink")
def test_ignore_pruning_resolves_target_symlink(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored/AGENTS.md").write_text("memory/cards/notes.md")
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    assert "ignored/AGENTS.md" not in life._references(link, {})
