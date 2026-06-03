from __future__ import annotations

import json

from brigade import operator_cmd


def test_operator_plan_lists_safe_local_configs(tmp_path, capsys):
    assert operator_cmd.plan(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["steps"]}
    assert {"daily", "handoff-sources", "work-scanners", "security", "tools"} <= ids
    assert "Does not start services." in payload["boundaries"]


def test_operator_init_dry_run_does_not_write(tmp_path, capsys):
    assert operator_cmd.init(target=tmp_path, dry_run=True, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert not (tmp_path / ".brigade" / "daily.toml").exists()
