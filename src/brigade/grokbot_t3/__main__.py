"""python -m brigade.grokbot_t3 watch|install-watch"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import grokbot_ops
from .contracts import T3DispatchError
from . import lifecycle


def main(argv: list[str] | None = None) -> int:
    from .. import grokbot_packs

    parser = argparse.ArgumentParser(prog="python -m brigade.grokbot_t3")
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch", help="Run one wake-watcher pass.")
    watch.add_argument("--target", type=Path, required=True)
    install = sub.add_parser("install-watch", help="Write the watcher service and timer units.")
    install.add_argument("--target", type=Path, required=True)
    install.add_argument("--out-dir", type=Path, required=True)
    install.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            summary = lifecycle.watch_once(args.target)
            print(json.dumps(summary, sort_keys=True))
            return 1 if summary["failed"] else 0
        paths = lifecycle.write_watch_units(args.target, args.out_dir, force=args.force)
        print(json.dumps({"units": [path.name for path in paths]}))
        return 0
    except (T3DispatchError, grokbot_packs.PackError, grokbot_ops.ServiceRenderError, OSError):
        print("error: t3-dispatch configuration is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
