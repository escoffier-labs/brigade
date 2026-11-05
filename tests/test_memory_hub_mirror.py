"""One-way mirror contracts, with the S14 client and replica boundary."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest

from brigade import cli
from brigade import memory_hub_client as hubc
from brigade import memory_hub_mirror as mirror
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync


@pytest.fixture
def setup(tmp_path, monkeypatch):
    target = tmp_path / "workspace"
    target.mkdir()
    settings = hubc.HubSettings(mode="read", read_scopes=("fleet",), hub_url="http://127.0.0.1:3774", token="fixture")
    client = hubc.client_for(settings, transport=lambda *_: (200, {}))
    replica = store.Replica(store.root(target) / store.REPLICA)
    store.ensure_root(target)
    replica.meta = {
        "hub": client.identity,
        "principal": "node-test",
        "seq": 1,
        "log_epoch": "epoch",
        "read_scopes": ["fleet"],
    }
    replica.save()
    monkeypatch.setattr(sync, "pull", lambda *_: {"seq": 1, "warnings": []})
    return target, settings, client, tmp_path / "vault", replica


def card(replica, card_id="alpha", **extra):
    head = {
        "scope": "fleet",
        "card_id": card_id,
        "revision": 1,
        "scan_state": "clean",
        "trust": "reviewed",
        "state": "live",
        "text": "A [[Beta]] link.\n",
        "writer": "node-test",
        "updated_at": "2026-11-05T00:00:00Z",
        **extra,
    }
    replica.heads[store.Replica.key("fleet", card_id)] = head
    replica.save()
    return head


def run(setup, **kwargs):
    target, settings, client, out, _ = setup
    return mirror.mirror(target, client, settings, out, **kwargs)


def test_idempotence_and_frontmatter(setup):
    *_, out, replica = setup
    card(replica, text="---\ntitle: Alpha\ntags: [one, two]\n---\n[[Beta]] café\n")
    assert len(run(setup)["written"]) == 2
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()}
    assert run(setup)["written"] == []
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in out.iterdir()}
    note = next(out.glob("alpha-*.md")).read_text(encoding="utf-8")
    for field in ("card_id:", "scope:", "revision:", "updated_at:", "source:", "tags:", "scan_state:"):
        assert field in note
    assert "[[Beta]] café" in note
    assert 'tags: ["one", "two"]' in note


@pytest.mark.parametrize(
    "title",
    [
        "../../escape",
        "/absolute",
        "..",
        "CON",
        "NUL.txt",
        "COM1",
        "LPT9",
        "a\\b:*?<>|",
        "\x00\x01",
        "日本語",
        "A" * 500,
    ],
)
def test_safe_names(setup, title):
    *_, out, replica = setup
    card(replica, text=f"---\ntitle: {title}\n---\nbody\n")
    run(setup)
    names = [p.name for p in out.glob("*.md") if p.name != mirror.INDEX]
    assert len(names) == 1
    assert mirror._safe_name(names[0])
    assert ".." not in names[0]
    assert not any(char in names[0] for char in '\\/:*?"<>|\x00\x01')
    assert len(names[0]) < 100


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"state": "retired"}, "retired"),
        ({"trust": "flagged"}, "flagged"),
        ({"scan_state": "flagged"}, "flagged"),
        ({"scan_state": "pending"}, "scan-pending"),
        ({"scan_state": None}, "scan-unknown"),
        ({"text": "---\nvisibility: private\n---\nsecret"}, "visibility-private"),
        ({"text": "\ufeff---\nvisibility: private\n---\nsecret"}, "visibility-private"),
        ({"text": '---\n"Visibility": private\n---\nsecret'}, "visibility-private"),
        ({"text": "---\nvisibility: mystery\n---\nsecret"}, "visibility-unrecognized"),
        ({"text": "---\nvisibility: private\nsecret"}, "visibility-unrecognized"),
    ],
)
def test_skip_reasons(setup, extra, reason):
    *_, out, replica = setup
    card(replica, **extra)
    report = run(setup)
    assert report["skipped"] == {reason: 1}
    assert list(out.glob("*.md")) == [out / mirror.INDEX]


def test_supersession_chain_and_stable_names(setup):
    *_, out, replica = setup
    card(replica, "alpha")
    card(replica, "beta", text="---\nsupersedes: alpha\n---\nbeta")
    card(replica, "gamma", text="---\nsupersedes: beta\n---\ngamma")
    report = run(setup)
    assert report["skipped"] == {"superseded": 2}
    filename = next(p.name for p in out.glob("gamma-*.md"))
    card(replica, "gamma", text="---\ntitle: Renamed\nsupersedes: beta\n---\ngamma")
    run(setup)
    assert (out / filename).exists()


def test_deletion_boundary_and_skip_cleanup(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    foreign = out / "foreign.md"
    foreign.write_text("mine", encoding="utf-8")
    card(replica, scan_state="pending")
    assert len(run(setup)["deleted"]) == 1
    assert foreign.read_text(encoding="utf-8") == "mine"
    card(replica)
    run(setup)
    replica.heads.clear()
    replica.save()
    assert len(run(setup)["deleted"]) == 1
    assert foreign.exists()


def test_marker_refusal_and_init(setup):
    *_, out, _ = setup
    out.mkdir()
    (out / "foreign.md").write_text("mine", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="--init"):
        run(setup)
    run(setup, init=True)
    assert (out / "foreign.md").read_text(encoding="utf-8") == "mine"


def test_foreign_collision_refused(setup):
    *_, out, _ = setup
    out.mkdir()
    (out / mirror.INDEX).write_text("mine", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="foreign"):
        run(setup, init=True)


def test_dry_run_writes_and_deletes_nothing(setup):
    *_, out, replica = setup
    card(replica)
    assert len(run(setup, dry_run=True)["written"]) == 2
    assert not out.exists()
    run(setup)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    replica.heads.clear()
    replica.save()
    assert len(run(setup, dry_run=True)["deleted"]) == 1
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}


def test_offline_and_unavailable_preserve_folder(setup, monkeypatch):
    target, _, _, out, replica = setup
    card(replica)
    run(setup)

    def down(*_):
        raise hubc.HubUnavailable("unreachable")

    monkeypatch.setattr(sync, "pull", down)
    assert run(setup)["warnings"]
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    store.discard_replica(target)
    with pytest.raises(hubc.HubUnavailable, match="hub and usable replica"):
        run(setup)
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}


def test_partial_replica_and_refusals_preserve_folder(setup, monkeypatch):
    *_, out, replica = setup
    card(replica)
    run(setup)
    replica.missing["missing"] = {"scope": "fleet", "card_id": "large"}
    replica.save()
    with pytest.raises(hubc.HubConfigError, match="incomplete"):
        run(setup)

    def refused(*_):
        raise hubc.HubRefusal(403, {"code": "memory_grant_missing"})

    monkeypatch.setattr(sync, "pull", refused)
    with pytest.raises(hubc.HubRefusal):
        run(setup)
    assert next(out.glob("alpha-*.md")).exists()


def test_tampered_marker_and_symlinks(setup):
    *_, out, _ = setup
    run(setup)
    marker = out / mirror.MARKER
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["files"]["../foreign.md"] = True
    marker.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="unsafe"):
        run(setup)


def test_off_and_unconfigured(tmp_path, capsys, monkeypatch):
    assert cli.main(["memory", "hub", "mirror", "--target", str(tmp_path), "--out", str(tmp_path / "out")]) != 0
    assert "mode" in capsys.readouterr().err
    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="read", read_scopes=("fleet",)))
    assert cli.main(["memory", "hub", "mirror", "--target", str(tmp_path), "--out", str(tmp_path / "out")]) != 0
    assert "no-hub-configured" in capsys.readouterr().out


def test_s14_pull_preserves_scan_metadata(tmp_path):
    from tests.test_memory_hub_client import FakeHub, client, workspace

    target = workspace(tmp_path / "workspace")
    fake = FakeHub()
    hub = client(fake)
    settings = hubc.HubSettings(mode="read", read_scopes=("fleet",), hub_url=hub.hub_url, token="fixture")
    fake._commit("card.put", "fleet", "alpha", "body", "live", hubc.event_id())
    original = fake.do_card

    def with_metadata(body):
        status, reply = original(body)
        if status == 200:
            reply["card"].update(scan_state="clean", writer="node-test", updated_at="2026-11-05T00:00:00Z")
        return status, reply

    fake.do_card = with_metadata
    out = tmp_path / "vault"
    report = mirror.mirror(target, hub, settings, out)
    assert len(report["written"]) == 2
    assert store.replica(target).heads["fleet\nalpha"]["scan_state"] == "clean"
    assert mirror.mirror(target, hub, settings, out)["written"] == []
    fake.mode = "down"
    assert mirror.mirror(target, hub, settings, out)["written"] == []


def test_symlink_refused(setup, tmp_path):
    *_, out, replica = setup
    card(replica)
    run(setup)
    foreign = tmp_path / "foreign.md"
    foreign.write_text("mine", encoding="utf-8")
    owned = next(out.glob("alpha-*.md"))
    owned.unlink()
    try:
        owned.symlink_to(foreign)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(hubc.HubConfigError, match="symlink"):
        run(setup)
    assert foreign.read_text(encoding="utf-8") == "mine"


def test_missing_replica_heads_never_wipes_mirror(setup, monkeypatch):
    *_, out, replica = setup
    card(replica)
    run(setup)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    (replica.directory / "heads.json").unlink()

    def down(*_):
        raise hubc.HubUnavailable("unreachable")

    monkeypatch.setattr(sync, "pull", down)
    with pytest.raises(hubc.HubConfigError, match="missing or inconsistent"):
        run(setup)
    assert before == {p.name: p.read_bytes() for p in out.iterdir()}


PREDATES = "predates scan metadata"


def legacy(replica, card_id="alpha"):
    """A head written by a pre-scan-metadata pull: no state, scan_state, writer or updated_at."""
    head = card(replica, card_id)
    for name in ("state", "scan_state", "writer", "updated_at"):
        head.pop(name)
    replica.save()
    return head


def hub_transport(replica, calls, **overrides):
    """A hub whose `card` read answers with the head's metadata, recording each call."""

    def transport(url, token, body, timeout):
        assert body["action"] == "card", body
        calls.append(body)
        head = replica.heads[store.Replica.key(body["scope"], body["card_id"])]
        reply = {
            "scope": body["scope"],
            "card_id": body["card_id"],
            "revision": body["revision"],
            "text": head["text"],
            "next_offset": None,
            "trust": "reviewed",
            "state": "live",
            "scan_state": "clean",
            "writer": "node-test",
            "updated_at": "2026-11-05T00:00:00Z",
            **overrides,
        }
        return 200, {"card": reply, "log_epoch": "epoch"}

    return transport


