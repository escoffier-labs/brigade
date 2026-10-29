"""Supported claim boundaries preserve the original Markdown bytes and offsets."""

from __future__ import annotations

import hashlib

import pytest

from brigade import memory_proposal_v2_ops as delta, memory_proposals as legacy


@pytest.fixture(params=[("\n", False), ("\n", True), ("\r\n", False), ("\r\n", True)])
def markdown(request):
    newline, final = request.param

    def render(body):
        return (body.rstrip("\n") + ("\n" if final else "")).replace("\n", newline)

    return render


EXCLUDED = [
    "```text\nvalue ^c-abcdef\n```",
    "~~~text\nvalue ^c-abcdef\n~~~",
    "```text\nvalue ^c-abcdef",  # Unclosed fences remain excluded to EOF.
    "- ```text\n  value ^c-abcdef\n  ```",
    "- > quoted ^c-abcdef\n  lazy continuation ^c-fedcba",
    "-     code ^c-abcdef\n      continued ^c-fedcba",
    "1. Title ^c-abcdef\n   =",
    "- Title ^c-abcdef\n  -",
    "-\t> quoted ^c-abcdef\n\tlazy ^c-fedcba",
    "- ~~~text\n\n  value ^c-abcdef\n  ~~~",
    "    value ^c-abcdef\n    another line",
    "\tvalue ^c-abcdef",
    " \tvalue ^c-abcdef",
    "> quoted ^c-abcdef\nlazy continuation ^c-fedcba",
    '[docs]: https://example.com\n  "title ^c-abcdef"',
    '[docs]:\nhttps://example.com\n"title ^c-abcdef"',
    "Name | Value\n--- | ---\nvalue | literal ^c-abcdef",
    "Name | Value\n--- | ---\nvalue ^c-abcdef",
    "| Name | Value |\n| :--- | ---: |\n| value | literal ^c-abcdef |",
    "# Heading ^c-abcdef",
    "Heading ^c-abcdef\n-",
    "- Title ^c-abcdef\n  =",
    "1. Title ^c-abcdef\n   -",
    "previously old wording ^c-abcdef",
    "***",
    "<section>\nambiguous content ^c-abcdef\n</section>",
    "<!-- hidden comment\n\nliteral ^c-abcdef\n-->",
]


