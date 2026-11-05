"""Read [memory] from hub.toml without adding a TOML crate to the native front."""
import json
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if path.exists():
    with path.open("rb") as stream:
        memory = tomllib.load(stream).get("memory", {})
    embedder = memory.get("embedder")
    if isinstance(embedder, dict) and isinstance(embedder.get("token_file"), str):
        token = Path(embedder["token_file"]).expanduser()
        embedder["token_file"] = str(token if token.is_absolute() else path.parent / token)
else:
    memory = {}
print(json.dumps(memory))