def with_transport(setup, transport):
    target, settings, _, out, replica = setup
    client = hubc.client_for(settings, transport=transport)
    return target, settings, client, out, replica


def unchanged(out, before):
    return before == {p.name: p.read_bytes() for p in out.iterdir()}


def mirror_with_note(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    return out, replica, {p.name: p.read_bytes() for p in out.iterdir()}


def test_legacy_replica_during_outage_never_deletes(setup, monkeypatch):
    target, settings, client, out, replica = with_transport(setup, lambda *_: pytest.fail("no card call offline"))
    out, replica, before = mirror_with_note(setup)

    def down(*_):
        raise hubc.HubUnavailable("unreachable")

    legacy(replica)
    monkeypatch.setattr(sync, "pull", down)
    with pytest.raises(hubc.HubConfigError, match=PREDATES):
        mirror.mirror(target, client, settings, out)
    assert unchanged(out, before)
    assert "scan_state" not in store.replica(target).heads["fleet\nalpha"]


def test_reachable_hub_without_scan_state_never_deletes(setup):
    out, replica, before = mirror_with_note(setup)
    legacy(replica)
    target, settings, _, _, _ = setup
    transport = hub_transport(replica, [])

    def without(url, token, body, timeout):
        status, reply = transport(url, token, body, timeout)
        del reply["card"]["scan_state"]
        return status, reply

    client = hubc.client_for(settings, transport=without)
    with pytest.raises(hubc.HubConfigError, match=PREDATES):
        mirror.mirror(target, client, settings, out)
    assert unchanged(out, before)


def test_legacy_heads_refetched_in_place_under_pull_lock(setup, monkeypatch):
    from brigade.inbox_lock import InboxLockTimeout

    calls: list[dict] = []
    out, replica, before = mirror_with_note(setup)
    legacy(replica)
    legacy(replica, "beta")
    base = hub_transport(replica, calls)
    target = setup[0]

    def transport(url, token, body, timeout):
        # The live replica keeps serving and the pull lock is held for every card read.
        assert store.replica(target).exists
        with pytest.raises(InboxLockTimeout), store.pulling(target, deadline_seconds=0):
            pass
        return base(url, token, body, timeout)

    monkeypatch.setattr(store, "discard_replica", lambda *_: pytest.fail("mirror must not discard the replica"))
    _, settings, client, _, _ = with_transport(setup, transport)
    report = mirror.mirror(target, client, settings, out)
    assert len(calls) == 2
    assert report["deleted"] == []
    assert store.replica(target).heads["fleet\nalpha"]["scan_state"] == "clean"
    assert any(p.name.startswith("beta-") for p in out.iterdir())
    assert (out / next(p.name for p in out.glob("alpha-*.md"))).exists()


def test_refetch_skips_heads_that_already_have_scan_state(setup):
    calls: list[dict] = []
    *_, replica = setup
    card(replica, "alpha")
    legacy(replica, "beta")
    target, settings, client, out, _ = with_transport(setup, hub_transport(replica, calls))
    mirror.mirror(target, client, settings, out)
    assert [call["card_id"] for call in calls] == ["beta"]


def test_note_crash_after_one_write_recovers(setup, monkeypatch):
    *_, out, replica = setup
    card(replica, "alpha")
    card(replica, "beta")
    real = mirror._atomic
    writes: list[str] = []

    def crash(path, text):
        if path.name.endswith(".md") and path.name != mirror.INDEX:
            if writes:
                raise OSError("simulated kill")
            writes.append(path.name)
        real(path, text)

    monkeypatch.setattr(mirror, "_atomic", crash)
    with pytest.raises(OSError, match="simulated"):
        run(setup)
    monkeypatch.setattr(mirror, "_atomic", real)
    # The first note landed on disk and the marker already owns it and every desired file.
    marker = json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))
    assert writes[0] in marker["files"]
    assert mirror.INDEX in marker["files"]
    report = run(setup)
    assert report["ok"]
    assert {p.name for p in out.iterdir()} == set(json.loads((out / mirror.MARKER).read_text())["files"]) | {
        mirror.MARKER,
        mirror.LOCK,
    }


