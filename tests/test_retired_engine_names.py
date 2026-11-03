"""Retired-name counts may shrink during the rename, but may not grow."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "retired_engine_names.py"
RATCHET = runpy.run_path(str(SCRIPT))


def test_retired_engine_names_do_not_grow() -> None:
    baseline = json.loads((ROOT / RATCHET["BASELINE"]).read_text())
    assert not (failures := RATCHET["violations"](ROOT, baseline)), "\n".join(failures)


@pytest.mark.parametrize("new_file", [False, True])
def test_new_retired_name_line_fails_ratchet(tmp_path: Path, new_file: bool) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "example.txt"
    source.write_text("GraphTrail\nMiseLedger\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    baseline = RATCHET["counts"](tmp_path)
    baseline_path = tmp_path / RATCHET["BASELINE"]
    baseline_path.parent.mkdir(parents=True)
    baseline_path.write_text(json.dumps(baseline))
    # Decreases must pass with the original baseline.
    source.write_text("GraphTrail\n")
    assert RATCHET["violations"](tmp_path, baseline) == []
    if new_file:
        source = tmp_path / "added.txt"
        source.write_text("MISELEDGER\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    else:
        source.write_text("GraphTrail\nMiseLedger\nGRAPH_TRAIL\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert f"{source.name}:" in result.stdout
    assert "exceeds baseline" in result.stdout


def test_paths_and_spelling_variants_are_counted(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "Graph-Trail.txt").write_text("MISE_LEDGER mise-ledger Mise Ledger graphtrail-mcp")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert RATCHET["counts"](tmp_path) == {"Graph-Trail.txt": 5}
