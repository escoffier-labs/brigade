"""Oracle browser adapter: exact argv assertions, no CLI execution.

Oracle is not installed on CI machines, so these argv shapes are confirmed
against oracle's published docs/cli-reference.md and docs/gemini.md rather than
a local `oracle --help`. That is why they live here and not in
tests/test_agents_model_pin.py, whose stated invariant is that every adapter it
covers has a confirmed model flag on an installed CLI.
"""

from brigade import agents


def test_oracle_argv_pins_the_browser_engine():
    assert agents.build_argv("oracle", "P") == [
        "oracle",
        "--engine",
        "browser",
        "-p",
        "P",
    ]


def test_oracle_argv_is_identical_under_read_only():
    # Oracle has no filesystem write path, so read-only needs no flag and no
    # prompt instruction. The argv must not change at all.
    assert agents.build_argv("oracle", "P", read_only=True) == agents.build_argv("oracle", "P")
    assert agents.build_argv("oracle", "P", sandbox="read-only") == agents.build_argv("oracle", "P")


def test_oracle_argv_pins_model_after_the_command():
    assert agents.build_argv("oracle", "P", model="gemini-3.1-pro") == [
        "oracle",
        "--model",
        "gemini-3.1-pro",
        "--engine",
        "browser",
        "-p",
        "P",
    ]


def test_oracle_argv_never_emits_heartbeat_or_an_api_path():
    # stdout is the answer channel: --heartbeat would interleave progress lines
    # into it, and dropping --engine browser would fall back to an API key.
    for argv in (
        agents.build_argv("oracle", "P"),
        agents.build_argv("oracle", "P", read_only=True),
        agents.build_argv("oracle", "P", model="gemini-3.1-pro"),
    ):
        assert "--heartbeat" not in argv
        assert argv[argv.index("--engine") + 1] == "browser"


def test_oracle_read_only_enforcement_is_hard():
    assert agents.read_only_enforcement("oracle") == "hard"
    assert agents.read_only_enforcement("oracle", sandbox="read-only") == "hard"


def test_oracle_supports_model_pinning_but_not_reasoning():
    assert agents.supports_model_pinning("oracle") is True
    # --browser-thinking-time is deferred to the ChatGPT Pro phase.
    assert agents.supports_reasoning("oracle") is False
