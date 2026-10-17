"""Shared Deck theme: tokens, base CSS, and the HTML shell (#1495)."""

import re

from brigade import ui_theme


def test_tokens_define_every_color_once():
    for name in (
        "--canvas",
        "--surface",
        "--surface-raised",
        "--line",
        "--line-quiet",
        "--ink",
        "--muted",
        "--faint",
        "--signal",
        "--signal-quiet",
        "--ok",
        "--bad",
    ):
        assert ui_theme.TOKENS.count(f"{name}:") == 1, name
    assert "color-scheme: dark;" in ui_theme.TOKENS


def test_tokens_derive_spacing_properties_from_the_exported_scale():
    declarations = re.findall(r"--space-(\d+):\s*(\d+)px;", ui_theme.TOKENS)
    assert [(int(index), int(pixels)) for index, pixels in declarations] == list(enumerate(ui_theme.SPACING, 1))


def test_base_css_carries_the_spacing_scale():
    # Token substitution preserves the existing sizes, including optical 10px table padding.
    css = ui_theme.BASE_CSS
    assert ".panel, .repo-panel { padding: var(--space-4); }" in css
    assert ".panel + .panel { margin-top: var(--space-4); }" in css
    assert "th, td { padding: 10px var(--space-3);" in css
    assert ".hint { display: block; margin-top: var(--space-2);" in css
    assert ".roster-actions button { min-height: var(--space-8);" in css
    assert ".chip-ok" in css and ".chip-bad" in css and ".callout" in css


def test_document_emits_shell_with_nonce_and_optional_refresh():
    html = ui_theme.document("Title <x>", "n0nce", "<main>body</main>", as_of="2026-09-06 12:00:00 UTC")
    assert html.startswith("<!doctype html>")
    assert "<title>Title &lt;x&gt;</title>" in html
    assert '<style nonce="n0nce">' in html
    assert 'http-equiv="refresh"' not in html
    assert '<body class="deck" data-as-of="2026-09-06 12:00:00 UTC"><main>body</main></body>' in html
    assert '<meta name="theme-color" content="#111617">' in html
    refreshed = ui_theme.document("t", "n", "b", as_of="x", refresh_seconds=10, extra_css=".x{}", script="tick();")
    assert '<meta http-equiv="refresh" content="10">' in refreshed
    assert ".x{}" in refreshed
    assert '<script nonce="n">tick();</script>' in refreshed


def test_spacing_export_and_shared_component_alias_contract():
    assert ui_theme.SPACING == (4, 8, 12, 16, 20, 24, 32, 40)
    for component in ("page-kicker", "page-title", "page-meta", "deck-nav"):
        assert f".{component}" in ui_theme.BASE_CSS
    assert ".hint { display: block; margin-top: var(--space-2); color: var(--muted);" in ui_theme.BASE_CSS
