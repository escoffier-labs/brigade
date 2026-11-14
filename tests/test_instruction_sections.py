"""The instruction section parser: units, pins, pointer lines, managed blocks and fences (matrix S and P rows)."""

from __future__ import annotations

import random

from brigade.instruction_sections import (
    KEEP,
    PointerSpan,
    heading_lines,
    managed_blocks,
    parse,
    pointer_spans,
    whole_section_count,
)

POINTER = (
    b"- Moved to memory 2026-11-14: memory/cards/instructions/user-md--a.md"
    b" (undo: 20261114T061500Z-instructions-1a2b3c4d)\n"
)


def test_h1_text_is_fixed_and_level_two_sections_are_units():
    doc = parse(b"# User\n\nintro\n\n## A\nbody\n\n### A1\nsub\n\n## B\nbody\n")
    assert [u.heading for u in doc.units] == [b"## A", b"## B"]
    assert doc.units[0].headings == (b"## A", b"### A1")
    assert doc.data[: doc.fixed_end] == b"# User\n\nintro\n\n"


def test_fences_and_setext_are_body_text():
    doc = parse(b"## A\n```\n## not a heading\n```\nText\n====\n## B\n")
    assert [u.heading for u in doc.units] == [b"## A", b"## B"]
    assert doc.eof_in_fence is False and parse(b"## A\n```\ncode\n").eof_in_fence is True


def test_pins_under_any_heading_of_the_unit():
    doc = parse(f"## A\n{KEEP}\nx\n## B\n### B1\n<!-- brigade:keep until=2026-12-14 -->\ny\n## C\nz\n".encode())
    assert [u.pin for u in doc.units] == ["marker", "until:2026-12-14", None]


def test_unbalanced_markers_stop_the_file():
    assert parse(b"## A\n<!-- TOOL:START -->\nx\n## B\n").problem == "unbalanced-markers"
    assert parse(b"## A\n<!-- TOOL:START -->\nx\n<!-- TOOL:END -->\n## B\n").units[0].blocked == "managed-block"


def test_trailing_pointer_run_belongs_to_no_unit():
    doc = parse(b"## A\nbody\n\n" + POINTER + b"\n## B\nbody\n" + POINTER)
    assert doc.data[doc.units[0].start : doc.units[0].end] == b"## A\nbody\n\n"
    assert doc.units[1].end == len(b"## A\nbody\n\n" + POINTER + b"\n## B\nbody\n")
    assert [u.blocked for u in doc.units] == [None, None]


def test_pointer_line_in_a_body_blocks_the_unit():  # matrix P4
    assert parse(b"## A\n" + POINTER + b"more text\n").units[0].blocked == "pointer"


def test_links_classification():
    doc = parse(b"## R\n- [a](x.md)\n- `memory/cards/b.md`\nplain\n## P\none\ntwo\n")
    assert [u.links for u in doc.units] == [True, False]


def test_not_utf8_has_no_units():
    doc = parse(b"## A\n\xff\n")
    assert (doc.problem, doc.units) == ("not-utf8", ())


def test_pointer_spans_find_only_exact_lines():
    data = b"x\n" + POINTER + b"- Moved to memory soon: nowhere\n"
    assert pointer_spans(data) == (PointerSpan(2, 2 + len(POINTER), False),)


def test_pointer_spans_mark_fences_and_managed_blocks():  # matrix P17
    data = b"## A\n```\n" + POINTER + b"```\n## B\n<!-- X:START -->\n" + POINTER + b"<!-- X:END -->\n"
    assert [span.in_block for span in pointer_spans(data)] == [True, True]


def test_marker_rule_body_text_and_unbalanced():  # matrix S19
    assert parse(b"## A\n<!-- END of notes -->\n<!-- begin draft -->\nx <!-- TOOL:START --> y\n").problem is None
    assert parse(b"## A\n<!-- foo:start -->\nx\n").problem == "unbalanced-markers"


def test_managed_block_pairing_by_name():
    integration = b"<!-- BEGIN BRIGADE INTEGRATION v:1 profile:full hash:ab -->\n<!-- END BRIGADE INTEGRATION -->\n"
    assert parse(b"## A\n" + integration).problem is None
    assert parse(b"## A\n<!-- brigade:user-profile:start -->\n<!-- brigade:user-profile:end -->\n").problem is None
    assert (
        parse(b"## A\n<!-- A:START -->\n<!-- B:START -->\n<!-- A:END -->\n<!-- B:END -->\n").problem
        == "unbalanced-markers"
    )


