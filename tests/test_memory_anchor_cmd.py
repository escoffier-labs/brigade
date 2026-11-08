"""`brigade memory anchor` backfills claim anchors through the proposal path's own function."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from brigade import cli, memory_anchor_cmd, memory_hub_sync
from brigade import memory_proposal_v2_ops as delta
from tests.test_memory_proposal_capabilities import SAFE_DIRECTORY_CAPABILITY

pytestmark = pytest.mark.skipif(not SAFE_DIRECTORY_CAPABILITY, reason="safe directory descriptors unavailable")

ANCHOR_SUFFIX = re.compile(r" \^c-[0-9a-f]{6}$", re.MULTILINE)
CARDS = "memory/cards"


def make_card(target: Path, name: str, body: str, *, extra: str = "", mode: int | None = None) -> Path:
    path = target / CARDS / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntopic: {name}\nscope: repo\nevidence: []\n{extra}---\n{body}\n")
    if mode is not None:
        path.chmod(mode)
    return path


def snapshot(target: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(target).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(target.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def run(target: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict | str]:
    code = cli.main(["memory", "anchor", "--target", str(target), *args])
    out = capsys.readouterr().out
    return code, (json.loads(out) if "--json" in args else out)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    target = tmp_path / "ws"
    make_card(target, "plain", "Keep the cache warm.\n\n- First item\n- Second item")
    make_card(target, "code", "```\nprint('x')\n```")
    make_card(target, "html", "<div>\nraw html card\n</div>")
    make_card(target, "quoted", "> only a quote\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    make_card(target, "done", "Already anchored. ^c-abcdef\n", extra='claim_anchor_ids: ["c-abcdef"]\n')
    make_card(target, "secret", "Private note.", extra="visibility: private\n")
    return target


def test_dry_run_reports_counts_and_changes_nothing(corpus, capsys):
    before = snapshot(corpus)
    code, payload = run(corpus, "--json", capsys=capsys)
    assert code == 0
    assert snapshot(corpus) == before
    assert payload["mode"] == "dry-run"
    assert payload["files_scanned"] == 6
    assert payload["files_to_change"] == 1
    assert payload["blocks_to_anchor"] == 3
    assert payload["files_changed"] == 0 and payload["blocks_anchored"] == 0
    assert payload["files_already_anchored"] == 1
    assert payload["files_skipped"]["opaque"] == 3
    assert payload["files_skipped"]["withheld"] == 1
    assert payload["files_skipped_total"] == 4


def test_dry_run_text_output_never_prints_card_text(corpus, capsys):
    code, out = run(corpus, capsys=capsys)
    assert code == 0
    assert "Dry run only" in out
    for fragment in ("Keep the cache warm", "First item", "plain", "raw html"):
        assert fragment not in out


def test_apply_matches_anchor_text_exactly(corpus, capsys):
    path = corpus / CARDS / "plain.md"
    original = path.read_bytes().decode()
    expected = delta.anchor_text(original, f"{CARDS}/plain.md")
    code, payload = run(corpus, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert payload["files_changed"] == 1 and payload["blocks_anchored"] == 3
    assert path.read_bytes().decode() == expected
    ids = re.findall(r"\^(c-[0-9a-f]{6})", expected)
    assert len(ids) == 3
    frontmatter = expected.split("---")[1]
    assert all(ident in frontmatter for ident in ids)
    assert [b.block_id for b in delta.blocks(expected)] == ids


def test_apply_is_idempotent_and_leaves_anchored_cards_byte_identical(corpus, capsys):
    run(corpus, "--apply", capsys=capsys)
    after_first = snapshot(corpus)
    code, payload = run(corpus, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert snapshot(corpus) == after_first
    assert payload["files_changed"] == 0 and payload["files_to_change"] == 0
    assert payload["files_already_anchored"] == 2
    code, dry = run(corpus, "--json", capsys=capsys)
    assert dry["files_to_change"] == 0


def test_opaque_blocks_and_html_cards_are_untouched(corpus, capsys):
    originals = {n: (corpus / CARDS / f"{n}.md").read_bytes() for n in ("code", "html", "quoted", "secret")}
    run(corpus, "--apply", capsys=capsys)
    for name, data in originals.items():
        assert (corpus / CARDS / f"{name}.md").read_bytes() == data


def test_opaque_blocks_inside_an_anchored_card_stay_unanchored(tmp_path, capsys):
    target = tmp_path / "ws"
    make_card(target, "mixed", "Plain claim.\n\n```\ncode block\n```\n\n> a quote\n\nSecond claim.")
    run(target, "--apply", capsys=capsys)
    text = (target / CARDS / "mixed.md").read_text()
    assert len(ANCHOR_SUFFIX.findall(text)) == 2
    assert "code block\n```" in text and "> a quote\n" in text


def test_symlinked_card_file_is_never_touched(tmp_path, capsys):
    target = tmp_path / "ws"
    make_card(target, "real", "Real claim.")
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntopic: x\n---\nOutside claim.\n")
    (target / CARDS / "link.md").symlink_to(outside)
    before = outside.read_bytes()
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert outside.read_bytes() == before
    assert (target / CARDS / "link.md").is_symlink()
    assert payload["files_changed"] == 1


def test_symlinked_card_root_and_directory_are_refused(tmp_path, capsys):
    target = tmp_path / "ws"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text("---\ntopic: v\n---\nOutside claim.\n")
    (target / "memory").mkdir(parents=True)
    (target / CARDS).symlink_to(outside, target_is_directory=True)
    before = victim.read_bytes()
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert victim.read_bytes() == before
    assert payload["files_skipped"]["symlink"] == 1 and payload["files_changed"] == 0

    (target / CARDS).unlink()
    (target / CARDS).mkdir()
    (target / CARDS / "sub").symlink_to(outside, target_is_directory=True)
    # Directory symlinks below a real root are not traversed, so the card is never reached.
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0 and payload["files_scanned"] == 0
    assert victim.read_bytes() == before


def test_file_mode_is_preserved(tmp_path, capsys):
    target = tmp_path / "ws"
    path = make_card(target, "mode", "Mode claim.", mode=0o640)
    run(target, "--apply", capsys=capsys)
    assert ANCHOR_SUFFIX.search(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o640


def test_no_temp_files_remain(corpus, capsys):
    run(corpus, "--apply", capsys=capsys)
    assert not [p for p in (corpus / CARDS).iterdir() if p.name.endswith(".tmp")]


def test_json_shape(corpus, capsys):
    code, payload = run(corpus, "--json", capsys=capsys)
    assert code == 0
    assert set(payload) == {
        "schema",
        "target",
        "mode",
        "limit",
        "files_scanned",
        "files_already_anchored",
        "files_to_change",
        "blocks_to_anchor",
        "files_changed",
        "blocks_anchored",
        "files_deferred",
        "files_failed",
        "files_skipped",
        "files_skipped_total",
    }
    assert payload["schema"] == "brigade.memory-anchor.v1"
    assert set(payload["files_skipped"]) == set(memory_anchor_cmd.SKIP_REASONS)


def test_path_filter_and_limit(tmp_path, capsys):
    target = tmp_path / "ws"
    for name in ("a", "b", "c"):
        make_card(target, name, f"Claim {name}.")
    code, payload = run(target, "--path", f"{CARDS}/a.md", "--json", capsys=capsys)
    assert payload["files_scanned"] == 1 and payload["files_to_change"] == 1

    code, payload = run(target, "--apply", "--limit", "2", "--json", capsys=capsys)
    assert payload["files_changed"] == 2 and payload["files_deferred"] == 1
    assert not ANCHOR_SUFFIX.search((target / CARDS / "c.md").read_text())

    code, payload = run(target, "--apply", "--limit", "2", "--json", capsys=capsys)
    assert payload["files_changed"] == 1 and payload["files_deferred"] == 0
    assert ANCHOR_SUFFIX.search((target / CARDS / "c.md").read_text())


@pytest.mark.parametrize(
    "args",
    [["--limit", "0"], ["--path", "../escape.md"], ["--path", "/abs/card.md"]],
)
def test_invalid_arguments_exit_two(corpus, capsys, args):
    before = snapshot(corpus)
    code = cli.main(["memory", "anchor", "--target", str(corpus), "--apply", *args])
    assert code == 2
    assert "error:" in capsys.readouterr().err
    assert snapshot(corpus) == before


def test_missing_target_and_bad_config_exit_two(tmp_path, capsys):
    assert cli.main(["memory", "anchor", "--target", str(tmp_path / "absent")]) == 2
    target = tmp_path / "ws"
    make_card(target, "a", "Claim.")
    (target / ".brigade").mkdir()
    (target / ".brigade/memory-care.toml").write_text("card_roots = 5\n")
    assert cli.main(["memory", "anchor", "--target", str(target)]) == 2
    assert "invalid memory-care config" in capsys.readouterr().err


def test_uses_configured_card_roots_and_read_only_paths(tmp_path, capsys):
    target = tmp_path / "ws"
    for root in ("notes", "frozen"):
        (target / root).mkdir(parents=True)
        (target / root / "n.md").write_text("---\ntopic: n\n---\nClaim.\n")
    (target / "memory/cards").mkdir(parents=True)
    (target / "memory/cards/ignored.md").write_text("---\ntopic: i\n---\nClaim.\n")
    (target / ".brigade").mkdir()
    (target / ".brigade/memory-care.toml").write_text(
        'card_roots = ["notes", "frozen"]\nread_only_paths = ["frozen/*"]\n'
    )
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert payload["files_scanned"] == 2
    assert payload["files_changed"] == 1 and payload["files_skipped"]["read-only"] == 1
    assert ANCHOR_SUFFIX.search((target / "notes/n.md").read_text())
    assert not ANCHOR_SUFFIX.search((target / "frozen/n.md").read_text())
    assert not ANCHOR_SUFFIX.search((target / "memory/cards/ignored.md").read_text())


def test_set_matches_the_sync_push_set(corpus):
    pushed = {c["rel"] for c in memory_hub_sync._local_cards(corpus)}
    report_paths = set()
    from brigade import memory_cmd

    for path in memory_cmd._iter_cards(corpus, memory_cmd._config_or_default(corpus)):
        report_paths.add(path.relative_to(corpus).as_posix())
    assert pushed == report_paths
    report = memory_anchor_cmd.run_anchor(corpus)
    assert report.files_scanned == len(pushed)


def test_crlf_card_matches_anchor_text(tmp_path, capsys):
    target = tmp_path / "ws"
    path = target / CARDS / "crlf.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"---\r\ntopic: crlf\r\n---\r\nFirst claim.\r\n\r\nSecond claim.\r\n")
    expected = delta.anchor_text(path.read_bytes().decode(), f"{CARDS}/crlf.md")
    run(target, "--apply", capsys=capsys)
    assert path.read_bytes() == expected.encode()


def test_malformed_and_oversized_cards_are_skipped(tmp_path, capsys):
    target = tmp_path / "ws"
    make_card(target, "good", "Good claim.")
    bad = target / CARDS / "bad.md"
    bad.write_bytes(b"---\ntopic: bad\n---\n\xff\xfe broken\n")
    huge = target / CARDS / "huge.md"
    huge.write_text("---\ntopic: huge\n---\n" + "word " * 60000 + "\n")
    before = (bad.read_bytes(), huge.read_bytes())
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert (bad.read_bytes(), huge.read_bytes()) == before
    assert payload["files_skipped"]["unreadable"] == 1
    assert payload["files_skipped"]["too-large"] == 1
    assert payload["files_changed"] == 1


def test_duplicate_anchor_ids_are_reported(tmp_path, capsys):
    target = tmp_path / "ws"
    make_card(target, "dup", "One. ^c-abcdef\n\nTwo. ^c-abcdef\n\nThree.")
    before = (target / CARDS / "dup.md").read_bytes()
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert (target / CARDS / "dup.md").read_bytes() == before
    assert payload["files_skipped"]["invalid-anchor-state"] == 1


def test_changed_card_is_refused_and_write_failure_exits_one(tmp_path, capsys, monkeypatch):
    target = tmp_path / "ws"
    path = make_card(target, "race", "Race claim.")
    real = memory_anchor_cmd._plan_file

    def stale(file_path, rel):
        reason, change = real(file_path, rel)
        file_path.write_text("---\ntopic: race\n---\nSomeone else edited this.\n")
        return reason, change

    monkeypatch.setattr(memory_anchor_cmd, "_plan_file", stale)
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 1
    assert payload["files_failed"] == 1 and payload["files_changed"] == 0
    assert path.read_text() == "---\ntopic: race\n---\nSomeone else edited this.\n"
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_replacement_failure_cleans_up_and_keeps_original(tmp_path, monkeypatch):
    target = tmp_path / "ws"
    path = make_card(target, "boom", "Boom claim.")
    original = path.read_bytes()
    reason, change = memory_anchor_cmd._plan_file(path, f"{CARDS}/boom.md")
    assert reason is None and change is not None

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        memory_anchor_cmd._replace(path, change)
    assert path.read_bytes() == original
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_same_inode_edit_before_rename_is_refused(tmp_path, monkeypatch):
    target = tmp_path / "ws"
    path = make_card(target, "inplace", "In place claim.")
    reason, change = memory_anchor_cmd._plan_file(path, f"{CARDS}/inplace.md")
    assert reason is None and change is not None
    edited = b"---\ntopic: inplace\n---\nAn editor wrote this into the same inode.\n"
    real = memory_anchor_cmd._keep_owner
    inode = path.stat().st_ino

    def edit_then_keep(fd, before):
        # Runs after the content check and the temp write, before the final recheck.
        with open(path, "r+b") as handle:
            handle.write(edited)
            handle.truncate()
        real(fd, before)

    monkeypatch.setattr(memory_anchor_cmd, "_keep_owner", edit_then_keep)
    with pytest.raises(OSError, match="changed since it was scanned"):
        memory_anchor_cmd._replace(path, change)
    assert path.stat().st_ino == inode
    assert path.read_bytes() == edited
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_byte_order_mark_card_is_skipped_unchanged(tmp_path, capsys):
    target = tmp_path / "ws"
    path = target / CARDS / "bom.md"
    path.parent.mkdir(parents=True)
    original = b"\xef\xbb\xbf---\ntopic: bom\nscope: repo\nevidence: []\n---\nBOM claim.\n"
    path.write_bytes(original)
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 0
    assert payload["files_skipped"]["byte-order-mark"] == 1
    assert payload["files_changed"] == 0
    assert path.read_bytes() == original


def test_locked_card_is_refused(tmp_path, capsys):
    fcntl = pytest.importorskip("fcntl")
    target = tmp_path / "ws"
    path = make_card(target, "locked", "Locked claim.")
    original = path.read_bytes()
    with open(path, "rb") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 1
    assert payload["files_failed"] == 1 and payload["files_changed"] == 0
    assert path.read_bytes() == original


def test_owner_that_cannot_be_kept_refuses_the_card(tmp_path, monkeypatch):
    out = tmp_path / "temp"
    out.write_bytes(b"x")
    fd = os.open(out, os.O_RDONLY)
    try:
        created = os.fstat(fd)
        fields = list(created)
        fields[4] = created.st_uid + 1  # st_uid of a different owner
        foreign = os.stat_result(fields)

        def refuse(*args, **kwargs):
            raise PermissionError("not permitted")

        monkeypatch.setattr(os, "fchown", refuse)
        with pytest.raises(OSError, match="cannot keep the card's owner"):
            memory_anchor_cmd._keep_owner(fd, foreign)
        memory_anchor_cmd._keep_owner(fd, created)
    finally:
        os.close(fd)


def test_owner_failure_leaves_the_card_and_no_temp(tmp_path, capsys, monkeypatch):
    target = tmp_path / "ws"
    path = make_card(target, "owned", "Owned claim.")
    original = path.read_bytes()

    def refuse(fd, before):
        raise OSError("cannot keep the card's owner")

    monkeypatch.setattr(memory_anchor_cmd, "_keep_owner", refuse)
    code, payload = run(target, "--apply", "--json", capsys=capsys)
    assert code == 1 and payload["files_failed"] == 1
    assert path.read_bytes() == original
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
