"""Workspace exclusion for cooperative skill writers, not hostile filesystem writers."""

from __future__ import annotations

import sys
import os
import threading
import time
from contextlib import contextmanager, ExitStack
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar, cast

from .. import dirfd
from ..inbox_lock import _held_inbox_lock, _HeldInboxLock, _validate_lock_descriptor, _msvcrt_lock_with_deadline

_NT_GUARD = threading.RLock()
_NT_HELD: dict[str, _HeldInboxLock] = {}


@contextmanager
def _windows_lock(workspace: Path, anchor: Any) -> Iterator[_HeldInboxLock]:
    # RLock owns nested calls in one thread. The handle-relative open refuses
    # reparse points before msvcrt touches the file. No pathname fallback.
    key = str(workspace / ".brigade/skills.mutation.lock")
    with _NT_GUARD:
        if key in _NT_HELD:
            held = _NT_HELD[key]
            held.verify()
            yield held
            return
        fd = dirfd.open_child_file(anchor.fd, "skills.mutation.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _validate_lock_descriptor(fd)
            _msvcrt_lock_with_deadline(fd, key, time.monotonic() + 30, 30)
        except BaseException:
            os.close(fd)
            raise
        held = _HeldInboxLock(key, None, "skills.mutation.lock", fd)
        _NT_HELD[key] = held
        try:
            held.verify()
            yield held
        finally:
            del _NT_HELD[key]
            held.release()


F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def skill_mutation(workspace: Path) -> Iterator[None]:
    # Reuse the hardened single-link, no-follow, OS-backed and thread-reentrant
    # lock implementation. State anchoring also refuses redirected parents.
    from . import registry

    workspace = workspace.expanduser().resolve()
    with registry._held_state_root(workspace) as anchor:
        with (
            _windows_lock(workspace, anchor)
            if os.name == "nt"
            else _held_inbox_lock(workspace / ".brigade" / "skills.mutation.lock")
        ) as held:
            anchor.revalidate()
            held.verify()
            yield
            held.verify()
            anchor.revalidate()


def serialized_skill_mutation(function: F) -> F:
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        workspace = kwargs.get("workspace", kwargs.get("target"))
        if not isinstance(workspace, Path):
            raise TypeError("skill mutation requires a workspace Path")
        with skill_mutation(workspace):
            return function(*args, **kwargs)

    return cast(F, run)


def _enter_transaction(function: F, kwargs: dict[str, Any], stack: ExitStack) -> None:
    workspace = kwargs.get("workspace", kwargs.get("target"))
    if not isinstance(workspace, Path):
        raise TypeError("skill mutation requires a workspace Path")
    stack.enter_context(skill_mutation(workspace))


def skill_command(function: F) -> F:
    """Translate acquisition refusals while preserving command-body failures."""

    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        from .registry import SkillsStatePathError

        if (function.__name__ == "sync" or function.__module__ == "brigade.harness_profile_cmd") and not kwargs.get(
            "write", False
        ):
            return function(*args, **kwargs)
        with ExitStack() as stack:
            try:
                _enter_transaction(function, kwargs, stack)
            except (OSError, SkillsStatePathError) as exc:
                print(f"error: skill mutation refused: {exc}", file=sys.stderr)
                return 2
            return function(*args, **kwargs)

    return cast(F, run)


def registry_import_mutation(function: F) -> F:
    @wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        from .registry import SkillsStatePathError

        with ExitStack() as stack:
            try:
                _enter_transaction(function, kwargs, stack)
            except (OSError, SkillsStatePathError) as exc:
                return None, str(exc), 2
            return function(*args, **kwargs)

    return cast(F, run)
