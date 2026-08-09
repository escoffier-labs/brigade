"""Local memory-retrieval eval harness (#722).

Dev tooling that compares keyword search, a naive grep baseline, and an
optional on-device semantic adapter over a checked-in fixture corpus.
Not a public CLI command; run with ``python -m brigade.memory_retrieval_eval``.
"""

from __future__ import annotations

from .harness import DEFAULT_FIXTURE_ROOT, DEFAULT_K, run_eval

__all__ = ["DEFAULT_FIXTURE_ROOT", "DEFAULT_K", "run_eval"]