def test_managed_block_rule_edges():
    # An end before its start, a second start of an open name and a bad BEGIN BRIGADE line all stop the file.
    assert parse(b"## A\n<!-- X:END -->\n<!-- X:START -->\n").problem == "unbalanced-markers"
    assert parse(b"## A\n<!-- X:START -->\n<!-- X:START -->\n<!-- X:END -->\n").problem == "unbalanced-markers"
    assert parse(b"## A\n<!-- BEGIN BRIGADE -->\n").problem == "unbalanced-markers"
    # Different names nest when each closes inside the other's span, and a heading inside a block is not one.
    nested = b"## A\n<!-- A:START -->\n<!-- B:START -->\n## inside\n<!-- B:END -->\n<!-- A:END -->\n## C\n"
    doc = parse(nested)
    assert [u.heading for u in doc.units] == [b"## A", b"## C"]
    # A marker inside a fence is body text, so the fence's own text never unbalances the file.
    assert parse(b"## A\n```\n<!-- X:START -->\n```\n").problem is None


def test_whole_section_count_ignores_substrings_and_fences():  # matrix S18
    section = b"## A\nbody\n"
    assert whole_section_count(parse(b"# T\n\n" + section + b"## B\nx\n"), section) == 1
    assert whole_section_count(parse(b"## Z\n" + section.replace(b"## ", b"text ")), section) == 0
    assert whole_section_count(parse(b"## Z\n```\n" + section + b"```\n"), section) == 0
    assert whole_section_count(parse(section + b"more body\n"), section) == 0


def test_whole_section_count_spans_touching_units_only():
    one, two = b"## A\nx\n", b"## B\ny\n"
    assert whole_section_count(parse(one + two + b"## C\nz\n"), one + two) == 1
    # A pointer between two units is a gap, so the pair no longer matches as one body.
    assert whole_section_count(parse(one + POINTER + two), one + two) == 0
    assert whole_section_count(parse(one + two), b"") == 0


def test_pointer_in_a_fence_is_not_a_trailing_run():
    doc = parse(b"## A\n```\n" + POINTER)
    assert doc.eof_in_fence is True
    assert (doc.units[0].end, doc.units[0].blocked) == (len(doc.data), "pointer")


def test_invalid_keep_until_date_is_a_permanent_pin():
    doc = parse(b"## A\n<!-- brigade:keep until=2026-13-45 -->\nx\n")
    assert doc.units[0].pin == "marker"


def test_unit_digest_and_span_cover_the_unit_bytes():
    import hashlib

    doc = parse(b"## A\nx\n## B\ny\n")
    assert doc.units[0].sha256 == hashlib.sha256(b"## A\nx\n").hexdigest()
    assert (doc.units[0].start, doc.units[0].end, doc.units[1].start) == (0, 7, 7)


def test_first_heading_levels_and_later_level_one_unit():
    doc = parse(b"# T\n### x\nbody\n## y\nbody\n### z\n# Next\nq\n")
    assert [u.heading for u in doc.units] == [b"### x", b"## y", b"# Next"]
    assert doc.units[1].headings == (b"## y", b"### z")
    assert parse(b"# Only a title\nbody\n").units == ()


def test_parse_never_raises_and_units_stay_ordered_and_disjoint():
    pieces = [
        b"## A\n",
        b"### s\n",
        b"# T\n",
        b"```\n",
        b"~~~\n",
        b"<!-- X:START -->\n",
        b"<!-- X:END -->\n",
        POINTER,
        b"\n",
        b"text\n",
        KEEP.encode() + b"\n",
        b"\xff\xfe",
        b"- [l](m.md)\n",
        b"no newline",
    ]
    for seed in range(200):
        rng = random.Random(seed)
        data = b"".join(rng.choice(pieces) for _ in range(rng.randrange(0, 12)))
        doc = parse(data)
        previous_end = doc.fixed_end
        assert 0 <= doc.fixed_end <= len(data)
        for unit in doc.units:
            assert previous_end <= unit.start < unit.end <= len(data)
            previous_end = unit.end
        pointer_spans(data)


def test_heading_lines_skip_fences_and_managed_blocks():
    data = b"# T\n## A\n```\n## no\n```\n<!-- X:START -->\n## no\n<!-- X:END -->\n### B \n"
    assert heading_lines(data) == (b"# T", b"## A", b"### B ")


def test_managed_blocks_are_the_outermost_marker_to_marker_spans():
    inner = b"<!-- B:START -->\nb\n<!-- B:END -->\n"
    block = b"<!-- A:START -->\na\n" + inner + b"<!-- A:END -->\n"
    other = b"<!-- BEGIN BRIGADE X v:1 profile:full hash:ab -->\nx\n<!-- END BRIGADE X -->\n"
    assert managed_blocks(b"## A\n" + block + b"text\n" + other) == (block, other)
    assert managed_blocks(b"## A\ntext\n") == ()
