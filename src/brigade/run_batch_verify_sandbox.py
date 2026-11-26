"""Filesystem, process, network, and import boundaries for batch verification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .work_cmd import verify_required

_CANDIDATE = Path("/candidate")
_BOOTSTRAP = "/brigade-python"
_FLAGS = [
    "--unshare-all",
    "--unshare-user",
    "--disable-userns",
    "--die-with-parent",
    "--new-session",
    "--cap-drop",
    "ALL",
]
_SYSTEM_MOUNTS = (
    "/bin",
    "/lib",
    "/lib64",
    "/usr/bin",
    "/usr/lib",
    "/usr/lib64",
    "/usr/libexec",
    "/usr/local/bin",
    "/usr/local/lib",
    "/usr/share/git-core",
    "/etc/ld.so.cache",
)
_PYTHON_RUNTIME_DIRS = ("bin", "Scripts", "lib", "lib32", "lib64", "libx32")
_PYTHON_RUNTIME_FILES = ("pyvenv.cfg",)
_PRIVATE_TOOL_METADATA = frozenset({"pipx_metadata.json", "direct_url.json"})

# The trusted startup hook loads the verification entry points before exposing
# candidate imports. A candidate package named pytest cannot replace the tool.
_SITE_CUSTOMIZE = """\
import importlib
import importlib.util
import json
import os
import pathlib
import sys

for tool in ("pytest", "mypy", "ruff"):
    if importlib.util.find_spec(tool) is not None:
        importlib.import_module(tool)
root = "/candidate"
src = root + "/src"
sys.path[0:0] = ([src] if os.path.isdir(src) else []) + [root]
"""
_IMPORT_PROBE = """\
import importlib.util
import json
import sys
from pathlib import Path

root = Path("/candidate")
paths = ([str(root / "src")] if (root / "src").is_dir() else []) + [str(root)]
if sys.path[:len(paths)] != paths:
    sys.exit(41)
for name in json.loads(sys.argv[1]):
    spec = importlib.util.find_spec(name)
    origins = [] if spec is None else list(spec.submodule_search_locations or ())
    if spec is not None and spec.origin is not None:
        origins.append(spec.origin)
    paths = [Path(origin) for origin in origins]
    if not paths or any(not path.is_absolute() or not path.resolve().is_relative_to(root) for path in paths):
        sys.exit(42)
