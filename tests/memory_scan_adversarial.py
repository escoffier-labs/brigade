"""Shared generated near-misses, also consumed by the native cost test."""

import json
from pathlib import Path
import sys
import time

from brigade.memory_content_scan import blocked_rule


def vectors(name, size):
    if name == "codex-exact":
        return [(name, "ｌａｂｅｌ．" * 8000 + " @")]
    if name == "claude-exact":
        return [(name, "label." * 87_000 + "@")]
    case = json.loads((Path(__file__).parent / "fixtures/memory-scan-adversarial.json").read_text())[name]
    prefix = "".join(case["prefix_parts"])
    literal = case["literal"]
    unit = case["unit"]
    result = []
    for folded in (False, True):
        length = size // (3 if folded else 1)
        run = (unit * (length // len(unit) + 1))[: length - len(prefix) - len(literal) - 3]
        for position, text in [
            ("before", literal + "! " + prefix + run + "!"),
            ("after", prefix + run + literal + "!!!"),
            ("isolated", prefix + run + "! " + literal + "!"),
        ]:
            if folded:
                text = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else "\u3000" if c == " " else c for c in text)
                text += "!" * (size - len(text.encode()))
            result.append((position + ("-fullwidth" if folded else ""), text))
    return result


if __name__ == "__main__":
    for label, text in vectors(sys.argv[1], int(sys.argv[2])):
        if len(sys.argv) > 3 and label != sys.argv[3]:
            continue
        start = time.perf_counter()
        result = blocked_rule([text])
        elapsed = time.perf_counter() - start
        print(sys.argv[1], label, len(text.encode()), f"{elapsed:.6f}", result, flush=True)
        expected = (
            None
            if sys.argv[1].endswith("-exact")
            else json.loads((Path(__file__).parent / "fixtures/memory-scan-adversarial.json").read_text())[sys.argv[1]][
                "expected"
            ][label.removesuffix("-fullwidth")]
        )
        assert result == expected
