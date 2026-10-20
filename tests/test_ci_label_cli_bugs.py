import re
import yaml
from pathlib import Path


def test_workflow_regex_matches_fingerprint_format():
    workflow_path = Path(".github/workflows/label-cli-bugs.yml")
    workflow_yaml = yaml.safe_load(workflow_path.read_text())

    run_script = workflow_yaml["jobs"]["label-cli-bugs"]["steps"][0]["run"]

    # Extract the regex
    match = re.search(r"grep -qE '\^bf-\[0-9a-f\]\{12\}\$'", run_script)
    assert match is not None, "Could not find exact grep pattern in workflow: " + run_script

    regex = re.compile(r"^bf-[0-9a-f]{12}$", re.MULTILINE)

    # Matches
    assert regex.search("bf-1234567890ab") is not None
    assert regex.search("Some text\nbf-1234567890ab\nMore text") is not None
    assert regex.search("bf-abcdef123456") is not None

    # Rejects near misses
    assert regex.search("bf-1234567890a") is None  # Too short
    assert regex.search("bf-1234567890abc") is None  # Too long
    assert regex.search("bf-1234567890aG") is None  # Invalid hex
    assert regex.search(" bf-1234567890ab") is None  # Leading space on line
    assert regex.search("bf-1234567890ab ") is None  # Trailing space on line
