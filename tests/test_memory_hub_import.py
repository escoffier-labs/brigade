"""`brigade memory hub import` (S20): discovery, ids, dedupe, dry run, idempotence."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from brigade import memory_content_scan as scanner
from brigade import memory_hub_client as hubc
from brigade import memory_hub_import as importer
from brigade.memory_hub_mirror import MARKER
from tests.test_memory_hub_client import (
    HUB_URL,
    NODE_CREDENTIAL,
    WITHHELD,
    WITHHELD_IDS,
    FakeHub,
    client,
    run,
    workspace,
)

SCOPE = "repo:test/app"


class ImportHub(FakeHub):
    """FakeHub with the parts import depends on: scan, import grant, ruleset report, content hashes in the log."""

    def __init__(self) -> None:
        super().__init__()
        self.ruleset: str | None = scanner.ruleset_version()
        self.can_import = True
        self.import_down = False
        self.overrides: dict[str, tuple[int, dict]] = {}
        self.hashless: set[int] = set()  # log seqs whose event carries no content_hash

    def do_status(self, body):
        status, reply = super().do_status(body)
        if status == 200:
            reply["scan"] = {"available": self.ruleset is not None, "ruleset_version": self.ruleset}
        return status, reply

    def do_log(self, body):
        status, reply = super().do_log(body)
        reply["events"] = [
            event
            if event["seq"] in self.hashless
            else {
                **event,
                "content_hash": self.revisions[(event["scope"], event["card_id"], event["revision"])]["hash"],
            }
            for event in reply["events"]
        ]
        return status, reply

    def do_import(self, body):
        if self.import_down:
            raise ConnectionRefusedError("down")
        stamp = hubc.event_id_ms(body["client_event_id"]) or 0
        if stamp > time.time() * 1000 + 300_000:
            return 409, {"error": "future", "code": "memory_client_event_expired"}
        if not self.can_import:
            return 403, {"error": "no import grant", "code": "memory_grant_missing"}
        if body["card_id"] in self.overrides:
            return self.overrides[body["card_id"]]
        verdict, rules = scanner.scan_verdict([hubc.normalize_text(body["text"])])
        if verdict in {"blocked", "flagged"}:
            return 422, {"error": "refused", "code": "memory_content_blocked", "blocked": list(rules)}
        return self.do_put(body, kind="card.import")

    def imports(self) -> list[dict]:
        return [c for c in self.calls if c["action"] == "import"]

    def live(self, scope: str = SCOPE) -> dict[str, str]:
        return {c: h["hash"] for (s, c), h in self.cards.items() if s == scope and h["state"] == "live"}


@pytest.fixture
def hub(monkeypatch, tmp_path):
    fake = ImportHub()
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", HUB_URL)
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", NODE_CREDENTIAL)
    monkeypatch.delenv("BRIGADE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(hubc, "urllib_transport", fake)
    return fake


def make_vault(root: Path, notes: dict[str, str | bytes]) -> Path:
    for rel, text in notes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return root


def import_cli(tmp_path, capsys, *args: str, mode: str = "sync") -> tuple[int, list[dict], str, str]:
    target = workspace(tmp_path / "ws", mode=mode)
    code, out, err = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--json", *args], capsys
    )
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return code, rows, out, err


def notes_of(rows: list[dict]) -> dict[str, dict]:
    return {row["path"]: row for row in rows if row["type"] == "note"}


def summary_of(rows: list[dict]) -> dict:
    return rows[-1]


VAULT = {
    "Projects/alpha.md": "# Alpha\nshipping notes\n",
    "Projects/beta.md": "---\nid: beta-plan\n---\n# Beta\nplan\n",
    "Daily/2026-01-01.md": "# Day\nstandup\n",
    "readme.txt": "not a note\n",
}


# --- discovery ----------------------------------------------------------------------------


def test_discovery_skips_dot_dirs_mirror_dirs_excludes_and_symlinks(tmp_path):
    vault = make_vault(
        tmp_path / "vault",
        {
            **VAULT,
            ".obsidian/workspace.md": "# cfg\n",
            ".trash/gone.md": "# gone\n",
            "Hub Memory/card.md": "# mirrored\n",
            "Hub Memory/" + MARKER: "{}",
            "Templates/daily.md": "# tpl\n",
            "Projects/draft-wip.md": "# wip\n",
            "Projects/Upper.MD": "# upper\n",
        },
    )
    outside = make_vault(tmp_path / "outside", {"linked.md": "# linked\n"})
    (vault / "Linked").symlink_to(outside, target_is_directory=True)
    (vault / "Projects" / "link.md").symlink_to(outside / "linked.md")
    found = importer.discover([vault], root=None, excludes=["Templates/", "*-wip.md"])
    assert [rel for _, rel in found.sources] == [
        "Daily/2026-01-01.md",
        "Projects/Upper.MD",
        "Projects/alpha.md",
        "Projects/beta.md",
    ]
    assert dict(found.stats) == {"notes": 4, "dot_dirs": 2, "mirror_dirs": 1, "excluded": 2, "symlinks": 2}


def test_a_folder_that_is_itself_a_mirror_and_a_file_inside_one_are_never_imported(tmp_path):
    mirror = make_vault(tmp_path / "mirror", {"a.md": "# a\n", MARKER: "{}"})
    assert importer.discover([mirror], root=None, excludes=[]).sources == []
    assert importer.discover([mirror / "a.md"], root=None, excludes=[]).sources == []


def test_a_subfolder_or_file_below_a_mirror_folder_imports_nothing_and_says_why(tmp_path):
    mirror = make_vault(tmp_path / "mirror", {"deep/er/a.md": "# a\n", "deep/b.md": "# b\n", MARKER: "{}"})
    for argument in (mirror / "deep", mirror / "deep" / "er", mirror / "deep" / "er" / "a.md"):
        found = importer.discover([argument], root=None, excludes=[])
        assert found.sources == [] and found.stats["mirror_dirs"] == 1
        found = importer.discover([argument], root=tmp_path, excludes=[])
        assert found.sources == [] and found.stats["mirror_dirs"] == 1


def test_a_symlink_into_a_mirror_subfolder_is_caught_by_its_real_path(tmp_path):
    mirror = make_vault(tmp_path / "data" / "mirror", {"sub/a.md": "# a\n", MARKER: "{}"})
    vault = make_vault(tmp_path / "vault", {".obsidian/app.json": "{}", "keep.md": "# k\n"})
    (vault / "linked").symlink_to(mirror / "sub", target_is_directory=True)
    (vault / "linked-note.md").symlink_to(mirror / "sub" / "a.md")
    for argument in (vault / "linked", vault / "linked-note.md"):
        found = importer.discover([argument], root=vault, excludes=[])
        assert found.sources == [] and found.stats["mirror_dirs"] == 1


def test_without_a_vault_marker_or_root_the_run_warns_that_ids_depend_on_the_arguments(hub, tmp_path, capsys):
    plain = make_vault(tmp_path / "plain", {"x.md": "# X\n"})
    vault = make_vault(tmp_path / "vault", {".obsidian/app.json": "{}", "x.md": "# X\n"})
    target = workspace(tmp_path / "ws", mode="read")
    base = ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run"]
    code, out, _ = run([*base, str(plain)], capsys)
    warnings = [line for line in out.splitlines() if line.startswith("warning:")]
    assert code == 0 and len(warnings) == 1 and "--root" in warnings[0] and "card ids depend" in warnings[0]
    _, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(plain))
    assert len(summary_of(rows)["warnings"]) == 1
    for quiet in ([str(vault)], ["--root", str(plain), str(plain)]):
        code, out, _ = run([*base, *quiet], capsys)
        assert code == 0 and "warning:" not in out


def test_a_file_argument_without_a_vault_marker_is_named_by_its_own_folder_and_root_overrides(tmp_path):
    folder = make_vault(tmp_path / "plain", {"Projects/alpha.md": "# A\n", "Daily/d.md": "# D\n"})
    note = folder / "Projects" / "alpha.md"
    [(_, rel)] = importer.discover([note], root=None, excludes=[]).sources
    assert rel == "alpha.md"
    [(_, rel)] = importer.discover([note], root=folder, excludes=[]).sources
    assert rel == "Projects/alpha.md"
    with pytest.raises(hubc.HubConfigError, match="outside the import root"):
        importer.discover([folder], root=folder / "Projects", excludes=[])
    (folder / ".obsidian").mkdir()  # now the folder is a vault, so the file is named from the vault root
    [(_, rel)] = importer.discover([note], root=None, excludes=[]).sources
    assert rel == "Projects/alpha.md"


def test_the_same_vault_gives_the_same_ids_however_it_is_named(tmp_path, hub, capsys):
    vault = make_vault(tmp_path / "vault", {".obsidian/app.json": "{}", "Daily/x.md": "# X\n", "Other/y.md": "# Y\n"})

    def rel_of(*arguments: Path) -> dict[str, str]:
        return {rel: path.name for path, rel in importer.discover(list(arguments), root=None, excludes=[]).sources}

    assert rel_of(vault)["Daily/x.md"] == "x.md"
    for arguments in ([vault], [vault / "Daily"], [vault / "Daily" / "x.md"]):
        found = importer.discover(arguments, root=None, excludes=[])
        assert found.root == str(vault) and "Daily/x.md" in [rel for _, rel in found.sources]
    ids = []
    for argument in (vault, vault / "Daily", vault / "Daily" / "x.md"):
        _, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(argument))
        ids.append(notes_of(rows)["Daily/x.md"]["card_id"])
    assert len(set(ids)) == 1 and ids[0] == importer.derive_card_id("Daily/x.md")
    first = importer.discover([vault, vault / "Daily" / "x.md"], root=None, excludes=[]).sources
    second = importer.discover([vault / "Daily" / "x.md", vault], root=None, excludes=[]).sources
    assert first == second and [rel for _, rel in first].count("Daily/x.md") == 1


def test_without_a_vault_marker_the_common_ancestor_names_the_notes(tmp_path):
    make_vault(tmp_path / "A", {"x.md": "# A\n"})
    make_vault(tmp_path / "B", {"x.md": "# B\n"})
    found = importer.discover([tmp_path / "A", tmp_path / "B"], root=None, excludes=[])
    assert found.root == str(tmp_path)
    assert [rel for _, rel in found.sources] == ["A/x.md", "B/x.md"]
    found = importer.discover([tmp_path / "B" / "x.md", tmp_path / "A" / "x.md"], root=None, excludes=[])
    assert [rel for _, rel in found.sources] == ["A/x.md", "B/x.md"]
    [(_, rel)] = importer.discover([tmp_path / "A"], root=None, excludes=[]).sources
    assert rel == "x.md"


def test_arguments_from_different_vaults_need_an_explicit_root(hub, tmp_path, capsys):
    make_vault(tmp_path / "one", {".obsidian/app.json": "{}", "x.md": "# 1\n"})
    make_vault(tmp_path / "two", {".obsidian/app.json": "{}", "x.md": "# 2\n"})
    with pytest.raises(hubc.HubConfigError, match="different Obsidian vaults"):
        importer.discover([tmp_path / "one", tmp_path / "two"], root=None, excludes=[])
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run"]
        + [str(tmp_path / "one"), str(tmp_path / "two")],
        capsys,
    )
    assert code == 2 and "different Obsidian vaults" in err and out == "" and hub.calls == []
    found = importer.discover([tmp_path / "one", tmp_path / "two"], root=tmp_path, excludes=[])
    assert [rel for _, rel in found.sources] == ["one/x.md", "two/x.md"]


def test_the_same_note_named_twice_is_planned_once(tmp_path):
    vault = make_vault(tmp_path / "vault", VAULT)
    found = importer.discover([vault, vault / "Projects" / "alpha.md"], root=vault, excludes=[])
    assert len([rel for _, rel in found.sources if rel == "Projects/alpha.md"]) == 1


# --- card ids --------------------------------------------------------------------------------


def test_a_frontmatter_id_is_kept_when_the_hub_accepts_it_and_a_path_id_is_derived_otherwise():
    assert importer.note_card_id("---\nid: beta-plan\n---\n# B\n", "x/b.md") == "beta-plan"
    assert importer.note_card_id("---\ncard_id: 'gamma.1'\n---\n# G\n", "x/g.md") == "gamma.1"
    assert importer.note_card_id("﻿---\r\nid: delta\r\n---\r\n# D\r\n", "x/d.md") == "delta"
    minted = "card-12345678-1234-4234-8234-1234567890ab"
    assert importer.note_card_id(f"---\nid: {minted.upper()}\n---\n", "x/m.md") == minted
    for bad in ("Has Space", "UPPER", "-lead", "x" * 200, ""):
        derived = importer.note_card_id(f"---\nid: {bad}\n---\n# T\n", "x/t.md")
        assert derived == importer.derive_card_id("x/t.md")
    assert importer.note_card_id("# no frontmatter\n", "x/t.md") == importer.derive_card_id("x/t.md")


def test_derived_ids_are_stable_valid_and_unique_per_path():
    ids = {
        rel: importer.derive_card_id(rel)
        for rel in (
            "Projects/alpha.md",
            "Projects/Alpha.md",
            "projects/alpha.md",
            "Café ☕ Notes/日本語.md",
            "..hidden/.md",
            "a" * 400 + ".md",
            "Café/x.md",
        )
    }
    assert all(hubc.valid_card_id(card_id) and len(card_id) <= 128 for card_id in ids.values())
    assert len(set(ids.values())) == len(ids)
    assert importer.derive_card_id("Projects/alpha.md") == importer.derive_card_id("Projects/alpha.md")
    assert importer.derive_card_id("Projects/alpha.md").startswith("projects-alpha-")
    assert importer.derive_card_id("Café/x.md") == importer.derive_card_id("Café/x.md")


def test_two_notes_with_one_id_are_a_collision_and_neither_imports(hub, tmp_path, capsys):
    vault = make_vault(
        tmp_path / "vault",
        {"a/one.md": "---\nid: shared\n---\n# One\n", "b/two.md": "---\nid: shared\n---\n# Two\n", "c/ok.md": "# Ok\n"},
    )
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    notes = notes_of(rows)
    assert code == 0
    assert (notes["a/one.md"]["action"], notes["b/two.md"]["action"]) == ("collision", "collision")
    assert notes["c/ok.md"]["action"] == "import"
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    assert code == 0 and sorted(hub.live()) == [importer.derive_card_id("c/ok.md")]


# --- dedupe ----------------------------------------------------------------------------------


def seed(hub: ImportHub, card_id: str, text: str, scope: str = SCOPE) -> int:
    reply = client(hub).call(
        "put", scope=scope, card_id=card_id, base_revision=None, text=text, client_event_id=hubc.event_id()
    )
    return reply["event"]["revision"]


def test_dedupe_by_the_hubs_content_hash(hub, tmp_path, capsys):
    seed(hub, "held", "# Same\nbody\n")
    vault = make_vault(
        tmp_path / "vault",
        {
            "copy.md": "# Same\r\nbody",  # another id, same hub hash: normalization is the hub's
            "Projects/beta.md": "---\nid: beta-plan\n---\n# Beta\nplan\n",
            "z-first.md": "# Twin\n",
            "z-second.md": "# Twin\n",
        },
    )
    seed(hub, "beta-plan", "---\nid: beta-plan\n---\n# Beta\nplan\n")  # now the hub holds the note's own text
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    notes = notes_of(rows)
    assert code == 0
    assert notes["copy.md"]["action"] == "duplicate"
    assert notes["Projects/beta.md"]["action"] == "unchanged"
    assert (notes["z-first.md"]["action"], notes["z-second.md"]["action"]) == ("import", "duplicate")
    assert summary_of(rows)["appends"] == 1


def test_a_changed_note_is_kept_unless_update_names_the_hubs_revision(hub, tmp_path, capsys):
    revision = seed(hub, "beta-plan", "# Beta\nold\n")
    vault = make_vault(tmp_path / "vault", {"beta.md": "---\nid: beta-plan\n---\n# Beta\nnew\n"})
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    assert (code, notes_of(rows)["beta.md"]["action"], hub.imports()) == (0, "changed", [])
    assert notes_of(rows)["beta.md"]["appends"] is False
    code, rows, _, _ = import_cli(tmp_path, capsys, "--update", "--yes", str(vault))
    [call] = hub.imports()
    assert (call["base_revision"], call["base_epoch"]) == (revision, hub.epoch)
    assert notes_of(rows)["beta.md"]["result"]["status"] == "imported"
    assert hub.cards[(SCOPE, "beta-plan")]["text"].endswith("new\n")


def retire_beta(hub: ImportHub) -> int:
    revision = seed(hub, "beta-plan", "# Beta\nold\n")
    return client(hub).call(
        "retire",
        scope=SCOPE,
        card_id="beta-plan",
        base_revision=revision,
        base_epoch=hub.epoch,
        client_event_id=hubc.event_id(),
    )["event"]["revision"]


def test_a_retired_card_is_kept_retired_unless_update_brings_it_back_on_its_retired_revision(hub, tmp_path, capsys):
    retired = retire_beta(hub)
    vault = make_vault(tmp_path / "vault", {"beta.md": "---\nid: beta-plan\n---\n# Beta\nold\n"})
    code, rows, out, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    row = notes_of(rows)["beta.md"]
    assert (code, row["action"], row["appends"], hub.imports()) == (0, "revive", False, [])
    assert summary_of(rows)["counts"] == {"revive": 1} and summary_of(rows)["appends"] == 0
    code, rows, _, _ = import_cli(tmp_path, capsys, "--update", "--yes", str(vault))
    assert code == 0 and notes_of(rows)["beta.md"]["result"]["status"] == "imported"
    assert (hub.imports()[0]["base_revision"], hub.imports()[0]["base_epoch"]) == (retired, hub.epoch)


def test_the_dry_run_shows_revive_and_what_update_would_do(hub, tmp_path, capsys):
    retire_beta(hub)
    vault = make_vault(tmp_path / "vault", {"beta.md": "---\nid: beta-plan\n---\n# Beta\nold\n"})
    target = workspace(tmp_path / "ws", mode="read")
    base = ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run"]
    code, out, _ = run([*base, str(vault)], capsys)
    assert code == 0 and "beta-plan  revive  kept, pass --update to revive" in out
    assert f"  {'would append':<24}0" in out and f"  {'revive':<24}1" in out
    code, out, _ = run([*base, "--update", str(vault)], capsys)
    assert "beta-plan  revive  will revive" in out and f"  {'would append':<24}1" in out


# --- visibility, scan, ruleset ---------------------------------------------------------------


@pytest.mark.parametrize(("text", "reason"), WITHHELD, ids=WITHHELD_IDS)
def test_import_withholds_a_note_whose_visibility_is_not_plainly_public(hub, tmp_path, capsys, text, reason):
    vault = make_vault(tmp_path / "vault", {"p.md": text.encode("utf-8")})
    code, rows, out, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    expected = "private" if reason == "visibility-private" else "visibility-unrecognized"
    assert (code, notes_of(rows)["p.md"]["action"], notes_of(rows)["p.md"]["reason"]) == (0, expected, reason)
    assert hub.imports() == [] and notes_of(rows)["p.md"]["card_id"] is None


def test_blocked_flagged_and_private_class_notes_report_rule_ids_and_never_their_text(hub, tmp_path, capsys):
    word = "needle-" + "marker"
    vault = make_vault(
        tmp_path / "vault",
        {
            "blocked.md": f"# B\n{word} ‮ hidden\n",
            "flagged.md": f"# F\n{word} a‍b\n",
            "private-class.md": f"# P\n{word} write to someone@example.com\n",
            "clean.md": f"# C\n{word} fine\n",
        },
    )
    code, rows, out, err = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    notes = notes_of(rows)
    assert code == 0 and word not in out + err and "someone@example" not in out + err
    assert notes["blocked.md"]["action"] == "blocked" and notes["blocked.md"]["rules"]
    assert notes["flagged.md"]["action"] == "flagged" and notes["flagged.md"]["rules"]
    assert notes["private-class.md"]["action"] == "import" and notes["private-class.md"]["scan"] == "private"
    assert notes["clean.md"]["scan"] == "clean"
    summary = summary_of(rows)
    assert summary["appends"] == 2 and summary["private_class_imports"] == 1
    assert {rule for rule, _ in summary["top_rules"]} == set(notes["blocked.md"]["rules"]) | set(
        notes["flagged.md"]["rules"]
    )
    code, text, err = run(
        ["memory", "hub", "import", "--target", str(tmp_path / "ws"), "--scope", SCOPE, "--dry-run", str(vault)], capsys
    )
    assert code == 0 and word not in text + err and "blocked.md" in text and notes["blocked.md"]["rules"][0] in text


def test_a_blocked_note_never_shows_its_frontmatter_id(hub, tmp_path, capsys):
    sentinel = ".".join(["10", "1", "2", "3"])  # a valid card id that is itself a private-class finding
    shared = ".".join(["10", "9", "8", "7"])  # the same, shared by notes that collide
    vault = make_vault(
        tmp_path / "vault",
        {
            "tripped.md": f"---\nid: {sentinel}\n---\n# T\n‮\n",
            "fine-id.md": "---\nid: clean-id\n---\n# F\n‮\n",
            "dup-a.md": f"---\nid: {shared}\n---\n# A\n‮\n",  # a blocked body
            "dup-b.md": f"---\nid: {shared}\n---\n# B\n",
            "tpl-a.md": "---\nid: tpl\n---\n# A\n",
            "tpl-b.md": "---\nid: tpl\n---\n# B\n",
        },
    )
    code, rows, out, err = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    notes = notes_of(rows)
    assert code == 0 and sentinel not in out + err and shared not in out + err
    assert (notes["tripped.md"]["action"], notes["tripped.md"]["card_id"]) == ("blocked", None)
    assert (notes["fine-id.md"]["action"], notes["fine-id.md"]["card_id"]) == ("blocked", "clean-id")
    for name in ("dup-a.md", "dup-b.md"):  # collisions are decided before the scan, so the id is checked on its own
        assert (notes[name]["action"], notes[name]["card_id"]) == ("collision", None)
    for name in ("tpl-a.md", "tpl-b.md"):  # a clean shared id is still shown
        assert (notes[name]["action"], notes[name]["card_id"]) == ("collision", "tpl")
    target = workspace(tmp_path / "ws", mode="read")
    code, text, err = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run", str(vault)], capsys
    )
    assert sentinel not in text + err and shared not in text + err and "clean-id" not in text
    assert "- tripped.md  -  blocked  " in text and "- fine-id.md  -  blocked  " in text
    assert "- dup-a.md  -  collision" in text and "- dup-b.md  -  collision" in text
    assert "- tpl-a.md  tpl  collision" in text


def test_an_import_with_only_a_private_class_id_shows_it_because_the_card_will_exist_under_it(hub, tmp_path, capsys):
    private_id = ".".join(["10", "4", "5", "6"])  # the id is the note's only finding: private class, so it imports
    vault = make_vault(tmp_path / "vault", {"n.md": f"---\nid: {private_id}\n---\n# N\n"})
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    row = notes_of(rows)["n.md"]
    assert code == 0 and (row["action"], row["scan"], row["card_id"], row["appends"]) == (
        "import",
        "private",
        private_id,
        True,
    )


def test_a_different_scan_ruleset_refuses_before_anything_is_planned(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    hub.ruleset = "brigade-memory-scan.v5:" + "0" * 64
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run", str(vault)], capsys
    )
    assert code == 2 and "different scan rulesets" in err and scanner.ruleset_version() in err and out == ""
    assert {c["action"] for c in hub.calls} == {"status"}
    hub.ruleset = None
    code, _, err = run(["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, str(vault)], capsys)
    assert code == 2 and "scanner is unavailable" in err and hub.imports() == []


def test_the_local_ruleset_id_is_the_hubs_version_and_file_digest():
    rules = Path(scanner.__file__).parent / "guard" / "memory-rules.json"
    version = json.loads(rules.read_text(encoding="utf-8"))["version"]
    assert scanner.ruleset_version() == f"{version}:{hashlib.sha256(rules.read_bytes()).hexdigest()}"


# --- the run ---------------------------------------------------------------------------------


def test_the_dry_run_works_read_only_writes_nothing_and_lists_what_the_real_run_appends(hub, tmp_path, capsys):
    vault = make_vault(
        tmp_path / "vault", {**VAULT, "bad.md": "# B\n‮\n", "priv.md": "---\nvisibility: private\n---\n# P\n"}
    )
    hub.can_import = False  # a principal with read and no import grant
    before = hub.snapshot()
    code, dry, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault), mode="read")
    assert code == 0 and hub.snapshot() == before
    assert {c["action"] for c in hub.calls} <= {"status", "log"}
    assert not (tmp_path / "ws" / ".brigade" / "memory" / "hub").exists()
    planned = {row["card_id"] for row in dry if row["type"] == "note" and row["appends"]}
    assert len(planned) == 3 and summary_of(dry)["appends"] == 3
    hub.can_import = True
    code, real, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    assert code == 0
    assert {c for c in hub.live()} == planned  # exactly the dry run's list, nothing else

    def plan(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "result"} for r in rows if r["type"] == "note"]

    assert plan(real) == plan(dry)
    assert all(r["result"]["status"] == "imported" for r in real if r["type"] == "note" and r["appends"])


def test_a_second_run_appends_nothing(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    code, first, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    appended = len(hub.imports())
    assert code == 0 and appended == 3 and summary_of(first)["appends"] == 3
    before = hub.snapshot()
    code, second, _, _ = import_cli(tmp_path, capsys, str(vault))
    assert code == 0 and len(hub.imports()) == appended and hub.snapshot() == before
    assert summary_of(second)["appends"] == 0 and summary_of(second)["counts"] == {"unchanged": 3}
    assert summary_of(second)["first_import"] is False


def test_a_repeated_request_carries_one_client_event_id_and_the_hub_replays_it(hub, tmp_path, monkeypatch):
    vault = make_vault(tmp_path / "vault", VAULT)
    stale = importer.read_heads(client(hub), SCOPE)  # what a crashed run planned from
    args = {
        "scope": SCOPE,
        "paths": [vault],
        "root": None,
        "excludes": [],
        "dry_run": False,
        "update": False,
        "yes": True,
    }
    day = int(time.time() * 1000) // 86_400_000 * 86_400_000
    importer.run(client(hub), now_ms=day + 1_000, **args)
    sent = [c["client_event_id"] for c in hub.imports()]
    events = len(hub.events)
    monkeypatch.setattr(importer, "read_heads", lambda *_: stale)
    again = importer.run(client(hub), now_ms=day + 5_000, **args)
    assert [c["client_event_id"] for c in hub.imports()[len(sent) :]] == sent
    assert len(hub.events) == events and again["ok"]
    assert len(set(sent)) == len(sent) and {hubc.event_id_ms(i) for i in sent} == {day - importer.FUTURE_MS}


def test_a_first_import_into_a_scope_needs_yes(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    code, rows, out, _ = import_cli(tmp_path, capsys, str(vault))
    assert code == 1 and hub.imports() == []
    assert summary_of(rows)["needs_confirmation"] is True and summary_of(rows)["appends"] == 3
    target = workspace(tmp_path / "ws", mode="sync")
    code, text, _ = run(["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, str(vault)], capsys)
    assert code == 1 and "would append 3 card(s). Run again with --yes." in text
    assert import_cli(tmp_path, capsys, "--yes", str(vault))[0] == 0 and len(hub.imports()) == 3


def test_the_dry_run_says_the_real_run_needs_yes_and_both_headers_show_the_flags(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", {".obsidian/app.json": "{}", **VAULT})
    target = workspace(tmp_path / "ws", mode="sync")
    base = ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE]
    code, out, _ = run([*base, "--dry-run", str(vault)], capsys)
    first = out.splitlines()[0]
    assert code == 0 and first.endswith(f"(dry run, nothing written), --update off, root {vault}:")
    assert f"the real run needs --yes: this would be the first import into {SCOPE}" in out
    code, out, _ = run([*base, "--update", "--yes", str(vault)], capsys)
    assert code == 0 and out.splitlines()[0] == f"memory hub import into {SCOPE}, --update on, root {vault}:"
    code, out, _ = run([*base, "--dry-run", str(vault)], capsys)
    assert "needs --yes" not in out
    _, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", "--update", str(vault))
    assert (summary_of(rows)["root"], summary_of(rows)["update"]) == (str(vault), True)


def test_a_missing_import_grant_stops_the_run_once(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    hub.can_import = False
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    results = [r["result"]["status"] for r in rows if r["type"] == "note" and r["appends"]]
    assert code == 1 and len(hub.imports()) == 1 and results == ["refused", "not-sent", "not-sent"]
    assert "needs the import grant" in summary_of(rows)["stopped"]
    code, text, _ = run(
        ["memory", "hub", "import", "--target", str(tmp_path / "ws"), "--scope", SCOPE, "--yes", str(vault)], capsys
    )
    assert code == 1 and "stopped: the hub refused the import (403 memory_grant_missing)" in text


def test_a_read_only_credential_cannot_plan_without_the_scope_read_grant(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    hub.grants["node-1"] = {"fleet"}
    code, _, out, err = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    assert code == 2 and "needs a read grant on repo:test/app" in err and hub.imports() == []


def test_refusals_are_reported_per_note_and_the_run_continues(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", {"a.md": "# A\n", "b.md": "# B\n", "c.md": "# C\n"})
    retry = {"error": "scan unavailable", "code": "memory_scan_unavailable", "retryable": True}
    hub.overrides[importer.derive_card_id("b.md")] = (503, retry)
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    notes = notes_of(rows)
    assert code == 1
    assert [notes[n]["result"]["status"] for n in ("a.md", "b.md", "c.md")] == ["imported", "refused", "imported"]
    assert notes["b.md"]["result"] == {"status": "refused", "code": "memory_scan_unavailable", "http": 503}
    hub.overrides.clear()
    code, rows, _, _ = import_cli(tmp_path, capsys, str(vault))
    assert code == 0 and summary_of(rows)["counts"] == {"import": 1, "unchanged": 2}
    assert [c["card_id"] for c in hub.imports()[-1:]] == [importer.derive_card_id("b.md")]


def test_an_unserving_hub_stops_the_run_after_three_tries(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", {f"n{i}.md": f"# N{i}\n" for i in range(6)})
    hub.import_down = True
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    results = [r["result"]["status"] for r in rows if r["type"] == "note"]
    assert code == 1 and results == ["unavailable"] * 3 + ["not-sent"] * 3
    assert "stopped serving imports" in summary_of(rows)["stopped"]


def test_a_dead_scanner_stops_the_run_after_three_retryable_refusals(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", {f"n{i}.md": f"# N{i}\n" for i in range(6)})
    retry = {"error": "scan unavailable", "code": "memory_scan_unavailable", "retryable": True}
    for i in range(6):
        hub.overrides[importer.derive_card_id(f"n{i}.md")] = (503, retry)
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    results = [r["result"]["status"] for r in rows if r["type"] == "note"]
    assert code == 1 and results == ["refused"] * 3 + ["not-sent"] * 3 and len(hub.imports()) == 3
    assert "stopped serving imports (memory_scan_unavailable)" in summary_of(rows)["stopped"]


def test_a_retryable_refusal_never_resets_the_count_but_a_plain_refusal_does(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", {f"n{i}.md": f"# N{i}\n" for i in range(5)})
    retry = {"error": "scan unavailable", "code": "memory_scan_unavailable", "retryable": True}
    plain = {"error": "stale", "code": "memory_base_mismatch"}
    ids = [importer.derive_card_id(f"n{i}.md") for i in range(5)]
    for index in (0, 2, 4):
        hub.overrides[ids[index]] = (503, retry)
    hub.overrides[ids[1]] = (409, plain)  # a plain refusal between retryable ones resets the count
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    results = [r["result"]["status"] for r in rows if r["type"] == "note"]
    assert code == 1 and "not-sent" not in results and summary_of(rows)["stopped"] is None
    hub.overrides[ids[1]] = (503, retry)
    hub.overrides[ids[3]] = (503, retry)
    hub.overrides.pop(ids[0])
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    assert "stopped serving imports" in summary_of(rows)["stopped"]


KINDS_THAT_MAY_CHANGE_TEXT = ("card.put", "proposal.apply", "proposal.rollback")


def put_again_without_hash(hub: ImportHub, card_id: str, text: str, kind: str) -> None:
    """A second head event for the card, of `kind`, whose log entry carries no content_hash."""
    first = seed(hub, card_id, text)
    body = {
        "scope": SCOPE,
        "card_id": card_id,
        "base_revision": first,
        "base_epoch": hub.epoch,
        "text": text,
        "client_event_id": hubc.event_id(),
    }
    _, reply = hub.do_put(body, kind=kind)
    hub.hashless.add(reply["event"]["seq"])


def test_a_trust_event_without_a_hash_keeps_the_last_hash_the_card_had(hub, tmp_path, capsys):
    text = "---\nid: kept\n---\n# Kept\nbody\n"
    put_again_without_hash(hub, "kept", text, "card.trust")
    vault = make_vault(tmp_path / "vault", {"kept.md": text})
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    assert code == 0 and notes_of(rows)["kept.md"]["action"] == "unchanged"
    assert importer.read_heads(client(hub), SCOPE).head_hash["kept"] == hubc.content_hash(text)


def test_a_latest_head_event_without_a_hash_that_may_change_text_is_unknown(hub, tmp_path, capsys):
    notes = {}
    for index, kind in enumerate(KINDS_THAT_MAY_CHANGE_TEXT):
        card_id = f"card-{index}"
        text = f"---\nid: {card_id}\n---\n# Card {index}\n"
        put_again_without_hash(hub, card_id, text, kind)
        notes[f"n{index}.md"] = text
    vault = make_vault(tmp_path / "vault", notes)
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", "--update", str(vault))
    assert code == 0 and hub.imports() == []
    assert {n["action"] for n in notes_of(rows).values()} == {"unknown-head"}


def test_a_head_with_no_hash_anywhere_is_unknown_and_never_changed_or_appended(hub, tmp_path, capsys):
    seed(hub, "bare", "# Bare\nbody\n")
    hub.hashless.add(hub.seq)
    vault = make_vault(tmp_path / "vault", {"bare.md": "---\nid: bare\n---\n# Bare\nnew text\n"})
    for args in (["--dry-run"], ["--yes"], ["--update", "--yes"]):
        code, rows, out, _ = import_cli(tmp_path, capsys, *args, str(vault))
        row = notes_of(rows)["bare.md"]
        assert code == 0 and (row["action"], row["appends"], row["reason"]) == (
            "unknown-head",
            False,
            "head-without-content-hash",
        )
    assert hub.imports() == []
    assert summary_of(rows)["counts"] == {"unknown-head": 1}


def test_collisions_that_share_a_frontmatter_id_get_a_summary_hint(hub, tmp_path, capsys):
    vault = make_vault(
        tmp_path / "vault",
        {
            "Templates/t.md": "---\nid: tpl\n---\n# T\n",
            "a.md": "---\nid: tpl\n---\n# A\n",
            "b.md": "---\nid: tpl\n---\n# B\n",
            "ok.md": "# Ok\n",
        },
    )
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault))
    assert summary_of(rows)["frontmatter_id_collisions"] == 3 and summary_of(rows)["counts"]["collision"] == 3
    target = workspace(tmp_path / "ws", mode="read")
    code, text, _ = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run", str(vault)], capsys
    )
    assert "hint: 3 collision(s) share a frontmatter id" in text


def test_text_output_lists_each_note_and_a_summary(hub, tmp_path, capsys):
    vault = make_vault(
        tmp_path / "vault", {"ok.md": "# Ok\n", "bad.md": "# B\n‮\n", "e.md": "   \n", ".obsidian/x.md": "# x\n"}
    )
    target = workspace(tmp_path / "ws", mode="read")
    code, out, err = run(
        ["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, "--dry-run", str(vault)], capsys
    )
    assert code == 0 and err == ""
    assert "(dry run, nothing written)" in out
    assert "- bad.md  " in out and "  blocked  " in out and "- e.md  -  error  empty" in out
    assert f"  {'would append':<24}1" in out and "discovery: " in out and "dot_dirs 1" in out
    assert "top rules: " in out


def test_oversized_unreadable_and_missing_notes_are_errors_not_crashes(hub, tmp_path, capsys):
    vault = make_vault(
        tmp_path / "vault",
        {"big.md": "x" * (importer.MAX_TEXT_BYTES + 2048), "bin.md": b"\xff\xfe\x00bad", "ok.md": "# Ok\n"},
    )
    code, rows, _, _ = import_cli(tmp_path, capsys, "--dry-run", str(vault), str(vault / "gone.md"))
    notes = notes_of(rows)
    assert code == 0
    assert (notes["big.md"]["action"], notes["big.md"]["reason"]) == ("error", "too-large")
    assert (notes["bin.md"]["action"], notes["bin.md"]["reason"]) == ("error", "not-utf8")
    assert (notes["gone.md"]["action"], notes["gone.md"]["reason"]) == ("error", "unreadable")
    assert notes["ok.md"]["action"] == "import"
