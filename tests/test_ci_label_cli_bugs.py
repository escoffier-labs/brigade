import subprocess
from pathlib import Path


def test_workflow_detects_fingerprint_and_labels_cli_bugs():
    workflow_path = Path(".github/workflows/label-cli-bugs.yml")
    workflow_yaml = workflow_path.read_text()

    # Extract the run script using a simple text search
    run_script = workflow_yaml.split("run: |")[1].strip()

    # We need to test the script itself by passing fake gh CLI and different bodies
    def run_workflow_script(issue_body, initial_labels=""):
        # We need a fake gh script to intercept calls
        fake_gh = """
if [ "$1" = "label" ] && [ "$2" = "create" ]; then
    if [[ "$INITIAL_LABELS" == *"from-cli"* ]]; then
        echo "Label already exists" >&2
        return 1
    fi
    echo "CREATED_LABEL: $3"
elif [ "$1" = "issue" ] && [ "$2" = "edit" ]; then
    echo "EDITED_ISSUE: $3 WITH_LABELS: ${5}"
else
    echo "UNKNOWN_CMD: $@"
    return 1
fi
"""

        env = {
            "GH_TOKEN": "fake_token",
            "ISSUE_NUMBER": "123",
            "ISSUE_BODY": issue_body,
            "INITIAL_LABELS": initial_labels,
        }

        # Run the extracted script, but with a mock gh function
        script_to_run = f"""
function gh() {{
{fake_gh}
}}
{run_script}
"""

        result = subprocess.run(
            ["bash", "-e", "-c", script_to_run],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    # Test 1: Valid fingerprint, no initial labels
    valid_body = "Some text\nbf-1234567890ab\nMore text"
    output = run_workflow_script(valid_body)
    assert "CREATED_LABEL: from-cli" in output
    assert "EDITED_ISSUE: 123 WITH_LABELS: bug,from-cli" in output

    # Test 2: Valid fingerprint, label already exists (idempotency check)
    output = run_workflow_script(valid_body, initial_labels="from-cli")
    assert "EDITED_ISSUE: 123 WITH_LABELS: bug,from-cli" in output

    # Test 3: Invalid fingerprint
    invalid_body = "Some text\nbf-1234567890a\nMore text"
    output = run_workflow_script(invalid_body)
    assert "CREATED_LABEL" not in output
    assert "EDITED_ISSUE" not in output

    # Test 4: Shell injection attempt in the body
    injection_body = "Some text\nbf-1234567890ab\n$(echo injected) `echo injected`"
    output = run_workflow_script(injection_body)
    assert "injected" not in output  # The shell shouldn't evaluate this
    assert "EDITED_ISSUE: 123 WITH_LABELS: bug,from-cli" in output  # Still matches since fingerprint is valid
