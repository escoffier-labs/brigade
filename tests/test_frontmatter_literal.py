"""Bounded literal evaluation of front matter values (#2415)."""

from __future__ import annotations

import functools
import time
from typing import Callable

import pytest

from brigade import card_fingerprint, memory_cmd
from brigade.frontmatter_literal import LITERAL_MAX_CHARS, LITERAL_MAX_DEPTH, bounded_literal_list

SMALL = LITERAL_MAX_CHARS * 2
LARGE = SMALL * 16


def _parsers() -> list[Callable[[str], tuple[dict, bool]]]:
    return [memory_cmd._parse_frontmatter, card_fingerprint._parse_frontmatter_lines]


def _card(value: str) -> str:
    return f"---\nid: demo\nnotes: {value}\n---\nbody\n"


def _flat(items: int) -> str:
    return "[" + ", ".join(["'x'"] * items) + "]"


def _nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def _numeric(digits: int) -> str:
    return "[" + "9" * digits + "]"


def _unary(count: int) -> str:
    return "[" + "-" * count + "1]"


def _best_of(fn: Callable[[], object], runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _assert_cost_does_not_scale(build: Callable[[int], str]) -> None:
    """Cost of a hostile value must not grow with its size (an unbounded parse is linear or worse)."""
    small_card, large_card = _card(build(SMALL)), _card(build(LARGE))
    for parse in _parsers():
        t_small = max(_best_of(functools.partial(parse, small_card)), 1e-4)
        t_large = _best_of(functools.partial(parse, large_card))
        # A 16x larger value: linear work would be ~16x, bounded work stays near 1x.
        assert t_large / t_small < 6, (parse.__qualname__, t_small, t_large)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda n: _flat(n // 4), id="oversized-flat"),
        pytest.param(lambda n: _nested(n // 2), id="deeply-nested"),
        pytest.param(_numeric, id="pathological-numeric"),
        pytest.param(_unary, id="unary-chain"),
    ],
)
def test_hostile_value_cost_does_not_scale_and_stays_text(build: Callable[[int], str]) -> None:
    _assert_cost_does_not_scale(build)
    value = build(LARGE)
    for parse in _parsers():
        data, closed = parse(_card(value))
        assert closed is True
        assert data["notes"] == value
        assert data["id"] == "demo"


def test_nesting_just_over_the_cap_is_text_and_at_the_cap_still_parses() -> None:
    at_cap = _nested(LITERAL_MAX_DEPTH)
    assert bounded_literal_list(at_cap) == eval(at_cap)  # noqa: S307 - fixed bracket literal
    over = _nested(LITERAL_MAX_DEPTH + 1)
    assert bounded_literal_list(over) == over


def test_length_just_over_the_cap_is_text() -> None:
    over = "['" + "x" * LITERAL_MAX_CHARS + "']"
    assert bounded_literal_list(over) == over
    under = "['" + "x" * (LITERAL_MAX_CHARS - 4) + "']"
    assert bounded_literal_list(under) == ["x" * (LITERAL_MAX_CHARS - 4)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("[]", []),
        ("['a', 'b']", ["a", "b"]),
        ('["a", "b"]', ["a", "b"]),
        ("['a[[[[[[b', 'c]]]]']", ["a[[[[[[b", "c]]]]"]),
        ("[1, 2.5, True, None]", [1, 2.5, True, None]),
        ("[['a'], ['b', 'c']]", [["a"], ["b", "c"]]),
        ("[memory, handoff]", "[memory, handoff]"),
        ("[unterminated", "[unterminated"),
    ],
)
def test_normal_values_parse_as_before(value: str, expected: object) -> None:
    assert bounded_literal_list(value) == expected
    for parse in _parsers():
        data, _ = parse(_card(value))
        assert data["notes"] == expected


def test_evaluator_failures_fall_back_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import ast

    for error in (MemoryError, RecursionError, TypeError, OverflowError, SyntaxError, ValueError):

        def boom(_value: str, error: type[BaseException] = error) -> object:
            raise error("boom")

        monkeypatch.setattr(ast, "literal_eval", boom)
        assert bounded_literal_list("['a']") == "['a']"


def test_moderately_deep_nesting_stays_text_in_every_parser() -> None:
    """Under the tokenizer's own limit, so the old code evaluated it; the bound now keeps it text."""
    value = _nested(LITERAL_MAX_DEPTH * 3)
    for parse in _parsers():
        data, _ = parse(_card(value))
        assert data["notes"] == value
