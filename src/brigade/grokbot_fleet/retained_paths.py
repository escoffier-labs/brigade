"""Retained instance validation for the pack relay until Task 4."""

import os
from pathlib import Path
from typing import NoReturn
from brigade import grokbot_paths
from .contracts import FleetError


def _environment_error() -> NoReturn:
    raise FleetError("invalid_request", "Fleet environment is invalid")


def normalize_absolute_path(path_value: str) -> str:
    trimmed = os.path.normpath(path_value).rstrip("/")
    return trimmed if trimmed else "/"


def paths_overlap(left: str, right: str) -> bool:
    if os.name == "nt":
        normalized_left = os.path.normcase(normalize_absolute_path(left)).replace("\\", "/")
        normalized_right = os.path.normcase(normalize_absolute_path(right)).replace("\\", "/")
    else:
        normalized_left = normalize_absolute_path(left)
        normalized_right = normalize_absolute_path(right)
    if normalized_left == normalized_right:
        return True
    right_prefix = normalized_right if normalized_right.endswith("/") else f"{normalized_right}/"
    left_prefix = normalized_left if normalized_left.endswith("/") else f"{normalized_left}/"
    return normalized_left.startswith(right_prefix) or normalized_right.startswith(left_prefix)


def assert_disjoint_paths(paths: list[str]) -> None:
    for left_index, left in enumerate(paths):
        for right in paths[left_index + 1 :]:
            if paths_overlap(left, right):
                _environment_error()


def _reject_windows_network_or_device_root(path_text: str) -> None:
    if not grokbot_paths.has_allowed_absolute_root(path_text, windows=os.name == "nt"):
        _environment_error()


def validate_absolute_reference(path_text: object) -> str:
    if not isinstance(path_text, str) or not path_text or "\0" in path_text:
        _environment_error()
    _reject_windows_network_or_device_root(path_text)
    candidate = Path(path_text)
    if any(part in {".", ".."} for part in candidate.parts):
        _environment_error()
    if any(segment in {".", ".."} for segment in path_text.replace("\\", "/").split("/")):
        _environment_error()
    if any(seg and (seg.endswith(".") or seg.endswith(" ")) for seg in path_text.replace("\\", "/").split("/")):
        _environment_error()
    return normalize_absolute_path(path_text)


def validate_disjoint_state_paths(
    runtime_path: str,
    ledger_path: str,
    action_state_path: str,
    approval_dir: str,
) -> dict[str, str]:
    paths = {
        "runtime_path": validate_absolute_reference(runtime_path),
        "ledger_path": validate_absolute_reference(ledger_path),
        "action_state_path": validate_absolute_reference(action_state_path),
        "approval_dir": validate_absolute_reference(approval_dir),
    }
    assert_disjoint_paths(list(paths.values()))
    return paths
