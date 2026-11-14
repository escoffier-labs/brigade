from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "{http://www.w3.org/2000/svg}"


def test_readme_header_matches_brigade_tools_brand() -> None:
    # The wordmark has no panel, so it sits on the page background. GitHub picks the
    # light-ink file for dark themes through the <picture> source; PyPI uses the <img>.
    inks = {"brigade-wordmark.svg": "#0f1216", "brigade-wordmark-dark.svg": "#e9ecef"}
    for name, ink in inks.items():
        root = ElementTree.fromstring((ROOT / "docs" / "assets" / name).read_text(encoding="utf-8"))
        assert root.attrib["role"] == "img"
        assert root.attrib["viewBox"] == "0 0 920 200"
        assert root.attrib["width"] == "920"
        assert root.attrib["height"] == "200"
        assert root.attrib["aria-labelledby"].split() == ["title", "desc"]
        title = root.find(f"{SVG_NS}title[@id='title']")
        description = root.find(f"{SVG_NS}desc[@id='desc']")
        assert title is not None
        assert title.text == "BrigadeStack by Escoffier Labs"
        assert description is not None and description.text

        # No direct child rect: a full-size panel would stop the mark blending in.
        assert root.find(f"{SVG_NS}rect") is None
        stack_mark, lettering = root.findall(f"{SVG_NS}g")
        assert stack_mark.attrib["fill"] == ink
        assert len(stack_mark.findall(f"{SVG_NS}rect")) == 3
        labels = lettering.findall(f"{SVG_NS}text")
        assert [label.text for label in labels] == ["BrigadeStack", "by Escoffier Labs"]
        assert labels[0].attrib["fill"] == ink
        for label in labels:
            assert 0 < float(label.attrib["x"]) < 920
            assert 0 < float(label.attrib["y"]) < 200

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # Images use absolute raw URLs on main because relative paths break on the PyPI project page.
    raw_assets = "https://raw.githubusercontent.com/escoffier-labs/brigadestack/main/docs/assets/"
    assert f'<img src="{raw_assets}brigade-wordmark.svg" alt="BrigadeStack by Escoffier Labs"' in readme
    assert f'<source media="(prefers-color-scheme: dark)" srcset="{raw_assets}brigade-wordmark-dark.svg">' in readme
    assert "Run agents anywhere. Same memory everywhere." in readme
    # The CI badge is GitHub's own, filtered to merge queue runs. The push run on main
    # is cancelled whenever another pull request merges, so a badge on it never settles.
    ci_badge = "https://github.com/escoffier-labs/brigadestack/actions/workflows/ci.yml/badge.svg?event=merge_group"
    assert readme.count(ci_badge) == 1
    # PyPI points at `brigadestack`, the package the install line uses.
    shield_urls = (
        "https://shieldcn.dev/pypi/v/brigadestack.svg?label=pypi&size=xs",
        "https://shieldcn.dev/https/raw.githubusercontent.com/escoffier-labs/brigadestack/download-metrics/downloads.json.svg?size=xs",
        "https://shieldcn.dev/badge/python-3.11+-blue.svg?logo=python&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs",
    )
    assert readme.count("https://shieldcn.dev/") == len(shield_urls)
    for shield_url in shield_urls:
        assert readme.count(shield_url) == 1
