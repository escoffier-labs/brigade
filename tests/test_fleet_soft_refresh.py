"""Soft refresh, page titles, and the SVG icon for the Fleet Hub Deck pages."""

import re
import xml.etree.ElementTree as ET

import pytest

from brigade import fleet_deck_shell, fleet_deck_sidebar, ui_theme
from tests.test_fleet_command_deck import CONFIG, _asset_request, _bearer, _request, _start_hub

EXPECTED_CSP_TEMPLATE = (
    "default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
    "style-src 'nonce-{nonce}'; img-src 'self'; manifest-src 'self'; connect-src {connect}; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _nonce(csp: str) -> str:
    match = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert match
    return match[1]


def test_csp_is_exact_for_soft_refresh_and_plain_pages(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        _status, headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        csp = headers["content-security-policy"]
        assert ui_theme.SOFT_REFRESH_BODY_MARKER in body
        assert csp == EXPECTED_CSP_TEMPLATE.format(nonce=_nonce(csp), connect="'self'")

        # The Repos page renders without a refresh, so it keeps connect-src 'none'.
        _status, headers, body = _request(hub, "GET", "/deck/repos", headers=_bearer())
        csp = headers["content-security-policy"]
        assert "data-soft-refresh" not in body and "<noscript>" not in body
        assert csp == EXPECTED_CSP_TEMPLATE.format(nonce=_nonce(csp), connect="'none'")

        # Non-HTML responses never widen connect-src.
        _status, headers, _body = _request(hub, "GET", "/deck/missing-page", headers=_bearer())
        assert "connect-src 'none'" in headers["content-security-policy"]


def test_soft_refresh_script_is_nonced_only_when_refresh_is_on():
    on = fleet_deck_shell.deck_page("t", "overview", "", "", "n0nce", as_of="x", refresh_seconds=10)
    meta = '<meta http-equiv="refresh" content="10">'
    assert f"<noscript>{meta}</noscript>" in on
    assert on.count("<script") == 1 and '<script nonce="n0nce">' in on
    assert fleet_deck_shell.soft_refresh_script(10) in on
    assert 'data-soft-refresh="10"' in on and on.index("<noscript>") < on.index("<title>")
    assert meta not in on.replace(f"<noscript>{meta}</noscript>", "")

    off = fleet_deck_shell.deck_page("t", "overview", "", "", "n0nce", as_of="x", refresh_seconds=None)
    assert "http-equiv" not in off and "<noscript>" not in off
    assert "data-soft-refresh" not in off and "fetch(" not in off


def test_soft_refresh_script_never_executes_or_leaves_origin():
    script = fleet_deck_shell.soft_refresh_script(15)
    assert "var SECONDS=15," in script and "__SECONDS__" not in script
    banned_calls = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function")
    for banned in banned_calls:
        assert banned not in script, banned
    assert "new DOMParser()" in script and "querySelectorAll('script')" in script and "s.remove()" in script
    assert "credentials:'same-origin'" in script and "cache:'no-store'" in script
    assert "'X-Fleet-Soft-Refresh':'1'" in script and "redirect:'manual'" in script
    assert "opaqueredirect" in script and "status===401" in script
    for skip in ("document.hidden", "typing()", "selecting()", "dirty", "visibilitychange"):
        assert skip in script, skip
    assert "text/html" in script


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("/deck", "Overview · Fleet Hub"),
        ("/deck/alerts", "Alerts · Fleet Hub"),
        ("/deck/capacity", "Machines · Fleet Hub"),
        ("/deck/repos", "Repositories · Fleet Hub"),
        ("/deck/projects", "Projects · Fleet Hub"),
        ("/deck/pr-rules", "PR rules · Fleet Hub"),
        ("/deck/skills", "Skills · Fleet Hub"),
    ],
)
def test_page_title_is_the_tab_name(tmp_path, path, title):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        status, _headers, body = _request(hub, "GET", path, headers=_bearer())
    assert status == 200, path
    assert f"<title>{title}</title>" in body
    assert "Command Deck</title>" not in body


def test_page_title_helper_and_fallback():
    assert fleet_deck_sidebar.page_title("stats") == "Reporting · Fleet Hub"
    assert fleet_deck_sidebar.page_title("no-such-page") == "Fleet Hub"


def test_svg_icon_asset_is_served_and_inert(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _db):
        status, headers, body = _asset_request(hub, "/favicon.svg")
        _s, _h, page = _request(hub, "GET", "/deck", headers=_bearer())
    assert status == 200
    assert headers["content-type"].startswith("image/svg+xml")
    assert headers["x-content-type-options"] == "nosniff"
    text = body.decode("utf-8")
    root = ET.fromstring(text)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    lowered = text.lower()
    assert lowered.count("http://") == 1  # only the xmlns declaration
    banned_markup = ("<script", "javascript:", "https://", "xlink:href", "<image", "<foreignobject", "<use")
    for banned in (*banned_markup, "@import", "url("):
        assert banned not in lowered, banned
    for element in root.iter():
        for attribute in element.attrib:
            assert not attribute.lower().startswith("on"), attribute
    icon_links = re.findall(r'<link rel="icon"[^>]*>', page)
    assert icon_links[0] == '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
    assert 'href="/favicon.ico"' in page and 'href="/favicon-32x32.png"' in page
