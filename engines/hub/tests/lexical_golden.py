"""Python-generated goldens for the hub's lexical scorer (S13b).

The hub copies the local BM25 scorer (`brigade.memory_bm25`) and the search
fields of `brigade.memory_cmd`. Python is the oracle, so this script writes:

- `src/memory/pytables.rs`: the Unicode classes the tokenizer and the query
  normalizer need, read from this Python's own `re` and `str` methods (`\\w`,
  `str.isprintable`, `str.lower` with its final sigma rule).
- `tests/lexical-golden.json`: tokenizer samples, card search fields,
  `round(x, 4)` values and ranked results over small corpora.

Run from the repository root with `PYTHONPATH=src`:

    python engines/hub/tests/lexical_golden.py --write
    python engines/hub/tests/lexical_golden.py --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from brigade import memory_bm25, memory_cmd

HUB = Path(__file__).resolve().parents[1]
TABLES = HUB / "src/memory/pytables.rs"
GOLDEN = HUB / "tests/lexical-golden.json"
WORD = re.compile(r"\w")
SIGMA = "Σ"
FINAL = "ς"
# Escape text for lone surrogates, written in pieces so that no tool on the
# way joins a high and a low escape into one character.
HIGH = "\\u" + "d83c"
LOW = "\\u" + "df89"

TOKENIZE = [
    "Straße STRASSE ẞ",
    "İstanbul ıi I",
    "ΟΔΟΣ ΟΔΟΣ. ΣΑΣ Σ aΣ́ aΣ́b",
    "école café naïve",
    "記憶検索 日本語のテキスト",
    "한국어 텍스트",
    "①② ²³ ¼ Ⅻ ٣٤ 42 ４２",
    "snake_case __init__ a_b-c/d.e",
    "ǅungla ǈ",
    "ＡＢＣ ｆｕｌｌ",
    "Ꭰ Ꮳ Ა",
    "K Ω Å",
    "ﬁne ﬀ",
    "हिन्दी देवनागरी",
    "ไทย",
    "zero‍width‌join",
    "emoji \U0001f389 test",
    "tab\tsep\x0bvt\x1cfs",
    "‿under‿tie",
    "x·y Ⓐ ⓐ",
    "\U0001d400\U0001d401 \U0001d7cf",
    "ͅ MiXeD123abc",
    ":: -- //",
    "",
]

FIELDS = [
    ("plain", "---\ntitle: Build notes\ntags: [build, ci]\nsummary: How we build\n---\nBody text.\n"),
    ("no-front-matter", "# Heading\nbody\n"),
    ("precedence", "---\ndescription: D\nsummary: S\n---\nx\n"),
    ("title-list", "---\ntitle: ['a', \"b's\", 3, 2.50, True, None, [1, (2,)], (), (4)]\n---\nx\n"),
    ("tag-escapes", "---\ntags: ['caf\\u00e9', 'tab\\there', r'raw\\n', 'a' 'b', u'u', '\\101\\x41']\n---\nx\n"),
    ("tags-unquoted", "---\ntags: [a, b]\n---\nx\n"),
    ("tags-scalar", "---\ntags: single\n---\nx\n"),
    ("tags-bool", "---\ntags: TRUE\ntitle: false\n---\nx\n"),
    ("empty-values", "---\ntitle: ''\ntags: []\nsummary:\n---\nbody\n"),
    ("duplicate-keys", "---\ntitle: one\ntitle: two\n---\nx\n"),
    ("unterminated", "---\ntitle: x\nbody\n"),
    ("spaced-end", "---\ntitle: spaced\n  ---  \nafter\n"),
    ("keys-and-colons", "---\n title : a: b\n: nokey\nnocolon\nsummary: \"quoted\"'\n---\nx\n"),
    ("vertical-tab", "---\ntitle: v\x0bsummary: split\n---\nx\n"),
    ("numbers", "---\ntags: [1_000, 0, -0, +5, 1e3, .5, 1., 1e999, -2.5e-7, 00]\n---\nx\n"),
    ("bad-number", "---\ntags: [007]\n---\nx\n"),
    ("bad-literals", "---\ntags: [1, 2] + [3]\ntitle: [f'x']\nsummary: [--5]\n---\nx\n"),
    (
        "repr-escapes",
        "---\ntitle: ['\\x00', '\\u200b', 'é', '\\U0001F600', \"it's\", 'say \"hi\"', 'both \\' \"', '\\\\']\n---\nx\n",
    ),
    ("trailing-comma", "---\ntags: ['x', 'y',]\ntitle: [ ]\n---\nx\n"),
    ("top-level-tuple", "---\ntags: [1], ['two']\ntitle: [1], [2],\nsummary: ['s'] , \n---\nx\n"),
    ("not-at-start", "\n---\ntitle: x\n---\ny\n"),
    # A lone surrogate reads as U+FFFD in the hub's fields, and its repr keeps the escape.
    (
        "lone-surrogates",
        f"---\ntags: [\"deploy\", \"{HIGH}{LOW}\", 'a{SIGMA}{HIGH}', ['{LOW}']]\n"
        f"title: ['x{HIGH}', '\\U0000{LOW[2:]}', '{LOW}{HIGH}']\n---\nx\n",
    ),
    (
        "radix-ints",
        "---\ntags: [0x1F, 0o17, 0b101, 0X_ff, -0x10, -0x0, 0b_1_0, 0O7, 0B1, 0x00]\n"
        "title: [0xa_b, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF]\n---\nx\n",
    ),
    ("radix-bad", "---\ntags: [0x1__2]\ntitle: [0o8]\nsummary: [0x1e+5]\n---\nx\n"),
    ("radix-more", "---\ntags: [0x, 0b2, 0x1_, 0x1.5, 00x1, 0xg]\ntitle: [0x 1]\n---\nx\n"),
    ("name-escape-unknown", "---\ntags: ['\\N{NO SUCH CHARACTER NAME}']\n---\nx\n"),
    ("comments", "---\ntags: ['ops'] # owner: team ]\ntitle: ['a', # c ]\nsummary: [1]#x ]\n---\nx\n"),
    ("comment-after-string", "---\ntags: ['a' # b ']\n---\nx\n"),
    # CPython's tokenizer allows 200 open brackets and refuses the 201st.
    ("nesting-200", "---\ntags: [" + "[" * 199 + "'deep'" + "]" * 199 + "]\ntitle: [" + "(" * 199 + "1" + ")" * 199 + "]\n---\nx\n"),
    ("nesting-201", "---\ntags: [" + "[" * 200 + "'deep'" + "]" * 200 + "]\ntitle: [" + "(" * 200 + "1" + ")" * 200 + "]\n---\nx\n"),
    # Python 3.12 converts integers of at most 4,300 decimal digits.
    ("int-digits", "---\ntags: [" + "1" * 4300 + "]\ntitle: [" + "1" * 4301 + "]\nsummary: [0b" + "1" * 14284 + "]\n---\nx\n"),
    ("unicode-key-space", "---\n title : nbsp\n---\nx\n"),
]

ROUND = [0.03125, 0.00015, 1.23455, 2.5, 0.000049999, 12.34565, 1e-05, 3.14159265, 0.99995, 7.00005]

FLEET = ["fleet"]
CORPORA = {
    "ties": {
        "cards": [
            ("fleet", "a", "---\ntitle: Same\n---\nshared words here\n"),
            ("fleet", "a-b", "---\ntitle: Same\n---\nshared words here\n"),
            ("fleet", "build", "build build build tooling\n"),
            ("fleet", "other", "words elsewhere\n"),
            ("fleet", "a.b", "---\ntitle: Shared\n---\nshared\n"),
        ],
        "queries": [
            (q, FLEET)
            for q in (
                "shared",
                "build build build",
                "words shared",
                "a",
                "a-b",
                "Shared WORDS",
                "nothing",
                "",
                ":",
                "here here words",
            )
        ],
    },
    "scopes": {
        "cards": [
            ("fleet", "alpha", "deploy pipeline\n"),
            ("repo:one", "alpha", "deploy deploy notes\n"),
            ("repo:two", "beta", "pipeline notes\n"),
            ("principal:agent:dot", "mine", "deploy notes private\n"),
        ],
        "queries": [
            ("deploy", FLEET),
            ("deploy", ["repo:one"]),
            ("deploy notes", ["fleet", "repo:one"]),
            ("deploy pipeline notes", ["fleet", "repo:one", "repo:two"]),
            ("notes", ["repo:two", "principal:agent:dot"]),
        ],
    },
    "retired": {
        "cards": [
            ("fleet", "live", "deploy schedule\n"),
            ("fleet", "gone", "deploy deploy deploy\n", "retired"),
            ("fleet", "third", "schedule notes\n"),
        ],
        "queries": [("deploy", FLEET), ("schedule", FLEET), ("deploy schedule notes", FLEET)],
    },
    "front-matter": {
        "cards": [
            ("fleet", "fm", "---\ntitle: Runbook\ntags: [ops, backup]\ndescription: Nightly backup\n---\nSteps here.\n"),
            ("fleet", "plain", "Runbook steps without front matter.\n"),
            ("fleet", "tagged", "---\ntags: backup\n---\nbackup backup\n"),
        ],
        "queries": [
            ("tags", FLEET),
            ("title description", FLEET),
            ("backup", FLEET),
            ("runbook", FLEET),
            ("ops", FLEET),
        ],
    },
    "literal": {
        "cards": [
            ("fleet", "colons", "path::to::thing -> arrow\n"),
            ("fleet", "arrow", "an -> only\n"),
            ("fleet", "plain", "no punctuation\n"),
            ("fleet", "upper", "TITLE ~~ TILDE\n"),
        ],
        "queries": [(q, FLEET) for q in ("::", "->", ":: ->", "~~", "!!", "  ", "-")],
    },
}


def ranges(pred) -> list[int]:
    out: list[int] = []
    start = None
    for cp in range(0x110000):
        ok = pred(chr(cp))
        if ok and start is None:
            start = cp
        elif not ok and start is not None:
            out += [start, cp - 1]
            start = None
    if start is not None:
        out += [start, 0x10FFFF]
    return out


def cased(c: str) -> bool:
    return (" " + c + SIGMA).lower()[-1] == FINAL


def case_ignorable(c: str) -> bool:
    return not cased(c) and ("A" + c + SIGMA).lower()[-1] == FINAL


def tables() -> str:
    lower: list[int] = []
    multi: list[tuple[int, str]] = []
    for cp in range(0x110000):
        if cp == 0x3A3:
            continue
        mapped = chr(cp).lower()
        if mapped == chr(cp):
            continue
        if len(mapped) == 1:
            lower += [cp, ord(mapped)]
        else:
            multi.append((cp, mapped))
    sections = [
        ("WORD", "Code point ranges Python's `re` `\\w` matches, as start, end pairs.", ranges(lambda c: WORD.fullmatch(c) is not None)),
        ("PRINTABLE", "Ranges where `str.isprintable()` is true.", ranges(str.isprintable)),
        ("CASED", "Ranges `str.lower()` treats as cased for the final sigma rule.", ranges(cased)),
        ("CASE_IGNORABLE", "Ranges `str.lower()` skips for the final sigma rule.", ranges(case_ignorable)),
        ("LOWER", "Single code point `str.lower()` mappings, as from, to pairs. U+03A3 is handled apart.", lower),
    ]
    out = [
        "//! Generated by `engines/hub/tests/lexical_golden.py --write`. Do not edit.",
        f"//! Python {sys.version_info.major}.{sys.version_info.minor}, Unicode {unicodedata.unidata_version}.",
        "",
        "#[cfg(test)]",
        f'pub const UNICODE: &str = "{unicodedata.unidata_version}";',
    ]
    for name, doc, values in sections:
        out += ["", f"/// {doc}", "#[rustfmt::skip]", f"pub const {name}: &[u32] = &["]
        for i in range(0, len(values), 10):
            out.append("    " + " ".join(f"0x{v:x}," for v in values[i : i + 10]))
        out.append("];")
    out += ["", "/// Multi code point `str.lower()` mappings.", "#[rustfmt::skip]", "pub const LOWER_MULTI: &[(u32, &str)] = &["]
    for cp, mapped in multi:
        escaped = "".join(f"\\u{{{ord(ch):x}}}" for ch in mapped)
        out.append(f'    (0x{cp:x}, "{escaped}"),')
    out.append("];")
    return "\n".join(out) + "\n"


def display(text: str) -> str:
    """A field as the hub holds it: a lone surrogate reads as U+FFFD."""
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


def search_fields(card_id: str, text: str) -> dict:
    """`memory_cmd._card_search_fields` for a card stored as `cards/<card_id>.md`."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        path = target / "cards" / f"{card_id}.md"
        path.parent.mkdir()
        path.write_bytes(text.encode("utf-8"))
        return memory_cmd._card_search_fields(path, target)


