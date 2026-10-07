"""Pure, offline validation and HTML links for optional Center metadata.

Configured means syntactically supported, never reachable or authenticated.
See docs/fleet-center-links.md for the deliberately conservative URL contract.
"""

from __future__ import annotations

import re
from html import escape
from typing import Literal
from urllib.parse import urlsplit

MAX_CENTER_URL_LENGTH = 2048
CenterURLStatus = Literal["unset", "invalid", "configured"]
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_BROWSER_NUMBER = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)", re.IGNORECASE)
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def validate_center_url(value: object) -> str | None:
    """Return the original supported URL, or None, without fetching or repairing it."""
    if not isinstance(value, str) or not value or len(value) > MAX_CENTER_URL_LENGTH:
        return None
    # Check before urlsplit, which silently removes some controls/whitespace.
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        return None
    if any(char in value for char in "\\?#"):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not value.lower().startswith(f"{parsed.scheme}://"):
        return None
    authority = parsed.netloc
    # Percent escapes, credentials, IP brackets and multiple colons are unsupported.
    if not authority or any(char in authority for char in "%@[]") or authority.count(":") > 1:
        return None
    host, separator, port = authority.partition(":")
    if separator and (not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535):
        return None
    if len(host) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in host.split(".")):
        return None
    # WHATWG treats numeric final labels as IPv4, including shortened/hex/octal forms.
    if _BROWSER_NUMBER.fullmatch(host.rsplit(".", 1)[-1]):
        return None
    if _BAD_PERCENT_ESCAPE.search(parsed.path):
        return None
    return value


def center_url_status(value: object) -> CenterURLStatus:
    """Classify optional metadata, without echoing unsafe input into diagnostics."""
    if value is None:
        return "unset"
    return "configured" if validate_center_url(value) is not None else "invalid"


def render_center_link(value: object) -> str:
    """Render a fixed-label escaped anchor, or no markup for unset/invalid values."""
    url = validate_center_url(value)
    return f'<a href="{escape(url, quote=True)}">Open Center</a>' if url is not None else ""
