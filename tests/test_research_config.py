from pathlib import Path
from brigade.research import config


def test_corpus_resolution(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        '[[corpus]]\nname = "cs101"\npaths = ["notes/**/*.md", "readings"]\n[caps]\nmax_rounds = 5\n'
    )
    cfg = config.load(tmp_path)
    assert cfg.corpus_paths("cs101") == ["notes/**/*.md", "readings"]
    assert cfg.caps_overrides()["max_rounds"] == 5


def test_unknown_corpus_returns_empty(tmp_path: Path):
    cfg = config.load(tmp_path)
    assert cfg.corpus_paths("nope") == []


def test_source_adapters_returns_configured_sources(tmp_path: Path):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "research.toml").write_text(
        '[[source]]\nid = "cli-one"\ntype = "cli"\ncommand = ["tool", "{query}"]\n'
    )
    cfg = config.load(tmp_path)
    assert cfg.source_adapters() == [{"id": "cli-one", "type": "cli", "command": ["tool", "{query}"]}]
