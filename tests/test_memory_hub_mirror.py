"""One-way mirror contracts, with the S14 client and replica boundary."""

from __future__ import annotations

import json
import os

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
        mirror.MARKER
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
            if len(markers) == 2:
                raise OSError("simulated kill")
        real(path, text)

    monkeypatch.setattr(mirror, "_atomic", crash)
    with pytest.raises(OSError, match="simulated"):
        run(setup)
    monkeypatch.setattr(mirror, "_atomic", real)
    # The note is gone and the marker from before the deletion still lists it.
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
    assert front.count("tags:") == 1
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
