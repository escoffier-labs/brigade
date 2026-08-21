from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "{http://www.w3.org/2000/svg}"


def test_readme_header_matches_brigade_tools_brand() -> None:
    asset = ROOT / "docs" / "assets" / "brigade-wordmark.svg"
    source = asset.read_text(encoding="utf-8")
    root = ElementTree.fromstring(source)

    title = root.find(f"{SVG_NS}title")
    assert title is not None
    assert title.text == "Brigade (by Escoffier Labs)"

    panel = root.find(f"{SVG_NS}rect[@id='panel']")
    assert panel is not None
    assert panel.attrib["x"] == "0"
    assert panel.attrib["y"] == "0"
    assert panel.attrib["width"] == "920"
    assert panel.attrib["height"] == "280"
    assert panel.attrib["fill"] == "#0d1014"
    assert "stroke" not in panel.attrib
    assert "stroke-width" not in panel.attrib
    assert "rx" not in panel.attrib

    wordmark = root.find(f"{SVG_NS}g[@id='wordmark']")
    assert wordmark is not None
    assert wordmark.attrib["fill"] == "#dde3ea"
    assert wordmark.attrib["transform"] == "translate(180.5 8)"
    assert [use.attrib["x"] for use in wordmark.findall(f".//{SVG_NS}use")] == [
        "0",
        "93.139648",
        "155.279297",
        "196.418945",
        "290.558594",
        "376.698242",
        "469.837891",
    ]

    maker = root.find(f"{SVG_NS}g[@id='maker-line']")
    assert maker is not None
    assert maker.attrib["fill"] == "#9aa4b2"
    assert maker.findall(f".//{SVG_NS}use")

    accent_elements = [element for element in root.iter() if element.attrib.get("fill") == "#e0a45c"]
    assert [(element.tag, element.attrib.get("id")) for element in accent_elements] == [(f"{SVG_NS}circle", "i-dot")]
    i_dot = root.find(f"{SVG_NS}circle[@id='i-dot']")
    assert i_dot is not None
    assert i_dot.attrib["cx"] == "356.7"

    assert root.findall(f".//{SVG_NS}path")
    assert not root.findall(f".//{SVG_NS}text")
    assert "arial" not in source.lower()
    assert "#5d8dff" not in source.lower()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shield_urls = (
        "https://shieldcn.dev/github/ci/escoffier-labs/brigade.svg?workflow=ci.yml&branch=main&label=ci&size=xs",
        "https://shieldcn.dev/pypi/v/brigade-cli.svg?label=pypi&size=xs",
        "https://shieldcn.dev/pypi/dm/brigade-cli.svg?size=xs",
        "https://shieldcn.dev/badge/python-3.10+-blue.svg?logo=python&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs",
        "https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs",
    )
    assert readme.count("https://shieldcn.dev/") == len(shield_urls)
    for shield_url in shield_urls:
        assert readme.count(shield_url) == 1