def test_crash_before_final_marker_write_is_recoverable(setup, monkeypatch):
    *_, out, replica = setup
    card(replica, "alpha")
    run(setup)
    gone = next(p.name for p in out.glob("alpha-*.md"))
    card(replica, "alpha", scan_state="pending")
    real = mirror._atomic
    markers: list[int] = []

    def crash(path, text):
        if path.name == mirror.MARKER:
            markers.append(1)
            raise OSError("simulated kill")
        real(path, text)

    monkeypatch.setattr(mirror, "_atomic", crash)
    with pytest.raises(OSError, match="simulated"):
        run(setup)
    monkeypatch.setattr(mirror, "_atomic", real)
    # The marker already owned every file, so only the final write is left. The note is gone and still listed.
    assert not (out / gone).exists()
    assert gone in json.loads((out / mirror.MARKER).read_text())["files"]
    assert run(setup)["ok"]
    assert gone not in json.loads((out / mirror.MARKER).read_text())["files"]


def test_unowned_file_equal_to_desired_output_is_adopted(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    (out / mirror.MARKER).unlink()
    report = run(setup, init=True)
    assert report["written"] == []
    assert json.loads((out / mirror.MARKER).read_text())["files"]


def test_front_matter_in_card_becomes_one_block(setup):
    *_, out, replica = setup
    text = "---\ntitle: Alpha\ntags: [one]\naliases: [Al]\nrelated: Beta\n---\n\nBody [[Beta]]\n"
    card(replica, text=text)
    run(setup)
    note = next(out.glob("alpha-*.md")).read_text(encoding="utf-8")
    assert note.count("---\n") == 2
    assert note.startswith("---\n")
    front, body = note[4:].split("\n---\n", 1)
    assert body.lstrip("\n") == "Body [[Beta]]\n"
    assert sum(line.startswith("tags:") for line in front.splitlines()) == 1
    assert 'card: {"aliases": ["Al"], "related": "Beta", "title": "Alpha"}' in front


def test_bom_front_matter_keeps_title_and_strips_block(setup):
    *_, out, replica = setup
    card(replica, text="\ufeff---\ntitle: Bom Title\ntags: [x]\n---\nbody\n")
    run(setup)
    note = next(out.glob("bom-title-*.md")).read_text(encoding="utf-8")
    assert note.count("---\n") == 2
    assert "\ufeff" not in note
    assert note.endswith("\nbody\n")


def test_card_without_front_matter_keeps_body(setup):
    *_, out, replica = setup
    card(replica, text="plain --- body\n")
    run(setup)
    assert next(out.glob("alpha-*.md")).read_text(encoding="utf-8").endswith("\n---\n\nplain --- body\n")


def test_symlinked_ancestor_of_out_is_allowed(setup, tmp_path):
    *_, replica = setup
    card(replica)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    target, settings, client, _, _ = setup
    report = mirror.mirror(target, client, settings, link / "vault" / "hub")
    assert len(report["written"]) == 2
    assert (real / "vault" / "hub" / mirror.MARKER).is_file()


def test_symlinked_out_is_refused(setup, tmp_path):
    target, settings, client, _, replica = setup
    card(replica)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(hubc.HubConfigError, match="symlink"):
        mirror.mirror(target, client, settings, link)
    assert list(real.iterdir()) == []
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(hubc.HubConfigError, match="symlink"):
        mirror.mirror(target, client, settings, dangling)
    assert not (tmp_path / "nowhere").exists()


def test_relative_out_works(setup, tmp_path, monkeypatch):
    target, settings, client, _, replica = setup
    card(replica)
    monkeypatch.chdir(tmp_path)
    assert len(mirror.mirror(target, client, settings, __import__("pathlib").Path("Hub Memory"))["written"]) == 2


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_owned_file_swapped_to_symlink_is_refused_by_open(setup, tmp_path, monkeypatch):
    *_, out, replica = setup
    card(replica)
    run(setup)
    owned = next(out.glob("alpha-*.md"))
    foreign = tmp_path / "foreign.md"
    foreign.write_text("mine", encoding="utf-8")
    owned.unlink()
    owned.symlink_to(foreign)
    # A swap between the is_symlink check and the open must still fail closed.
    monkeypatch.setattr(type(owned), "is_symlink", lambda self: False)
    with pytest.raises(hubc.HubConfigError, match="symlink"):
        run(setup)
    assert foreign.read_text(encoding="utf-8") == "mine"


def test_owned_name_that_is_a_directory_is_a_config_error(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    owned = next(out.glob("alpha-*.md"))
    owned.unlink()
    owned.mkdir()
    with pytest.raises(hubc.HubConfigError, match="not a regular file"):
        run(setup)
    assert owned.is_dir()


def test_out_that_is_a_file_is_a_config_error(setup, tmp_path):
    *_, out, _ = setup
    out.write_text("file", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="not a directory"):
        run(setup)
    assert out.read_text(encoding="utf-8") == "file"


def refuse_grant(url, token, body, timeout):
    assert body["action"] == "card", body
    return 403, {"error": "no read grant", "code": "memory_grant_missing"}


def test_revoked_scope_during_metadata_refresh_drops_its_heads(setup):
    from brigade import memory_hub_search

    target, settings, _, out, replica = setup
    card(replica, "gamma", text="gamma link\n")
    legacy(replica)
    client = hubc.client_for(settings, transport=refuse_grant)
    with pytest.raises(hubc.HubConfigError):
        mirror.mirror(target, client, settings, out)
    saved = store.replica(target)
    assert saved.heads == {}
    assert "fleet" not in saved.meta["read_scopes"]
    # Offline search reads the saved replica: nothing may come back from the revoked scope.
    assert memory_hub_search._replica_matches(target, "link", client.identity, settings) == ([], False)


def test_unavailable_hub_during_metadata_refresh_leaves_the_replica(setup):
    target, settings, _, out, replica = setup
    card(replica, "gamma")
    legacy(replica)

    def down(*_):
        raise hubc.HubUnavailable("unreachable")

    client = hubc.client_for(settings, transport=down)
    with pytest.raises(hubc.HubConfigError, match=PREDATES):
        mirror.mirror(target, client, settings, out)
    saved = store.replica(target)
    assert len(saved.heads) == 2
    assert saved.meta["read_scopes"] == ["fleet"]


def _second_workspace(setup, tmp_path):
    """Another workspace with its own replica that targets the same output folder."""
    target, settings, client, out, _ = setup
    other = tmp_path / "other-workspace"
    other.mkdir()
    store.ensure_root(other)
    replica = store.Replica(store.root(other) / store.REPLICA)
    replica.meta = dict(store.replica(target).meta)
    replica.save()
    return other, replica


def test_overlapping_runs_on_one_folder_serialize(setup, tmp_path, monkeypatch):
    from brigade.inbox_lock import InboxLockTimeout

    target, settings, client, out, replica = setup
    card(replica, "alpha")
    other, other_replica = _second_workspace(setup, tmp_path)
    card(other_replica, "beta")
    monkeypatch.setattr(mirror, "LOCK_DEADLINE_SECONDS", 0.2)
    seen: list[str] = []
    real_note = mirror._note

    def note_then_overlap(head, meta):
        if not seen:
            seen.append(head["card_id"])
            # The second run starts while the first holds the folder, mid-plan.
            with pytest.raises(InboxLockTimeout):
                mirror.mirror(other, client, settings, out)
        return real_note(head, meta)

    monkeypatch.setattr(mirror, "_note", note_then_overlap)
    assert len(mirror.mirror(target, client, settings, out)["written"]) == 2
    monkeypatch.setattr(mirror, "_note", real_note)
    # Run in turn, the second run sees the first run's marker and owns its note.
    # It comes from another workspace, so it adopts the folder with --init.
    report = mirror.mirror(other, client, settings, out, init=True)
    assert [name.split("-")[0] for name in report["deleted"]] == ["alpha"]
    marker = json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))
    assert sorted(marker["files"]) == sorted([mirror.INDEX, *(p.name for p in out.glob("beta-*.md"))])
    assert not list(out.glob("alpha-*.md"))
    assert len(list(out.glob("beta-*.md"))) == 1


