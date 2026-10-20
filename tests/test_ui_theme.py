"""Shared Deck theme: tokens, base CSS, and the HTML shell (#1495)."""

import re

import pytest

from brigade import ui_theme


COLOR_TOKENS = (
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
    "--working",
    "--idle",
    "--offline",
    "--warn-surface",
    "--warn-line",
    "--bar-track",
    "--surface-inset",
    "--error-line",
    "--on-accent",
    "--on-signal",
    "--control-line",
)


def test_tokens_define_every_color_for_dark_and_light():
    dark, light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")
    for name in COLOR_TOKENS:
        assert dark.count(f"{name}:") == 1, name
        assert light.count(f"{name}:") == 1, name
    assert "color-scheme: light dark;" in dark
    assert "--space-1:" in dark and "--space-1:" not in light


def test_light_palette_follows_the_design():
    light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")[1]
    for declaration in (
        "--canvas: #f6f7f8;",
        "--surface: #ffffff;",
        "--line: #e3e6e8;",
        "--ink: #1d2327;",
        "--muted: #5f6b72;",
        "--faint: #636e74;",
        "--working: #3b6fd8;",
        "--idle: #3f9a5d;",
        "--warn-surface: #fbf3e6;",
        "--warn-line: #e8cfa4;",
    ):
        assert declaration in light, declaration


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    a, b = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def _palette(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*#([0-9a-f]{6});", block))


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_icons_controls_and_off_seats_clear_the_graphics_contrast_floor(scheme):
    dark, light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")
    colors = {**_palette(dark), **_palette(light)} if scheme == "light" else _palette(dark)
    assert _contrast(colors["--on-signal"], colors["--signal"]) >= 4.5
    for field in ("--surface", "--surface-raised", "--canvas"):
        assert _contrast(colors["--control-line"], colors[field]) >= 3, field
    assert _contrast(colors["--offline"], colors["--surface"]) >= 3


def test_alert_icon_controls_and_off_seats_use_their_tokens():
    css = ui_theme.BASE_CSS + ui_theme.OVERVIEW_CSS
    icon = re.search(r"\.alert-icon \{[^}]*\}", css).group(0)
    assert "color: var(--on-signal)" in icon
    assert re.search(r"\.tree-filter input \{[^}]*border: 1px solid var\(--control-line\)", css)
    assert re.search(r'input\[type="text"\][^{]*\{[^}]*border: 1px solid var\(--control-line\)', css)
    off = re.search(r"\.seg--off \{[^}]*\}", css).group(0)
    assert "inset 0 0 0 1px var(--offline)" in off


def test_dark_tokens_pin_a_page_to_dark():
    assert "color-scheme: dark;" in ui_theme.DARK_TOKENS
    assert "prefers-color-scheme" not in ui_theme.DARK_TOKENS
    for name in COLOR_TOKENS:
        assert ui_theme.DARK_TOKENS.count(f"{name}:") == 1, name
    assert "--canvas: #111617;" in ui_theme.DARK_TOKENS


def test_document_scheme_selects_the_token_sheet():
    auto = ui_theme.document("t", "n", "b", as_of="x")
    assert ui_theme.TOKENS in auto
    dark = ui_theme.document("t", "n", "b", as_of="x", scheme="dark")
    assert ui_theme.DARK_TOKENS in dark and "prefers-color-scheme" not in dark
    with pytest.raises(ValueError):
        ui_theme.document("t", "n", "b", as_of="x", scheme="sepia")


def test_base_css_takes_inset_and_error_colors_from_tokens():
    assert ".empty { margin: 0; padding: var(--space-3); color: var(--muted); background: var(--surface-inset);" in (
        ui_theme.BASE_CSS
    )
    assert ".callout--error { border-color: var(--error-line); }" in ui_theme.BASE_CSS
    assert ".banner--error { border-color: var(--error-line); }" in ui_theme.BASE_CSS


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
    assert '<meta name="theme-color" content="#111617" media="(prefers-color-scheme: dark)">' in html
    refreshed = ui_theme.document("t", "n", "b", as_of="x", refresh_seconds=10, extra_css=".x{}", script="tick();")
    assert '<meta http-equiv="refresh" content="10">' in refreshed
    assert ".x{}" in refreshed
    assert '<script nonce="n">tick();</script>' in refreshed


def test_theme_color_follows_the_scheme():
    auto = ui_theme.document("t", "n", "b", as_of="x")
    assert '<meta name="theme-color" content="#111617" media="(prefers-color-scheme: dark)">' in auto
    assert '<meta name="theme-color" content="#f6f7f8" media="(prefers-color-scheme: light)">' in auto
    dark = ui_theme.document("t", "n", "b", as_of="x", scheme="dark")
    assert dark.count('name="theme-color"') == 1
    assert '<meta name="theme-color" content="#111617">' in dark


def test_spacing_export_and_shared_component_alias_contract():
    assert ui_theme.SPACING == (4, 8, 12, 16, 20, 24, 32, 40)
    for component in ("page-kicker", "page-title", "page-meta", "deck-nav"):
        assert f".{component}" in ui_theme.BASE_CSS
    assert ".hint { display: block; margin-top: var(--space-2); color: var(--muted);" in ui_theme.BASE_CSS


def test_shell_css_lives_in_base_css_and_overview_css_stays_separate():
    assert ".shell-sidebar" in ui_theme.BASE_CSS and ".tree-row[hidden]" in ui_theme.BASE_CSS
    assert "@media (max-width: 900px)" in ui_theme.BASE_CSS
    assert ".cards" in ui_theme.OVERVIEW_CSS and ".cards" not in ui_theme.BASE_CSS
    declared = set(re.findall(r"(--[\w-]+)\s*:", ui_theme.TOKENS))
    referenced = set(re.findall(r"var\(\s*(--[\w-]+)", ui_theme.BASE_CSS + ui_theme.OVERVIEW_CSS))
    assert referenced <= declared, referenced - declared
