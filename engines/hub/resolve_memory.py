"""Read [memory] from hub.toml without adding a TOML crate to the native front."""
import json
import sys
import tomllib
from pathlib import Path

# `--required`: the operator named this file, or the hub already saw it exist,
# so a file gone by the time it is opened is a read failure, never lexical mode.
required = sys.argv[1:2] == ["--required"]
path = Path(sys.argv[-1]).expanduser()
if required or path.exists():
    try:
        with path.open("rb") as stream:
            memory = tomllib.load(stream).get("memory", {})
    except FileNotFoundError:
        sys.exit("memory configuration disappeared before it was read")
    embedder = memory.get("embedder")
    if isinstance(embedder, dict) and isinstance(embedder.get("token_file"), str):
        token = Path(embedder["token_file"]).expanduser()
        embedder["token_file"] = str(token if token.is_absolute() else path.parent / token)
else:
    memory = {}
print(json.dumps(memory))