def test_lock_file_is_never_deleted_or_foreign(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    lock = out / mirror.LOCK
    assert lock.is_file()
    replica.heads.clear()
    replica.save()
    run(setup)
    assert lock.is_file()
    assert json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))["files"] == {mirror.INDEX: True}
    # A folder holding only the lock file counts as empty: no --init needed.
    for path in out.iterdir():
        if path != lock:
            path.unlink()
    run(setup)


def test_lock_file_alone_does_not_block_a_markerless_folder(setup):
    *_, out, replica = setup
    out.mkdir()
    (out / mirror.LOCK).write_bytes(b"")
    card(replica)
    assert len(run(setup)["written"]) == 2


def test_refused_markerless_folder_gets_no_lock_file(setup):
    *_, out, _ = setup
    out.mkdir()
    (out / "foreign.md").write_text("mine", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="--init"):
        run(setup)
    assert [p.name for p in out.iterdir()] == ["foreign.md"]


def test_busy_destination_exits_75(setup, monkeypatch, capsys):
    from brigade.inbox_lock import held_file_lock

    target, settings, client, out, replica = setup
    card(replica)
    run(setup)
    monkeypatch.setattr(mirror, "LOCK_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(hubc, "load_settings", lambda _: settings)
    monkeypatch.setattr(hubc, "client_for", lambda *_args, **_kw: client)
    with held_file_lock(out.resolve() / mirror.LOCK, deadline_seconds=1):
        code = cli.main(["memory", "hub", "mirror", "--target", str(target), "--out", str(out)])
    assert code == 75
    assert "another memory hub" in capsys.readouterr().err


def test_skipped_card_keeps_its_filename_when_it_returns(setup):
    *_, out, replica = setup
    card(replica, text="---\ntitle: Original\n---\nbody")
    run(setup)
    original = next(p.name for p in out.glob("*.md") if p.name != mirror.INDEX)
    assert original.startswith("original-")
    card(replica, scan_state="pending", text="---\ntitle: Retitled\n---\nbody")
    assert len(run(setup)["deleted"]) == 1
    assert not (out / original).exists()
    marker = json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))
    assert original in marker["notes"].values()
    assert original not in marker["files"]
    card(replica, text="---\ntitle: Retitled\n---\nbody")
    run(setup)
    assert [p.name for p in out.glob("*.md") if p.name != mirror.INDEX] == [original]


