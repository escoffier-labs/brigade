import locale
import subprocess
import sys

from brigade import proc


def test_run_captures_exit_and_output():
    r = proc.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert r.code == 3
    assert r.stdout.strip() == "hi"


def test_run_passes_explicit_stdin_bytes_without_a_shell():
    payload = b'{"body":"hello"}\n'
    result = proc.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=payload,
    )

    assert result.code == 0
    assert result.stdout == payload.decode()


def test_run_json_parses_stdout():
    r = proc.run([sys.executable, "-c", "print('{\"a\": 1}')"])
    assert r.json() == {"a": 1}


def test_run_json_returns_none_on_nonjson():
    r = proc.run(["python3", "-c", "print('not json')"])
    assert r.json() is None


def test_which_detects_present_and_absent():
    assert proc.which("python3") is not None
    assert proc.which("definitely-not-a-real-binary-xyz") is None


def test_run_preserves_partial_output_on_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["worker"],
            timeout=3.0,
            output=b"partial stdout\n",
            stderr=b"partial stderr\n",
        )

    monkeypatch.setattr(proc.subprocess, "run", timeout)

    result = proc.run(["worker"], timeout=3.0)

    assert result.code == 124
    assert result.stdout == "partial stdout\n"
    assert result.stderr == "partial stderr\ntimeout after 3.0s"


def test_run_feeds_stdin_when_provided(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=kwargs.get("args") or args, returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    result = proc.run(["codex", "exec", "-"], stdin=b"plan prompt")
    assert result.code == 0
    assert captured["input"] == b"plan prompt"
    assert "stdin" not in captured


def test_run_uses_devnull_stdin_when_stdin_omitted(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=kwargs.get("args") or args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    proc.run(["true"])
    assert captured["stdin"] is subprocess.DEVNULL
    assert "input" not in captured


def test_run_decodes_valid_utf8_with_byte_0x9d_despite_cp1252_locale(monkeypatch):
    payload = "review complete: \u275d\n".encode("utf-8")
    assert b"\x9d" in payload

    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        return subprocess.CompletedProcess(args[0], 0, payload, b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args, **kwargs: "cp1252")

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.stdout == "review complete: \u275d\n"
    assert result.stderr == ""


def test_run_timeout_normalizes_none_output(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["worker"], timeout=3.0, output=None, stderr=None)

    monkeypatch.setattr(proc.subprocess, "run", timeout)

    result = proc.run(["worker"], timeout=3.0)

    assert result.code == 124
    assert result.stdout == ""
    assert result.stderr == "timeout after 3.0s"


def test_run_returns_typed_failure_for_invalid_utf8(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"\x9d alone is invalid utf-8", b"stderr ok\n")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout_decode_error is not None
    assert result.stderr_decode_error is None
    assert "\ufffd" in result.stdout
    assert "alone is invalid utf-8" in result.stdout
    assert result.stderr.startswith("stderr ok\nchild stdout is not valid UTF-8 (utf-8):")


def test_run_preserves_valid_prefix_on_invalid_utf8(monkeypatch):
    payload = b"prefix\n\x9d"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, payload, b"")

    monkeypatch.setattr(proc.subprocess, "run", fake_run)

    result = proc.run(["worker"])

    assert result.code == 0
    assert result.decode_failed is True
    assert result.stdout.startswith("prefix\n")
    assert "\ufffd" in result.stdout
    assert result.stdout_decode_error is not None
    assert "child stdout is not valid UTF-8 (utf-8):" in result.stderr
