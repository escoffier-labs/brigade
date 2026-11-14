"""One-release removal response for historical service entry points."""

from brigade.cli.removed_commands import removed_profile


def main(argv: list[str] | None = None) -> int:
    return removed_profile("t3-dispatch", "python -m brigade.grokbot_t3")


if __name__ == "__main__":
    raise SystemExit(main())
