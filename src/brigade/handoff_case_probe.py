"""Whether a directory's filesystem folds letter case.

Ingest protects a handoff whose name or file identity is indexed by Git. On a
filesystem that aliases case, a different spelling can name the same file, so
the guard has to compare casefolded names. That is a property of the mounted
filesystem, not of the platform, so this module asks the directory itself and
never guesses from `sys.platform`.

The probe must stay conservative in two directions. Unicode case pairs are not
case-insensitive aliases on NTFS, APFS or ext4 with casefold enabled, so only
pure-ASCII spellings are sampled. A sample that is unlinked or replaced while
it is read proves nothing, so it is discarded rather than read as a
case-sensitive directory. Every undecidable outcome is reported as None and
callers keep the fail-closed comparison.
"""

from __future__ import annotations

import os
from pathlib import Path

# Bound the probe so a crowded inbox cannot turn it into a directory walk.
CASE_PROBE_LIMIT = 32

_ASCII_CASE_FLIP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)


def _ascii_case_flip(name: str) -> str | None:
    """Swap ASCII letter case, or None when `name` cannot probe aliasing.

    A filesystem that folds case does not fold every Unicode pair: dotless i
    and I, sharp s and SS, and dotted I and i-with-dot stay distinct names on
    NTFS, APFS and ext4 with casefold enabled. Probing with those spellings
    would report an aliasing directory as case-sensitive, so only pure-ASCII
    names that contain an ASCII letter are eligible.
    """
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        return None
    flipped = name.translate(_ASCII_CASE_FLIP)
    return None if flipped == name else flipped


def _probe_alias_sample(directory: Path, name: str, alternate: str) -> bool | None:
    """One stable observation: True if the spellings alias, False if distinct.

    None means the entry was unlinked or replaced while it was sampled, or the
    observation could not be read, so it says nothing about case folding.
    """
    try:
        before = (directory / name).stat()
    except OSError:
        return None
    try:
        alias = (directory / alternate).stat()
    except FileNotFoundError:
        alias = None
    except OSError:
        return None
    try:
        after = (directory / name).stat()
    except OSError:
        # A sample that vanished mid-probe cannot speak for this directory.
        return None
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        return None
    if alias is None:
        # Only an unchanged entry makes an absent alternate meaningful.
        return False
    return (alias.st_dev, alias.st_ino) == (before.st_dev, before.st_ino)


def probe_case_aliases(directory: Path) -> bool | None:
    """Whether two spellings can name one file in `directory`.

    Case sensitivity is a property of the mounted filesystem, not the
    platform, so ask the directory itself: stat ASCII case-swapped spellings
    of entries that exist. True means the spellings alias one file. False
    means they are distinct names, concluded only from agreeing samples. None
    means the probe could not decide, and callers then treat the spellings as
    aliasing.
    """
    aliased = False
    distinct = 0
    truncated = False
    scanned = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if scanned >= CASE_PROBE_LIMIT:
                    truncated = True
                    break
                scanned += 1
                alternate = _ascii_case_flip(entry.name)
                if alternate is None:
                    continue
                sample = _probe_alias_sample(directory, entry.name, alternate)
                if sample is None:
                    continue
                if sample:
                    aliased = True
                else:
                    distinct += 1
    except OSError:
        return None
    if aliased and distinct:
        # Disagreeing samples mean a sample was disturbed; fail closed.
        return None
    if aliased:
        return True
    # Two agreeing sensitive samples, or every eligible entry when the scan
    # saw them all, are enough to call this directory case-sensitive.
    if distinct >= 2 or (distinct == 1 and not truncated):
        return False
    return None
