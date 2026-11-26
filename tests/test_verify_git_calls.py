"""Git launch budget and equivalence for work verification."""

import json
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from brigade import code_graph_delta, localio, receipts_cmd, runguard, verify_manifest, verify_manifest_git
from brigade.work_cmd import helpers, verification, verify_runtime


def _git(target, *args):
    return subprocess.run(["git", *args], cwd=target, capture_output=True, text=True, check=True).stdout.strip()


def _initialize(target, *, ignore_work=True):
    target.mkdir()
    _git(target, "init", "-b", "main")
    if ignore_work:
        (target / ".gitignore").write_text(".brigade/\nmemory/outcome/\n", encoding="utf-8")
    (target / "source.py").write_text("value = 1\n", encoding="utf-8")
    _git(target, "add", ".")
    _git(target, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    return target


def _set_origin(target):
    _git(target, "remote", "add", "origin", "https://github.com/example/project.git")
    _git(target, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(target, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _commit(target):
    _git(target, "add", ".")
    _git(target, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture update")


@pytest.fixture
def isolated_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(code_graph_delta, "capture_before", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(code_graph_delta, "capture_after_and_diff", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", lambda *_args, **_kwargs: ("completed", 0, "", ""))
    engine = tmp_path / "test-evidence"
    engine.touch()
    monkeypatch.setattr(receipts_cmd.component_bins, "resolve", lambda _: str(engine))
    run = subprocess.run

    def accept_import(argv, *args, **kwargs):
        if argv[0] != str(engine):
            return run(argv, *args, **kwargs)
        return subprocess.CompletedProcess(
            argv, 0, '{"inserted_items":1,"already_known":false,"status":"complete"}', ""
        )

    monkeypatch.setattr(subprocess, "run", accept_import)


def _verify(target, capsys, *, reuse, capture=None):
    assert (
        verification.verify_run(
            target=target, commands=[["python", "-V"]], json_output=True, capture=capture, reuse=reuse
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def test_captured_verify_git_launch_budget(tmp_path, isolated_verify, monkeypatch, capsys):
    target = _initialize(tmp_path / "repo")
    manifests = target / "verify" / "manifests"
    manifests.mkdir(parents=True)
    for index in range(7):
        (manifests / f"sample-{index}.json").write_text(json.dumps({"manifest_id": f"sample-{index}"}))
    _commit(target)
    _set_origin(target)
    calls = Counter()
    popen = subprocess.Popen

    class CountGit(popen):
        def __init__(self, argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv[0] == "git":
                calls[tuple(argv[1:])] += 1
            super().__init__(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", CountGit)
    fresh = _verify(target, capsys, reuse=False, capture="brigade-work")
    assert fresh["tree_stable"] is True
    assert fresh["outcome_scoreability"]["registered_manifest_ids"] == [f"sample-{i}" for i in range(7)]
    fresh_count = calls.total()
    assert sum(count for argv, count in calls.items() if argv[-3:] == ("remote", "get-url", "origin")) == 1
    calls.clear()
    reused = _verify(target, capsys, reuse=True, capture="brigade-work")
    assert reused["reused_from"] == fresh["run_id"]
    # Leave one launch of headroom above the 24/21 calls in this fixture.
    assert fresh_count <= 25
    assert calls.total() <= 22
    assert receipts_cmd._repository_export_result(target=target, limit=0, new_only=True)["skipped_count"] == 2


def _legacy_snapshot(target, **_kwargs):
    head = helpers._git_value(target, "rev-parse", "HEAD")
    branch = helpers._git_value(target, "rev-parse", "--abbrev-ref", "HEAD")
    status = helpers._git(target, "status", "--porcelain")
    if head is None or branch is None or status.returncode != 0:
        return None
    return {"head": head, "branch": branch, "dirty_files": len(status.stdout.splitlines())}


@pytest.mark.parametrize("topology", ["normal", "detached", "unborn", "worktree", "shallow", "unignored", "ambiguous"])
def test_receipt_git_stamp_matches_legacy(tmp_path, topology):
    target = tmp_path / "café with spaces"
    if topology == "unborn":
        target.mkdir()
        _git(target, "init", "-b", "main")
    else:
        source = _initialize(tmp_path / "source", ignore_work=topology != "unignored")
        if topology == "worktree":
            _git(source, "worktree", "add", "-b", "topic", str(target))
            assert _git(target, "rev-parse", "--git-dir") != _git(target, "rev-parse", "--git-common-dir")
        elif topology == "shallow":
            _git(tmp_path, "clone", "--depth=1", source.as_uri(), str(target))
            assert _git(target, "rev-parse", "--is-shallow-repository") == "true"
        else:
            source.rename(target)
    if topology == "detached":
        _git(target, "switch", "--detach", "HEAD")
    if topology == "ambiguous":
        _git(target, "tag", "main")
    if topology == "unignored":
        (target / ".brigade").mkdir()
        (target / ".brigade" / "state.txt").write_text("generated", encoding="utf-8")
    (target / "ñ with spaces.txt").write_text("untracked", encoding="utf-8")
    expected = _legacy_snapshot(target)
    assert verification._receipt_git_snapshot(target) == expected
    facts = verify_manifest_git.VerifyGitFacts(
        target, read_identity=lambda path: verification._git_head_branch(path, paths=True)
    )
    assert verification._receipt_git_snapshot(target, facts=facts) == expected
    assert facts.tree == localio.tree_fingerprint_with_head(target)


def test_verify_base_scope_is_repo_keyed_and_discarded(tmp_path):
    original = _initialize(tmp_path / "original")
    other = _initialize(tmp_path / "other")
    _set_origin(original)
    original_facts = verify_manifest_git.VerifyGitFacts(original)
    assert original_facts.base.source == "default_branch"
    assert verify_manifest_git.VerifyGitFacts(other).base.source == "no_remote"
    with pytest.raises(ValueError, match="target"):
        verify_manifest.registered_manifest_ids(other, facts=original_facts)
    _git(original, "remote", "remove", "origin")
    assert verify_manifest_git.VerifyGitFacts(original).base.source == "no_remote"


def test_fresh_and_reused_receipt_bytes_match_unbatched_git(tmp_path, isolated_verify, monkeypatch, capsys):
    target = _initialize(tmp_path / "repo")
    _set_origin(target)
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ids = (UUID("aaaaaa00-0000-0000-0000-000000000000"), UUID("bbbbbb00-0000-0000-0000-000000000000"))
    monkeypatch.setattr(helpers, "_now", lambda: start)

    def pair():
        run_ids = iter(ids)
        monkeypatch.setattr(verification, "uuid4", lambda: next(run_ids))
        fresh = _verify(target, capsys, reuse=False)
        reused = _verify(target, capsys, reuse=True)
        assert reused["reused_from"] == fresh["run_id"]
        return tuple((Path(receipt["path"]) / "receipt.json").read_bytes() for receipt in (fresh, reused))

    actual = pair()
    shutil.rmtree(target / ".brigade")
    monkeypatch.setattr(verification, "_receipt_git_snapshot", _legacy_snapshot)
    monkeypatch.setattr(
        verify_manifest_git.VerifyGitFacts,
        "base",
        property(lambda facts: verify_manifest_git.resolve_base(facts.target)),
    )
    monkeypatch.setattr(verify_manifest_git.VerifyGitFacts, "base_files", None)
    assert pair() == actual


def test_verify_rereads_tree_and_base_after_child(tmp_path, isolated_verify, monkeypatch, capsys):
    target = _initialize(tmp_path / "repo")
    _set_origin(target)
    path = target / "verify" / "manifests" / "new-base.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"manifest_id": "new-base"}', encoding="utf-8")
    _git(target, "add", ".")
    _git(target, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "new base")

    def change_tree(*_args, **_kwargs):
        assert verify_manifest_git.resolve_base(target).source == "default_branch"
        _git(target, "remote", "remove", "origin")
        (target / "source.py").write_text("value = 2\n", encoding="utf-8")
        return "completed", 0, "", ""

    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", change_tree)
    receipt = _verify(target, capsys, reuse=False, capture="brigade-work")
    assert receipt["tree_stable"] is False
    assert receipt["tree_fingerprint_after"] != receipt["tree_fingerprint"]
    assert "new-base" in receipt["outcome_scoreability"]["registered_manifest_ids"]
    assert verify_manifest_git.resolve_base(target).source == "no_remote"


def test_reuse_admission_reads_live_tree_in_the_same_process(tmp_path, isolated_verify, capsys):
    target = _initialize(tmp_path / "repo")
    fresh = _verify(target, capsys, reuse=False, capture="brigade-work")
    (target / "source.py").write_text("value = 2\n", encoding="utf-8")
    second = _verify(target, capsys, reuse=True, capture="brigade-work")
    assert "reused_from" not in second
    assert second["tree_stable"] is True
    assert second["tree_fingerprint"] != fresh["tree_fingerprint"]


def test_patch_identity_is_rejected_if_tree_changes_while_collecting_it(tmp_path, monkeypatch):
    target = _initialize(tmp_path / "repo")
    run_dir = tmp_path / "receipt"
    collect = runguard.collect_changes_patch

    def collect_and_edit(*args, **kwargs):
        result = collect(*args, **kwargs)
        (target / "source.py").write_text("value = 2\n", encoding="utf-8")
        return result

    monkeypatch.setattr(runguard, "collect_changes_patch", collect_and_edit)
    identity = verification._capture_verify_identity(target, run_dir)
    assert identity["tree_fingerprint"] is None
    assert identity["changes_patch_sha256"] is None
    assert not (run_dir / "changes.patch").exists()


def test_registry_batch_matches_individual_trusted_reads(tmp_path):
    target = _initialize(tmp_path / "repo")
    directory = target / "verify" / "manifests"
    directory.mkdir(parents=True)
    for name, contents in {
        "café with spaces.json": b'{"manifest_id":"first"}',
        "second.json": b'{"manifest_id":"first"}',
        "broken.json": b"{",
        "array.json": b"[]",
        "bad-encoding.json": b"\xff",
        "large.json": b" " * (verify_manifest_git.MAX_MANIFEST_BYTES + 1),
    }.items():
        (directory / name).write_bytes(contents)
    (directory / "nested.json").mkdir()
    (directory / "nested.json" / "unlisted.json").write_text('{"manifest_id":"unlisted"}')
    _commit(target)
    _set_origin(target)
    (directory / "second.json").unlink()
    (directory / "café with spaces.json").write_text('{"manifest_id":"edited"}')
    base = verify_manifest_git.resolve_base(target)
    expected = set()
    for path in verify_manifest_git.list_base_manifests(target, base):
        try:
            expected.add(verify_manifest_git.load_base_manifest(target, path, base).payload["manifest_id"])
        except verify_manifest_git.ManifestGitError:
            continue
    assert verify_manifest.registered_manifest_ids(target) == sorted(expected) == ["first"]


def test_export_resolves_rewritten_origin_once_and_refreshes_it_per_export(
    tmp_path, isolated_verify, monkeypatch, capsys
):
    target = _initialize(tmp_path / "repo")
    _set_origin(target)
    _git(target, "remote", "set-url", "origin", "shortcut:example/project.git")
    _git(target, "config", "url.https://github.com/.insteadOf", "shortcut:")
    fresh = _verify(target, capsys, reuse=False)
    _verify(target, capsys, reuse=True)
    git_value = receipts_cmd._git_value
    calls = []

    def count_origin(path, *args):
        calls.append((path, args))
        return git_value(path, *args)

    monkeypatch.setattr(receipts_cmd, "_git_value", count_origin)

    def export():
        result = receipts_cmd._repository_export_result(target=target, limit=0, new_only=False)
        assert result["exported_count"] == 2
        return result["lines"]

    actual = export()
    assert calls == [(target, ("remote", "get-url", "origin"))]
    for value, _digest in actual:
        row = json.loads(value)
        assert row["item"]["metadata"]["project"] == "project"
        assert row["links"][0]["url"] == f"https://github.com/example/project/commit/{fresh['git']['head']}"

    uncached_parts = receipts_cmd._ExportOrigin.parts.func
    with monkeypatch.context() as uncached:
        uncached.setattr(receipts_cmd._ExportOrigin, "parts", property(uncached_parts))
        assert export() == actual

    _git(target, "remote", "remove", "origin")
    calls.clear()
    rows = [json.loads(value) for value, _digest in export()]
    assert calls == [(target, ("remote", "get-url", "origin"))]
    assert all(row["item"]["metadata"]["project"] == target.name and not row["links"] for row in rows)
