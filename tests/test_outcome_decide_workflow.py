"""The scheduled writer keeps compute read-only and publishes from main."""

import re

from tests.test_ci_workflow import ROOT, _workflow_job_section

WORKFLOW = (ROOT / ".github/workflows/outcome-decide.yml").read_text(encoding="utf-8")


def test_outcome_decide_schedule_and_main_gate():
    triggers = WORKFLOW.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert triggers == "  schedule:\n    - cron: '17 6 * * *'\n  workflow_dispatch:"
    assert "\npermissions: {}\n" in WORKFLOW
    assert "\nconcurrency:\n  group: outcome-decide\n  cancel-in-progress: false\n" in WORKFLOW
    for name in ("compute", "publish"):
        job = _workflow_job_section(WORKFLOW, name)
        assert "    if: github.ref == 'refs/heads/main'\n" in job
        assert "          ref: main\n          fetch-depth: 0" in job
    assert "    needs: compute" in _workflow_job_section(WORKFLOW, "publish")


def test_outcome_decide_job_permissions_are_exact():
    for name, expected in (
        ("compute", {"contents": "read", "actions": "read"}),
        ("publish", {"contents": "write", "pull-requests": "write", "actions": "write"}),
    ):
        job = _workflow_job_section(WORKFLOW, name)
        permissions = job.split("    permissions:\n", 1)[1].split("    steps:", 1)[0]
        assert dict(re.findall(r"^      ([a-z-]+): (\w+)$", permissions, re.MULTILINE)) == expected
    assert "persist-credentials: false" in _workflow_job_section(WORKFLOW, "compute")


def test_outcome_decide_actions_are_pinned_and_shell_runs_are_inert():
    actions = re.findall(r"^      - uses: (.+)$", WORKFLOW, re.MULTILINE)
    assert len(actions) == 4
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*", action) for action in actions)
    runs = re.findall(r"^        run: (.*(?:\n {10,}[^\n]*)*)", WORKFLOW, re.MULTILINE)
    assert len(runs) == 2 and all("${{" not in command for command in runs)
    assert all("python3 -I -S scripts/ledger_decide.py" in command for command in runs)
    assert "scripts/ledger_decide.py compute" in runs[0]
    assert "scripts/ledger_decide.py publish" in runs[1]
