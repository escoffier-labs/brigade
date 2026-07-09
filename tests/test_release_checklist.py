from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_checklist_bumps_then_verifies_then_tags():
    text = (ROOT / "RELEASE.md").read_text()

    write = text.index(".venv/bin/python scripts/version_sync.py --write")
    check = text.index(".venv/bin/python scripts/version_sync.py --check")
    verify = text.index("./scripts/verify")
    cold_start = text.index("brigade runbook run docs/runbooks/cold-start-gate.json --target . --approved")
    merge = text.index("## 4. Merge the release commit")
    tag = text.index("## 5. Tag the merged commit")

    assert write < check < verify < cold_start < merge < tag


def test_release_checklist_cleans_up_isolated_pipx_state():
    text = (ROOT / "RELEASE.md").read_text()

    assert text.count("(\n  set -euo pipefail") == 2
    assert 'cleanup() { rm -rf "$smoke_root"; }' in text
    assert "trap cleanup EXIT" in text


def test_release_checklist_verifies_pypi_version():
    text = (ROOT / "RELEASE.md").read_text()

    tag_push = text.index('git push origin "v$version"')
    workflow_wait = text.index('gh run watch "$run_id" --exit-status')
    pypi_check = text.index("https://pypi.org/pypi/brigade-cli/json")

    assert tag_push < workflow_wait < pypi_check
    assert "published != expected" in text
