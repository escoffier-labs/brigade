import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_verify_script_runs_mypy_and_coverage_floor():
    text = (ROOT / "scripts/verify").read_text()

    assert '"$PY/mypy"' in text
    assert '"$PY/pytest" -q --cov=brigade --cov-report=term --cov-fail-under=78' in text


def test_verify_script_documents_same_fast_gate_as_ci():
    text = (ROOT / "scripts/verify").read_text()

    assert "ruff lint, ruff format, mypy, version sync, pytest with coverage" in text


def test_root_ruff_configuration_excludes_non_python_trees():
    text = (ROOT / "pyproject.toml").read_text()
    ruff_config = text.split("[tool.ruff]\n", maxsplit=1)[1].split("\n[tool.ruff.", maxsplit=1)[0]
    exclude_line = next(line for line in ruff_config.splitlines() if line.startswith("extend-exclude = "))
    exclusions = ast.literal_eval(exclude_line.partition("=")[2].strip())

    assert "engines" in exclusions
    assert "*.md" in exclusions
    assert "force-exclude = true" in ruff_config
