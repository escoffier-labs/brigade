"""Two-column page shell for the Command Deck pages.

``deck_page`` wraps an already-escaped sidebar and body in the shared
``ui_theme.document`` and attaches the machine filter script. The script is a
constant: it builds the filter input with DOM calls, reads ``textContent`` and
the input value only, and never writes markup. Without JavaScript the input is
simply absent and the machine tree stays fully visible.
"""

from __future__ import annotations

import html

from . import ui_theme

FILTER_SCRIPT = (
    "(function () {"
    "function start() {"
    "var host = document.querySelector('[data-tree-filter]');"
    "if (!host) { return; }"
    "var input = document.createElement('input');"
    "input.type = 'search';"
    "input.placeholder = 'Filter machines';"
    "input.setAttribute('aria-label', 'Filter machines and cloud providers');"
    "host.appendChild(input);"
    "function apply() {"
    "var query = input.value.toLowerCase();"
    "document.querySelectorAll('[data-filter-row]').forEach(function (row) {"
    "row.hidden = query !== '' && (row.textContent || '').toLowerCase().indexOf(query) === -1;"
    "});}"
    "try { input.value = sessionStorage.getItem('deck-filter') || ''; } catch (error) { input.value = ''; }"
    "input.addEventListener('input', function () {"
    "try { sessionStorage.setItem('deck-filter', input.value); } catch (error) { /* storage blocked */ }"
    "apply();});"
    "input.addEventListener('focus', function () {"
    "try { sessionStorage.setItem('deck-filter-focus', '1'); } catch (error) { /* storage blocked */ }"
    "});"
    "input.addEventListener('blur', function () {"
    "try { sessionStorage.removeItem('deck-filter-focus'); } catch (error) { /* storage blocked */ }"
    "});"
    "apply();"
    "try {"
    "if (sessionStorage.getItem('deck-filter-focus') === '1') {"
    "input.focus();"
    "input.setSelectionRange(input.value.length, input.value.length);"
    "}"
    "} catch (error) { /* storage blocked */ }"
    "}"
    "if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); } else { start(); }"
    "})();"
)


def deck_page(
    title: str,
    active: str,
    sidebar_html: str,
    body_html: str,
    nonce: str,
    *,
    as_of: str,
    refresh_seconds: int | None = 10,
    extra_css: str = "",
    script: str = "",
) -> str:
    """The shared shell. *sidebar_html* and *body_html* must already be escaped."""
    shell = (
        f'<div class="shell" data-active="{html.escape(active, quote=True)}">'
        f"{sidebar_html}"
        f'<div class="shell-main">{body_html}</div></div>'
    )
    return ui_theme.document(
        title,
        nonce,
        shell,
        as_of=as_of,
        refresh_seconds=refresh_seconds,
        extra_css=extra_css,
        script=FILTER_SCRIPT + script,
    )
