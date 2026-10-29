from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "{http://www.w3.org/2000/svg}"


def test_readme_header_matches_brigade_tools_brand() -> None:
    asset = ROOT / "docs" / "assets" / "brigade-wordmark.svg"
    root = ElementTree.fromstring(asset.read_text(encoding="utf-8"))
    assert root.attrib["role"] == "img"
    assert root.attrib["viewBox"] == "0 0 920 280"
    assert root.attrib["width"] == "920"
    assert root.attrib["height"] == "280"

    labelled_by = root.attrib["aria-labelledby"].split()
    assert labelled_by == ["title", "desc"]
    title = root.find(f"{SVG_NS}title[@id='title']")
    description = root.find(f"{SVG_NS}desc[@id='desc']")
    assert title is not None
    assert title.text == "Brigade Stack by Escoffier Labs"
    assert description is not None and description.text

    panel = root.find(f"{SVG_NS}rect")
    assert panel is not None
    assert float(panel.attrib.get("x", "0")) == 0
    assert float(panel.attrib.get("y", "0")) == 0
    assert panel.attrib["width"] == "920"
    assert panel.attrib["height"] == "280"
    assert panel.attrib["fill"] == "#111111"
    assert "stroke" not in panel.attrib
    assert "stroke-width" not in panel.attrib
    assert "rx" not in panel.attrib

    lettering = root.find(f"{SVG_NS}g")
    assert lettering is not None
    assert lettering.attrib["fill"] == "#ffffff"
    assert lettering.attrib["text-anchor"] == "middle"
    labels = lettering.findall(f"{SVG_NS}text")
    assert [label.text for label in labels] == ["Brigade Stack", "by Escoffier Labs"]
    for label in labels:
        assert 0 < float(label.attrib["x"]) < 920
        assert 0 < float(label.attrib["y"]) < 280

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shield_urls = (
        "https://shieldcn.dev/github/ci/escoffier-labs/brigade.svg?workflow=ci.yml&branch=main&label=ci&size=xs",
        "https://shieldcn.dev/pypi/v/brigade-cli.svg?label=pypi&size=xs",
        "https://shieldcn.dev/https/raw.githubusercontent.com/escoffier-labs/brigade/download-metrics/downloads.json.svg?size=xs",
        "https://shieldcn.dev/badge/python-3.10+-blue.svg?logo=python&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs",
    )
    assert readme.count("https://shieldcn.dev/") == len(shield_urls)
    for shield_url in shield_urls:
        assert readme.count(shield_url) == 1
