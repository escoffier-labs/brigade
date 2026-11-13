import json

from brigade import memory_care_tend as tend
from brigade.cli import main


def test_dry_run_writes_nothing(tmp_path):
    assert tend.run(tmp_path, dry_run=True)["dry_run"]
    assert list(tmp_path.iterdir()) == []


def test_manifest_exempt_missing_duplicate_and_undo(tmp_path, capsys):
    cards = tmp_path / "memory/cards"
    cards.mkdir(parents=True)
    (cards / "valid.md").write_bytes(b"valid\r\n")
    (cards / "protected.md").write_text("protected")
    (cards / "never.md").write_text("---\narchive: never\n---\nkeep")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/memory-care.toml").write_text('read_only_paths = ["memory/cards/protected.md"]\n')
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            json.dumps({"card": f"memory/cards/{name}.md", "reason": "ended"})
            for name in ["valid", "protected", "never", "missing", "valid"]
        )
    )
    assert main(["memory", "care", "archive", "--target", str(tmp_path), "--from-file", str(manifest), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["archived"] == 1
    assert len(result["refused"]) == 4
    assert main(["memory", "care", "undo", result["decision_ids"][0], "--target", str(tmp_path), "--json"]) == 0
    assert (cards / "valid.md").read_bytes() == b"valid\r\n"


def test_relative_target_archives_and_undoes_in_place(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    cards = workspace / "memory/cards"
    cards.mkdir(parents=True)
    original = b"body\n"
    (cards / "old.md").write_bytes(original)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({"card": "memory/cards/old.md", "reason": "ended"}))
    monkeypatch.chdir(tmp_path)
    assert main(["memory", "care", "archive", "--target", "workspace", "--from-file", "manifest.jsonl", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["archived"] == 1
    assert (workspace / "memory/archive/cards/old.md").read_bytes() == original
    assert not (cards / "old.md").exists()
    assert not (workspace / "workspace").exists()
    assert main(["memory", "care", "undo", result["decision_ids"][0], "--target", "workspace", "--json"]) == 0
    capsys.readouterr()
    assert (cards / "old.md").read_bytes() == original
    assert not (workspace / "memory/archive/cards/old.md").exists()
    assert not (workspace / "workspace").exists()
