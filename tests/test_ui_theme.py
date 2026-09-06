"""Shared Deck theme: tokens, base CSS, and the HTML shell (#1495)."""

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


def test_base_css_carries_the_spacing_scale():
    css = ui_theme.BASE_CSS
    assert ".panel, .repo-panel { padding: 16px; }" in css
    assert ".panel + .panel { margin-top: 16px; }" in css
    assert "th, td { padding: 10px 12px;" in css
    assert ".hint { display: block; margin-top: 8px;" in css
    assert ".roster-actions button { min-height: 40px;" in css
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