def test_card_gone_from_the_heads_forgets_its_filename(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    replica.heads.clear()
    replica.save()
    run(setup)
    assert json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))["notes"] == {}


def test_unparseable_leading_block_stays_in_the_body(setup):
    *_, out, replica = setup
    card(replica, text="---\njust a loose note\nno pairs here\n---\nbody\n")
    meta, body = mirror._split("---\njust a loose note\nno pairs here\n---\nbody\n")
    assert meta == {}
    assert body.startswith("---\njust a loose note")
    run(setup)
    note = next(out.glob("alpha-*.md")).read_text(encoding="utf-8")
    assert "just a loose note\nno pairs here\n---\nbody" in note


def test_stale_temp_files_are_removed_under_the_lock(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    stale = out / ".brigade-mirror-abcd1234.tmp"
    fresh = out / ".brigade-mirror-wxyz5678.tmp"
    unrelated = out / ".brigade-mirror-notes.txt"
    for path in (stale, fresh, unrelated):
        path.write_text("partial", encoding="utf-8")
    old = time.time() - 11 * 60
    os.utime(stale, (old, old))
    os.utime(unrelated, (old, old))
    run(setup, dry_run=True)
    assert stale.exists()
    run(setup)
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_stale_temp_cleanup_skips_a_refused_folder(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    stale = out / ".brigade-mirror-abcd1234.tmp"
    stale.write_text("partial", encoding="utf-8")
    old = time.time() - 11 * 60
    os.utime(stale, (old, old))
    (out / mirror.MARKER).write_text("{}", encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="invalid mirror marker"):
        run(setup)
    assert stale.exists()


def test_owned_name_that_is_a_directory_is_refused_before_open(setup, monkeypatch):
    *_, out, replica = setup
    card(replica)
    run(setup)
    owned = next(out.glob("alpha-*.md"))
    owned.unlink()
    owned.mkdir()
    # Windows raises PermissionError when a directory is opened. The check must not rely on the open.
    real_open = os.open

    def deny_directories(path, *args, **kwargs):
        if Path(path) == owned:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_directories)
    with pytest.raises(hubc.HubConfigError, match="not a regular file"):
        run(setup)


def test_plan_is_rebuilt_from_the_replica_under_the_lock(setup, monkeypatch):
    target, settings, client, out, replica = setup
    card(replica, "alpha")
    run(setup)
    note = next(out.glob("alpha-*.md"))
    real_lock = mirror.held_file_lock
    state = {"racing": False}

    from contextlib import contextmanager

    @contextmanager
    def lock_after_a_newer_run(*args, **kwargs):
        if not state["racing"]:
            state["racing"] = True
            # Run A has pulled and planned. Before it takes the lock the hub flags the
            # card and run B pulls that state and finishes.
            flagged = store.replica(target)
            flagged.heads[store.Replica.key("fleet", "alpha")]["trust"] = "flagged"
            flagged.save()
            assert mirror.mirror(target, client, settings, out)["deleted"] == [note.name]
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(mirror, "held_file_lock", lock_after_a_newer_run)
    # A planned from the clean card. Its stale view must not put the flagged note back.
    report = mirror.mirror(target, client, settings, out)
    assert report["written"] == []
    assert report["skipped"] == {"flagged": 1}
    assert not note.exists()
    assert not list(out.glob("alpha-*.md"))


def test_scan_state_is_rechecked_under_the_lock(setup, monkeypatch):
    target, settings, client, out, replica = setup
    card(replica, "alpha")
    run(setup)
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}
    real_lock = mirror.held_file_lock
    from contextlib import contextmanager

    @contextmanager
    def lock_after_a_legacy_replica(*args, **kwargs):
        legacy(store.replica(target))
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(mirror, "held_file_lock", lock_after_a_legacy_replica)
    with pytest.raises(hubc.HubConfigError, match=PREDATES):
        run(setup)
    assert before == {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}


