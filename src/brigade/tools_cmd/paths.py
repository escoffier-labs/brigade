"""Path helpers for the tools command family."""

from __future__ import annotations

from pathlib import Path

from . import constants


def config_path(target: Path) -> Path:
    return target / constants.CONFIG_REL_PATH


def calls_path(target: Path) -> Path:
    return target / constants.CALLS_REL_PATH


def runs_path(target: Path) -> Path:
    return target / constants.RUNS_REL_PATH


def checkpoints_path(target: Path) -> Path:
    return target / constants.CHECKPOINTS_REL_PATH


def runtimes_config_path(target: Path) -> Path:
    return target / constants.RUNTIMES_REL_PATH


def runtime_state_path(target: Path) -> Path:
    return target / constants.RUNTIME_STATE_REL_PATH


def policy_path(target: Path) -> Path:
    return target / constants.POLICY_REL_PATH


def parity_closeouts_path(target: Path) -> Path:
    return target / constants.PARITY_CLOSEOUTS_REL_PATH
