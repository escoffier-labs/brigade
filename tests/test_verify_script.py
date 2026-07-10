from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_verify_script_runs_mypy_and_coverage_floor():
    text = (ROOT / "scripts/verify").read_text()

    assert '"$PY/mypy"' in text
    assert '"$PY/pytest" -q --cov=brigade --cov-report=term --cov-fail-under=78' in text


def test_verify_script_documents_same_fast_gate_as_ci():
    text = (ROOT / "scripts/verify").read_text()

    assert "ruff lint, ruff format, mypy, version sync, pytest with coverage" in text
