from __future__ import annotations

import ipaddress
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import pytest

# Reuse the engine's private allow-comment parser rather than re-implementing
# marker semantics. This is stricter than the guard's own whole-line skip: an
# allow token clears only the rule id it names, so sibling findings still fail.
from brigade.guard.engine import _allow_comments_by_line, _allowed_by

# Entries must be exact matched strings, never addresses or model-number ranges.
EXACT_VALUE_ALLOW_LIST: frozenset[str] = frozenset()

REPO_ROOT = Path(__file__).resolve().parents[1]

_IPV4_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_TAILNET_HOSTNAME_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,2}ts\.net(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)
_CPU_MODEL_RE = re.compile(
    r"\b(?:"
    r"Intel\s+Core\s+(?:i[3579](?:-\s*|\s+)?\d+[A-Za-z0-9]*|Ultra\s+(?:[3579]\s+)?\d{3,}[A-Za-z0-9]*)"
    r"|Xeon(?:\s+(?!Xeon\b)[A-Za-z]+){0,3}\s+[A-Za-z]*\d[A-Za-z0-9-]*"
    r"|AMD\s+Ryzen\s+\d\s+\d{4}[A-Za-z0-9]*"
    r"|Threadripper(?:\s+PRO)?\s+\d{4}[A-Za-z0-9]*"
    r"|EPYC\s+\d{4}[A-Za-z0-9]*"
    r"|Apple\s+M[1-5](?:\s+(?:Pro|Max|Ultra))?"
    r")\b",
    re.IGNORECASE,
)
_KERNEL_BUILD_RE = re.compile(
    r"(?<![\d.])(?:"
    r"\d+\.\d+\.\d+-\d+-(?:generic|pve|lowlatency|aws|azure|gcp|amd64|arm64|cloud-amd64)"
    r"|\d+\.\d+\.\d+-arch\d+-\d+"
    r")\b",
    re.IGNORECASE,
)
_RAM_SIZE_RE = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?\s?(?:G(?:i)?B\s+(?:of\s+)?RAM|GB\s+DDR[45])\b",
    re.IGNORECASE,
)

_NUL_CHECK_BYTES = 8 * 1024
_NON_FILE_GIT_MODES = frozenset({b"120000", b"160000"})
_SCANNED_ROOTS = frozenset({"docs", "benchmarks"})


def matching_host_details(text: str, exact_values: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """Return rule ids and exact matches for bounded public-host details."""
    allowed = set(EXACT_VALUE_ALLOW_LIST if exact_values is None else exact_values)
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
    return bool(parts and (parts[0] in _SCANNED_ROOTS or (parts[0] == "engines" and "bench" in parts[1:])))


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


def _tracked_regular_files(root: Path) -> list[str]:
    """Return repo-relative paths of tracked regular files under `root`."""
    staged = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    paths: list[str] = []
    for record in staged.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator or not raw_path:
            continue
        mode = metadata.split(b" ", 1)[0]
        # Gitlinks (submodules) and symlinks are not regular file contents.
        if mode in _NON_FILE_GIT_MODES:
            continue
        paths.append(os.fsdecode(raw_path))
    return paths


def test_public_docs_and_benchmarks_exclude_host_details() -> None:
    root = REPO_ROOT
    try:
        tracked = _tracked_regular_files(root)
    except (OSError, subprocess.CalledProcessError) as error:
        # Inside a checkout (or on CI) a git failure is a real problem; only an
        # unreachable git outside both may skip.
        if (root / ".git").exists() or os.environ.get("CI"):
            raise AssertionError(f"git repository scan failed: {error}") from error
        pytest.skip(f"git repository root is unavailable: {error}")

    findings: list[str] = []
    for relative in tracked:
        if not _is_scanned_path(relative):
            continue
        absolute = root / relative
        try:
            if not stat.S_ISREG(absolute.lstat().st_mode):
                continue
            with absolute.open("rb") as handle:
                head = handle.read(_NUL_CHECK_BYTES)
                if b"\0" in head:
                    continue
                data = head + handle.read()
        except OSError:
            # A tracked-but-deleted path (or an unreadable one) is not a file.
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
    ("path", "scanned"),
    [
        ("docs/a.md", True),
        ("benchmarks/x", True),
        ("engines/hub/bench/x", True),
        ("engines/bench/x", True),
        ("engines/hub/benchmarks/x", False),
        ("src/bench/x", False),
    ],
)
def test_is_scanned_path(path: str, scanned: bool) -> None:
    assert _is_scanned_path(path) is scanned


@pytest.mark.parametrize(
    ("value", "rule_id"),
    [
        ("100.99.12.34", "tailnet-ipv4"),
        ("demo-box.example-tailnet.ts.net", "tailnet-hostname"),
        ("Intel Core Ultra 7 265K", "cpu-model"),
        ("Intel Core i7-12700K", "cpu-model"),
        ("Intel Core i9-14900KS", "cpu-model"),
        ("Intel Core i5-1240P", "cpu-model"),
        ("Xeon Gold 6248R", "cpu-model"),
        ("Threadripper PRO 5995WX", "cpu-model"),
        ("6.8.0-45-generic", "kernel-build"),
        ("6.1.0-18-amd64", "kernel-build"),
        ("6.1.0-18-arm64", "kernel-build"),
        ("6.1.0-18-cloud-amd64", "kernel-build"),
        ("6.12.1-arch1-1", "kernel-build"),
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


def test_matching_host_details_negative_xeon_prose() -> None:
    assert matching_host_details("Xeon servers are fast") == []


def test_xeon_pattern_scans_repeated_tokens_quickly() -> None:
    text = ("Xeon " * 4000) + "!"
    started = time.perf_counter()
    matching_host_details(text)
    assert time.perf_counter() - started < 0.2


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
