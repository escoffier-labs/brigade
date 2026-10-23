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
        "--canvas: #f7f7f7;",
        "--surface: #fcfcfc;",
        "--line: #e5e5e5;",
        "--ink: #171717;",
        "--muted: #5c5c5c;",
        "--faint: #6e6e6e;",
        "--working: #2563eb;",
        "--idle: #16a34a;",
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


def test_controls_and_off_seats_use_their_tokens():
    css = ui_theme.BASE_CSS + ui_theme.OVERVIEW_CSS
    assert re.search(r"\.tree-filter input \{[^}]*border: 1px solid var\(--control-line\)", css)
    assert re.search(r'input\[type="text"\][^{]*\{[^}]*border: 1px solid var\(--control-line\)', css)
    off = re.search(r"\.seg--off \{[^}]*\}", css).group(0)
    assert "inset 0 0 0 1px var(--offline)" in off


def test_dark_tokens_pin_a_page_to_dark():
    assert "color-scheme: dark;" in ui_theme.DARK_TOKENS
    assert "prefers-color-scheme" not in ui_theme.DARK_TOKENS
    for name in COLOR_TOKENS:
        assert ui_theme.DARK_TOKENS.count(f"{name}:") == 1, name
    assert "--canvas: #0a0a0a;" in ui_theme.DARK_TOKENS


def test_document_scheme_selects_the_token_sheet():
    auto = ui_theme.document("t", "n", "b", as_of="x")
    assert ui_theme.TOKENS in auto
    dark = ui_theme.document("t", "n", "b", as_of="x", scheme="dark")
    assert ui_theme.DARK_TOKENS in dark and "prefers-color-scheme" not in dark
    with pytest.raises(ValueError):
        ui_theme.document("t", "n", "b", as_of="x", scheme="sepia")


