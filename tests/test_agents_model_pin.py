"""Per-adapter model pinning: exact argv assertions, no CLI execution.

Every expected argv here is checked against the real CLI's --help flag; the
adapters covered are the ones with a confirmed model flag on an installed CLI.
"""

from pathlib import Path

import pytest

from brigade import agents


def test_pins_model_for_grok():
    assert agents.build_argv("grok", "P", model="grok-composer-2.5-fast") == [
        "grok",
        "-m",
        "grok-composer-2.5-fast",
        "-p",
        "P",
        "--always-approve",
    ]
    read_only = agents.build_argv("grok", "P", read_only=True, model="grok-composer-2.5-fast")
    assert read_only == [
        "grok",
        "-m",
        "grok-composer-2.5-fast",
        "-p",
        "P",
        "--permission-mode",
        "plan",
    ]


def test_pins_model_for_opencode():
    assert agents.build_argv("opencode", "P", model="openrouter/x-ai/grok") == [
        "opencode",
        "run",
        "-m",
        "openrouter/x-ai/grok",
        "P",
    ]
    # opencode has no read-only variant, so the model pin is the only change.
    assert agents.build_argv("opencode", "P", read_only=True, model="openrouter/x-ai/grok") == [
        "opencode",
        "run",
        "-m",
        "openrouter/x-ai/grok",
        "P",
    ]


def test_pins_model_for_pi():
    assert agents.build_argv("pi", "P", model="openai/gpt-4o") == [
        "pi",
        "--model",
        "openai/gpt-4o",
        "-p",
        "P",
    ]
    assert agents.build_argv("pi", "P", read_only=True, model="openai/gpt-4o") == [
        "pi",
        "--model",
        "openai/gpt-4o",
        "--tools",
        "read,grep,find,ls",
        "-p",
        "P",
    ]


def test_pins_model_for_kimi():
    assert agents.build_argv("kimi", "P", model="kimi-k2.5") == [
        "kimi",
        "-m",
        "kimi-k2.5",
        "--yolo",
        "--print",
        "-p",
        "P",
        "--final-message-only",
    ]
    assert agents.build_argv("kimi", "P", read_only=True, model="kimi-k2.5") == [
        "kimi",
        "-m",
        "kimi-k2.5",
        "--plan",
        "--print",
        "-p",
        "P",
        "--final-message-only",
    ]


def test_pins_model_for_cursor():
    assert agents.build_argv("cursor", "P", model="gpt-5") == [
        "cursor-agent",
        "--model",
        "gpt-5",
        "-p",
        "--output-format",
        "text",
        "-f",
        "P",
    ]
    read_only = agents.build_argv("cursor", "P", read_only=True, model="gpt-5")
    assert read_only == [
        "cursor-agent",
        "--model",
        "gpt-5",
        "-p",
        "--mode",
        "plan",
        "--output-format",
        "text",
        "--trust",
        "P",
    ]


def test_pins_model_for_antigravity():
    assert agents.build_argv("antigravity", "P", model="gpt-5") == [
        "agy",
        "--model",
        "gpt-5",
        "--add-dir",
        str(Path.cwd().resolve()),
        "--dangerously-skip-permissions",
        "--print",
        "P",
    ]
    assert agents.build_argv("antigravity", "P", read_only=True, model="gpt-5") == [
        "agy",
        "--model",
        "gpt-5",
        "--sandbox",
        "--print",
        "P",
    ]


def test_ollama_cloud_ref_keeps_full_model_name():
    # A cloud model id carries its own colon; only the `ollama:` prefix is stripped.
    assert agents.build_argv("ollama:qwen3-coder-next:cloud", "P") == [
        "ollama",
        "run",
        "qwen3-coder-next:cloud",
        "P",
    ]


def test_claude_and_codex_argv_unchanged_by_registry():
    assert agents.build_argv("claude", "P", model="claude-fable-5") == [
        "claude",
        "--model",
        "claude-fable-5",
        "-p",
        "P",
    ]
    assert agents.build_argv("codex", "P", model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "-m",
        "gpt-5.5-codex",
        "P",
    ]
    assert agents.build_argv("codex", "P", read_only=True, model="gpt-5.5-codex") == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.5-codex",
        "P",
    ]


def test_unsupported_adapter_still_raises():
    for cli in ("goose", "amp", "crush", "aider", "qwen", "copilot"):
        with pytest.raises(ValueError, match="does not support model pinning"):
            agents.build_argv(cli, "P", model="whatever")


def test_ollama_ref_rejects_separate_model():
    with pytest.raises(ValueError, match="model"):
        agents.build_argv("ollama:llama3.3", "P", model="mistral")


def test_supports_model_pinning_predicate():
    for cli in ("claude", "codex", "grok", "opencode", "pi", "kimi", "cursor", "antigravity"):
        assert agents.supports_model_pinning(cli)
    for cli in ("goose", "amp", "crush", "aider", "qwen", "copilot", "ollama:llama3.3"):
        assert not agents.supports_model_pinning(cli)
