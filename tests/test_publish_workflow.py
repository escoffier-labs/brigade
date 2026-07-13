from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_publish_job_is_gated_by_matching_version_tag_before_environment():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    job = text.index("  build-and-publish:")
    guard = text.index("    if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v')", job)
    environment = text.index("    environment: pypi", job)

    assert job < guard < environment


def test_publish_version_check_is_unconditional_and_precedes_build():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    version_check = text.index("      - name: Verify tag matches every declared version")
    install = text.index("      - name: Install build tooling")
    build = text.index("      - name: Build sdist + wheel")
    publish = text.index("      - name: Publish to PyPI")

    assert "        if:" not in text[version_check:install]
    assert version_check < install < build < publish


def test_ci_checks_managed_snapshot():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "python scripts/managed_snapshot.py --check" in text
