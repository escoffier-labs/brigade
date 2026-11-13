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