"""


def _probe(argv: list[str]) -> int | None:
    try:
        return subprocess.run(
            argv, cwd="/", env={}, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def bwrap() -> str | None:
    """Require the actual Linux namespace features, even on hosts with bwrap installed."""
    binary = shutil.which("bwrap", path=os.defpath) if sys.platform == "linux" else None
    if binary is None:
        return None
    argv = [binary, *_FLAGS]
    for path in _SYSTEM_MOUNTS:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    return binary if _probe([*argv, "--", "/usr/bin/true"]) == 0 else None


def has_escaping_symlinks(checkout: Path) -> bool:
    """Accept only relative links whose final destination remains in the checkout."""
    for root, dirs, files in os.walk(checkout, followlinks=False):
        directory = Path(root)
        if directory == checkout and ".git" in dirs:
            dirs.remove(".git")
        for name in (*dirs, *files):
            link = directory / name
            if link.is_symlink():
                try:
                    if Path(os.readlink(link)).is_absolute() or not link.resolve().is_relative_to(checkout):
                        return True
                except (OSError, RuntimeError):
                    return True
    return False


def expected_imports(source: Path, base: str, configured: object) -> list[str]:
    """Read declared top-level packages or infer regular packages from the base tree."""
    if configured is not None:
        if not isinstance(configured, list) or any(
            not isinstance(name, str) or not name.isidentifier() for name in configured
        ):
            raise ValueError("import_packages must list top-level Python identifiers")
        return sorted(set(configured))
    entries = verify_required._tree_entries(source, base, deadline=monotonic() + 30)
    src_packages = set()
    root_packages = set()
    for name, (mode, _) in entries.items():
        parts = name.split("/")
        if not verify_required._regular(mode) or parts[-1] != "__init__.py":
            continue
        if len(parts) == 3 and parts[0] == "src" and parts[1].isidentifier():
            src_packages.add(parts[1])
        elif len(parts) == 2 and parts[0].isidentifier() and parts[0] not in {"tests", "scripts", "docs", "verify"}:
            root_packages.add(parts[0])
    return sorted(src_packages | (root_packages if "pyproject.toml" in entries else set()))


@dataclass(frozen=True)
class Sandbox:
    prefix: tuple[str, ...]
    checkout: Path

    def command(self, argv: list[str]) -> list[str]:
        executable = Path(argv[0])
        if executable.is_relative_to(self.checkout):
            executable = _CANDIDATE / executable.relative_to(self.checkout)
        return [*self.prefix, "--", str(executable), *argv[1:]]

    def check_imports(self, packages: list[str]) -> int | None:
        return _probe(self.command([sys.executable, "-P", "-s", "-c", _IMPORT_PROBE, json.dumps(packages)]))


def _toolchain_mounts() -> list[tuple[Path, Path]]:
    """Mount runtime paths at their original locations for absolute shebangs."""
    prefixes = {Path(path) for path in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)}
    prefix_roots = {prefix.resolve() for prefix in prefixes}
    system_paths = {Path(path) for path in _SYSTEM_MOUNTS if Path(path).exists()}
    allowed = {path.resolve() for path in system_paths}
    allowed.update(root / name for root in prefix_roots for name in (*_PYTHON_RUNTIME_DIRS, *_PYTHON_RUNTIME_FILES))
    paths = set(system_paths)
    for prefix in prefixes:
        paths.update(prefix / name for name in _PYTHON_RUNTIME_DIRS if (prefix / name).is_dir())
        paths.update(prefix / name for name in _PYTHON_RUNTIME_FILES if (prefix / name).is_file())
    mounts = [(path.resolve(), path) for path in sorted(paths)]
    # An overlapping system or base mount must not re-expose an entire prefix.
    return [
        (source, target)
        for source, target in mounts
        if source in allowed and not any(p.is_relative_to(source) for p in prefix_roots)
    ]


def _raise_walk_error(error: OSError) -> None:
    raise error


def _metadata_masks(mounts: list[tuple[Path, Path]]) -> list[Path]:
    """Find private installer metadata, including aliases such as venv lib64."""
    metadata = {source for source, _ in mounts if source.name in _PRIVATE_TOOL_METADATA}
    for source in {source for source, _ in mounts if source.is_dir()}:
        for root, _, files in os.walk(source, onerror=_raise_walk_error):
            for name in _PRIVATE_TOOL_METADATA.intersection(files):
                metadata.add((Path(root) / name).resolve(strict=True))
    return sorted(
        {
            target / path.relative_to(source)
            for source, target in mounts
            for path in metadata
            if path.is_relative_to(source)
        }
    )


def prepare(binary: str, checkout: Path, env: dict[str, str]) -> Sandbox | None:
    """Expose just the verifier's toolchain alongside a writable checkout and scratch."""
    checkout = checkout.resolve()
    scratch = checkout.parent
    tmp = scratch / "tmp"
    (tmp / "home").mkdir()
    bootstrap = scratch / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(_SITE_CUSTOMIZE, encoding="utf-8")

    home = Path.home().resolve()
    protected = [scratch.parent.parent, Path("/run"), Path("/root"), Path("/var"), Path("/tmp")]
    for key in ("BRIGADE_HOME", "BRIGADE_STORE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        if os.environ.get(key):
            protected.append(Path(os.environ[key]).expanduser().resolve())
    mounts = _toolchain_mounts()
    args = [binary, *_FLAGS, "--clearenv"]
    for source, target in mounts:
        if home.is_relative_to(source) or any(source.is_relative_to(p) or p.is_relative_to(source) for p in protected):
            return None
        args += ["--ro-bind", str(source), str(target)]
    metadata = _metadata_masks(mounts)
    if metadata:
        mask = bootstrap / "private-metadata"
        mask.touch(mode=0)
        for path in metadata:
            args += ["--ro-bind", str(mask), str(path)]
    args += [
        "--proc",
        "/proc",
        "--remount-ro",
        "/proc",
        "--dev",
        "/dev",
        "--bind",
        str(checkout),
        str(_CANDIDATE),
        "--ro-bind",
        str(checkout / ".git"),
        str(_CANDIDATE / ".git"),
        "--bind",
        str(tmp),
        "/tmp",
        "--ro-bind",
        str(bootstrap),
        _BOOTSTRAP,
        "--remount-ro",
        "/",
        "--chdir",
        str(_CANDIDATE),
    ]
    sandbox_env = {
        "PATH": env["PATH"],
        "PY": env["PY"],
        "PYTHONPATH": _BOOTSTRAP,
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "HOME": "/tmp/home",
        "XDG_CONFIG_HOME": "/tmp/home",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name, value in sandbox_env.items():
        args += ["--setenv", name, value]
    return Sandbox(tuple(args), checkout)
