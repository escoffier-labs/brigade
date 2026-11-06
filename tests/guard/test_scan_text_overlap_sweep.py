"""scan_text claims spans with a sorted sweep and must decide exactly like the pairwise check."""

from __future__ import annotations

import json
import random
from pathlib import Path
from time import perf_counter

import pytest

from brigade.guard import engine
from brigade.guard.engine import _SpanSet, _apply_edits, scan_text
from brigade.guard.policy import load_policy
from brigade.guard.types import TextEdit

_REPO = Path(__file__).resolve().parents[2]
_CATEGORIES = ["secret", "pii", "infrastructure", "tooling"]


def _reference_precedence(start, end, category, is_root_path, claimed, roots):
    """The pairwise precedence check scan_text used before the sweep, verbatim."""
    precedence_spans = [span for spans in claimed for span in zip(spans.starts, spans.ends, spans.tags, strict=True)]
    root_spans = list(zip(roots.starts, roots.ends, strict=True))
    return any(
        start < prev_end
        and end > prev_start
        and not (is_root_path and start <= prev_start and prev_end <= end)
        and not (
            category in {"secret", "pii"}
            and category != prev_category
            and any(
                root_start <= min(start, prev_start) and max(end, prev_end) <= root_end
                for root_start, root_end in root_spans
            )
        )
        for prev_start, prev_end, prev_category in precedence_spans
    )


def _reference_root(start, end, category, roots):
    """The pairwise root-span check scan_text used before the sweep, verbatim."""
    root_spans = list(zip(roots.starts, roots.ends, strict=True))
    return any(
        start < root_end
        and end > root_start
        and not (category in {"secret", "pii"} and root_start <= start and end <= root_end)
        for root_start, root_end in root_spans
    )


def _reference_apply_edits(text, edits):
    """The per-edit rebuild _apply_edits used before the join, verbatim."""
    result = text
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


def _random_spans(rng: random.Random, ordered: bool) -> list[tuple[int, int]]:
    if ordered:
        spans, cursor = [], rng.randint(0, 10)
        while cursor < 220:
            start = cursor + rng.randint(0, 12)
            end = start + rng.randint(1, 25)
            spans.append((start, end))
            cursor = end + rng.randint(0, 3)
        return spans
    spans = []
    for _ in range(rng.randint(5, 40)):
        start = rng.randint(0, 220)
        spans.append((start, start + rng.randint(1, 30)))
    return spans


def test_sweep_matches_pairwise_check_on_random_span_streams():
    seen = {"blocked": 0, "kept": 0, "unordered_sets": 0, "enclosed": 0}
    for seed in range(300):
        rng = random.Random(seed)
        roots = _SpanSet()
        occupied: list[_SpanSet] = []
        for _ in range(rng.randint(2, 8)):
            category = rng.choice(_CATEGORIES)
            is_root_path = category == "infrastructure" and rng.random() < 0.6
            snapshot = rng.random() < 0.2  # The private-key rule ignores its own earlier spans.
            claimed = _SpanSet()
            sets = tuple(occupied) if snapshot else (*occupied, claimed)
            for start, end in _random_spans(rng, ordered=rng.random() < 0.7):
                got = (
                    engine._precedence_blocks(start, end, category, is_root_path, sets, roots),
                    engine._root_blocks(start, end, category, roots),
                )
                want = (
                    _reference_precedence(start, end, category, is_root_path, sets, roots),
                    _reference_root(start, end, category, roots),
                )
                assert got == want, (seed, start, end, category, is_root_path)
                if any(got):
                    seen["blocked"] += 1
                    continue
                seen["kept"] += 1
                if is_root_path:
                    roots.add(start, end, category)
                else:
                    claimed.add(start, end, category)
            if claimed:
                occupied.append(claimed)
                seen["unordered_sets"] += not claimed.ordered
        for probe in range(60):
            start = rng.randint(0, 200)
            end = start + rng.randint(0, 40)
            expected = any(
                root_start <= start and end <= root_end
                for root_start, root_end in zip(roots.starts, roots.ends, strict=True)
            )
            assert roots.encloses(start, end) == expected, (seed, probe)
            seen["enclosed"] += expected
    # The stream must exercise both outcomes, the ordered fast path and the fallback.
    assert all(seen.values()), seen


def _reference_engine(monkeypatch):
    monkeypatch.setattr(engine, "_precedence_blocks", _reference_precedence)
    monkeypatch.setattr(engine, "_root_blocks", _reference_root)
    monkeypatch.setattr(engine, "_apply_edits", _reference_apply_edits)


def _fixture_leaves(value, found: list[str]) -> None:
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        if value and all(isinstance(item, str) for item in value):
            found.append("".join(value))
        for item in value:
            _fixture_leaves(item, found)
    elif isinstance(value, dict):
        for item in value.values():
            _fixture_leaves(item, found)


