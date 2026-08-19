"""Opt-in POSIX scanner isolation (user + mount namespace).

Tier isolation is the only posture that closes the #957 same-UID class. It is
not the portable default. On Windows and on kernels that refuse unprivileged
user namespaces it reports unavailable and the default remains the crypto tier.

When isolation is active the child is launched inside ``CLONE_NEWUSER`` +
``CLONE_NEWNS`` and the operator authority key and parent store directory are
bind-mounted over with empty trees, so ``getpwuid`` / real-home lookups cannot
read the persisted store key.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ISOLATION_FLAG = "--isolated-scanners"
ISOLATION_ENV = "BRIGADE_ISOLATED_SCANNERS"


@dataclass(frozen=True)
class IsolationStatus:
    available: bool
    reason: str
    platform: str
    method: str = ""


def isolation_requested(*, env: Mapping[str, str] | None = None, isolated: bool = False) -> bool:
    if isolated:
        return True
    environment = env if env is not None else os.environ
    return environment.get(ISOLATION_ENV) == "1"


def probe_isolation() -> IsolationStatus:
    """Return whether user+mount namespace isolation can be used here."""

    platform = sys.platform
    if platform.startswith("win"):
        return IsolationStatus(
            available=False,
            reason="scanner isolation is POSIX-only; Windows stays on the crypto tier",
            platform=platform,
        )
    if os.name != "posix":
        return IsolationStatus(
            available=False,
            reason="scanner isolation requires POSIX user and mount namespaces",
            platform=platform,
        )
    import shutil

    if shutil.which("unshare") is None:
        return IsolationStatus(
            available=False,
            reason="unshare is not available for isolated scanners",
            platform=platform,
        )
    if not hasattr(os, "unshare") or not hasattr(os, "CLONE_NEWUSER") or not hasattr(os, "CLONE_NEWNS"):
        return IsolationStatus(
            available=False,
            reason="this Python build cannot unshare user or mount namespaces",
            platform=platform,
        )
    if not hasattr(os, "fork"):
        return IsolationStatus(
            available=False,
            reason="scanner isolation requires fork",
            platform=platform,
        )
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        try:
            os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
            os.write(writer, b"ok")
        except OSError as exc:
            os.write(writer, f"no:{exc.errno}".encode("ascii", "replace"))
        finally:
            os.close(writer)
            os._exit(0)
    os.close(writer)
    try:
        payload = os.read(reader, 32)
    finally:
        os.close(reader)
        os.waitpid(pid, 0)
    if payload.startswith(b"ok"):
        return IsolationStatus(
            available=True,
            reason="unprivileged user and mount namespaces are available",
            platform=platform,
            method="unshare-user-mount",
        )
    return IsolationStatus(
        available=False,
        reason="kernel refused an unprivileged user namespace; default is the crypto tier",
        platform=platform,
    )


def doctor_check() -> tuple[str, str, str]:
    """Return ``(status, name, detail)`` for doctor. Never silent."""

    status = probe_isolation()
    if status.available:
        return (
            "OK",
            "security: scanner isolation",
            "available (opt-in via --isolated-scanners); default remains crypto-only",
        )
    level = "MANUAL" if status.platform.startswith("win") else "WARN"
    return (level, "security: scanner isolation", status.reason)


def _operator_hide_paths(*, env: Mapping[str, str] | None = None) -> list[Path]:
    from . import authority_key, component_paths

    paths: list[Path] = []
    try:
        paths.append(authority_key.authority_dir(env=env))
    except OSError:
        pass
    try:
        paths.append(Path(component_paths.data_root(env=env)) / "brigade" / "directory-authority")
    except ValueError:
        pass
    try:
        import pwd

        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        paths.append(real_home / ".config" / "brigade" / "authority")
        paths.append(real_home / ".local" / "share" / "brigade" / "directory-authority")
    except (ImportError, KeyError, OSError):
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def prepare_isolation_covers(
    sandbox: Path,
    *,
    hide_paths: Sequence[Path] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[tuple[Path, Path]]:
    """Create cover directories and ensure hide targets exist so bind-mounts can attach."""

    hides = list(hide_paths) if hide_paths is not None else _operator_hide_paths(env=env)
    hide_root = sandbox / ".brigade-isolation-hides"
    pairs: list[tuple[Path, Path]] = []
    for index, path in enumerate(hides):
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            continue
        cover = hide_root / str(index)
        cover.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(cover, 0o700)
        except OSError:
            pass
        pairs.append((cover, path))
    return pairs


_UNSHARE_WRAPPER = """
import os
import sys

args = sys.argv[1:]
pairs = []
while args and args[0] != "--":
    pairs.append((args[0], args[1]))
    args = args[2:]
if args and args[0] == "--":
    args = args[1:]
if hasattr(os, "MS_REC") and hasattr(os, "MS_PRIVATE"):
    try:
        os.mount("none", "/", None, os.MS_REC | os.MS_PRIVATE, None)
    except OSError:
        pass
bind = getattr(os, "MS_BIND", 4096)
for cover, target in pairs:
    os.mount(cover, target, None, bind, None)
os.execvpe(args[0], args, os.environ)
"""


def isolated_argv(argv: Sequence[str], covers: Sequence[tuple[Path, Path]]) -> list[str]:
    """Wrap argv in ``unshare --user --map-root-user --mount`` plus bind-mounts."""

    import shutil

    unshare = shutil.which("unshare")
    if unshare is None:
        raise OSError("unshare is not available for isolated scanners")
    mounts: list[str] = []
    for cover, target in covers:
        mounts.extend([str(cover), str(target)])
    return [
        unshare,
        "--user",
        "--map-root-user",
        "--mount",
        "--",
        sys.executable,
        "-c",
        _UNSHARE_WRAPPER,
        *mounts,
        "--",
        *argv,
    ]
