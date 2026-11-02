#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from collections.abc import Mapping, Sequence

import pytest

# main() loads this file as a `-p` plugin so pytest-xdist workers, which
# rebuild their config from the invocation arguments, select the same shard.
PLUGIN_MODULE = Path(__file__).stem
PLUGIN_NAME = "brigade-ci-shard"
# Per-file test seconds from CI junit reports; refresh with scripts/ci_test_durations.py.
DURATIONS_PATH = Path(__file__).with_name("ci_test_durations.json")


def shard_for_nodeid(nodeid: str, count: int) -> int:
    if count <= 0:
        raise ValueError("shard index requires a positive shard count")
    return int(hashlib.sha256(nodeid.encode("utf-8")).hexdigest(), 16) % count


def _test_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def load_file_durations(path: Path | None) -> dict[str, float]:
    """Read per-file seconds, treating a missing or malformed file as no data."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        str(name): float(seconds)
        for name, seconds in files.items()
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0
    }


def assign_shards(nodeids: Sequence[str], count: int, durations: Mapping[str, float]) -> list[int]:
    """Balance timed files across shards, longest first; hash tests from untimed files."""
    if count <= 0:
        raise ValueError("shard index requires a positive shard count")
    timed = sorted({_test_file(nodeid) for nodeid in nodeids} & durations.keys(), key=lambda f: (-durations[f], f))
    loads = [0.0] * count
    owners: dict[str, int] = {}
    for name in timed:
        target = min(range(count), key=lambda index: (loads[index], index))
        owners[name] = target
        loads[target] += durations[name]
    # Untimed tests spread evenly by hash, so new files load every shard alike.
    return [owners.get(_test_file(nodeid), shard_for_nodeid(nodeid, count)) for nodeid in nodeids]


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


class ShardPlugin:
    def __init__(
        self,
        *,
        index: int,
        count: int,
        result_path: Path | None = None,
        durations: Mapping[str, float] | None = None,
    ) -> None:
        if count <= 0 or index < 0 or index >= count:
            raise ValueError(f"shard index must satisfy 0 <= index < count (got index={index}, count={count})")
        self.index = index
        self.count = count
        self.collected = 0
        self.selected = 0
        self.result_path = result_path
        self.durations = dict(durations or {})
        self.distributed = False

    def _write_marker(self, config: pytest.Config, *, newline: bool = False) -> None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            if newline:
                # xdist leaves the cursor after the progress percentage.
                reporter.write("\n")
            reporter.write_line(
                f"CI shard {self.index + 1}/{self.count}: selected {self.selected} of {self.collected} tests"
            )

    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.collected = len(items)
        owners = assign_shards([item.nodeid for item in items], self.count, self.durations)
        selected = [item for item, owner in zip(items, owners, strict=True) if owner == self.index]
        deselected = [item for item, owner in zip(items, owners, strict=True) if owner != self.index]
        self.selected = len(selected)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        if _is_xdist_worker(config):
            # Workers have no visible terminal; the controller reports for them.
            config.workeroutput["ci_shard"] = {"collected": self.collected, "selected": self.selected}  # type: ignore[attr-defined]
        else:
            self._write_marker(config)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error) -> None:
        # Every worker collects and selects the same deterministic shard.
        counts = getattr(node, "workeroutput", {}).get("ci_shard")
        if counts and not self.distributed:
            self.distributed = True
            self.collected = counts["collected"]
            self.selected = counts["selected"]

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if _is_xdist_worker(session.config):
            return
        if self.distributed:
            # Emitted before the terminal summary, which runs after this hook.
            self._write_marker(session.config, newline=True)
        if self.collected > 0 and self.selected == 0 and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
            session.exitstatus = pytest.ExitCode.OK
        if self.result_path is not None:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                counts = {key: len(reports) for key, reports in reporter.stats.items()}
                self.result_path.write_text(
                    json.dumps(
                        {
                            "collected": self.collected,
                            "selected": self.selected,
                            "counts": counts,
                        }
                    ),
                    encoding="utf-8",
                )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup(PLUGIN_NAME)
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=None)
    group.addoption("--ci-shard-result-json", default=None)
    group.addoption(
        "--ci-shard-durations",
        default=str(DURATIONS_PATH),
        help="Per-file durations JSON for balancing; an empty value uses hash sharding only",
    )


def pytest_configure(config: pytest.Config) -> None:
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if index is None or count is None:
        return
    result_json = config.getoption("ci_shard_result_json")
    durations_path = config.getoption("ci_shard_durations")
    plugin = ShardPlugin(
        index=index,
        count=count,
        result_path=Path(result_json) if result_json else None,
        durations=load_file_durations(Path(durations_path) if durations_path else None),
    )
    config.pluginmanager.register(plugin, PLUGIN_NAME)


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
    parser.add_argument("--result-json", type=Path, help="Write shard selection and pytest outcome counts")
    parsed = parser.parse_args(launcher_args)
    try:
        ShardPlugin(index=parsed.shard_index, count=parsed.shard_count)
    except ValueError as exc:
        parser.error(str(exc))
    # Popen xdist workers inherit this sys.path, so they can import the plugin too.
    plugin_dir = str(Path(__file__).resolve().parent)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    shard_args = [
        "-p",
        PLUGIN_MODULE,
        f"--ci-shard-index={parsed.shard_index}",
        f"--ci-shard-count={parsed.shard_count}",
    ]
    if parsed.result_json is not None:
        shard_args.append(f"--ci-shard-result-json={parsed.result_json.resolve()}")
    return int(pytest.main([*shard_args, *pytest_args]))


if __name__ == "__main__":
    raise SystemExit(main())
