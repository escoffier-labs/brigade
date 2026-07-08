from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OpfResult:
    available: bool
    changed: bool
    redacted_text: str
    error: str = ""


def default_opf_bin() -> str:
    return os.environ.get("CONTENT_GUARD_OPF_BIN") or str(Path.home() / ".opf-venv" / "bin" / "opf")


def run_opf(text: str, *, opf_bin: str | None = None, device: str = "cpu", timeout: int = 120) -> OpfResult:
    opf = opf_bin or default_opf_bin()
    if not os.path.exists(opf) or not os.access(opf, os.X_OK):
        return OpfResult(False, False, text, f"opf binary not found: {opf}")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(text)
        path = handle.name

    try:
        proc = subprocess.run(
            [opf, "--device", device, "-f", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return OpfResult(True, False, text, str(exc))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "opf failed").strip()
        return OpfResult(True, False, text, error)

    redacted = proc.stdout
    return OpfResult(True, redacted != text, redacted, "")
