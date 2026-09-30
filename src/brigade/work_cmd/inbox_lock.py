"""Historical import path for :mod:`brigade.inbox_lock`.

The implementation lives outside ``work_cmd`` so the Claude hook can take
``held_file_lock`` without importing the whole ``work_cmd`` package. This
module replaces itself in ``sys.modules`` with the real one, so
``brigade.work_cmd.inbox_lock`` and ``brigade.inbox_lock`` are the same module
object and monkeypatching either name affects every caller.
"""

from __future__ import annotations

import sys

from .. import inbox_lock as _inbox_lock
from ..inbox_lock import *  # noqa: F401,F403 - static re-export for type checkers

sys.modules[__name__] = _inbox_lock
