"""Race-free handoff between a test and the real child it spawns (#2188).

``Path.write_text`` creates the file before it writes the bytes, so a parent
that stops polling at ``is_file()`` can read an empty file. A child that wants
to tell the test "I am ready" or "my PID is N" publishes the value through a
temporary name and renames it into place, so the final name never exists empty.
The parent polls for non-empty content instead of for existence.

``process_is_running`` is the matching liveness check for a killed child.
"""

from __future__ import annotations

import time
from pathlib import Path

# Source for a child ``python -c`` program. It defines ``publish(path, text)``,
# which writes ``text`` to a sibling temporary file and renames it over ``path``
# (``os.replace`` is atomic on POSIX and Windows).
PUBLISH_SOURCE = """\
import os as _os

def publish(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w') as handle:
        handle.write(text)
        handle.flush()
    _os.replace(tmp, path)

"""


def wait_for_text(path: Path, *, timeout: float, interval: float = 0.01) -> str | None:
    """Poll until ``path`` holds non-empty text; ``None`` if the deadline passes first."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            text = path.read_text().strip()
        except OSError:
            text = ""
        if text:
            return text
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# /proc state letters for a task that has exited: Z is a zombie waiting for its
# parent, X (and x on older kernels) is dead and about to disappear. A SIGKILLed
# orphan is observable as X for a moment while its reaper catches up.
_EXITED_STATES = frozenset("ZXx")


def process_state(pid: int) -> str | None:
    """Return the Linux ``/proc`` state letter for ``pid``, or ``None`` when it is gone."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name is parenthesised and may contain spaces; state follows it.
    fields = stat.rpartition(")")[2].split()
    return fields[0] if fields else None


def process_is_running(pid: int) -> bool:
    state = process_state(pid)
    return state is not None and state not in _EXITED_STATES
