import os
import signal
import subprocess
import sys
from pathlib import Path


class _MockProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        try:
            os.kill(self.pid, 0)
            return None
        except OSError:
            return 0

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass


def _proc_ppid_map(proc_dir: Path) -> dict[int, int]:
    ppid_map: dict[int, int] = {}
    for proc_path in proc_dir.iterdir():
        if not proc_path.name.isdigit():
            continue
        try:
            stat_text = (proc_path / "stat").read_text()
            rparen_idx = stat_text.rfind(")")
            if rparen_idx != -1:
                parts = stat_text[rparen_idx + 1 :].split()
                if len(parts) >= 2:
                    pid = int(proc_path.name)
                    ppid = int(parts[1])
                    ppid_map[pid] = ppid
        except (OSError, ValueError):
            continue

    return ppid_map


def _darwin_ppid_map() -> dict[int, int]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=", "-o", "ppid="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
            timeout=2.0,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    ppid_map: dict[int, int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = (int(part, 10) for part in parts)
        except ValueError:
            continue
        ppid_map[pid] = ppid
    return ppid_map


def _collect_descendants(root_pid: int) -> set[int]:
    """Return running descendants from Darwin ps or the POSIX /proc snapshot."""
    if os.name != "posix":
        return set()

    if sys.platform == "darwin":
        ppid_map = _darwin_ppid_map()
    else:
        proc_dir = Path("/proc")
        if not proc_dir.is_dir():
            return set()
        ppid_map = _proc_ppid_map(proc_dir)

    descendants: set[int] = set()
    current_generation = {root_pid}
    while current_generation:
        descendants.update(current_generation)
        next_generation: set[int] = set()
        for pid, ppid in ppid_map.items():
            if ppid in current_generation and pid not in descendants:
                next_generation.add(pid)
        current_generation = next_generation

    descendants.discard(root_pid)
    return descendants


def _survivor_mocks(pids: set[int]) -> list[_MockProcess]:
    mocks: list[_MockProcess] = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            mocks.append(_MockProcess(pid))
        except OSError:
            pass
    return mocks
