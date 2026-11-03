"""Environment for test-spawned processes that run pytest, coverage, or their orchestrators.

The outer CI shard measures itself with pytest-cov, and ``[tool.coverage.run]
patch = ["subprocess"]`` makes every Python process it spawns start coverage
from the outer run's serialized configuration (``COVERAGE_PROCESS_CONFIG``,
``COVERAGE_PROCESS_START``). ``COVERAGE_FILE`` is a relative name in CI, so a
child started in the checkout writes its ``.coverage.<name>.<host>.pid<N>.<rand>``
sidecar into the outer shard's data directory, next to the files the outer
``coverage combine`` globs and deletes at session finish. A test that runs its own
pytest or coverage inside that environment lets the inner run create, erase, and
combine files in the outer namespace (#2195 added ``-n 4`` to the coverage shards,
and the merge queue then dropped #2219 and #2220 on an ``INTERNALERROR`` from
coverage while combining shard 3).

``isolated_coverage_env`` drops every variable that ties a child to the outer
run, so the child owns its coverage state. Pass the data file the child should use
as ``coverage_file``; omit it for a child that should not be measured.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

# Variables that tie a child to the outer measurement. ``COV_CORE_*`` is what
# pytest-cov before 7.0 exported; ``COVERAGE_PROCESS_*`` is what coverage's
# ``patch = ["subprocess"]`` and its ``.pth`` file read today.
_EXACT = frozenset({"COVERAGE_FILE", "COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG"})
_PREFIXES = ("COV_CORE_", "COVERAGE_PROCESS_")


def is_outer_coverage_variable(name: str) -> bool:
    return name in _EXACT or name.startswith(_PREFIXES)


def isolated_coverage_env(
    coverage_file: Path | str | None = None,
    *,
    base: Mapping[str, str] | None = None,
    **extra: str,
) -> dict[str, str]:
    """Return ``base`` (default ``os.environ``) without the outer run's coverage variables.

    ``COVERAGE_CORE`` is kept: it selects the tracer, not a data file, and the
    inner run should measure the way CI does. ``extra`` overrides after scrubbing.
    """
    source = os.environ if base is None else base
    env = {key: value for key, value in source.items() if not is_outer_coverage_variable(key)}
    if coverage_file is not None:
        env["COVERAGE_FILE"] = str(coverage_file)
    env.update(extra)
    return env


def scrub_coverage_environ(monkeypatch) -> None:
    """Drop the outer run's coverage variables from this process for in-process launchers.

    For tests that call code which builds a child environment from ``os.environ``
    (``scripts/pr_tier_shadow.py`` does), so the child starts without them.
    """
    for name in [name for name in os.environ if is_outer_coverage_variable(name)]:
        monkeypatch.delenv(name)
