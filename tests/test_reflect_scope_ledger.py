"""Scoped reflection retains submissions from scopes it did not inspect."""

import json

from brigade import memory_hub_client as hubc
from tests.test_memory_reflect_hub import run, setup as reflect_setup, writes  # noqa: F401


def test_scoped_run_preserves_other_scope_submissions(reflect_setup, capsys):  # noqa: F811
    target, hub, _ = reflect_setup
    (target / ".brigade/memory-care.toml").write_text('[hub]\nmode="read"\nread_scopes=["fleet","repo:example"]\n')
    for ident, anchor in (("a", "c-abcdef"), ("b", "c-fedcba")):
        hub._commit(
            "card.put",
            "repo:example",
            ident,
            f"---\nevidence: []\n---\nSame claim. ^{anchor}\n",
            "live",
            hubc.event_id(),
        )
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 2
    assert run(target, capsys, "run", "--scope", "fleet", "--dry-run")[0] == 0
    assert run(target, capsys, "run", "--scope", "fleet")[0] == 0
    assert run(target, capsys, "run")[0] == 0
    assert len(writes(hub)) == 2


def test_scoped_run_prunes_changed_pin_in_inspected_scope(reflect_setup, capsys):  # noqa: F811
    target, hub, _ = reflect_setup
    assert run(target, capsys, "run")[0] == 0
    path = target / ".brigade/memory/reflect/schedule.json"
    original = next(iter(json.loads(path.read_text())["submissions"]))
    text = hub.cards[("fleet", "a")]["text"]
    hub._commit("card.put", "fleet", "a", text, "live", hubc.event_id())
    assert run(target, capsys, "run", "--scope", "fleet")[0] == 0
    assert original not in json.loads(path.read_text())["submissions"]
