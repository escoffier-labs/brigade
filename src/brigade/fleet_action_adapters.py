"""Public local CLI transports. Never interpolate prompts into shell commands."""

from __future__ import annotations

import inspect
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .fleet_action_auth import ActionError, destination_reference, opaque

MAX_OUTPUT = 262144
T3_RUNTIME_MODES = frozenset({"approval-required", "auto-accept-edits", "auto", "full-access"})
T3_PREACCEPT_REJECTIONS = frozenset({"invalid_config", "invalid_request", "policy_rejected", "target_busy"})
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class T3PreacceptRefusal(ActionError):
    """A closed native refusal observed on a nonzero process exit."""

    def __init__(self, code: str) -> None:
        super().__init__("transport-preaccept-refused", 409)
        self.code = code


def invoke(executable: Path, argv: list[str], *, timeout: float = 3600) -> dict[str, Any]:
    """Bound both pipes in memory while running, with no provider output on disk.

    POSIX only: isolated process groups let cleanup stop ordinary descendants,
    including writers that outlive their parent. Descendants that deliberately
    escape that group are outside this transport's containment boundary.
    """
    if os.name != "posix":
        raise ActionError("transport-platform-unsupported", 503)
    deadline = time.monotonic() + timeout
    raw = bytearray()
    try:
        process = subprocess.Popen(
            [str(executable), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            shell=False,
        )
        try:
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                counts = {True: 0, False: 0}
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ActionError("transport-acceptance-unknown", 409)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        stdout = key.data
                        chunk = os.read(key.fd, min(65536, MAX_OUTPUT - counts[stdout] + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        counts[stdout] += len(chunk)
                        if counts[stdout] > MAX_OUTPUT:
                            raise ActionError("transport-acceptance-unknown", 409)
                        if stdout:
                            raw.extend(chunk)
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            # Stop the entire isolated group even if its leader already exited.
            # Close rather than unboundedly draining pipes after a refusal.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    except (OSError, subprocess.SubprocessError):
        raise ActionError("transport-acceptance-unknown", 409) from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ActionError("transport-acceptance-unknown", 409) from None
    if not isinstance(result, dict):
        raise ActionError("transport-acceptance-unknown", 409)
    if returncode != 0:
        # The public T3 unknown-state error may carry the only recoverable handle.
        # Retain only that bounded handle and fixed code, never error prose/detail.
        error = result.get("error")
        if (
            result.get("schema") == "t3-fleet.error.v1"
            and isinstance(error, dict)
            and isinstance(error.get("code"), str)
            and error["code"] in T3_PREACCEPT_REJECTIONS
            and result.get("request_id") is None
        ):
            raise T3PreacceptRefusal(error["code"])
        if isinstance(result.get("runId"), str) and result.get("status") not in {"ok", "completed"}:
            try:
                return {"status": "error", "runId": opaque(result["runId"])}
            except ActionError:
                raise ActionError("transport-acceptance-unknown", 409) from None
        if (
            result.get("schema") != "t3-fleet.error.v1"
            or not isinstance(error, dict)
            or error.get("code") != "unknown_state"
        ):
            raise ActionError("transport-acceptance-unknown", 409)
        try:
            reference = destination_reference("t3", result.get("request_id"))
        except ActionError:
            raise ActionError("transport-acceptance-unknown", 409) from None
        return {"schema": "t3-fleet.result.v1", "request_id": reference, "state": "unknown"}
    return result


def initial_reference(action: dict[str, Any], binding: dict[str, Any]) -> str | None:
    """Peer submit must omit request_id; only a returned native handle is usable.

    There is no precomputed handle for the supported transports. A lost submit
    response without a handle remains an honest unknown hold, never a resubmit.
    """
    return None


def _t3_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema") != "t3-fleet.result.v1":
        raise ActionError("transport-acceptance-unknown", 409)
    try:
        reference = destination_reference("t3", payload.get("request_id"))
    except ActionError:
        raise ActionError("transport-acceptance-unknown", 409) from None
    state = str(payload.get("state") or "")
    states = {
        "succeeded": "finished",
        "failed": "failed",
        "interrupted": "failed",
        "running": "running",
        "queued": "accepted",
        "preflight": "accepted",
        "dispatching": "accepted",
        "accepted": "accepted",
        "needs_attention": "needs-input",
        "needs-input": "needs-input",
        "waiting-for-input": "needs-input",
        # Offline is a hold, never evidence of execution or failure.
        "blocked_offline": "unknown",
    }
    receipt = {"status": states.get(state, "unknown"), "destination_ref": reference}
    thread = payload.get("thread_id")
    if isinstance(thread, str) and UUID.fullmatch(thread):
        receipt["destination_thread"] = thread
    return receipt


def dispatch(action: dict[str, Any], binding: dict[str, Any], executable: Path, prompt: Path) -> dict[str, Any]:
    target = action["request"]["target"]
    family, action_id = target["family"], action["action_id"]
    if family == "t3":
        # Optional protected binding field; generic installs retain T3's safe default.
        # Manifest validation must admit this enum separately from operator prompt text.
        runtime_mode = binding.get("runtime_mode", "approval-required")
        if not isinstance(runtime_mode, str) or runtime_mode not in T3_RUNTIME_MODES:
            raise ActionError("invalid-runtime-mode", 400)
        argv = [
            "submit",
            "--host",
            binding["host"],
            "--repository",
            binding["repository"],
            "--source-revision",
            binding["source_revision"],
            "--seat",
            action["request"]["model_id"],
            "--title",
            "Repair alert " + action_id,
            "--prompt-file",
            str(prompt),
            "--runtime-mode",
            runtime_mode,
        ]
        if binding.get("override_reason"):
            argv += ["--override-reason", binding["override_reason"]]
        try:
            return _t3_receipt(invoke(executable, argv, timeout=180))
        except T3PreacceptRefusal as error:
            return {"status": "failed", "rejection_code": error.code}
    if family == "openclaw":
        payload = invoke(
            executable,
            [
                "agent",
                "--agent",
                binding["agent_id"],
                "--session-key",
                binding.get("session_key", "brigade-action-" + action_id),
                "--message-file",
                str(prompt),
                # Gateway adds 30 seconds to this CLI wait; leave startup/cleanup
                # margin below invoke's 3600-second deadline. Never use --local.
                "--timeout",
                "3500",
                "--json",
            ],
        )
        # Blocking Gateway turns finish in this transient job. A zero exit alone is insufficient.
        if not isinstance(payload.get("runId"), str):
            raise ActionError("transport-acceptance-unknown", 409)
        return {
            "status": "finished" if payload.get("status") in {"ok", "completed"} else "failed",
            "destination_ref": opaque(payload["runId"]),
        }
    payload = invoke(
        Path(sys.executable),
        [
            "-m",
            "brigade.fleet_action_adapters",
            "--inbox-send",
            "--to",
            target["principal"],
            "--client-id",
            action_id,
            "--body-file",
            str(prompt),
        ],
        timeout=15,
    )
    message = payload.get("message")
    if not isinstance(message, dict) or message.get("client_id") != action_id or type(message.get("id")) is not int:
        raise ActionError("transport-acceptance-unknown", 409)
    return {
        "status": "accepted",
        "delivery": "acknowledged" if message.get("acked_at") else "delivered",
        "destination_ref": str(message["id"]),
    }


def _inbox_online(message: dict[str, Any]) -> dict[str, Any]:
    """Exactly one online call, supporting only the two co-versioned private APIs.

    The older broker backport selects credentials inside _online. Current builds
    capture and validate immutable authority before transport. Neither route may
    fall back after an exception, since acceptance may already have occurred.
    """
    from . import fleet_client_messages as messages

    online: Any = messages._online
    parameters = list(inspect.signature(online).parameters.values())
    parameter = inspect.Parameter
    if (
        len(parameters) != 3
        or parameters[0].name != "body"
        or parameters[0].kind != parameter.POSITIONAL_OR_KEYWORD
        or parameters[0].default is not parameter.empty
        or parameters[1].kind != parameter.KEYWORD_ONLY
        or parameters[2].name != "timeout"
        or parameters[2].kind != parameter.KEYWORD_ONLY
        or parameters[2].default is not None
    ):
        raise ValueError
    if parameters[1].name == "authority" and parameters[1].default is parameter.empty:
        authority = messages.MessageAuthority.capture()
        authority.validate()
        return online(message, authority=authority, timeout=10)
    if parameters[1].name == "token_file" and parameters[1].default is None:
        return online(message, token_file=None, timeout=10)
    raise ValueError


def _inbox_main(argv: list[str]) -> int:
    """Private subprocess entry point: data file in, minimal receipt out, no spool.

    Argument errors and upstream failures use closed output, never provider prose
    or tracebacks. The parent retains pipe caps and process-group supervision.
    """
    from . import fleet_client_messages as messages, fleet_hub_messages as limits

    try:
        if (
            len(argv) != 7
            or argv[0:2] != ["--inbox-send", "--to"]
            or argv[3] != "--client-id"
            or argv[5] != "--body-file"
        ):
            raise ValueError
        client_id = messages._client_id(argv[4])
        recipient = limits._text(argv[2], "to", 256)
        subject = limits._text("Repair alert " + client_id, "subject", limits.SUBJECT_LIMIT)
        reference = limits._text(client_id, "ref", limits.REF_LIMIT)
        metadata = messages._wire_metadata(thread=client_id)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(argv[6], flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limits.BODY_LIMIT:
                raise ValueError
            raw = stream.read(limits.BODY_LIMIT + 1)
        body = limits._text(raw.decode("utf-8"), "body", limits.BODY_LIMIT)
        message = {
            "action": "send",
            "to": recipient,
            "subject": subject,
            "body": body,
            "ref": reference,
            "priority": "normal",
            "client_id": client_id,
            **metadata,
        }
        payload = _inbox_online(message)
        response = payload.get("message")
        if (
            payload.get("queued") is True
            or not isinstance(response, dict)
            or response.get("client_id") != client_id
            or type(response.get("id")) is not int
            or not 1 <= response["id"] <= limits.MAX_SQLITE_INTEGER
        ):
            raise ValueError
        receipt = {"id": response["id"], "client_id": client_id}
        if response.get("acked_at"):
            receipt["acked_at"] = True
        print(json.dumps({"message": receipt}))
        return 0
    except Exception:
        print(json.dumps({"error": "transport-acceptance-unknown"}))
        return 1


def recover(action: dict[str, Any], executable: Path, reference: str | None) -> dict[str, Any]:
    family = action["request"]["target"]["family"]
    if family == "t3" and reference:
        reference = destination_reference("t3", reference)
        command = "recover" if action.get("status", "unknown") == "unknown" else "status"
        return _t3_receipt(invoke(executable, [command, "--request-id", reference], timeout=60))
    if family in {"grokbot", "dot"}:
        # Recovery only observes delivery; the ordinary CLI spool timer owns retries.
        cursor = 0
        for _ in range(10):
            flags = ["--id", reference] if reference else ["--after", str(cursor)]
            payload = invoke(executable, ["fleet", "msg", "sent", *flags, "--json"], timeout=15)
            messages = payload.get("messages", [])
            for message in messages if isinstance(messages, list) else []:
                if (
                    isinstance(message, dict)
                    and message.get("client_id") == action["action_id"]
                    and type(message.get("id")) is int
                    and (reference is None or str(message["id"]) == reference)
                ):
                    return {
                        "status": "accepted",
                        "delivery": "acknowledged" if message.get("acked_at") else "delivered",
                        "destination_ref": str(message["id"]),
                    }
            next_cursor = payload.get("next_cursor")
            if reference or type(next_cursor) is not int or next_cursor <= cursor or not messages:
                break
            cursor = next_cursor
        return (
            {"status": "unknown"}
            if reference is None
            else {"status": "accepted", "delivery": "delivered", "destination_ref": reference}
        )
    return {"status": "unknown", **({"destination_ref": reference} if reference else {})}


if __name__ == "__main__":
    raise SystemExit(_inbox_main(sys.argv[1:]))
