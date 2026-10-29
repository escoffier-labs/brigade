"""Keep the roster bulk-save controls in one form across detail panes."""

import re


def owned_controls(content: str) -> str:
    """Attach trusted renderer controls to the unchanged bulk POST form.

    Hidden detail panes still submit their controls. Policy preview forms keep
    their own form ownership and never pass through this helper.
    """
    return re.sub(r"(<(?:input|select|textarea|button)\b[^>]*)(>)", r'\1 form="roster-save"\2', content)
