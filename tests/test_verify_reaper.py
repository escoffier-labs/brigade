import os
from pathlib import Path
import select
import subprocess
import sys
from unittest.mock import Mock

import pytest

from brigade.work_cmd import verify_reaper


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant discovery")
def test_darwin_descendants_use_bounded_all_process_snapshot(monkeypatch):
    root = 1_000_000_001
    snapshot = subprocess.CompletedProcess(
        [],
        0,
        f"  {root} {root + 2}\n{root + 2} {root + 1}\n{root + 1} {root}\n"
        f"{root + 3} 1\nPID PPID\ninvalid\n{root + 4} {root} extra\n{root + 5} nope\n",
    )
    run = Mock(return_value=snapshot)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)

    assert verify_reaper._collect_descendants(root) == {root + 1, root + 2}
    run.assert_called_once_with(
        ["/bin/ps", "-A", "-o", "pid=", "-o", "ppid="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"LC_ALL": "C"},
        timeout=2.0,
        check=False,
        text=True,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant discovery")
@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 1, "1000000002 1000000001\n"),
        subprocess.TimeoutExpired("/bin/ps", 2.0),
        FileNotFoundError("ps unavailable"),
        subprocess.SubprocessError("ps failed"),
    ],
    ids=["nonzero", "timeout", "os-error", "subprocess-error"],
)
def test_darwin_snapshot_failure_has_no_descendants(monkeypatch, failure):
    run = Mock(**({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)

    assert verify_reaper._collect_descendants(1_000_000_001) == set()
    run.assert_called_once()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant discovery")
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_darwin_snapshot_propagates_interruption(monkeypatch, interruption):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=interruption))

    with pytest.raises(interruption):
        verify_reaper._collect_descendants(1_000_000_001)


@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"} or not Path("/bin/ps").is_file(),
    reason="requires Linux or Darwin absolute ps",
)
def test_ps_discovers_child_and_setsid_grandchild_with_eof_cleanup(monkeypatch):
    read_fd, write_fd = os.pipe()
    grandchild_code = "import os,sys; os.read(int(sys.argv[1]), 1)"
    child_code = (
        "import os,subprocess,sys; fd=int(sys.argv[1]); "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[2],str(fd)], "
        "pass_fds=(fd,),start_new_session=True); "
        "print(child.pid,flush=True); os.read(fd,1); child.wait(timeout=5)"
    )
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(read_fd), grandchild_code],
            pass_fds=(read_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(read_fd)
    try:
        assert select.select([child.stdout], [], [], 5)[0], "child did not announce grandchild"
        grandchild_pid = int(child.stdout.readline())
        monkeypatch.setattr(sys, "platform", "darwin")
        descendants = verify_reaper._collect_descendants(os.getpid())
        assert {child.pid, grandchild_pid} <= descendants
        assert os.getpid() not in descendants
    finally:
        os.close(write_fd)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
