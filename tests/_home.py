"""Shared home-directory isolation helpers for the test suite.

On Windows ``Path.home()`` and ``os.path.expanduser`` read ``USERPROFILE``
before ``HOME``, with ``HOMEDRIVE``/``HOMEPATH`` as fallback. Tests that only
set ``HOME`` therefore populate one temp home while the code under test reads
another. Use :func:`set_home` for ``monkeypatch`` based isolation and
:func:`home_env` for subprocess env dicts so the pair cannot drift.
"""

from __future__ import annotations

import os
from pathlib import Path


def _split(home: str) -> tuple[str, str, str]:
    drive, tail = os.path.splitdrive(home)
    return home, drive, tail if drive else home


def set_home(monkeypatch, home: Path | str) -> Path:
    """Point HOME, USERPROFILE, HOMEDRIVE and HOMEPATH at ``home``."""
    path = Path(home)
    home_str, drive, homepath = _split(str(path))
    monkeypatch.setenv("HOME", home_str)
    monkeypatch.setenv("USERPROFILE", home_str)
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", homepath)
    return path


def home_env(home: Path | str) -> dict[str, str]:
    """Return a HOME/USERPROFILE/HOMEDRIVE/HOMEPATH env mapping for ``home``."""
    home_str, drive, homepath = _split(str(home))
    return {
        "HOME": home_str,
        "USERPROFILE": home_str,
        "HOMEDRIVE": drive,
        "HOMEPATH": homepath,
    }