def test_writes_use_the_directory_the_lock_was_taken_on(setup, tmp_path, monkeypatch):
    *_, replica = setup
    card(replica)
    real_parent = tmp_path / "real"
    evil_parent = tmp_path / "evil"
    (real_parent / "hub").mkdir(parents=True)
    (evil_parent / "hub").mkdir(parents=True)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_parent)
    except OSError:
        pytest.skip("symlinks unavailable")
    real_lock = mirror.held_file_lock
    from contextlib import contextmanager

    @contextmanager
    def swap_after_the_lock(*args, **kwargs):
        with real_lock(*args, **kwargs):
            # The spelling `out` used now points somewhere else.
            alias.unlink()
            alias.symlink_to(evil_parent)
            yield

    monkeypatch.setattr(mirror, "held_file_lock", swap_after_the_lock)
    target, settings, client, _, _ = setup
    mirror.mirror(target, client, settings, alias / "hub")
    assert (real_parent / "hub" / mirror.MARKER).is_file()
    assert len(list((real_parent / "hub").glob("alpha-*.md"))) == 1
    assert [p.name for p in (evil_parent / "hub").iterdir()] == []


def test_marker_records_hub_and_principal(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    marker = json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))
    assert marker["hub"] == "http://127.0.0.1:3774"
    assert marker["principal"] == "node-test"


def _other_identity(setup, **meta):
    """The same workspace after its replica was pulled as another principal."""
    target, *_ = setup
    other = store.replica(target)
    other.meta.update(meta)
    other.save()


def test_folder_of_another_principal_is_refused_until_init(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}
    _other_identity(setup, principal="node-other")
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        run(setup)
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        run(setup, dry_run=True)
    assert before == {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}
    run(setup, init=True)
    assert json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))["principal"] == "node-other"
    assert run(setup)["written"] == []


def test_folder_of_another_hub_is_refused_until_init(setup):
    target, settings, client, out, replica = setup
    card(replica)
    run(setup)
    marker_path = out / mirror.MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["hub"] = "http://other-hub.example:3774"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        run(setup)
    run(setup, init=True)
    assert json.loads(marker_path.read_text(encoding="utf-8"))["hub"] == client.identity


def test_marker_without_identity_is_adopted_on_the_next_run(setup):
    *_, out, replica = setup
    card(replica)
    run(setup)
    marker_path = out / mirror.MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    del marker["hub"], marker["principal"]
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    assert run(setup)["written"] == []
    adopted = json.loads(marker_path.read_text(encoding="utf-8"))
    assert (adopted["hub"], adopted["principal"]) == ("http://127.0.0.1:3774", "node-test")


