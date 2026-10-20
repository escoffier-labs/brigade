"""Platform boundary for the Dot bridge's Linux local state contract."""

import sys

import pytest

requires_local_state = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Dot local runtime/state requires Linux flock and /proc/self/fd (docs/grokbot-dot.md, #1765)",
)
