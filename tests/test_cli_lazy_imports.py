# content-guard: allow home-path file
import sys


import os
import subprocess


def test_unrelated_command_does_not_import_worklore_sync_modules():
    # Run the assertion in an isolated subprocess to prevent cached `brigade.cli`
    # from hiding the bug, and to avoid disturbing module identities for other tests.
    script = """
import sys
from brigade.cli import main

# Run an unrelated command that won't require sync modules
status = main(["version"])
assert status == 0, f"Expected 0, got {status}"

assert "brigade.worklore_github_sync" not in sys.modules
assert "brigade.worklore_brigade_sync" not in sys.modules
print("LAZY_MODULES_OK")
"""
    env = dict(os.environ)
    env["BRIGADE_EXTRAS"] = "1"
    env["PYTHONPATH"] = "src"

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "LAZY_MODULES_OK" in result.stdout