def test_base_css_takes_empty_and_error_colors_from_tokens():
    assert (
        ".empty { margin: 0; padding: var(--space-6) var(--space-3); color: var(--muted); text-align: center; font-size: 13px; }"
        in ui_theme.BASE_CSS
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
    assert '<meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)">' in html
    refreshed = ui_theme.document("t", "n", "b", as_of="x", refresh_seconds=10, extra_css=".x{}", script="tick();")
    assert '<meta http-equiv="refresh" content="10">' in refreshed
    assert ".x{}" in refreshed
    assert '<script nonce="n">tick();</script>' in refreshed


def test_theme_color_follows_the_scheme():
    auto = ui_theme.document("t", "n", "b", as_of="x")
    assert '<meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)">' in auto
    assert '<meta name="theme-color" content="#f7f7f7" media="(prefers-color-scheme: light)">' in auto
    dark = ui_theme.document("t", "n", "b", as_of="x", scheme="dark")
    assert dark.count('name="theme-color"') == 1
    assert '<meta name="theme-color" content="#0a0a0a">' in dark


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


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_text_tokens_clear_the_body_text_floor(scheme):
    dark, light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")
    colors = {**_palette(dark), **_palette(light)} if scheme == "light" else _palette(dark)
    for token in ("--ink", "--muted", "--faint", "--bad"):
        for surface in ("--surface", "--canvas"):
            assert _contrast(colors[token], colors[surface]) >= 4.5, (scheme, token, surface)
    if scheme == "light":
        assert _contrast(colors["--ok"], colors["--surface"]) >= 4.5


def test_no_pure_white_or_black_surfaces():
    for block in ui_theme.TOKENS.split("@media (prefers-color-scheme: light)"):
        colors = _palette(block)
        for name in ("--canvas", "--surface", "--surface-raised", "--surface-inset"):
            assert colors[name] not in {"ffffff", "000000"}, name


def test_shape_and_type_tokens_are_declared_once():
    for tokens in (ui_theme.TOKENS, ui_theme.DARK_TOKENS):
        dark = tokens.split("@media (prefers-color-scheme: light)")[0]
        for declaration in ("--radius-card: 8px;", "--radius-control: 6px;", "--font-sans:", "--font-mono:"):
            assert tokens.count(declaration) == dark.count(declaration) == 1


def test_radius_rule_uses_the_shape_tokens():
    from brigade import fleet_command_deck, fleet_dashboard

    assert "border-radius: var(--radius-card)" in ui_theme.BASE_CSS
    assert "border-radius: var(--radius-control)" in ui_theme.BASE_CSS
    css = ui_theme.BASE_CSS + ui_theme.OVERVIEW_CSS + fleet_command_deck._STYLE + fleet_dashboard._BOARD_CSS
    assert set(re.findall(r"border-radius:\s*([^;]+);", css)) <= {
        "var(--radius-card)",
        "var(--radius-control)",
        "999px",
        "2px",
        "0",
    }


def test_buttons_follow_primary_and_secondary_roles():
    from brigade import fleet_dashboard

    primary = re.search(r'[^{}]*\.shell-main button\[type="submit"\][^{]*\{([^}]+)\}', ui_theme.BASE_CSS)
    assert primary and "background: var(--ink)" in primary.group(1) and "color: var(--canvas)" in primary.group(1)
    preview = re.search(r'\.shell-main button\[value="preview"\] \{([^}]+)\}', ui_theme.BASE_CSS)
    assert preview and "background: var(--surface)" in preview.group(1)
    roster = re.search(r"\.roster-actions button \{([^}]+)\}", ui_theme.BASE_CSS).group(1)
    board = re.search(r"\.fleet-controls button \{([^}]+)\}", fleet_dashboard._BOARD_CSS).group(1)
    assert "background:" not in roster and "background:" not in board
    dark, light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")
    for block in (dark, light):
        colors = _palette(block)
        assert _contrast(colors["--ink"], colors["--canvas"]) >= 4.5


def test_form_labels_are_not_uppercase():
    from brigade import fleet_dashboard

    for css, selector in (
        (ui_theme.BASE_CSS, ".roster-form label"),
        (fleet_dashboard._BOARD_CSS, ".fleet-controls label"),
    ):
        rule = re.search(re.escape(selector) + r" \{([^}]+)\}", css).group(1)
        assert "text-transform: uppercase" not in rule


def test_sidebar_resources_hide_on_narrow_screens():
    narrow = ui_theme.BASE_CSS.split("@media (max-width: 900px)")[1].split("@media")[0]
    hide = re.search(r"([^{}]+)\{ display: none; \}", narrow).group(1)
    assert ".resources" in hide and ".sidebar-meta" in hide


def test_table_wrap_contains_absolutely_positioned_cells():
    # The machines table's visually hidden Notes header is absolutely positioned; without a
    # positioned scroll wrapper it escapes the overflow clip and widens the page on phones.
    rule = re.search(r"^\.table-wrap \{([^}]+)\}", ui_theme.BASE_CSS, re.M).group(1)
    assert "position: relative" in rule and "overflow-x: auto" in rule
    assert re.search(r"^\.seen \{[^}]*white-space: nowrap", ui_theme.OVERVIEW_CSS, re.M)


def test_machine_table_gives_seen_room_and_keeps_shared_css_lean():
    assert ".machine-table th:nth-child(5) { width: 14%; text-align: right; }" in ui_theme.OVERVIEW_CSS
    # The arrow is decorative; empty alt text keeps it out of the link's accessible name.
    assert '.card-link::after { content: "\\2192" / "";' in ui_theme.BASE_CSS
    assert re.search(r"^\.pager a \{[^}]*border-radius: var\(--radius-control\)", ui_theme.BASE_CSS, re.M)
    assert ".sidebar-foot" not in ui_theme.BASE_CSS
    assert not re.search(r"^\.card \{", ui_theme.BASE_CSS, re.M)  # the overview strip owns .card
    assert not re.search(r"^\.sub \{", ui_theme.OVERVIEW_CSS, re.M)
