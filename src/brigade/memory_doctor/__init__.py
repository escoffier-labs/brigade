"""Embedded memory maintenance verbs (formerly the brigade memory CLI).

Status, lint, compact, and init-git live here. Handoff promotion uses
``brigade ingest`` (not the simpler retired brigade memory ingest path).
"""

from __future__ import annotations

__all__ = ["__version__"]

# Keep in lockstep with the retired brigade memory package last shipped version
# so operators can compare behaviour when migrating.
__version__ = "0.2.0"