def test_second_workspace_with_another_principal_does_not_take_over_the_folder(setup, tmp_path):
    target, settings, client, out, replica = setup
    card(replica, "alpha")
    run(setup)
    other, other_replica = _second_workspace(setup, tmp_path)
    other_replica.meta["principal"] = "node-other"
    other_replica.save()
    card(other_replica, "beta")
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        mirror.mirror(other, client, settings, out)
    assert len(list(out.glob("alpha-*.md"))) == 1
    assert not list(out.glob("beta-*.md"))


def test_non_403_grant_code_during_metadata_refresh_keeps_the_replica(setup):
    target, settings, _, out, replica = setup
    card(replica, "gamma")
    legacy(replica)

    def odd_status(url, token, body, timeout):
        return 409, {"error": "conflict", "code": "memory_grant_missing"}

    client = hubc.client_for(settings, transport=odd_status)
    with pytest.raises(hubc.HubConfigError):
        mirror.mirror(target, client, settings, out)
    saved = store.replica(target)
    assert len(saved.heads) == 2
    assert saved.meta["read_scopes"] == ["fleet"]


def _front(note: str) -> dict:
    """The note's own front matter, one JSON value per line."""
    block = note.split("\n---\n", 1)[0].removeprefix("---\n")
    return {line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1]) for line in block.splitlines()}


def test_multiline_description_survives_in_the_note(setup):
    *_, out, replica = setup
    block = "title: Alpha\ndescription: |\n  first line\n  second line\nmeta:\n  nested: yes\n"
    card(replica, text=f"---\n{block}---\nBody\n")
    run(setup)
    front = _front(next(out.glob("alpha-*.md")).read_text(encoding="utf-8"))
    assert front["card_frontmatter"] == block
    assert "second line" in front["card_frontmatter"] and "nested: yes" in front["card_frontmatter"]


def test_card_without_a_stripped_block_has_no_verbatim_field(setup):
    *_, out, replica = setup
    card(replica, text="plain body\n")
    run(setup)
    assert "card_frontmatter" not in _front(next(out.glob("alpha-*.md")).read_text(encoding="utf-8"))


def test_card_aliases_are_merged_into_the_top_level_list(setup):
    *_, out, replica = setup
    card(replica, text="---\ntitle: Alpha\naliases: [Al, alpha, Alpha, Al, ' ']\n---\nbody\n")
    card(replica, "beta", text="---\naliases: Bee\n---\nbody\n")
    run(setup)
    assert _front(next(out.glob("alpha-*.md")).read_text(encoding="utf-8"))["aliases"] == ["Alpha", "alpha", "Al"]
    assert _front(next(out.glob("beta-*.md")).read_text(encoding="utf-8"))["aliases"] == ["beta", "Bee"]


def _trace_writes(monkeypatch) -> list[str]:
    events: list[str] = []
    real_atomic, real_sync = mirror._atomic, mirror._sync_directory
    monkeypatch.setattr(
        mirror, "_atomic", lambda path, text: (events.append(f"write {path.name}"), real_atomic(path, text))[1]
    )
    monkeypatch.setattr(mirror, "_sync_directory", lambda directory: (events.append("sync"), real_sync(directory))[1])
    return events


def test_marker_write_is_synced_before_the_notes_change(setup, monkeypatch):
    *_, replica = setup
    card(replica)
    events = _trace_writes(monkeypatch)
    run(setup)
    # The first marker names what is about to be written, and is durable before any note lands.
    assert events[:2] == [f"write {mirror.MARKER}", "sync"]
    assert all(event.startswith("write ") and mirror.MARKER not in event for event in events[2:])


def test_final_marker_write_is_synced(setup, monkeypatch):
    *_, replica = setup
    card(replica)
    run(setup)
    replica.heads[store.Replica.key("fleet", "alpha")]["trust"] = "flagged"
    replica.save()
    events = _trace_writes(monkeypatch)
    assert run(setup)["deleted"]
    assert events[-2:] == [f"write {mirror.MARKER}", "sync"]


def test_directory_sync_fsyncs_the_folder(tmp_path, monkeypatch):
    synced: list[bool] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real_fsync(fd))[1])
    mirror._sync_directory(tmp_path)
    assert synced == [True]


def test_directory_sync_is_skipped_on_windows(tmp_path, monkeypatch):
    synced: list[int] = []
    monkeypatch.setattr(mirror.os, "name", "nt")
    monkeypatch.setattr(os, "fsync", synced.append)
    mirror._sync_directory(tmp_path)
    assert synced == []


def test_users_mirror_backup_tmp_survives_init_and_later_runs(setup):
    *_, out, replica = setup
    card(replica)
    out.mkdir()
    old = time.time() - 11 * 60
    keep = out / ".mirror-backup.tmp"
    keep.write_text("mine", encoding="utf-8")
    os.utime(keep, (old, old))
    run(setup, init=True)
    assert keep.read_text(encoding="utf-8") == "mine"
    run(setup)
    assert keep.read_text(encoding="utf-8") == "mine"


