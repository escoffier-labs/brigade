"""An installed shipped file that equals a past bundled release is refreshed; anything else is kept."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from brigade import care_cmd, care_runbooks, dirfd, memory_cmd, shipped_history, skill_wiring
from brigade.install import install_selection
from brigade.selection import Selection
from brigade.templates import template_root

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
BRIGADE_WORK = Path(".claude/skills/brigade-work/SKILL.md")
OLD = b"# an older bundled brigade-work\n"
OLD_KEY = "skill:brigade-work"


def _sel() -> Selection:
    return Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])


def _bundled(skill_id: str = "brigade-work") -> bytes:
    return (template_root() / "skills" / skill_id / "SKILL.md").read_bytes()


def _publish_history(tmp_path: Path, monkeypatch, files: dict[str, list[bytes]]) -> Path:
    """A throwaway manifest that records the given texts (skills by SKILL.md bytes, runbooks by payload dict JSON)."""
    body: dict[str, list[dict[str, str | None]]] = {}
    for key, texts in files.items():
        digest = shipped_history.skill_digest if key.startswith("skill:") else shipped_history.runbook_bytes_digest
        rows = [{"sha256": digest(text), "tag": f"v0.{i}.0"} for i, text in enumerate(texts, start=1)]
        body[key] = rows
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"schema": 1, "files": body}), encoding="utf-8")
    monkeypatch.setattr(shipped_history, "manifest_path", lambda: path)
    return path


def _history_for_skill(tmp_path, monkeypatch, *old: bytes):
    return _publish_history(tmp_path, monkeypatch, {OLD_KEY: [*old, _bundled()]})


def _proposals(target: Path) -> list[dict]:
    inbox = target / ".brigade" / "skills" / "inbox"
    return [json.loads(p.read_text()) for p in sorted(inbox.glob("*/proposal.json"))]


def _wired(target: Path) -> dict:
    return json.loads((target / ".brigade" / "skills" / "wired.json").read_text())["skills"]


def _leftovers(target: Path) -> list[str]:
    return sorted(p.name for p in (target / BRIGADE_WORK).parent.iterdir() if p.name != "SKILL.md")


# --- SKILL.md ---


def test_a_pristine_past_release_skill_is_refreshed_and_recorded(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD)
    _history_for_skill(tmp_path, monkeypatch, OLD)
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    out = capsys.readouterr().out
    assert f"brigade: refreshed (pristine): {tmp_path / BRIGADE_WORK}" in out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == _bundled()
    assert _leftovers(tmp_path) == []  # no .bak, no temp file
    record = _wired(tmp_path)[BRIGADE_WORK.as_posix()]
    assert record["sha256"] == hashlib.sha256(_bundled()).hexdigest()
    assert record["template_sha256"] == record["sha256"]
    assert _proposals(tmp_path) == []


def test_a_crlf_copy_of_a_past_release_is_refreshed(tmp_path, monkeypatch):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD.replace(b"\n", b"\r\n"))
    _history_for_skill(tmp_path, monkeypatch, OLD)

    assert install_selection(tmp_path, _sel()) == 0

    assert (tmp_path / BRIGADE_WORK).read_bytes() == _bundled()


def test_an_edited_copy_is_never_refreshed_even_when_a_past_release_is_recorded(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    edited = OLD + b"my local note\n"
    (tmp_path / BRIGADE_WORK).write_bytes(edited)
    _history_for_skill(tmp_path, monkeypatch, OLD)
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    assert f"kept (edited): {tmp_path / BRIGADE_WORK}" in capsys.readouterr().out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == edited
    assert len(_proposals(tmp_path)) == 1


def test_a_recorded_but_unreleased_stale_copy_keeps_todays_behavior(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD)
    state = tmp_path / ".brigade" / "skills" / "wired.json"
    payload = json.loads(state.read_text())
    payload["skills"][BRIGADE_WORK.as_posix()]["sha256"] = hashlib.sha256(OLD).hexdigest()
    state.write_text(json.dumps(payload))
    _history_for_skill(tmp_path, monkeypatch)  # the manifest does not know this text
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    assert f"kept (pristine-stale): {tmp_path / BRIGADE_WORK}" in capsys.readouterr().out
    assert (tmp_path / BRIGADE_WORK).read_bytes() == OLD


@pytest.mark.parametrize("break_manifest", ["missing", "corrupt", "no-current-text"])
def test_an_unusable_manifest_means_nothing_is_refreshed(tmp_path, monkeypatch, break_manifest):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD)
    path = _history_for_skill(tmp_path, monkeypatch, OLD)
    if break_manifest == "missing":
        path.unlink()
    elif break_manifest == "corrupt":
        path.write_text("{not json")
    else:  # the text about to be written is not a recorded bundled text
        _publish_history(tmp_path, monkeypatch, {OLD_KEY: [OLD]})

    assert install_selection(tmp_path, _sel()) == 0

    assert (tmp_path / BRIGADE_WORK).read_bytes() == OLD


def test_the_proposal_init_filed_for_a_stale_copy_is_superseded_by_the_refresh(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD)
    _history_for_skill(tmp_path, monkeypatch)  # first: the text is unknown, so init files a proposal
    assert install_selection(tmp_path, _sel()) == 0
    (stale,) = _proposals(tmp_path)
    assert stale["status"] == "pending"
    other = {**stale, "proposal_id": "other", "dedupe_key": "init:brigade-work:.codex/skills/brigade-work/SKILL.md:x"}
    other_dir = tmp_path / ".brigade" / "skills" / "inbox" / "other"
    other_dir.mkdir()
    (other_dir / "proposal.json").write_text(json.dumps(other))
    _history_for_skill(tmp_path, monkeypatch, OLD)  # then the release history learns it
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    assert f"superseded skills inbox proposal {stale['proposal_id']}" in capsys.readouterr().out
    by_id = {p["proposal_id"]: p for p in _proposals(tmp_path)}
    assert by_id[stale["proposal_id"]]["status"] == "superseded"
    assert by_id[stale["proposal_id"]]["reason"] == "refreshed to current bundled text"
    assert by_id["other"]["status"] == "pending"  # a proposal for another destination is untouched

    # The operator now edits the refreshed file: the same bundled text is proposed afresh, not muted.
    (tmp_path / BRIGADE_WORK).write_bytes(_bundled() + b"edited\n")
    assert install_selection(tmp_path, _sel()) == 0
    statuses = sorted(p["status"] for p in _proposals(tmp_path))
    assert statuses == ["pending", "pending", "superseded"]


def test_a_failed_publish_keeps_the_copy_and_the_pending_proposal(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    (tmp_path / BRIGADE_WORK).write_bytes(OLD)
    _history_for_skill(tmp_path, monkeypatch)
    assert install_selection(tmp_path, _sel()) == 0
    _history_for_skill(tmp_path, monkeypatch, OLD)
    real = dirfd.replace_children

    def failing(parent, source, destination, *, replace=True):
        if replace and destination == "SKILL.md":
            raise OSError(5, "disk on fire")
        return real(parent, source, destination, replace=replace)

    monkeypatch.setattr(dirfd, "replace_children", failing)
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    captured = capsys.readouterr()
    assert "could not refresh SKILL.md" in captured.err
    assert (tmp_path / BRIGADE_WORK).read_bytes() == OLD
    assert [p["status"] for p in _proposals(tmp_path)] == ["pending"]
    assert _leftovers(tmp_path) == []


@POSIX_ONLY
def test_a_copy_edited_between_the_compare_and_the_publish_is_kept(tmp_path, monkeypatch, capsys):
    assert install_selection(tmp_path, _sel()) == 0
    skill = tmp_path / BRIGADE_WORK
    skill.write_bytes(OLD)
    _history_for_skill(tmp_path, monkeypatch, OLD)
    real_open = os.open
    reads = []
    raced = OLD + b"typed while init ran\n"

    def racing_open(path, flags, *args, **kwargs):
        if str(path).endswith("SKILL.md") and not flags & os.O_CREAT:
            reads.append(path)
            if len(reads) == 2:  # the re-read right before the publish
                skill.write_bytes(raced)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) | {racing_open})
    capsys.readouterr()

    assert install_selection(tmp_path, _sel()) == 0

    assert len(reads) >= 2
    assert f"kept ({skill_wiring.RACED})" in capsys.readouterr().out
    assert skill.read_bytes() == raced
    assert _proposals(tmp_path) == []
    assert _leftovers(tmp_path) == []


@POSIX_ONLY
def test_a_symlinked_skill_md_is_not_refreshed_through_the_link(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "elsewhere" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_bytes(OLD)
    link = tmp_path / "ws" / BRIGADE_WORK
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    _history_for_skill(tmp_path, monkeypatch, OLD)

    assert install_selection(tmp_path / "ws", _sel()) == 0

    assert link.is_symlink()
    assert outside.read_bytes() == OLD
    assert f"kept ({skill_wiring.SYMLINKED})" in capsys.readouterr().out


# --- runbooks ---

DAILY = memory_cmd.MEMORY_CARE_RUNBOOK_NAMES[1]
OLD_PAYLOAD = {"id": "daily-care-pass", "description": "an older runbook", "allowed_commands": ["brigade"], "steps": []}


def _runbook_key(name: str) -> str:
    return shipped_history.runbook_key(f"{memory_cmd.MEMORY_CARE_RUNBOOKS_REL}/{name}")


def _seed_runbooks(tmp_path: Path, monkeypatch, *, known_old: bool = True) -> Path:
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0
    path = tmp_path / memory_cmd.MEMORY_CARE_RUNBOOKS_REL / DAILY
    current = json.loads(path.read_text())
    current_bytes = json.dumps({k: v for k, v in current.items() if k != "pins"}).encode()
    old_bytes = json.dumps(OLD_PAYLOAD).encode()
    texts = [old_bytes, current_bytes] if known_old else [current_bytes]
    _publish_history(tmp_path, monkeypatch, {_runbook_key(DAILY): texts})
    path.write_text(json.dumps({**OLD_PAYLOAD, "pins": current["pins"]}, indent=2) + "\n")
    return path


def _backups(path: Path) -> list[Path]:
    return list(path.parent.glob("*.bak-*"))


def test_a_pristine_past_release_runbook_is_refreshed_without_a_backup(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert f"refreshed (pristine): {path}" in capsys.readouterr().out
    refreshed = json.loads(path.read_text())
    assert refreshed["id"] == "daily-care-pass" and refreshed["steps"] and refreshed["pins"]
    assert _backups(path) == []
    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0  # now identical: silent
    assert capsys.readouterr().out == ""


def test_a_customized_or_unknown_runbook_is_kept_and_reported_once(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch, known_old=False)
    before = path.read_bytes()
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert capsys.readouterr().out.count(f"kept (drifted): {path}") == 1
    assert path.read_bytes() == before
    assert _backups(path) == []


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"a": 1, "a": 2}', b"\xff"])
def test_an_unparseable_runbook_is_kept_and_reported(tmp_path, monkeypatch, capsys, content):
    path = _seed_runbooks(tmp_path, monkeypatch)
    path.write_bytes(content)
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert f"kept (drifted): {path}" in capsys.readouterr().out
    assert path.read_bytes() == content


def test_a_pins_only_difference_is_silent_and_untouched(tmp_path, monkeypatch, capsys):
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=True) == 0
    path = tmp_path / memory_cmd.MEMORY_CARE_RUNBOOKS_REL / DAILY
    payload = json.loads(path.read_text())
    payload["pins"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert capsys.readouterr().out == ""
    assert path.read_bytes() == before


def test_quiet_mode_reports_runbook_outcomes_on_stderr(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    specs, _ = care_cmd._install_shipped_specs()
    other = path.with_name("ingest-sweep.json")
    other.write_text('{"id": "ingest-sweep", "operator": "mine"}\n')
    capsys.readouterr()

    assert care_runbooks.ensure(tmp_path, specs, quiet=True) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"refreshed (pristine): {path}" in captured.err
    assert f"kept (drifted): {other}" in captured.err


def test_a_dry_run_reports_the_refresh_and_writes_nothing(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    before = path.read_bytes()
    specs, _ = care_cmd._install_shipped_specs()
    capsys.readouterr()

    assert care_runbooks.ensure(tmp_path, specs, dry_run=True) == 0

    assert f"would refresh (pristine): {path}" in capsys.readouterr().out
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".runbooks-*"))


def test_a_runbook_edited_between_the_compare_and_the_publish_is_kept(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    raced = b'{"id": "daily-care-pass", "operator": "typed during install"}\n'
    real_stage = care_runbooks._stage

    def staging_then_edit(target, spec, stage_dir):
        result = real_stage(target, spec, stage_dir)
        if Path(spec.rel).name == DAILY:
            path.write_bytes(raced)
        return result

    monkeypatch.setattr(care_runbooks, "_stage", staging_then_edit)
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert path.read_bytes() == raced
    assert f"kept (drifted): {path}" in capsys.readouterr().out
    assert _backups(path) == []


@POSIX_ONLY
def test_a_runbook_swapped_for_a_link_before_the_publish_is_never_replaced(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(path.read_bytes())
    real_stage = care_runbooks._stage

    def staging_then_link(target, spec, stage_dir):
        result = real_stage(target, spec, stage_dir)
        if Path(spec.rel).name == DAILY:
            path.unlink()
            path.symlink_to(elsewhere)
        return result

    monkeypatch.setattr(care_runbooks, "_stage", staging_then_link)
    before = elsewhere.read_bytes()

    assert memory_cmd._write_memory_care_runbooks(tmp_path) == 0

    assert path.is_symlink() and elsewhere.read_bytes() == before


def test_an_explicit_force_refresh_keeps_its_backup_semantics(tmp_path, monkeypatch, capsys):
    path = _seed_runbooks(tmp_path, monkeypatch)
    capsys.readouterr()

    assert memory_cmd._write_memory_care_runbooks(tmp_path, refresh=True) == 0

    assert "replaced shipped runbook" in capsys.readouterr().out
    assert len(_backups(path)) == 1


def test_every_runbook_spec_has_its_current_text_recorded():
    specs, rc = care_cmd._install_shipped_specs()
    assert specs is not None and rc == 0
    from brigade import care_patrol

    for spec in [*specs, care_cmd._memory_closeout_spec(), *care_patrol.selected_specs(())] + [
        care_runbooks.RunbookSpec(care_patrol.RUNBOOK_REL, care_patrol.RUNBOOK_PAYLOAD)
    ]:
        known = shipped_history.digests(shipped_history.runbook_key(spec.rel))
        assert shipped_history.runbook_payload_digest(spec.payload) in known, spec.rel
