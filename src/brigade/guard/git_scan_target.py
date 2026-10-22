"""Enter a requested Git repository after isolated package loading."""

from __future__ import annotations

import os
import sys

from .git_scan import main

if __name__ == "__main__":
    # The isolated launcher has already pinned the package and imported this
    # entry point from a trusted cwd. Git still needs the requested repository.
    os.chdir(sys.argv.pop(1))
    raise SystemExit(main())
