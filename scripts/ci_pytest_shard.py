#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence

import pytest


def shard_for_nodeid(nodeid: str, count: int) -> int:
    if count <= 0:
        raise ValueError("shard index requires a positive shard count")
    return int(hashlib.sha256(nodeid.encode("utf-8")).hexdigest(), 16) % count


class ShardPlugin:
    def __init__(self, *, index: int, count: int) -> None:
        if count <= 0 or index < 0 or index >= count:
            raise ValueError(f"shard index must satisfy 0 <= index < count (got index={index}, count={count})")
        self.index = index
        self.count = count
        self.collected = 0
        self.selected = 0

    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.collected = len(items)
        selected = [item for item in items if shard_for_nodeid(item.nodeid, self.count) == self.index]
        deselected = [item for item in items if shard_for_nodeid(item.nodeid, self.count) != self.index]
        self.selected = len(selected)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"CI shard {self.index + 1}/{self.count}: selected {self.selected} of {self.collected} tests"
            )

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.collected > 0 and self.selected == 0 and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
            session.exitstatus = pytest.ExitCode.OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--" in arguments:
        separator = arguments.index("--")
        launcher_args = arguments[:separator]
        pytest_args = arguments[separator + 1 :]
    else:
        launcher_args = arguments
        pytest_args = []
    parser = argparse.ArgumentParser(description="Run one deterministic pytest shard")
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parsed = parser.parse_args(launcher_args)
    try:
        plugin = ShardPlugin(index=parsed.shard_index, count=parsed.shard_count)
    except ValueError as exc:
        parser.error(str(exc))
    return int(pytest.main(pytest_args, plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(main())
