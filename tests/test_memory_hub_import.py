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

    def do_status(self, body):
        status, reply = super().do_status(body)
        if status == 200:
            reply["scan"] = {"available": self.ruleset is not None, "ruleset_version": self.ruleset}
        return status, reply

    def do_log(self, body):
        status, reply = super().do_log(body)
        reply["events"] = [
            {**event, "content_hash": self.revisions[(event["scope"], event["card_id"], event["revision"])]["hash"]}
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
            ".graphtrail/index.md": "# gt\n",
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


def test_a_file_argument_is_named_by_its_own_folder_unless_root_says_otherwise(tmp_path):
    vault = make_vault(tmp_path / "vault", VAULT)
    [(_, rel)] = importer.discover([vault / "Projects" / "alpha.md"], root=None, excludes=[]).sources
    assert rel == "alpha.md"
    [(_, rel)] = importer.discover([vault / "Projects" / "alpha.md"], root=vault, excludes=[]).sources
    assert rel == "Projects/alpha.md"
    with pytest.raises(hubc.HubConfigError, match="outside the import root"):
        importer.discover([vault], root=vault / "Projects", excludes=[])


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


def test_a_retired_card_comes_back_on_its_retired_revision(hub, tmp_path, capsys):
    revision = seed(hub, "beta-plan", "# Beta\nold\n")
    retired = client(hub).call(
        "retire",
        scope=SCOPE,
        card_id="beta-plan",
        base_revision=revision,
        base_epoch=hub.epoch,
        client_event_id=hubc.event_id(),
    )["event"]["revision"]
    vault = make_vault(tmp_path / "vault", {"beta.md": "---\nid: beta-plan\n---\n# Beta\nold\n"})
    code, rows, _, _ = import_cli(tmp_path, capsys, "--yes", str(vault))
    assert code == 0 and notes_of(rows)["beta.md"]["result"]["status"] == "imported"
    assert hub.imports()[0]["base_revision"] == retired


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
    assert len(set(sent)) == len(sent) and {hubc.event_id_ms(i) for i in sent} == {day}


def test_a_first_import_into_a_scope_needs_yes(hub, tmp_path, capsys):
    vault = make_vault(tmp_path / "vault", VAULT)
    code, rows, out, _ = import_cli(tmp_path, capsys, str(vault))
    assert code == 1 and hub.imports() == []
    assert summary_of(rows)["needs_confirmation"] is True and summary_of(rows)["appends"] == 3
    target = workspace(tmp_path / "ws", mode="sync")
    code, text, _ = run(["memory", "hub", "import", "--target", str(target), "--scope", SCOPE, str(vault)], capsys)
    assert code == 1 and "would append 3 card(s). Run again with --yes." in text
    assert import_cli(tmp_path, capsys, "--yes", str(vault))[0] == 0 and len(hub.imports()) == 3


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