@pytest.mark.parametrize("marker", ["```", "~~~~"])
@pytest.mark.parametrize("false_close", ["{marker}not-a-close", "{other}", "{short}"])
def test_false_fence_close_keeps_code_excluded(markdown, marker, false_close):
    bad = false_close.format(marker=marker, other="~~~" if marker[0] == "`" else "```", short=marker[:-1])
    body = markdown(f"Before.\n\n{marker}text\ncode\n{bad}\nstill code\n{marker} \t\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["Before.", "After."]


@pytest.mark.parametrize("rule", ["---", "***", "___", "- - -", " * * * ", "_\t_\t_", "   ----"])
def test_thematic_break_variants_are_not_claims(markdown, rule):
    body = markdown(f"Before.\n\n{rule}\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["Before.", "After."]


@pytest.mark.parametrize("underline", ["-", "=", " -\t"])
def test_single_character_setext_is_heading(markdown, underline):
    body = markdown(f"Heading\ncontinued heading\n{underline}\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]


@pytest.mark.parametrize(
    "structure",
    [
        "> quoted text\nlazy continuation\ncontinued again",
        "- ```text\n\n  value\n  ```",
        "- Title\n  =",
        " \tvalue",
        "Name | Value\n--- | ---\nvalue",
        "<!-- hidden comment\n\nliteral\n-->",
    ],
)
def test_excluded_structure_leaves_adjacent_prose_addressable(markdown, structure):
    body = markdown(f"Before.\n\n{structure}\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["Before.", "After."]


@pytest.mark.parametrize(
    "reference",
    [
        '[docs]: https://example.com\n  "title"',
        '[docs]: https://example.com\n"multi line\ntitle"',
        '[docs]:\nhttps://example.com\n"title"',
    ],
)
def test_reference_definition_continuation_is_excluded(markdown, reference):
    body = markdown(f"{reference}\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]


@pytest.mark.parametrize("structure", EXCLUDED)
def test_claim_body_preserves_anchor_tokens_in_excluded_blocks(markdown, structure):
    body = markdown("Claim. ^c-123abc\n\n" + structure)
    assert delta.claim_body(body).encode() == body.replace("Claim. ^c-123abc", "Claim.", 1).encode()


def test_claim_offsets_match_original_text(markdown):
    body = markdown("---\ntopic: example\n---\n# Heading\n\nCafé claim.\ncontinued.\n\n- First.\n- Second.")
    newline = "\r\n" if "\r\n" in body else "\n"
    expected = [f"Café claim.{newline}continued.", "- First.", "- Second."]
    found = delta.blocks(body)
    assert [b.text for b in found] == expected
    for block, value in zip(found, expected, strict=True):
        assert (block.start, block.end) == (body.index(value), body.index(value) + len(value))
        assert body[block.start : block.end].encode() == value.encode()
        assert block.sha256 == hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.parametrize("structure", EXCLUDED)
def test_anchor_roundtrip_preserves_structure(markdown, structure):
    body = markdown("---\ntopic: example\n---\nClaim.\n\n" + structure)
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    assert delta.claim_body(anchored).encode() == body[legacy._body_offset(body) :].encode()
    assert [b.text.split(" ^c-")[0] for b in delta.blocks(anchored)] == ["Claim."]


@pytest.mark.parametrize("prefix,indent", [("- ", "  "), ("1. ", "   "), ("-\t", "\t"), ("  - ", "    ")])
@pytest.mark.parametrize(
    "structure",
    [
        "> quote ^c-abcdef\n{indent}lazy ^c-fedcba",
        "    code ^c-abcdef",
        "Title ^c-abcdef\n{indent}=",
        "Title ^c-abcdef\n{indent}-",
    ],
)
def test_nested_list_structure_uses_container_columns(markdown, prefix, indent, structure):
    nested = prefix + structure.format(indent=indent)
    body = markdown("- Parent.\n\n  " + nested.replace("\n", "\n  ") + "\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["- Parent.", "After."]
    assert delta.claim_body(body) == body


@pytest.mark.parametrize("marker", ["```", "~~~"])
def test_list_markers_inside_fence_cannot_close_it(markdown, marker):
    code = f"{marker}\n- {marker}\nstill code ^c-abcdef\n{marker}"
    body = markdown(f"Before.\n\n{code}\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["Before.", "After."]
    assert delta.claim_body(body) == body


def test_only_final_addressable_suffix_is_removed(markdown):
    body = markdown("First ^c-abcdef\ncontinued ^c-fedcba\n\n# Heading ^c-123abc")
    assert delta.claim_body(body) == body.replace("continued ^c-fedcba", "continued")


def test_plain_list_continuations_remain_addressable(markdown):
    body = markdown("- First\n  continuation\n  - Nested\n- Second\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == [
        "- First" + ("\r\n" if "\r\n" in body else "\n") + "  continuation",
        "  - Nested",
        "- Second",
        "After.",
    ]


def test_comment_content_cannot_establish_list_container(markdown):
    body = markdown("<!--\n- fake list\n-->\n\n    code ^c-abcdef\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]
    assert delta.claim_body(body) == body


def test_multiline_comment_in_paragraph_is_opaque(markdown):
    body = markdown("Text <!-- hidden\nliteral ^c-abcdef\n--> ^c-fedcba\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]
    assert delta.claim_body(body) == body


@pytest.mark.parametrize("opener", ["<section>", "previously old wording"])
def test_opaque_blocks_cannot_establish_list_container(markdown, opener):
    body = markdown(f"{opener}\n- literal ^c-abcdef\n</section>\n\n    code ^c-fedcba\n\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]
    assert delta.claim_body(body) == body


REVIEW_CASES = [
    pytest.param("- Restart steps:\n## Commands\n    code ^c-abcdef", ["- Restart steps:"], id="F1-heading"),
    pytest.param("- item\n```sh\nmake\n```\n    code ^c-abcdef", ["- item"], id="F1-fence"),
    pytest.param("- item\n<div>\n</div>\n\n    code ^c-abcdef", ["- item"], id="F1-html"),
    pytest.param("- item\n> quote\n\n    code ^c-abcdef", ["- item"], id="F1-quote"),
    pytest.param("- item\n[r]: /u\n\n    code ^c-abcdef", ["- item"], id="F1-reference"),
    pytest.param("1. num\n# Head\n    - deep ^c-abcdef", ["1. num"], id="F1-code-marker"),
    pytest.param("- Example:\n  ```\n  code\n```\n\ncode ^c-abcdef", ["- Example:"], id="F2-dedent-fence"),
    pytest.param("- ```\n~~~\n```\n- item ^c-abcdef", [], id="F2-other-fence"),
    pytest.param("- ```\n</div>\n  ```\ncode ^c-abcdef", [], id="F2-html"),
    pytest.param("Before.\n\n-\n\nAfter.", ["Before.", "After."], id="F3-empty"),
    pytest.param("-\n      code ^c-abcdef", [], id="F3-code"),
    pytest.param("lazy ^c-fedcba\n    code ^c-abcdef\n---", [], id="F4-setext"),
    pytest.param("a | b\n--- | ---\n[r]: /u\nPara one. ^c-abcdef", [], id="F5-table-reference"),
]


@pytest.mark.parametrize("source,expected", REVIEW_CASES)
def test_review_structure_lifetimes(markdown, source, expected):
    body = markdown(source)
    assert [b.text for b in delta.blocks(body)] == expected
    assert delta.claim_body(body).encode() == body.encode()
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    assert delta.claim_body(anchored).encode() == body.encode()
    assert all(b.block_id != "c-abcdef" for b in delta.blocks(body))
    if "^c-abcdef" in body:
        assert delta.claim_body(body) != delta.claim_body(body.replace("^c-abcdef", "^c-123abc"))


@pytest.mark.parametrize(
    "source,expected",
    [
        ("- First\nlazy ordinary continuation", ["- First\nlazy ordinary continuation"]),
        ("ordinary\n    continued", ["ordinary\n    continued"]),
        ("-\n\tOrdinary.", ["\tOrdinary."]),
        ("-\n  Ordinary.\n  - Nested.\n\nAfter.", ["  Ordinary.", "  - Nested.", "After."]),
        ("- item\n# H\n    code ^c-abcdef\n\nAfter.", ["- item", "After."]),
    ],
)
def test_review_positive_claims(markdown, source, expected):
    body = markdown(source)
    newline = "\r\n" if "\r\n" in body else "\n"
    assert [b.text for b in delta.blocks(body)] == [s.replace("\n", newline) for s in expected]
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    assert delta.claim_body(anchored) == body


@pytest.mark.parametrize("marker", ["-", "+", "*", "1.", "1)"])
def test_empty_list_item_blank_ends_container(markdown, marker):
    body = markdown(f"{marker}\n\n    code ^c-abcdef")
    assert delta.blocks(body) == []
    assert delta.claim_body(body) == body
    assert delta.anchor_text(body, "memory/cards/example.md") == body
    assert delta.claim_body(body) != delta.claim_body(body.replace("abcdef", "fedcba"))


@pytest.mark.parametrize(
    "source,expected",
    [
        ("-\n\n  Ordinary.", ["  Ordinary."]),
        ("- Parent.\n  +\n\n    Ordinary.", ["- Parent.", "    Ordinary."]),
        ("- Parent.\n\n    Ordinary.", ["- Parent.", "    Ordinary."]),
    ],
)
def test_empty_item_reset_preserves_content_bearing_containers(markdown, source, expected):
    body = markdown(source)
    assert [b.text for b in delta.blocks(body)] == expected


def test_previously_continuation_stays_in_complete_claim(markdown):
    body = markdown("The cache stays warm and\npreviously cold nodes recover.")
    assert [b.text for b in delta.blocks(body)] == [body.rstrip("\r\n")]
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.claim_body(anchored) == body
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    history = markdown("previously Old cache wording.\ncontinued history\n\nAfter.")
    assert [b.text for b in delta.blocks(history)] == ["After."]


@pytest.mark.parametrize(
    "opener,closer",
    [
        ("<pre>", "</pre>"),
        ("<SCRIPT type='example'>", "</SCRIPT>"),
        ("<style>", "</style>"),
        ("<textarea>", "</textarea>"),
        ("<?example", "?>"),
        ("<![CDATA[", "]]>"),
        ("<!DOCTYPE", ">"),
        ("<script><!--", "</script>"),
    ],
)
def test_delimited_html_preserves_blank_lines_and_literal_tokens(markdown, opener, closer):
    structure = f"{opener}\ncode\n\nmore ^c-abcdef\n{closer}"
    body = markdown(structure + "\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]
    assert delta.claim_body(body) == body
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.claim_body(anchored) == body
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    assert delta.claim_body(body) != delta.claim_body(body.replace("abcdef", "fedcba"))
    unclosed = markdown(f"{opener}\n\nmore ^c-abcdef")
    assert delta.blocks(unclosed) == []
    assert delta.claim_body(unclosed) == unclosed


def test_closed_html_on_one_line_leaves_next_claim_addressable(markdown):
    body = markdown("<pre>literal ^c-abcdef</pre>\nAfter.")
    assert [b.text for b in delta.blocks(body)] == ["After."]
    assert delta.claim_body(body) == body


@pytest.mark.parametrize(
    "opener,closer", [("<pre>", "</pre>"), ("<?example", "?>"), ("<![CDATA[", "]]>"), ("<!DOCTYPE", ">")]
)
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_list_raw_html_ends_at_nonblank_dedent(markdown, opener, closer, nested, closed):
    prefix, indent = ("- Parent.\n  - ", "    ") if nested else ("- ", "  ")
    structure = f"{prefix}{opener}\n{indent}literal ^c-abcdef\n\n"
    ending = f"\n\n<!-- {closer} -->\n" if closed else "\n"
    body = markdown(structure + "Outer claim. ^c-fedcba" + ending + "\nAfter.")
    expected = (["- Parent."] if nested else []) + ["Outer claim. ^c-fedcba", "After."]
    assert [b.text for b in delta.blocks(body)] == expected
    assert delta.claim_body(body) == body.replace("Outer claim. ^c-fedcba", "Outer claim.")
    anchored = delta.anchor_text(body, "memory/cards/example.md")
    assert delta.anchor_text(anchored, "memory/cards/example.md") == anchored
    assert delta.claim_body(anchored) == delta.claim_body(body)
    assert "literal ^c-abcdef" in delta.claim_body(anchored)
