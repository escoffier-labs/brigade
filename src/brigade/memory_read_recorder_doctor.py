"""Doctor check for the root read-recorder sentinel.

Kept out of ``doctor.py``, which is at its module size ratchet limit, and free
of any import from ``doctor`` so it can be imported lazily from the memory
station without a cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from . import memory_card_reads

WARN = "WARN"
CheckResult = Tuple[str, str, str]  # (status, name, detail)


def checks(target: Path) -> List[CheckResult]:
    """Report the root sentinel and its recorded cause, or nothing when absent."""
    message = memory_card_reads.disabled_message(target)
    return [(WARN, "memory: read recorder", message)] if message else []
