"""Python-generated goldens for the hub's claim-block classification.

The hub addresses claims with `memory::proposal_blocks::spans`. The local
reflection path may only propose and count a card as anchored when it reads the
same block bytes, so this script records the spans of one fixture set from the
single Python port in `brigade.memory_claim_blocks`. The hub's own test asserts
the same bytes back, and the Python test asserts the reflection classifier
agrees, so neither side can drift without failing.

Blocks are recorded as the exact text inside each span, final newline excluded.

Run from the repository root with `PYTHONPATH=src`:

    python engines/hub/tests/anchor_classification_golden.py --write
    python engines/hub/tests/anchor_classification_golden.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from brigade import memory_claim_blocks

HUB = Path(__file__).resolve().parents[1]
GOLDEN = HUB / "tests" / "anchor-classification-golden.json"

# Every case names one Markdown shape the classifier must agree on: inline code,
# fences, ordered and unordered lists, nested lists, quotes, headings, tables,
# front matter, CRLF and the block edges a suffix may sit on.
CASES = [
    ("inline-code", "Use `pytest` to test.\n\nPlain claim.\n"),
    ("inline-code-only", "Use `pytest` to test.\n"),
    ("paragraphs", "One.\ncontinued.\n\nTwo.\n"),
    ("fenced-code", "Before.\n\n```\nhidden ^c-abcdef\n```\n\nAfter.\n"),
    ("fenced-tilde", "Before.\n\n~~~\nhidden\n~~~\n\nAfter.\n"),
    ("fenced-unclosed", "Before.\n\n```\nnever closed\n"),
    ("fenced-empty", "```\n```\n\nClaim.\n"),
    ("ordered-list", "1. One.\n2. Two.\n"),
    ("ordered-paren", "1) One.\n2) Two.\n"),
    ("unordered-list", "- One.\n+ Two.\n* Three.\n"),
    ("nested-list", "- parent\n  - child\n- sibling\n"),
    ("nested-list-continuation", "- a\n  continued\n\nPlain.\n"),
    ("block-quote", "> quoted\n\nPlain.\n"),
    ("block-quote-only", "> quoted\nlazy continuation\n"),
    ("heading", "# Heading\n\nClaim.\n"),
    ("heading-anchored", "## Section ^c-abcdef\n\nClaim.\n"),
    ("setext-heading", "Title\n===\n\nClaim.\n"),
    ("table", "| a | b |\n|---|---|\n| 1 | 2 |\n\nClaim.\n"),
    ("table-pipe-only", "a | b\n\nClaim.\n"),
    ("front-matter", "---\ntitle: Notes\n---\n# Notes\n\nClaim.\n"),
    ("front-matter-paragraph", "---\ntitle: Notes\n---\nClaim.\n"),
    ("crlf", "One.\r\n\r\nTwo.\r\n"),
    ("crlf-front-matter", "---\r\ntitle: Notes\r\n---\r\n# Notes\r\n\r\nClaim.\r\n"),
    ("crlf-fence", "```\r\nHidden.\r\n```\r\n\r\nVisible.\t\r\n"),
    ("indented-code", "    code\n\nClaim.\n"),
    ("reference-definition", "[ref]: https://example.invalid\n\nClaim.\n"),
    ("thematic-break", "***\n\nClaim.\n"),
    ("previously-line", "previously Old.\n\nClaim.\n"),
    ("suffix-spaces", "Claim. ^c-abcdef   \n"),
    ("suffix-tab-crlf", "Caf\u00e9. ^c-abcdef\t\r\n"),
    ("hard-break", "Claim.  \ncontinued.\t\n"),
    ("unicode", "Caf\u00e9.\n\nTwo.\n"),
    ("unicode-line-separator", "Caf\u00e9.\u2028continues.\n"),
    ("non-white-control", "\u001cClaim.\n"),
]


def golden() -> list[dict[str, object]]:
    cases = []
    for name, text in CASES:
        data = text.encode("utf-8")
        spans = sorted(memory_claim_blocks.hub_spans(text))
        cases.append(
            {
                "name": name,
                "text": text,
                "blocks": [data[start:end].decode("utf-8") for start, end in spans],
            }
        )
    return cases


def render() -> dict[Path, str]:
    return {
        GOLDEN: json.dumps(golden(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    stale = []
    for path, content in render().items():
        if args.write:
            path.write_text(content, encoding="utf-8", newline="\n")
        elif not path.exists() or path.read_text(encoding="utf-8") != content:
            stale.append(str(path.relative_to(HUB)))
    if stale:
        print(
            "stale block classification goldens: " + ", ".join(stale) + " (run with --write)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