def test_stale_temps_of_both_prefixes_go_only_with_a_valid_marker(setup):
    *_, out, replica = setup
    card(replica)
    out.mkdir()
    old = time.time() - 11 * 60
    names = [".brigade-mirror-abcd1234.tmp", ".mirror-abcd1234.tmp"]
    for name in names:
        (out / name).write_text("partial", encoding="utf-8")
        os.utime(out / name, (old, old))
    # No marker yet (a first run on an existing folder): nothing is deleted.
    run(setup, init=True)
    assert all((out / name).exists() for name in names)
    run(setup)
    assert not any((out / name).exists() for name in names)


def test_first_run_killed_before_its_marker_reruns_without_init(setup):
    *_, out, replica = setup
    card(replica)
    out.mkdir()
    (out / mirror.LOCK).write_text("", encoding="utf-8")
    (out / ".brigade-mirror-abcd1234.tmp").write_text("partial", encoding="utf-8")
    assert len(run(setup)["written"]) == 2
    assert (out / mirror.MARKER).is_file()
    # An unrelated file in that folder is still foreign.
    other = out.parent / "other"
    other.mkdir()
    (other / ".brigade-mirror-notes.txt").write_text("mine", encoding="utf-8")
    target, settings, client, _, _ = setup
    with pytest.raises(hubc.HubConfigError, match="lacks ownership marker"):
        mirror.mirror(target, client, settings, other)


def test_temp_files_use_the_tools_own_prefix(setup, monkeypatch):
    *_, out, replica = setup
    card(replica)
    names: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace", lambda src, dst, *a, **k: (names.append(Path(src).name), real_replace(src, dst))[1]
    )
    run(setup)
    assert names and all(mirror._TEMP.fullmatch(name) for name in names)


def test_marker_records_the_workspace(setup):
    target, *_, out, replica = setup
    card(replica)
    run(setup)
    marker = json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))
    assert marker["workspace"] == str(target.resolve())


def test_run_from_another_workspace_is_refused_until_init(setup, tmp_path):
    target, settings, client, out, replica = setup
    card(replica, "alpha")
    run(setup)
    other, other_replica = _second_workspace(setup, tmp_path)
    card(other_replica, "beta")
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        mirror.mirror(other, client, settings, out)
    with pytest.raises(hubc.HubConfigError, match="another hub, principal or workspace"):
        mirror.mirror(other, client, settings, out, dry_run=True)
    assert before == {p.name: p.read_bytes() for p in out.iterdir() if p.name != mirror.LOCK}
    mirror.mirror(other, client, settings, out, init=True)
    assert json.loads((out / mirror.MARKER).read_text(encoding="utf-8"))["workspace"] == str(other.resolve())


def test_marker_without_a_workspace_is_adopted(setup):
    target, *_, out, replica = setup
    card(replica)
    run(setup)
    marker_path = out / mirror.MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    del marker["workspace"]
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    assert run(setup)["written"] == []
    assert json.loads(marker_path.read_text(encoding="utf-8"))["workspace"] == str(target.resolve())


def test_replica_pulled_as_another_principal_before_the_lock_is_refused(setup, monkeypatch):
    target, settings, client, out, replica = setup
    card(replica, "alpha")
    # The pull authenticated as node-test.
    monkeypatch.setattr(sync, "pull", lambda *_: {"seq": 1, "warnings": [], "principal": "node-test"})
    real_lock = mirror.held_file_lock

    from contextlib import contextmanager

    @contextmanager
    def other_principal_pulls_first(*args, **kwargs):
        pulled = store.replica(target)
        pulled.meta["principal"] = "node-other"
        card(pulled, "intruder")
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(mirror, "held_file_lock", other_principal_pulls_first)
    with pytest.raises(hubc.HubConfigError, match="another principal"):
        mirror.mirror(target, client, settings, out)
    assert not list(out.glob("*.md"))
    assert not (out / mirror.MARKER).exists()


def test_replica_pulled_as_another_principal_after_the_pull_is_refused(setup, monkeypatch):
    target, settings, client, out, replica = setup
    card(replica, "alpha")

    def pull_then_switch(*_):
        switched = store.replica(target)
        switched.meta["principal"] = "node-other"
        switched.save()
        return {"seq": 1, "warnings": [], "principal": "node-test"}

    monkeypatch.setattr(sync, "pull", pull_then_switch)
    with pytest.raises(hubc.HubConfigError, match="another principal"):
        mirror.mirror(target, client, settings, out)
    assert not out.exists() or not list(out.glob("*.md"))


def test_refresh_that_would_pass_the_replica_cap_changes_nothing(setup):
    target, settings, _, out, replica = setup
    legacy(replica)
    calls: list[dict] = []
    client = hubc.client_for(settings, transport=hub_transport(replica, calls))
    held = store.replica(target)
    # Room for the legacy head as it is, not for the metadata the refresh adds.
    tight = dataclasses.replace(settings, replica_max_bytes=held.bytes() + 10)
    before = (held.directory / "heads.json").read_bytes(), (held.directory / "meta.json").read_bytes()
    with pytest.raises(hubc.HubConfigError, match="cap"):
        sync.refresh_scan_metadata(target, client, tight)
    after = store.replica(target)
    assert (after.directory / "heads.json").read_bytes() == before[0]
    assert (after.directory / "meta.json").read_bytes() == before[1]
    assert all("scan_state" not in head for head in after.heads.values())
    # Within the cap the same refresh still works.
    assert sync.refresh_scan_metadata(target, client, settings) >= 1
