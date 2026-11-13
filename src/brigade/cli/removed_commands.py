"""One-release compatibility parsing for removed connector profiles and command families."""

from __future__ import annotations

import sys

REMOVED_IN = "0.32.0"
# Remove this compatibility table in the next ordinary release after REMOVED_IN.
RETIRED_PROFILES = frozenset({"grokbot-memory", "grokbot-inbox"})
RETIRED_PACKS = frozenset(
    {
        "fleet-steward",
        "obsidian-operator",
        "t3-dispatch",
        "operations-relay",
        "operator",
        "repository-scout",
        "implementation-worker",
    }
)
COMMAND_PATHS = (
    (("dot",), "--pack"),
    (("run", "cloud", "grokbot", "pack"), "--id"),
    (("run", "cloud", "grokbot", "serve"), "--pack"),
    (("hub", "connector"), "--id"),
)
REPLACEMENT = "Use brigade hub connector setup --id cloud-agent with independent credentials."
# The removed commands were the only way to find this state, so the message must name it.
FLEET_KEYS_REPLACEMENT = (
    "Client-side message encryption no longer ships. Plain messaging is unchanged. "
    "Private keys and trust files from an earlier `fleet keys init` may remain in fleet/mesh-keys "
    "inside the Brigade home directory (~/.brigade unless BRIGADE_HOME is set). "
    "Nothing reads them now. Delete that directory to remove them."
)


def removed(command: str, *, replacement: str | None = None) -> int:
    print(f"error: {command}: this was removed in {REMOVED_IN}. {replacement or REPLACEMENT}", file=sys.stderr)
    return 2


def compatibility(argv: list[str]) -> int | None:
    """Recognize historical IDs before loading command families or validating stale flags."""
    if argv[:2] == ["fleet", "keys"]:
        command = " ".join(argv[:3] if argv[2:3] in (["init"], ["show"], ["rotate"]) else argv[:2])
        return removed(command, replacement=FLEET_KEYS_REPLACEMENT)
    for prefix, flag in COMMAND_PATHS:
        if tuple(argv[: len(prefix)]) != prefix:
            continue
        tail = argv[len(prefix) :]
        for index, item in enumerate(tail):
            option, separator, attached = item.partition("=")
            matches = option.startswith("--") and len(option) > 2 and flag.startswith(option)
            value = (
                attached if matches and separator else (tail[index + 1] if matches and index + 1 < len(tail) else None)
            )
            if value in RETIRED_PACKS:
                return removed_profile(value, " ".join((*prefix, value)))
            if value in RETIRED_PROFILES:
                if tail[0:1] == ["remove"] and flag == "--id":
                    from brigade.dot.retired_instances import remove_command

                    return remove_command(tail[1:], value)
                return removed_profile(value, " ".join((*prefix, value)))
    queue_prefix = ("run", "cloud", "grokbot")
    if tuple(argv[:3]) == queue_prefix and len(argv) > 3:
        command = argv[3]
        if command in {"--help", "-h"} or (command == "serve" and argv[4:] in (["--help"], ["-h"])):
            return None
        if command not in {"pack", "serve"} or (
            command == "pack" and len(argv) > 4 and argv[4].startswith(("relay", "install-relay"))
        ):
            return removed(
                " ".join(argv[:5] if command == "pack" else argv[:4]),
                replacement=REPLACEMENT
                + " Stop installed units: systemctl --user disable --now brigade-grokbot-findings-relay.timer brigade-grokbot-findings-relay.service brigade-grokbot-operator.service brigade-grokbot-repository-scout.service brigade-grokbot-implementation-worker.service"
                + _client_unit_commands(("operator", "repository-scout", "implementation-worker")),
            )
        if command == "serve":
            tail = argv[4:]
            selected = None
            for index, item in enumerate(tail):
                option, separator, attached = item.partition("=")
                if option.startswith("--") and len(option) > 2 and "--pack".startswith(option):
                    selected = attached if separator else (tail[index + 1] if index + 1 < len(tail) else None)
            if selected not in {"dot", "duet"}:
                return removed(
                    "run cloud grokbot serve",
                    replacement=REPLACEMENT
                    + " Stop installed units: systemctl --user disable --now brigade-grokbot-operator.service brigade-grokbot-repository-scout.service brigade-grokbot-implementation-worker.service"
                    + _client_unit_commands(("operator", "repository-scout", "implementation-worker")),
                )
    if argv[:2] == ["fleet", "grokbot"]:
        return removed("fleet grokbot enroll-actor")
    return None


def removed_profile(value: str, command: str | None = None) -> int:
    """The removal message for a retired profile, with how to stop its installed units."""
    if value in RETIRED_PACKS:
        units = f"brigade-grokbot-{value}.service"
        if value == "t3-dispatch":
            units += (
                " brigade-grokbot-t3-watch.timer brigade-grokbot-t3-intake.timer"
                " brigade-grokbot-t3-watch.service brigade-grokbot-t3-intake.service"
            )
    else:
        units = f"brigade-{value}.service brigade-{value}-watch.service brigade-{value}-watch.timer"
    return removed(
        command or value,
        replacement=REPLACEMENT
        + " Stop installed units: systemctl --user disable --now "
        + units
        + (
            _client_unit_commands((value,))
            if value in {"operator", "repository-scout", "implementation-worker"}
            else ""
        ),
    )


def _client_unit_commands(roles: tuple[str, ...]) -> str:
    patterns = " ".join(f"'brigade-grokbot-{role}-*.service'" for role in roles)
    return (
        "\nClient-scoped units:\nsystemctl --user stop " + patterns + "\nsystemctl --user list-unit-files " + patterns
    )
