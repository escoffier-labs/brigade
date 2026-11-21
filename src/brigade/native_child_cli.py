"""Bounded JSON input and diagnostics shared by the native-child verbs."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .native_child_host import NativeChildCapacityError, NativeChildCustodyError, NativeChildError
from .native_child_journal import bound, refuse
from .run_batch import BatchError

_MAX_NATIVE_BYTES = 1_048_576


def read_native(path: str) -> object:
    """Read a bounded native projection from stdin or one regular file.

    A named pipe would block the CLI forever, so the path is lstat-checked
    first, then opened non-blocking and no-follow where the platform supports
    it, and the open handle is checked again before any read.
    """
    if path == "-":
        raw = sys.stdin.buffer.read(_MAX_NATIVE_BYTES + 1)
    else:
        target = Path(path).expanduser()
        try:
            before = os.lstat(target)
        except OSError:
            raise refuse("native-unreadable", "the native result file is not readable") from None
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise refuse("native-unreadable", "the native result is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError:
            raise refuse("native-unreadable", "the native result file is not readable") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise refuse("native-unreadable", "the native result is not a regular file")
            chunks: list[bytes] = []
            remaining = _MAX_NATIVE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            raise refuse("native-unreadable", "the native result file is not readable") from None
        finally:
            os.close(descriptor)
    if len(raw) > _MAX_NATIVE_BYTES:
        raise refuse("native-too-large", "the native result exceeds the size bound")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise refuse("native-invalid", "the native result is not valid JSON") from None


def emit(call: Any, *, output: Callable[[Any], None] | None = None, recovery: Mapping[str, str] | None = None) -> int:
    try:
        report = call()
    except (NativeChildError, NativeChildCapacityError, NativeChildCustodyError, BatchError) as exc:
        print(f"error [{exc.code}]: {bound(str(exc))}", file=sys.stderr)
        if recovery and exc.code in recovery:
            print(recovery[exc.code], file=sys.stderr)
        return 2
    if output is None:
        print(json.dumps(report, sort_keys=True))
    else:
        output(report)
    return 0
