"""`brigade version` - report Brigade and managed component versions."""

from __future__ import annotations

import json

import brigade
from brigade import component_report


def run(*, components: bool = False, json_output: bool = False) -> int:
    if not components:
        print(f"brigade {brigade.__version__}")
        return 0
    report = component_report.inspect_components()
    if json_output:
        print(json.dumps(component_report.render_json(report), indent=2, sort_keys=True))
    else:
        print(component_report.render_text(report), end="")
    return 0
