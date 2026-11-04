"""Resolve the native front's path with fleet serve's Python default."""
import json
import sys
from pathlib import Path

from brigade.cli.fleet import DEFAULT_DB_REL_PATH

path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / DEFAULT_DB_REL_PATH
print(json.dumps(str(path.resolve())))
