"""Whether a Fleet Hub form field is required, declared once per form.

A form lists its fields with :func:`form`. The page renderer reads that list for
the visible Required or Optional marker and for the HTML ``required``
attribute, and the request validator reads it to refuse a blank required
field, so the marker, the attribute and the server check cannot drift apart.

A field is one of three kinds:

* ``required``: always needed. Carries the ``required`` attribute and is
  checked by :func:`missing`.
* ``optional``: may be blank.
* ``conditional``: needed only in one case (for example, a label that only a
  "has a label" condition reads). It shows a marker that names the case, and
  never sets the ``required`` attribute, because the control is hidden or
  ignored in every other case and the browser would block the submit. The core
  validator that understands the case keeps enforcing it.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["Field", "Form", "attr", "caption", "conditional", "form", "marker", "missing", "optional", "required"]


@dataclass(frozen=True)
class Field:
    name: str
    is_required: bool = False
    needed_when: str = ""
    error: str = ""


Form = Mapping[str, Field]


def required(name: str, error: str = "") -> Field:
    """A field that must be filled. ``error`` is the message :func:`missing` callers raise."""
    return Field(name, is_required=True, error=error)


def optional(name: str) -> Field:
    return Field(name)


def conditional(name: str, needed_when: str) -> Field:
    """A field needed only in the named case, such as ``"for label actions"``."""
    return Field(name, needed_when=needed_when)


def form(*fields: Field) -> Form:
    return {field.name: field for field in fields}


def marker(field: Field) -> str:
    """The visible text marker. Words carry the meaning, so it works without color."""
    if field.is_required:
        text, kind = "Required", "required"
    elif field.needed_when:
        text, kind = f"Required {field.needed_when}", "conditional"
    else:
        text, kind = "Optional", "optional"
    return f'<span class="field-req field-req--{kind}">{html.escape(text)}</span>'


def caption(text: str, field: Field) -> str:
    """A label's text plus its marker as one inline element, so a grid label keeps one text row."""
    return f'<span class="field-label">{html.escape(text)} {marker(field)}</span>'


def attr(field: Field) -> str:
    """`` required`` for a required field, otherwise nothing."""
    return " required" if field.is_required else ""


def missing(spec: Form, values: Mapping[str, str]) -> list[Field]:
    """Required fields that are absent or blank in ``values``, in declaration order."""
    return [field for field in spec.values() if field.is_required and not str(values.get(field.name) or "").strip()]
