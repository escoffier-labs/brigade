"""Ceilings must block and redact the complete secret, on either backend."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from brigade.guard import scan_text
from brigade.guard.native import native_spans
from brigade.guard.rules import DEFAULT_RULES
from brigade.evidence_redaction import apply_origin_redaction

CASES = json.loads((Path(__file__).parents[1] / "fixtures/memory-scan-overbound.json").read_text())


@pytest.mark.parametrize("name", list(CASES))
@pytest.mark.parametrize("backend", ["python", "native"])
def test_round5_guard_and_evidence_redact_complete_secret(name, backend, monkeypatch):
    case = CASES[name]
    text = "".join(case["prefix_parts"]) + case["unit"] * case["repeat"] + "".join(case["suffix_parts"])
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", backend)
    if backend == "native":
        if not os.environ.get("BRIGADE_GUARD_BIN"):
            pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
        assert native_spans(text, list(DEFAULT_RULES)) is not None
    tail = case["unit"] * 60
    # PEM lines contain 64-character runs. Checking 60 catches any leaked line.
    if "private-key" in name:
        tail = "a" * 60
    result = scan_text(text + "\nUnrelated note.\n")
    assert any(f.category == "secret" for f in result.findings), name
    assert tail not in result.redacted_text, name
    assert result.redacted_text.endswith("\nUnrelated note.\n")
    verdict = apply_origin_redaction(text, origin="agent-session")
    assert verdict.status == "redacted"
    assert tail not in verdict.persisted_text, name
    process = subprocess.run(
        [sys.executable, "-m", "brigade", "guard", "redact"], input=text, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert tail not in process.stdout, name


@pytest.fixture(params=["python", "native"])
def pem_backend(request, monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", request.param)
    if request.param == "native":
        if not os.environ.get("BRIGADE_GUARD_BIN"):
            pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
        assert native_spans("ordinary text", list(DEFAULT_RULES)) is not None


@pytest.mark.parametrize("ending", ["closed", "unterminated"])
@pytest.mark.parametrize("repeat", [2, 140])
@pytest.mark.parametrize(
    "layout", ["plain", "indent", "quote", "escaped", "escaped-json", "spaces", "body-trailing-spaces"]
)
def test_pem_trailing_findings_keep_secret_and_unrelated_text(pem_backend, ending, repeat, layout):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    body = ("a" * 64 + "\n") * repeat
    if layout in {"indent", "quote", "body-trailing-spaces"}:
        prefix = "    " if layout == "indent" else "> "
        header = prefix + header
        suffix = " " if layout == "body-trailing-spaces" else ""
        body = (prefix + "a" * 64 + suffix + "\n") * repeat
    elif layout in {"escaped", "escaped-json", "spaces"}:
        separator = r"\n" if layout.startswith("escaped") else " "
        header = header.rstrip("\n") + separator
        body = separator.join(["a" * 64] * repeat) + ('"' if layout == "escaped-json" else "") + "\n"
    closing = "-----END PRIVATE KEY-----\n" if ending == "closed" else ""
    # Synthetic canaries exercise independent trailing decisions.
    trailing = "Contact planted@probe-corp-canary.com host 10.23.45.67 logs /home/example-user/work done.\n"  # content-guard: allow email content-guard: allow private-ipv4 content-guard: allow home-path
    text = header + body + closing + trailing
    expected_guard = scan_text(trailing).redacted_text
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert "a" * 60 not in result.redacted_text
    assert result.redacted_text.endswith(expected_guard)
    assert {"email", "private-ipv4", "home-path"} <= {f.rule_id for f in result.findings}
    assert "Contact " in expected_guard and " done." in expected_guard
    for origin in ("agent-session", "workspace", "external-web"):
        verdict = apply_origin_redaction(text, origin=origin)
        assert verdict.status == "redacted"
        assert "private-key-block" in verdict.detectors
        assert "a" * 60 not in verdict.persisted_text
        assert verdict.persisted_text.endswith(apply_origin_redaction(trailing, origin=origin).persisted_text)
    process = subprocess.run(
        [sys.executable, "-m", "brigade", "guard", "redact"], input=text, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "a" * 60 not in process.stdout
    assert expected_guard in process.stdout


@pytest.mark.parametrize(
    "layout", ["indent", "quote", "quote-blank", "quote-bare-blank", "escaped-json", "escaped-crlf-json"]
)
@pytest.mark.parametrize("consumer", ["guard", "agent-session", "workspace", "external-web"])
def test_unterminated_encrypted_pem_metadata(pem_backend, layout, consumer):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    lines = [header, "Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-256-CBC,0123456789ABCDEF", "", "a" * 64, "b" * 64]
    if layout in {"indent", "quote", "quote-blank", "quote-bare-blank"}:
        prefix = "    " if layout == "indent" else "> "
        pem_lines = [prefix + line if line or layout == "quote-blank" else "" for line in lines]
        if layout == "quote-bare-blank":
            pem_lines[3] = ">"
        pem = "\n".join(pem_lines) + "\n"
    else:
        separator = r"\n" if layout == "escaped-json" else r"\r\n"
        pem = '"' + separator.join(lines) + '"\n'
    trailing = "Contact planted@probe-corp-canary.com host 10.23.45.67 logs /home/example-user/work done.\n"  # content-guard: allow email content-guard: allow private-ipv4 content-guard: allow home-path
    text = pem + trailing
    if consumer == "guard":
        result = scan_text(text)
        assert any(f.category == "secret" for f in result.findings)
        assert {"email", "private-ipv4", "home-path"} <= {f.rule_id for f in result.findings}
        redacted = result.redacted_text
        expected = scan_text(trailing).redacted_text
    else:
        verdict = apply_origin_redaction(text, origin=consumer)
        assert verdict.status == "redacted"
        assert "private-key-block" in verdict.detectors
        redacted = verdict.persisted_text
        expected = apply_origin_redaction(trailing, origin=consumer).persisted_text
    assert "a" * 60 not in redacted
    assert "b" * 60 not in redacted
    assert redacted.endswith(expected)
    assert "Contact " in expected and " done." in expected


def test_json_metadata_quote_boundary_preserves_following_value(pem_backend):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    text = '{"k": "' + header + r'\nProc-Type: 4,ENCRYPTED", "note": "keep-this-json"}' + "\n"
    assert "keep-this-json" in scan_text(text).redacted_text


def test_stray_quote_in_raw_metadata_keeps_masking_key_material(pem_backend):
    # Only a quote followed by JSON structure ends metadata early.
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    body = "a" * 64
    text = header + ' Proc-Type: 4,ENC"x ' + body + " " + body + "\n"
    assert body not in scan_text(text).redacted_text


@pytest.mark.parametrize(
    "text,reply",
    [
        (
            "> -----BEGIN RSA PRIVATE KEY-----\n> "  # content-guard: allow private-key-block
            + "a" * 64
            + "\n>\n> ordinary reply\n",
            "ordinary reply",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\n"  # content-guard: allow private-key-block
            + "a" * 64
            + "\n> quoted reply\n",
            "quoted reply",
        ),
    ],
)
def test_truncated_pem_stops_before_ordinary_reply(pem_backend, text, reply):
    assert reply in scan_text(text).redacted_text


def test_header_followed_by_bare_quote_word_is_masked(pem_backend):
    text = "-----BEGIN RSA PRIVATE KEY-----\n>word\n"  # content-guard: allow private-key-block
    assert ">word" not in scan_text(text).redacted_text


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\r\n\r\n"])
def test_pem_header_only_keeps_following_paragraph(pem_backend, separator):
    text = "A log mentions:\n-----BEGIN " + "RSA PRIVATE KEY-----" + separator + "The following paragraph stays.\n"
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert result.redacted_text.endswith(separator + "The following paragraph stays.\n")
    verdict = apply_origin_redaction(text, origin="agent-session")
    assert verdict.persisted_text.endswith(separator + "The following paragraph stays.\n")


@pytest.mark.parametrize("origin", ["agent-session", "workspace", "external-web"])
def test_expanded_pem_supersedes_contained_nonsecret_edits(pem_backend, origin):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    text = (
        header
        + "Proc-Type: 4,ENCRYPTED\nDEK-Info: 10.23.45.67 planted@probe-corp-canary.com\n\n"  # content-guard: allow email content-guard: allow private-ipv4
        + "a" * 64
        + "\n"
    )
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert result.redacted_text == "[redacted-private-key]\n"
    verdict = apply_origin_redaction(text, origin=origin)
    assert verdict.status == "redacted"
    assert verdict.persisted_text == "[redacted-private-key]\n"


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n"])
def test_pem_blank_after_body_ends_mask(pem_backend, separator):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    text = header + "a" * 64 + separator + "Paragraph\n"
    result = scan_text(text)
    assert result.redacted_text == "[redacted-private-key]" + separator + "Paragraph\n"


@pytest.mark.parametrize("layout", ["headers", "bodies", "final-end"])
def test_pem_repeated_headers_scan_time(pem_backend, layout):
    # Compare an 8x input step in one process, with a separate hang cap.
    program = r"""
