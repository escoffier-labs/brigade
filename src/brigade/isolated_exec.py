"""Run this Brigade installation without ambient Python import paths."""

from __future__ import annotations

import sys
from pathlib import Path

# A single line also fits generated systemd ExecStart directives. Keep the
# installation's dependencies behind stdlib. -I skips user site, but the
# interpreter's own site-packages and their .pth startup code still run.
_BOOTSTRAP = (
    "import importlib.util as u,os,pathlib as p,runpy as r,sys as s;"
    "i,m=s.argv.pop(1),s.argv.pop(1);"
    "f=p.Path(i).is_file() and u.spec_from_file_location(m.partition('.')[0],i);"
    "f and f.loader or (s.stderr.write('brigade:cannot-load:'+i+'\\n'),s.exit(78));"
    "d=p.Path(f.origin).resolve().parent;os.chdir(d);s.path.append(str(d.parent));"
    "q=u.module_from_spec(f);s.modules[f.name]=q;f.loader.exec_module(q);"
)


def trusted_cwd() -> Path:
    """Return the installed package directory, never the caller's workspace."""
    return Path(__file__).resolve().parent


def module_argv(module: str, *args: str, python: str | None = None) -> list[str]:
    """Build an isolated invocation anchored to the running Brigade package."""
    return package_argv(trusted_cwd() / "__init__.py", module, *args, python=python)


def package_argv(
    package_init: Path,
    module: str,
    *args: str,
    python: str | None = None,
    cwd_after_load: Path | None = None,
) -> list[str]:
    """Pin a configured package before entering an optional Git target."""
    bootstrap = _BOOTSTRAP
    if cwd_after_load is not None:
        bootstrap += "os.chdir(s.argv.pop(1));"
        args = (str(cwd_after_load.expanduser().resolve()), *args)
    bootstrap += "r.run_module(m,run_name='__main__',alter_sys=True)"
    return [
        python or sys.executable,
        "-I",
        "-c",
        bootstrap,
        str(package_init.expanduser().resolve()),
        module,
        *args,
    ]
