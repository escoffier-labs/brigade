#!/usr/bin/env python3
"""Structurally validate the release workflow version-output contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

JOB_NAME = "release"
PRE_STEP_NAME = "Verify built release binaries"
VALIDATOR_STEP_NAME = "Validate release workflow contract"
PUBLISH_STEP_NAME = "Publish release"
POST_STEP_NAME = "Verify published install assets"

RELEASE_JOB_RUNS_ON = "ubuntu-latest"
RELEASE_JOB_ALLOWED_KEYS = frozenset({"runs-on"})

MISMATCH_ECHO = (
    'echo "${bin} version mismatch: got ${got}, want ${want} (from ${GITHUB_REF_NAME})"'
)

PRE_PUBLISH_BLOCK = (
    'expected="${GITHUB_REF_NAME#v}"',
    "for bin in miseledger sessionfind; do",
    '  got="$(./dist/${bin}-linux-amd64 version)"',
    '  want="miseledger ${expected}"',
    '  if [ "$got" != "$want" ]; then',
    f'    {MISMATCH_ECHO}',
    "    exit 1",
    "  fi",
    "done",
)

POST_PUBLISH_BLOCK = (
    'expected="${GITHUB_REF_NAME#v}"',
    "for bin in miseledger sessionfind; do",
    '  got="$(./verify/${bin}-linux-amd64 version)"',
    '  want="miseledger ${expected}"',
    '  if [ "$got" != "$want" ]; then',
    f'    {MISMATCH_ECHO}',
    "    exit 1",
    "  fi",
    "done",
)

POST_PUBLISH_STEP = (
    "mkdir -p verify",
    'gh release download "$GITHUB_REF_NAME" --pattern miseledger-linux-amd64 --pattern sessionfind-linux-amd64 --pattern checksums.txt --dir verify',
    "(cd verify && sha256sum -c checksums.txt --ignore-missing)",
    "chmod +x verify/miseledger-linux-amd64",
    "chmod +x verify/sessionfind-linux-amd64",
    *POST_PUBLISH_BLOCK,
    'MISELEDGER="$PWD/verify/miseledger-linux-amd64" scripts/smoke_archive.sh',
)

VALIDATOR_RUN = ("scripts/check_release_workflow.sh",)

PUBLISH_STEP = (
    'if gh release view "$GITHUB_REF_NAME" >/dev/null 2>&1; then',
    '  gh release upload "$GITHUB_REF_NAME" dist/* --clobber',
    "else",
    '  gh release create "$GITHUB_REF_NAME" dist/* --generate-notes',
    "fi",
)

TOKEN_ENV_EXACT = {"GH_TOKEN": "${{ github.token }}"}

PRE_ALLOWED_KEYS = frozenset({"name", "run"})
VALIDATOR_ALLOWED_KEYS = frozenset({"name", "run"})
PUBLISH_ALLOWED_KEYS = frozenset({"name", "run", "env"})
POST_ALLOWED_KEYS = frozenset({"name", "run", "env"})

FORBIDDEN_PATTERNS = (
    "awk '{print",
    'awk "{print',
    "| cut ",
    "| sed -n",
    "grep -o",
    "{print $",
    "read -r version",
    "head -1",
    "tail -1",
    '[[ "$got" == *',
    '[[ "$got" = *',
    "grep -q",
)

CONTRACT_MARKERS = (
    'got="$(./dist/${bin}-linux-amd64 version)"',
    'got="$(./verify/${bin}-linux-amd64 version)"',
)

STEP_SCALAR_KEYS = frozenset(
    {
        "name",
        "if",
        "continue-on-error",
        "shell",
        "working-directory",
        "timeout-minutes",
    }
)

ANCHOR_PATTERN = re.compile(r"&[A-Za-z_][A-Za-z0-9_-]*")
ALIAS_PATTERN = re.compile(r"(?:^|[\s:,])\*[A-Za-z_][A-Za-z0-9_-]*")
MERGE_KEY_PATTERN = re.compile(r"<<:")


class WorkflowParseError(ValueError):
    """Raised when YAML metadata cannot be parsed safely."""


def forbid_yaml_features(text: str, label: str) -> None:
    if MERGE_KEY_PATTERN.search(text):
        raise WorkflowParseError(f"{label} uses forbidden YAML merge key")
    if ANCHOR_PATTERN.search(text):
        raise WorkflowParseError(f"{label} uses forbidden YAML anchor")
    if ALIAS_PATTERN.search(text):
        raise WorkflowParseError(f"{label} uses forbidden YAML alias")


def assign_unique(mapping: dict[str, Any], key: str, value: Any, label: str) -> None:
    if key in mapping:
        raise WorkflowParseError(f"duplicate key {key!r} in {label}")
    mapping[key] = value


def extract_scalar(line: str) -> str:
    _, _, value = line.partition(":")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def step_line_offset(line: str) -> int:
    dash = line.find("-")
    return dash + 1 if dash >= 0 else 0


def extract_run_block(lines: list[str], index: int) -> tuple[str, int]:
    line = lines[index]
    forbid_yaml_features(line, "step run metadata")
    _, _, value = line.partition(":")
    value = value.strip()
    if value in {"|", ">-", "|-", ">"}:
        if index + 1 >= len(lines):
            return "", index + 1
        base_indent = len(lines[index + 1]) - len(lines[index + 1].lstrip())
        index += 1
        run_lines: list[str] = []
        while index < len(lines):
            current = lines[index]
            if current.strip() == "":
                run_lines.append("")
                index += 1
                continue
            indent = len(current) - len(current.lstrip())
            if indent < base_indent:
                break
            run_lines.append(current[base_indent:])
            index += 1
        return "\n".join(run_lines).rstrip("\n"), index
    return value, index + 1


def extract_mapping_block(
    lines: list[str],
    index: int,
    label: str,
) -> tuple[dict[str, str], int]:
    line = lines[index]
    forbid_yaml_features(line, label)
    _, _, value = line.partition(":")
    value = value.strip()
    if value:
        return {}, index + 1

    if index + 1 >= len(lines):
        return {}, index + 1

    base_indent = len(lines[index + 1]) - len(lines[index + 1].lstrip())
    index += 1
    mapping: dict[str, str] = {}
    while index < len(lines):
        current = lines[index]
        if current.strip() == "":
            index += 1
            continue
        indent = len(current) - len(current.lstrip())
        if indent < base_indent:
            break
        stripped = current.strip()
        if ":" not in stripped:
            index += 1
            continue
        forbid_yaml_features(stripped, label)
        key, _, _ = stripped.partition(":")
        key = key.strip()
        assign_unique(mapping, key, extract_scalar(stripped), label)
        index += 1
    return mapping, index


def parse_step_scalar(stripped: str) -> tuple[str, str] | None:
    for key in STEP_SCALAR_KEYS:
        if stripped.startswith(f"{key}:"):
            return key, extract_scalar(stripped)
    return None


def parse_steps_block(steps_body: str) -> list[dict[str, Any]]:
    lines = steps_body.splitlines()
    steps: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not re.match(r"^\s*-\s", line):
            index += 1
            continue

        step: dict[str, Any] = {}
        step_label = f"release job step {len(steps) + 1}"
        forbid_yaml_features(line, step_label)
        step_indent = len(line) - len(line.lstrip())
        step_line = line[step_line_offset(line) :].strip()
        parsed = parse_step_scalar(step_line)
        if parsed is not None:
            forbid_yaml_features(step_line, step_label)
            assign_unique(step, parsed[0], parsed[1], step_label)
        index += 1
        while index < len(lines):
            current = lines[index]
            if current.strip() == "":
                index += 1
                continue
            indent = len(current) - len(current.lstrip())
            if indent <= step_indent and current.lstrip().startswith("-"):
                break
            stripped = current.strip()
            if stripped.startswith("run:"):
                if "run" in step:
                    raise WorkflowParseError(f"duplicate key 'run' in {step_label}")
                run_script, index = extract_run_block(lines, index)
                step["run"] = run_script
                continue
            if stripped.startswith("env:"):
                if "env" in step:
                    raise WorkflowParseError(f"duplicate key 'env' in {step_label}")
                env_block, index = extract_mapping_block(lines, index, f"{step_label} env")
                step["env"] = env_block
                continue
            forbid_yaml_features(stripped, step_label)
            parsed = parse_step_scalar(stripped)
            if parsed is not None:
                assign_unique(step, parsed[0], parsed[1], step_label)
            else:
                key = stripped.split(":", 1)[0].strip()
                if key:
                    assign_unique(step, key, extract_scalar(stripped), step_label)
            index += 1
        steps.append(step)
    return steps


def parse_release_job(content: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    job_match = re.search(
        r"^  release:\s*\n(.*?)(?=^  \S|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if not job_match:
        return None

    job_body = job_match.group(1)
    job_meta: dict[str, Any] = {}
    for line in job_body.splitlines():
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if indent != 4:
            continue
        stripped = line.strip()
        if stripped.startswith("steps:"):
            break
        if ":" not in stripped:
            continue
        forbid_yaml_features(stripped, "release job")
        key, _, value_part = stripped.partition(":")
        key = key.strip()
        value = value_part.strip()
        if value:
            assign_unique(job_meta, key, extract_scalar(stripped), "release job")
        else:
            assign_unique(job_meta, key, True, "release job")

    steps_match = re.search(
        r"^    steps:\s*\n(.*)",
        job_body,
        re.MULTILINE | re.DOTALL,
    )
    if not steps_match:
        return job_meta, []

    return job_meta, parse_steps_block(steps_match.group(1))


def parse_release_job_steps(content: str) -> list[dict[str, Any]]:
    parsed = parse_release_job(content)
    if parsed is None:
        return []
    return parsed[1]


def executable_lines(run_script: str) -> list[str]:
    lines: list[str] = []
    for line in run_script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == ":" or stripped.startswith(":"):
            continue
        lines.append(stripped)
    return lines


CRITICAL_STEP_NAMES = (
    PRE_STEP_NAME,
    VALIDATOR_STEP_NAME,
    PUBLISH_STEP_NAME,
    POST_STEP_NAME,
)


def count_named_steps(steps: list[dict[str, Any]], name: str) -> int:
    return sum(1 for step in steps if step.get("name") == name)


def find_named_step(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for step in steps:
        if step.get("name") == name:
            return step
    return None


def validate_unique_critical_steps(steps: list[dict[str, Any]]) -> tuple[bool, str]:
    for name in CRITICAL_STEP_NAMES:
        count = count_named_steps(steps, name)
        if count == 0:
            return False, f"missing step {name!r}"
        if count > 1:
            return False, f"step {name!r} must occur exactly once (found {count})"
    return True, "ok"


def validate_post_step_is_final(steps: list[dict[str, Any]], post_index: int) -> tuple[bool, str]:
    if post_index != len(steps) - 1:
        return False, f"step {POST_STEP_NAME!r} must be the final release job step"
    return True, "ok"


def step_index(steps: list[dict[str, Any]], name: str) -> int | None:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    return None


def validate_release_job(job_meta: dict[str, Any]) -> tuple[bool, str]:
    keys = set(job_meta.keys())
    extra = sorted(keys - RELEASE_JOB_ALLOWED_KEYS)
    if extra:
        return False, f"release job has forbidden keys: {', '.join(extra)}"
    if job_meta.get("runs-on") != RELEASE_JOB_RUNS_ON:
        return False, f"release job runs-on must be {RELEASE_JOB_RUNS_ON!r}"
    return True, "ok"


def validate_contract(run_script: str, expected: tuple[str, ...], label: str) -> tuple[bool, str]:
    expected_script = "\n".join(expected)
    if run_script != expected_script:
        return False, f"step {label!r} run script does not match contract"

    for pattern in FORBIDDEN_PATTERNS:
        if pattern in run_script:
            return False, f"forbidden pattern in {label}: {pattern}"

    if re.search(r'if\s+\[\s*"\$got"\s*=\s*"\$want"\s*\]', run_script):
        return False, f"weakened equality check in {label}"

    return True, "ok"


def validate_step_contract(step: dict[str, Any], expected: tuple[str, ...]) -> tuple[bool, str]:
    name = step.get("name", "<unnamed>")
    run_script = step.get("run")
    if not isinstance(run_script, str) or not run_script.strip():
        return False, f"step {name!r} has no run script"

    return validate_contract(run_script, expected, name)


def validate_step_keys(
    step: dict[str, Any],
    allowed_keys: frozenset[str],
    label: str,
) -> tuple[bool, str]:
    extra = sorted(set(step.keys()) - allowed_keys)
    if extra:
        return False, f"step {label!r} has forbidden keys: {', '.join(extra)}"
    return True, "ok"


def validate_token_env(step: dict[str, Any], step_name: str) -> tuple[bool, str]:
    env = step.get("env")
    if env is None:
        return False, (
            f"step {step_name!r} must have env with "
            "GH_TOKEN: ${{ github.token }}"
        )
    if not isinstance(env, dict):
        return False, f"step {step_name!r} env must be a mapping"
    if env != TOKEN_ENV_EXACT:
        return False, (
            f"step {step_name!r} env must be exactly "
            "GH_TOKEN: ${{ github.token }}"
        )
    return True, "ok"


def validate_publish_env(step: dict[str, Any]) -> tuple[bool, str]:
    return validate_token_env(step, PUBLISH_STEP_NAME)


def validate_post_env(step: dict[str, Any]) -> tuple[bool, str]:
    return validate_token_env(step, POST_STEP_NAME)


def validate_workflow(path: Path) -> tuple[bool, str]:
    try:
        content = path.read_text()
    except OSError as exc:
        return False, f"cannot read workflow: {exc}"

    try:
        parsed = parse_release_job(content)
    except WorkflowParseError as exc:
        return False, str(exc)

    if parsed is None:
        return False, f"missing {JOB_NAME!r} job"

    job_meta, steps = parsed
    ok, msg = validate_release_job(job_meta)
    if not ok:
        return False, msg

    if not steps:
        return False, f"missing {JOB_NAME!r} job steps"

    ok, msg = validate_unique_critical_steps(steps)
    if not ok:
        return False, msg

    pre_step = find_named_step(steps, PRE_STEP_NAME)
    validator_step = find_named_step(steps, VALIDATOR_STEP_NAME)
    publish_step = find_named_step(steps, PUBLISH_STEP_NAME)
    post_step = find_named_step(steps, POST_STEP_NAME)
    assert pre_step is not None
    assert validator_step is not None
    assert publish_step is not None
    assert post_step is not None

    pre_index = step_index(steps, PRE_STEP_NAME)
    validator_index = step_index(steps, VALIDATOR_STEP_NAME)
    publish_index = step_index(steps, PUBLISH_STEP_NAME)
    post_index = step_index(steps, POST_STEP_NAME)
    assert pre_index is not None
    assert validator_index is not None
    assert publish_index is not None
    assert post_index is not None

    if not (pre_index < validator_index < publish_index < post_index):
        return False, (
            f"step order must be {PRE_STEP_NAME!r}, then {VALIDATOR_STEP_NAME!r}, "
            f"then {PUBLISH_STEP_NAME!r}, then {POST_STEP_NAME!r}"
        )

    ok, msg = validate_post_step_is_final(steps, post_index)
    if not ok:
        return False, msg

    ok, msg = validate_step_keys(pre_step, PRE_ALLOWED_KEYS, PRE_STEP_NAME)
    if not ok:
        return False, msg

    ok, msg = validate_step_keys(validator_step, VALIDATOR_ALLOWED_KEYS, VALIDATOR_STEP_NAME)
    if not ok:
        return False, msg

    ok, msg = validate_step_keys(publish_step, PUBLISH_ALLOWED_KEYS, PUBLISH_STEP_NAME)
    if not ok:
        return False, msg

    ok, msg = validate_step_keys(post_step, POST_ALLOWED_KEYS, POST_STEP_NAME)
    if not ok:
        return False, msg

    ok, msg = validate_publish_env(publish_step)
    if not ok:
        return False, msg

    ok, msg = validate_post_env(post_step)
    if not ok:
        return False, msg

    ok, msg = validate_step_contract(pre_step, PRE_PUBLISH_BLOCK)
    if not ok:
        return False, msg

    ok, msg = validate_step_contract(validator_step, VALIDATOR_RUN)
    if not ok:
        return False, msg

    ok, msg = validate_step_contract(publish_step, PUBLISH_STEP)
    if not ok:
        return False, msg

    ok, msg = validate_step_contract(post_step, POST_PUBLISH_STEP)
    if not ok:
        return False, msg

    for step in steps:
        name = step.get("name")
        if name in {PRE_STEP_NAME, VALIDATOR_STEP_NAME, PUBLISH_STEP_NAME, POST_STEP_NAME}:
            continue
        other_run = step.get("run")
        if not isinstance(other_run, str):
            continue
        other_lines = executable_lines(other_run)
        for marker in CONTRACT_MARKERS:
            if marker in other_lines:
                return False, f"verification script outside named step ({name!r})"

    return True, "ok"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <workflow.yml>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    ok, msg = validate_workflow(path)
    if not ok:
        print(f"release workflow: {msg}", file=sys.stderr)
        return 1

    print("release workflow: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
