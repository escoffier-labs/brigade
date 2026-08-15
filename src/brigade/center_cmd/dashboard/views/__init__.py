"""Dashboard view modules and registry.

Each view module exposes:

- ``NAME: str`` - URL slug, e.g. ``"status"``
- ``TITLE: str`` - navigation label, e.g. ``"Status"``
- ``ORDER: int`` - navigation sort order
- ``fetch(target: Path) -> dict`` - loads data via ``data.run_json`` only
- ``render(payload: dict, nonce: str) -> str`` - returns an escaped HTML fragment
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Protocol

from brigade.center_cmd.dashboard import render
from brigade.center_cmd.dashboard.views import (
    agent_activity,
    code_graph,
    memory_operations,
    outcome_rank,
    run_timeline,
    status_grid,
    work,
)

_VIEW_MODULES: tuple[ModuleType, ...] = (
    status_grid,
    memory_operations,
    outcome_rank,
    run_timeline,
    work,
    agent_activity,
    code_graph,
)


class DashboardView(Protocol):
    NAME: str
    TITLE: str
    ORDER: int

    def fetch(self, target: Path) -> dict: ...

    def render(self, payload: dict, nonce: str) -> str: ...


def all_views() -> list[ModuleType]:
    """Return registered view modules sorted by ``ORDER``."""
    return sorted(_VIEW_MODULES, key=lambda module: module.ORDER)


def view_by_name(name: str) -> ModuleType | None:
    """Look up a view module by its ``NAME`` slug."""
    for module in _VIEW_MODULES:
        if module.NAME == name:
            return module
    return None


def render_nav(current: str) -> str:
    """Build the top navigation bar. *current* is the active view ``NAME``."""
    items = []
    for module in all_views():
        href = render.esc(f"/view/{module.NAME}")
        label = render.esc(module.TITLE)
        if module.NAME == current:
            items.append(f'<li><a href="{href}" aria-current="page">{label}</a></li>')
        else:
            items.append(f'<li><a href="{href}">{label}</a></li>')
    links = "".join(items)
    return f'<nav class="dashboard-nav"><ul>{links}</ul></nav>'
