"""Decorative icon contracts for Deck navigation and resources."""

import re

from brigade import fleet_deck_icons


OUTER_TAG = (
    '<svg class="icon" viewBox="0 0 24 24" width="16" height="16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true" focusable="false">'
)


def test_nav_icons_cover_every_nav_item():
    assert set(fleet_deck_icons.NAV_ICONS) == {
        "overview",
        "alerts",
        "repos",
        "work",
        "roster",
        "policy",
        "owners",
        "ci",
        "stats",
        "boards",
    }


def test_icons_are_decorative_and_carry_no_colors():
    icons = [
        *fleet_deck_icons.NAV_ICONS.values(),
        fleet_deck_icons.DOCS_ICON,
        fleet_deck_icons.BUG_ICON,
        fleet_deck_icons.STAR_ICON,
        fleet_deck_icons.EXTERNAL_ICON,
    ]
    for icon in icons:
        assert icon.startswith(OUTER_TAG) and icon.endswith("</svg>")
        assert "#" not in icon and "rgb(" not in icon and "<script" not in icon
        assert not re.search(r"\son\w+=", icon)
