"""CLI wiring tests for ``brigade update``."""

from __future__ import annotations

import pytest

from brigade import cli


def test_update_parser_defaults_and_dispatch(monkeypatch):
    captured = {}

    def fake_update(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("brigade.update_cmd.run_update", fake_update)

    assert cli.main(["update"]) == 0
    assert captured == {"channel": "stable", "dry_run": False, "switch_channel": False}


@pytest.mark.parametrize("argv", (["update", "--channel", "beta"], ["update", "--dry-run", "--switch-channel"]))
def test_update_parser_accepts_channel_controls(monkeypatch, argv):
    monkeypatch.setattr("brigade.update_cmd.run_update", lambda **_kwargs: 0)
    assert cli.main(argv) == 0


def test_update_rejects_unknown_channel():
    with pytest.raises(SystemExit) as exc:
        cli.main(["update", "--channel", "nightly"])
    assert exc.value.code == 2
