from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import pytest

from brigade.guard.engine import _allow_comments_by_line, _allowed_by

# Entries must be exact matched strings, never addresses or model-number ranges.
EXACT_VALUE_ALLOW_LIST: frozenset[str] = frozenset()

_IPV4_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_TAILNET_HOSTNAME_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,2}ts\.net(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)
_CPU_MODEL_RE = re.compile(
    r"\b(?:"
    r"Intel\s+Core\s+(?:i[3579](?:[- ]\s*)?\d+|Ultra\s+(?:[3579]\s+)?\d{3,}[A-Za-z0-9]*)"
    r"|Xeon(?:\s+[A-Za-z]+)*\s+[A-Za-z]*\d[A-Za-z0-9-]*"
    r"|AMD\s+Ryzen\s+\d\s+\d{4}[A-Za-z0-9]*"
    r"|Threadripper\s+\d{4}[A-Za-z0-9]*"
    r"|EPYC\s+\d{4}[A-Za-z0-9]*"
    r"|Apple\s+M[1-5](?:\s+(?:Pro|Max|Ultra))?"
    r")\b",
    re.IGNORECASE,
)
_KERNEL_BUILD_RE = re.compile(
    r"(?<![\d.])\d+\.\d+\.\d+-\d+-(?:generic|pve|lowlatency|aws|azure|gcp)\b",
    re.IGNORECASE,
)
_RAM_SIZE_RE = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?\s?(?:G(?:i)?B\s+(?:of\s+)?RAM|GB\s+DDR[45])\b",
    re.IGNORECASE,
)


def matching_host_details(text: str, exact_values: Iterable[str] = EXACT_VALUE_ALLOW_LIST) -> list[tuple[str, str]]:
    """Return rule ids and exact matches for bounded public-host details."""
    allowed = set(exact_values)
    matches: list[tuple[str, str]] = []
    for candidate in _IPV4_CANDIDATE_RE.finditer(text):
        value = candidate.group()
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        if address in ipaddress.IPv4Network("100.64.0.0/10") and value not in allowed:
            matches.append(("tailnet-ipv4", value))
    for rule_id, pattern in (
        ("tailnet-hostname", _TAILNET_HOSTNAME_RE),
        ("cpu-model", _CPU_MODEL_RE),
        ("kernel-build", _KERNEL_BUILD_RE),
        ("ram-size", _RAM_SIZE_RE),
    ):
        matches.extend((rule_id, match.group()) for match in pattern.finditer(text) if match.group() not in allowed)
    return matches


def _is_scanned_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts and (parts[0] in {"docs", "benchmarks"} or (parts[0] == "engines" and "bench" in parts[2:])))


def findings_for_text(text: str) -> list[tuple[int, str]]:
    """Return (line number, rule id) pairs that no content-guard allow comment exempts."""
    candidate_lines = [
        (line_number, matches)
        for line_number, line in enumerate(text.splitlines(), 1)
        if (matches := matching_host_details(line))
    ]
    if not candidate_lines:
        return []
    allow_by_line, file_allows, _ignored = _allow_comments_by_line(text)
    findings: list[tuple[int, str]] = []
    for line_number, matches in candidate_lines:
        for rule_id, _value in matches:
            if _allowed_by(rule_id, "public-host-details", line_number, allow_by_line, file_allows) is None:
                findings.append((line_number, rule_id))
    return findings


def test_public_docs_and_benchmarks_exclude_host_details() -> None:
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = Path(root_result.stdout.strip())
        if not root.is_dir():
            pytest.skip("repository root is unavailable")
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git or repository root is unavailable")

    findings: list[str] = []
    for raw_path in tracked:
        if not raw_path:
            continue
        try:
            relative = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not _is_scanned_path(relative):
            continue
        data = (root / relative).read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        file_findings = findings_for_text(text)
        if not file_findings:
            continue
        findings.extend(f"{relative}:{line_number} {rule_id}" for line_number, rule_id in file_findings)

    assert not findings, "public host details found:\n" + "\n".join(findings)


@pytest.mark.parametrize(
    ("value", "rule_id"),
    [
        ("100.99.12.34", "tailnet-ipv4"),
        ("demo-box.example-tailnet.ts.net", "tailnet-hostname"),
        ("Intel Core Ultra 7 265K", "cpu-model"),
        ("6.8.0-45-generic", "kernel-build"),
        ("32 GB DDR5", "ram-size"),
        ("32GB DDR5", "ram-size"),
        ("64GB RAM", "ram-size"),
    ],
)
def test_matching_host_details_positive(value: str, rule_id: str) -> None:
    assert matching_host_details(value) == [(rule_id, value)]


@pytest.mark.parametrize(
    "value",
    ["1.4 GB of memory", "16 cores", "100.63.255.1", "10.0.0.1", "1100.64.0.1"],  # content-guard: allow private-ipv4
)
def test_matching_host_details_negative(value: str) -> None:
    assert matching_host_details(value) == []


def test_matching_host_details_honors_exact_value_list() -> None:
    value = "100.100.20.30"
    assert matching_host_details(value, {value}) == []


def test_findings_for_text_honors_inline_allow_comment() -> None:
    text = "tailnet host: 100.99.12.34 <!-- content-guard: allow tailnet-ipv4 -->\n"
    assert matching_host_details(text) == [("tailnet-ipv4", "100.99.12.34")]
    assert findings_for_text(text) == []


def test_findings_for_text_keeps_finding_under_other_rule_allow_comment() -> None:
    text = "tailnet host: 100.99.12.34 <!-- content-guard: allow ram-size -->\n"
    assert matching_host_details(text) == [("tailnet-ipv4", "100.99.12.34")]
    assert findings_for_text(text) == [(1, "tailnet-ipv4")]
