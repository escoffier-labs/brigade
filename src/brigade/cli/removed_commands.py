"""One-release compatibility parsing for removed connector profiles."""

from __future__ import annotations

import sys

REMOVED_IN = "0.32.0"
# Remove this compatibility table in the next ordinary release after REMOVED_IN.
RETIRED_PROFILES = frozenset({"grokbot-memory", "grokbot-inbox"})
COMMAND_PATHS = (
    (("dot",), "--pack"),
    (("run", "cloud", "grokbot", "pack"), "--id"),
    (("run", "cloud", "grokbot", "serve"), "--pack"),
    (("hub", "connector"), "--id"),
)
REPLACEMENT = "Use brigade hub connector setup --id cloud-agent with independent credentials."


def removed(command: str, *, replacement: str | None = None) -> int:
    print(f"error: {command}: this was removed in {REMOVED_IN}. {replacement or REPLACEMENT}", file=sys.stderr)
    return 2


def compatibility(argv: list[str]) -> int | None:
    """Recognize historical IDs before loading command families or validating stale flags."""
    for prefix, flag in COMMAND_PATHS:
        if tuple(argv[: len(prefix)]) != prefix:
            continue
        tail = argv[len(prefix) :]
        for index, item in enumerate(tail):
            value = (
                item[len(flag) + 1 :]
                if item.startswith(flag + "=")
                else (tail[index + 1] if item == flag and index + 1 < len(tail) else None)
            )
            if value in RETIRED_PROFILES:
                if tail[0:1] == ["remove"] and flag == "--id":
                    from brigade.dot.retired_instances import remove_command

                    return remove_command(tail[1:], value)
                return removed(
                    " ".join((*prefix, value)),
                    replacement=REPLACEMENT
                    + f" Stop installed units: systemctl --user disable --now brigade-{value}.service "
                    + f"brigade-{value}-watch.service brigade-{value}-watch.timer",
                )
    return None