def ranked(cards: list, query: str, readable: list[str]) -> list:
    visible = [c for c in cards if c[0] in readable and (len(c) < 4 or c[3] == "live")]
    texts = [memory_bm25.card_text(search_fields(c[1], c[2])) for c in visible]
    rows = [(visible[doc], score) for doc, score in memory_bm25.rank(texts, query)]
    rows.sort(key=lambda row: (-row[1], f"cards/{row[0][1]}.md", row[0][0]))
    return [[card[0], card[1], score] for card, score in rows]


def golden() -> dict:
    fields = []
    for card_id, text in FIELDS:
        f = search_fields(card_id, text)
        full = memory_bm25.card_text(f)
        fields.append(
            {
                "card_id": card_id,
                "text": text,
                "title": display(f["title"]),
                "tags": [display(t) for t in f["tags"]],
                "summary": display(f["summary"]),
                "card_text": display(full),
                "length": len(memory_bm25.tokenize(full)),
            }
        )
    corpora = {}
    for name, corpus in CORPORA.items():
        cards = corpus["cards"]
        corpora[name] = {
            "cards": [{"scope": c[0], "card_id": c[1], "text": c[2], "state": c[3] if len(c) > 3 else "live"} for c in cards],
            "queries": [
                {"query": q, "readable": readable, "expected": ranked(cards, q, readable)}
                for q, readable in corpus["queries"]
            ],
        }
    return {
        "unicode": unicodedata.unidata_version,
        "k1": memory_bm25.K1,
        "b": memory_bm25.B,
        "digits": memory_bm25.SCORE_DIGITS,
        "tokenize": [{"text": t, "lower": t.lower(), "tokens": memory_bm25.tokenize(t)} for t in TOKENIZE],
        "single_line": [{"text": t, "line": " ".join("".join(c if c.isprintable() else " " for c in t).split())} for t in TOKENIZE],
        "fields": fields,
        "round": [[x, round(x, memory_bm25.SCORE_DIGITS)] for x in ROUND],
        "corpora": corpora,
    }


def render() -> dict[Path, str]:
    return {
        TABLES: tables(),
        GOLDEN: json.dumps(golden(), indent=1, sort_keys=True, ensure_ascii=True) + "\n",
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
        print("stale lexical goldens: " + ", ".join(stale) + " (run with --write)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
