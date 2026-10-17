"""The only t3-fleet and git subprocess boundary for t3-dispatch.

t3-fleet runs in machine mode: argv is the binary plus the action, and the
request is one JSON object on stdin. Prompts never appear on argv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable

REQUEST_SCHEMA = "t3-fleet.request.v1"
RESULT_SCHEMA = "t3-fleet.result.v1"
ERROR_SCHEMA = "t3-fleet.error.v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUBMIT_TIMEOUT = 60
CALL_TIMEOUT = 30
GIT_TIMEOUT = 20
MAX_OUTPUT_BYTES = 1_048_576
ENV_KEYS = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "USER",
    "LOGNAME",
    "SSH_AUTH_SOCK",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "T3_FLEET_STATE",
    "T3_FLEET_CONFIG",
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class FleetCallError(Exception):
    def __init__(self, code: str, reason: str | None = None, request_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.request_id = request_id


class FleetProtocolError(FleetCallError):
    """t3-fleet ran but its output could not be trusted, so any side effect is unknown."""


class FleetTimeout(Exception):
    pass


class RevisionError(Exception):
    pass


def child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ENV_KEYS if key in os.environ}


class FleetRunner:
    def __init__(
        self, t3_fleet_bin: str, git_bin: str, *, run: Run = subprocess.run, runtime_mode: str = "full-access"
    ):
        self.t3_fleet_bin = t3_fleet_bin
        self.git_bin = git_bin
        self._run = run
        self.runtime_mode = runtime_mode

    def call(self, action: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps({"schema": REQUEST_SCHEMA, "action": action, **payload}, separators=(",", ":"))
        try:
            proc = self._run(
                [self.t3_fleet_bin, action],
                input=body,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FleetTimeout(action) from exc
        except OSError as exc:
            raise FleetCallError("internal_error") from exc
        except UnicodeDecodeError as exc:
            # Undecodable output may follow an accepted submit, so it is in doubt like any malformed result.
            raise FleetProtocolError("internal_error") from exc
        output = proc.stdout or proc.stderr or ""
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise FleetProtocolError("internal_error")
        try:
            result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise FleetProtocolError("internal_error") from exc
        if not isinstance(result, dict):
            raise FleetProtocolError("internal_error")
        if result.get("schema") == ERROR_SCHEMA:
            raw_error = result.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            raw_code = error.get("code")
            raw_reason = error.get("reason")
            request_id = result.get("request_id") or error.get("request_id")
            raise FleetCallError(
                raw_code if isinstance(raw_code, str) else "internal_error",
                raw_reason if isinstance(raw_reason, str) else None,
                request_id if isinstance(request_id, str) else None,
            )
        if result.get("schema") != RESULT_SCHEMA or proc.returncode != 0:
            request_id = result.get("request_id") if result.get("schema") == RESULT_SCHEMA else None
            raise FleetProtocolError("internal_error", None, request_id if isinstance(request_id, str) else None)
        nested = result.get("result")
        if isinstance(nested, dict):
            flattened = {key: value for key, value in result.items() if key != "result"}
            flattened.update(nested)
            return flattened
        return result

    def capacity(self) -> dict[str, Any]:
        return self.call("capacity", {}, timeout=CALL_TIMEOUT)

    def submit(
        self,
        *,
        host: str,
        repository: str,
        source_revision: str,
        title: str,
        seat: str,
        prompt: str,
        interaction_mode: str,
        override_reason: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host": host,
            "repository": repository,
            "source_revision": source_revision,
            "title": title,
            "seat": seat,
            "prompt": prompt,
            "runtime_mode": self.runtime_mode,
            "interaction_mode": interaction_mode,
        }
        if override_reason is not None:
            payload["override_reason"] = override_reason
        return self.call("submit", payload, timeout=SUBMIT_TIMEOUT)

    def status(self, request_id: str) -> dict[str, Any]:
        return self.call("status", {"request_id": request_id}, timeout=CALL_TIMEOUT)

    def wait(self, request_id: str, timeout_s: int) -> dict[str, Any]:
        return self.call(
            "wait", {"request_id": request_id, "timeout": float(timeout_s)}, timeout=timeout_s + CALL_TIMEOUT
        )

    def followup(self, request_id: str, prompt: str, interaction_mode: str = "default") -> dict[str, Any]:
        payload = {
            "request_id": request_id,
            "prompt": prompt,
            "runtime_mode": self.runtime_mode,
            "interaction_mode": interaction_mode,
        }
        return self.call("followup", payload, timeout=SUBMIT_TIMEOUT)

    def answer(self, request_id: str, input_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        payload = {"request_id": request_id, "input_id": input_id, "answers": answers}
        return self.call("answer", payload, timeout=CALL_TIMEOUT)

    def approve(self, request_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        payload = {"request_id": request_id, "approval_id": approval_id, "decision": decision}
        return self.call("approve", payload, timeout=CALL_TIMEOUT)

    def interrupt(self, request_id: str) -> dict[str, Any]:
        return self.call("interrupt", {"request_id": request_id}, timeout=CALL_TIMEOUT)

    def resolve_revision(self, remote: str, branch: str) -> str:
        try:
            proc = self._run(
                [self.git_bin, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                env=child_env(),
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RevisionError(branch) from exc
        if proc.returncode != 0:
            raise RevisionError(branch)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if len(lines) != 1:
            raise RevisionError(branch)
        sha = lines[0].split("\t", 1)[0].strip()
        if not SHA_RE.fullmatch(sha):
            raise RevisionError(branch)
        return sha
