import subprocess

from brigade import proc


def test_run_captures_exit_and_output():
    r = proc.run(["python3", "-c", "import sys; print('hi'); sys.exit(3)"])
    assert r.code == 3
    assert r.stdout.strip() == "hi"


def test_run_passes_explicit_stdin_bytes_without_a_shell():
    payload = b'{"body":"hello"}\n'
    result = proc.run(
        ["python3", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        stdin=payload,
    )

    assert result.code == 0
    assert result.stdout == payload.decode()


def test_run_json_parses_stdout():
    r = proc.run(["python3", "-c", "print('{\"a\": 1}')"])
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
