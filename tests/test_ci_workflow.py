from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_workflow_does_not_skip_docs_only_content_guard():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "paths-ignore:" not in text
    assert "content-guard:" in text
    assert "python -m content_guard scan" in text


def test_agents_doc_names_ci_only_jobs_outside_local_verify():
    text = (ROOT / "AGENTS.md").read_text()

    assert "CI-only" in text
    for job in ("content-guard", "repo-metadata", "install-from-source", "quickstart-smoke"):
        assert job in text


def test_quickstart_smoke_covers_linux_macos_and_windows_powershell():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in text
    assert "shell: pwsh" in text
    assert "brigade operator quickstart --target $target" in text
    assert "brigade operator doctor --target $target" in text