def _guard_corpora() -> list[str]:
    texts: list[str] = []
    roots = [_REPO / "tests/guard/fixtures", _REPO / "src/brigade/guard/examples"]
    for root in roots:
        texts.extend(path.read_text() for path in sorted(root.rglob("*")) if path.is_file())
    for path in sorted((_REPO / "tests/fixtures").glob("memory-scan-*.json")):
        leaves: list[str] = []
        _fixture_leaves(json.loads(path.read_text()), leaves)
        texts.extend(leaf for leaf in leaves if len(leaf) < 20_000)
    texts.append("\n".join(texts))
    return texts


def _snippets() -> list[str]:
    home = "/".join(("", "home", "fixture-user", "repo"))
    github = "ghp_" + "A" * 36
    email = "user" + "@" + "fixture-provider.com"
    phone = "202" + "-555-" + "0137"
    return [
        email,
        github + "@" + "fixture-provider.com",
        "Bearer " + "A" * 20 + "/" + github,
        "xoxb-" + phone + "-" + "A" * 20,
        phone,
        home + "/src/file.py",
        "-----BEGIN " + "PRIVATE KEY-----\n" + "a" * 64 + "\n",
        "-----BEGIN " + "PRIVATE KEY-----\n",
        "local" + "host:8080",
        "api_key = " + "k" * 24,
        "plain words",
        "\n",
        " ",
    ]


def _random_documents() -> list[str]:
    snippets = _snippets()
    documents = []
    for seed in range(60):
        rng = random.Random(seed)
        documents.append("".join(rng.choice(snippets) for _ in range(rng.randint(5, 80))))
    return documents


@pytest.mark.parametrize("policy_name", [None, "public-repo", "pr-draft"])
def test_scan_text_output_is_identical_to_the_pairwise_check(policy_name, monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    policy = load_policy(_REPO / f"src/brigade/guard/policies/{policy_name}.json") if policy_name else None
    documents = _guard_corpora() + _random_documents()
    swept = [scan_text(text, policy=policy) for text in documents]
    assert sum(len(result.findings) for result in swept) > 200
    with monkeypatch.context() as patched:
        _reference_engine(patched)
        pairwise = [scan_text(text, policy=policy) for text in documents]
    assert swept == pairwise


def test_apply_edits_join_matches_per_edit_rebuild():
    rng = random.Random(2310)
    text = "".join(rng.choice("abcdefgh \n") for _ in range(400))
    joined = rebuilt = 0
    for _ in range(400):
        edits = []
        for start, end in _random_spans(rng, ordered=rng.random() < 0.6):
            end = min(end, len(text)) if rng.random() < 0.9 else start  # Some zero-width edits.
            edits.append(TextEdit(min(start, len(text)), end, rng.choice(["[x]", "", "<longer-replacement>"])))
        rng.shuffle(edits)
        assert _apply_edits(text, edits) == _reference_apply_edits(text, edits)
        ordered = sorted(edits, key=lambda item: item.start)
        disjoint = all(e.start < e.end for e in ordered) and all(
            a.end <= b.start for a, b in zip(ordered, ordered[1:], strict=False)
        )
        joined += disjoint
        rebuilt += not disjoint
    assert joined and rebuilt


def _layout(name: str, count: int) -> str:
    if name == "emails":
        return "".join(f"user{i}@fixture-provider.com\n" for i in range(count))
    home = "/".join(("", "home"))
    return "".join(
        f"user{i}@fixture-provider.com {home}/user{i}/repo ghp_{i:036d} 202-555-{i % 10000:04d}\n" for i in range(count)
    )


def _best_of_three(text: str) -> tuple[float, int]:
    best, findings = float("inf"), 0
    for _ in range(3):
        start = perf_counter()
        findings = len(scan_text(text).findings)
        best = min(best, perf_counter() - start)
    return best, findings


@pytest.mark.parametrize("layout", ["emails", "mixed"])
def test_scan_text_time_scales_linearly_in_findings(layout, monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    small_text, large_text = _layout(layout, 1000), _layout(layout, 8000)
    scan_text(small_text)  # Warm caches before comparing sizes.
    small, small_findings = _best_of_three(small_text)
    large, large_findings = _best_of_three(large_text)
    assert large_findings >= 8 * small_findings * 0.99, (small_findings, large_findings)
    ratio = large / small
    print(layout, small_findings, large_findings, round(small, 4), round(large, 4), round(ratio, 2))
    # An 8x step in findings is about 8x when linear and about 64x when quadratic.
    # 24 sits near their geometric midpoint, as in test_overbound_secrets.
    assert ratio < 24, (layout, small, large, ratio)
