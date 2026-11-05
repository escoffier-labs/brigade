"""One-way mirror contracts, with the S14 client and replica boundary."""

from __future__ import annotations

import json

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
        ({"visibility": "private"}, "visibility-private"),
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