import sys
from time import perf_counter
from brigade.guard import scan_text
unit = "-----BEGIN " + "PRIVATE KEY-----\n"
if sys.argv[1] != "headers":
    unit += "a" * 64 + "\n"
def measure(size):
    text = (unit * (size // len(unit) + 1))[:size]
    if sys.argv[1] == "final-end":
        text += "\n-----END PRIVATE KEY-----"
    start = perf_counter()
    result = scan_text(text)
    elapsed = perf_counter() - start
    assert any(f.rule_id == "private-key-block" for f in result.findings)
    assert "a" * 60 not in result.redacted_text
    return elapsed
measure(64 * 1024)  # Warm caches before comparing input sizes.
small = min(measure(64 * 1024) for _ in range(3))
large = min(measure(512 * 1024) for _ in range(3))
print(small, large, large / small, flush=True)
"""
    result = subprocess.run([sys.executable, "-c", program, layout], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    print(layout, os.environ["BRIGADE_GUARD_BACKEND"], result.stdout.strip())
    small, large, ratio = map(float, result.stdout.split())
    assert large < 20, result.stdout
    # An 8x input step is about 8x when linear and about 64x when quadratic.
    # 24 sits near their geometric midpoint, so shared-runner noise (16.9 seen
    # on CI) passes while the pre-memo engine (49x) still fails. The sorted
    # span sweep measures about 8x locally.
    assert ratio < 24, result.stdout


@pytest.mark.parametrize("action", ["allow", "warn", "block"])
def test_pem_policy_keeps_later_hostname_decision(pem_backend, action):
    from brigade.guard.policy import Policy

    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    text = header + "DEK-Info: https://service.corp\n\naaaa\n"  # content-guard: allow private-hostname
    result = scan_text(text, policy=Policy(rules={"private-key-block": action}))
    findings = {finding.rule_id: finding for finding in result.findings}
    assert findings["private-key-block"].action == action
    assert findings["private-hostname"].action == "block"
    assert "service.corp" not in result.redacted_text  # content-guard: allow private-hostname
    if action == "block":
        assert result.redacted_text == "[redacted-private-key]\n"
    else:
        assert header in result.redacted_text
        assert "DEK-Info: https://" in result.redacted_text
        assert result.redacted_text.endswith("\n\naaaa\n")


@pytest.mark.parametrize("suffix", ["-Broken:", ":", "!", "=invalid"])
def test_pem_inline_partial_token_is_not_body(pem_backend, suffix):
    header = "-----BEGIN " + "RSA PRIVATE KEY-----"
    remainder = r"\nProc" + suffix + "\nFollowing paragraph stays.\n"
    result = scan_text(header + remainder)
    assert result.redacted_text == "[redacted-private-key]" + remainder
    verdict = apply_origin_redaction(header + remainder, origin="agent-session")
    assert verdict.persisted_text == "[redacted-private-key]" + remainder
