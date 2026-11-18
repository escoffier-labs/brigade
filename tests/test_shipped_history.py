"""The shipped-file history: digests, the manifest loader, and the generator/check script."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from brigade import shipped_history

ROOT = Path(__file__).resolve().parents[1]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _script():
    spec = importlib.util.spec_from_file_location("shipped_file_history_test", ROOT / "scripts/shipped_file_history.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- digests ---


def test_skill_digest_folds_crlf_only():
    assert shipped_history.skill_digest(b"a\r\nb\n") == shipped_history.skill_digest(b"a\nb\n")
    assert shipped_history.skill_digest(b"a\nb\n") != shipped_history.skill_digest(b"a\nb")


def test_runbook_digest_drops_pins_and_only_pins():
    body = {"id": "x", "steps": [{"id": "a", "run": "brigade doctor"}], "allowed_commands": ["brigade"]}
    plain = shipped_history.runbook_payload_digest(body)
    assert shipped_history.runbook_payload_digest({**body, "pins": [{"sha256": "1"}]}) == plain
    assert shipped_history.runbook_payload_digest(dict(reversed(list(body.items())))) == plain
    assert shipped_history.runbook_payload_digest({**body, "operator_note": "mine"}) != plain
    nested = {**body, "steps": [{"id": "a", "run": "brigade doctor", "pins": 1}]}
    assert shipped_history.runbook_payload_digest(nested) != plain  # only the top-level key is dropped


def test_runbook_digest_keeps_non_ascii_text_literal():
    one = shipped_history.runbook_payload_digest({"id": "é"})
    assert one == shipped_history.runbook_bytes_digest('{"id": "é"}'.encode())
    assert one == shipped_history.runbook_bytes_digest(b'{"id": "\\u00e9"}')


@pytest.mark.parametrize("raw", [b"[]", b"3", b"{", b"\xff", b'{"a": 1, "a": 2}', b"[" * 5000])
def test_runbook_bytes_that_are_not_a_plain_object_have_no_digest(raw):
    assert shipped_history.runbook_bytes_digest(raw) is None


# --- manifest loader ---

GOOD = "a" * 64


def _manifest(files: object, schema: object = 1) -> bytes:
    return json.dumps({"schema": schema, "files": files}).encode()


def test_parse_manifest_accepts_a_wellformed_record():
    parsed = shipped_history.parse_manifest(
        _manifest({"skill:note": [{"sha256": GOOD, "tag": "v0.5.0"}, {"sha256": "b" * 64, "tag": None}]})
    )
    assert parsed["skill:note"] == (shipped_history.Entry(GOOD, "v0.5.0"), shipped_history.Entry("b" * 64, None))


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"schema": 1}).encode(),
        _manifest({}, schema=2),
        _manifest({}, schema=True),
        _manifest([]),
        _manifest({"other:x": [{"sha256": GOOD, "tag": None}]}),
        _manifest({"skill:x": []}),
        _manifest({"skill:x": [{"sha256": "short", "tag": None}]}),
        _manifest({"skill:x": [{"sha256": GOOD.upper(), "tag": None}]}),
        _manifest({"skill:x": [{"sha256": GOOD, "tag": "0.5.0"}]}),
        _manifest({"skill:x": [{"sha256": GOOD, "tag": None, "extra": 1}]}),
        _manifest({"skill:x": [{"sha256": GOOD, "tag": None}, {"sha256": GOOD, "tag": "v1.0.0"}]}),
    ],
)
def test_parse_manifest_rejects_malformed_records(raw):
    with pytest.raises(ValueError):
        shipped_history.parse_manifest(raw)


def test_the_committed_manifest_loads_and_covers_every_shipped_file():
    manifest = shipped_history.load()
    assert "skill:brigade-work" in manifest and "runbook:.brigade/memory-care/runbooks/daily-care-pass.json" in manifest
    assert shipped_history.digests("skill:brigade-work")
    assert shipped_history.digests("skill:unknown") == frozenset()


def test_the_loader_gives_no_digests_for_a_missing_oversized_or_special_file(tmp_path):
    missing = tmp_path / "missing.json"
    big = tmp_path / "big.json"
    big.write_bytes(_manifest({"skill:x": [{"sha256": GOOD, "tag": None}]}) + b" " * shipped_history.MAX_MANIFEST_BYTES)
    folder = tmp_path / "dir.json"
    folder.mkdir()
    for path in (missing, big, folder):
        assert shipped_history.digests("skill:x", path) == frozenset()


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks and FIFOs")
def test_the_loader_refuses_a_symlinked_manifest_and_a_fifo(tmp_path):
    real = tmp_path / "real.json"
    real.write_bytes(_manifest({"skill:x": [{"sha256": GOOD, "tag": None}]}))
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert shipped_history.digests("skill:x", real) == frozenset({GOOD})
    assert shipped_history.digests("skill:x", link) == frozenset()
    with pytest.raises(OSError):
        shipped_history.load(link)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert shipped_history.digests("skill:x", fifo) == frozenset()


# --- generator and check against a throwaway repository ---

SKILLS = ("brigade-work", "ultra-work-scout", "note")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def _write_sources(repo: Path, generation: str) -> None:
    for skill in SKILLS:
        path = repo / f"src/brigade/templates/skills/{skill}/SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {skill} {generation}\n", encoding="utf-8")
    runbooks = repo / "src/brigade/templates/memory-care/runbooks"
    runbooks.mkdir(parents=True, exist_ok=True)
    (runbooks / "daily-care-pass.json").write_text(json.dumps({"id": "daily", "gen": generation}), encoding="utf-8")
    cmd = repo / "src/brigade/care_cmd.py"
    cmd.write_text(
        "from typing import Any\n"
        f'DAILY_OBSERVABILITY_RUNBOOK_PAYLOAD: dict[str, Any] = {{"id": "obs", "description": ("a " "{generation}")}}\n'
        f'MEMORY_CLOSEOUT_RUNBOOK_PAYLOAD = {{"id": "closeout", "gen": "{generation}"}}\n',
        encoding="utf-8",
    )
    (repo / "src/brigade/care_patrol.py").write_text('RUNBOOK_PAYLOAD = {"id": "patrol", "g": 1}\n', encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write_sources(root, "one")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    _git(root, "tag", "v0.1.0")
    _write_sources(root, "two")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "two")
    _git(root, "tag", "-a", "-m", "v0.2.0", "v0.2.0")
    _write_sources(root, "three")  # the working tree: unreleased
    script = _script()
    monkeypatch.setattr(script, "ROOT", root)
    monkeypatch.setenv(script.BASE_REF_ENV, "no-such-ref")
    return script, root


def _entries(script, key):
    return [(e.tag, e.sha256) for e in shipped_history.load(script.ROOT / script.MANIFEST_REL)[key]]


def test_write_records_tags_in_release_order_then_the_working_tree(repo):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)

    script.localio.write_text_atomic(manifest, script.render(script.build({})))

    key = "skill:brigade-work"
    assert [tag for tag, _ in _entries(script, key)] == ["v0.1.0", "v0.2.0", None]
    assert [(t, d) for t, d in _entries(script, key)][0][1] == shipped_history.skill_digest(b"# brigade-work one\n")
    # Python payloads are read as literals, including implicit string concatenation.
    obs = "runbook:.brigade/memory-care/runbooks/daily-observability.json"
    assert [tag for tag, _ in _entries(script, obs)] == ["v0.1.0", "v0.2.0", None]
    assert _entries(script, "runbook:.brigade/runbooks/run-patrol.json")[0][0] == "v0.1.0"
    assert len(_entries(script, "runbook:.brigade/runbooks/run-patrol.json")) == 1  # unchanged text, one entry
    assert script.check(manifest) == []


def test_check_fails_when_a_template_changes_without_regenerating(repo):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(script.render(script.build({})), encoding="utf-8")

    note = root / "src/brigade/templates/skills/note/SKILL.md"
    note.write_text("# note changed\n", encoding="utf-8")
    problems = script.check(manifest)
    assert any("skill:note" in p and "not recorded" in p for p in problems)

    json_edit = root / "src/brigade/templates/memory-care/runbooks/daily-care-pass.json"
    json_edit.write_text(json.dumps({"id": "daily", "gen": "again"}), encoding="utf-8")
    assert any("daily-care-pass" in p for p in script.check(manifest))


def test_check_rejects_a_missing_or_malformed_manifest(repo):
    script, root = repo
    missing = root / "nope.json"
    assert script.check(missing) and "--write" in script.check(missing)[0]
    bad = root / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert script.check(bad)


def test_check_enforces_tag_provenance(repo):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    body = json.loads(script.render(script.build({})))
    # Claim the working-tree text shipped in v0.1.0, and drop a released text.
    key = "skill:note"
    body["files"][key][-1]["tag"] = "v0.1.0"
    del body["files"]["skill:brigade-work"][0]
    manifest.write_text(json.dumps(body), encoding="utf-8")

    problems = script.check(manifest)

    assert any(key in p and "claims tag v0.1.0" in p for p in problems)
    assert any("skill:brigade-work" in p and "shipped in v0.1.0 is not recorded" in p for p in problems)


def test_write_is_append_only_and_check_rejects_dropped_entries(repo, monkeypatch):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(script.render(script.build({})), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "manifest")

    # The working tree moves on; a regenerate keeps the earlier unreleased text.
    _write_sources(root, "four")
    again = script.build(shipped_history.load(manifest))
    assert [e["tag"] for e in again["skill:note"]] == ["v0.1.0", "v0.2.0", None, None]
    manifest.write_text(script.render(again), encoding="utf-8")
    assert script.check(manifest) == []

    # Dropping an entry the base ref recorded fails the gate.
    monkeypatch.setenv(script.BASE_REF_ENV, "HEAD")
    pruned = json.loads(manifest.read_text(encoding="utf-8"))
    del pruned["files"]["skill:note"][2]
    manifest.write_text(json.dumps(pruned), encoding="utf-8")
    assert any("skill:note" in p and "was dropped" in p for p in script.check(manifest))


def test_an_unreleased_entry_that_later_ships_is_promoted_to_its_tag(repo):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(script.render(script.build({})), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "three")
    _git(root, "tag", "-a", "-m", "v0.3.0", "v0.3.0")

    promoted = script.build(shipped_history.load(manifest))

    assert [e["tag"] for e in promoted["skill:note"]] == ["v0.1.0", "v0.2.0", "v0.3.0"]


def test_check_without_tags_skips_provenance_and_says_so(repo, capsys):
    script, root = repo
    manifest = root / script.MANIFEST_REL
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(script.render(script.build({})), encoding="utf-8")
    for tag in ("v0.1.0", "v0.2.0"):
        _git(root, "tag", "-d", tag)

    assert script.check(manifest) == []
    assert "tag provenance not checked" in capsys.readouterr().err


def test_the_repository_manifest_passes_its_own_check():
    script = _script()
    assert script.check(ROOT / script.MANIFEST_REL) == []
