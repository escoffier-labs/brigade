# tests/test_research_local_sources.py
from pathlib import Path
from brigade.research.sources import local


def _corpus(tmp_path: Path):
    (tmp_path / "a.md").write_text("Photosynthesis converts light into chemical energy in plants.")
    (tmp_path / "b.md").write_text("The mitochondria is the powerhouse of the cell and makes ATP.")
    (tmp_path / "c.txt").write_text("Stock markets fluctuate with interest rates and inflation.")
    return tmp_path


def test_resolve_paths_and_rank(tmp_path: Path):
    root = _corpus(tmp_path)
    idx = local.build_index([str(root / "*.md"), str(root / "*.txt")])
    hits = idx.search("how do plants make energy from light", limit=2)
    assert hits, "expected at least one hit"
    assert hits[0]["source"].endswith("a.md")
    assert "trust" not in hits[0] or hits[0].get("trust") == "local"


def test_chunking_long_file(tmp_path: Path):
    big = tmp_path / "big.md"
    big.write_text(("para one about cells.\n\n" * 50) + ("para two about energy.\n\n" * 50))
    idx = local.build_index([str(big)], chunk_chars=500)
    assert idx.num_chunks > 1
