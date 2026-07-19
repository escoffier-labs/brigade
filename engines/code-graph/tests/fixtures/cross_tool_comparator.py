#!/usr/bin/env python3
"""Protocol stub for exercising the optional cross-tool adapter boundary."""

import json
from pathlib import Path
import subprocess
import sys


request = json.load(sys.stdin)
if "expected_files" in request or "expected_tokens" in request:
    sys.exit("comparator request must not contain correctness labels")

indexed_branch = subprocess.run(
    ["git", "branch", "--show-current"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if request.get("branch_transition"):
    subprocess.run(
        ["git", "switch", "-q", request["branch_transition"]["to"]], check=True
    )

files = sorted(
    path.relative_to(Path.cwd()).as_posix()
    for path in Path.cwd().rglob("*")
    if path.is_file() and ".git" not in path.parts
)
contents = "\n".join(Path(path).read_text(encoding="utf-8") for path in files)
branch = subprocess.run(
    ["git", "branch", "--show-current"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
index_status = "index mismatch" if indexed_branch != branch else "index current"
print(
    json.dumps(
        {
            "response": f"{index_status}\n{contents}",
            "files": files,
            "setup_steps": 2 if request.get("branch_transition") else 1,
            "tool_calls": 1,
        }
    )
)
