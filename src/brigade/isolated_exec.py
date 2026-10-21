"""Run this Brigade installation without ambient Python import paths."""

from __future__ import annotations

import sys
from pathlib import Path

# A single line also fits generated systemd ExecStart directives. Keep the
# installation's dependencies behind stdlib and never process its .pth files.
_BOOTSTRAP = (
    "import importlib.util, pathlib, runpy, sys; "
    "spec = importlib.util.spec_from_file_location('brigade', sys.argv.pop(1)); "
    "sys.path.append(str(pathlib.Path(spec.origin).resolve().parent.parent)); "
    "package = importlib.util.module_from_spec(spec); "
    "sys.modules['brigade'] = package; "
    "spec.loader.exec_module(package); "
    "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
)


def trusted_cwd() -> Path:
    """Return the installed package directory, never the caller's workspace."""
    return Path(__file__).resolve().parent


def module_argv(module: str, *args: str, python: str | None = None) -> list[str]:
    """Build an isolated invocation anchored to the running Brigade package."""
    return [python or sys.executable, "-I", "-c", _BOOTSTRAP, str(trusted_cwd() / "__init__.py"), module, *args]
