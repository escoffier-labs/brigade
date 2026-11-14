"""Platform boundary for the Dot bridge's Linux local state contract."""

import importlib.util
import sys

import pytest

requires_local_state = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Dot local runtime/state requires Linux flock and /proc/self/fd (docs/grokbot-dot.md, #1765)",
)

requires_connector_extra = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None or importlib.util.find_spec("anyio") is None,
    reason="requires the connector extra (mcp and anyio)",
)
