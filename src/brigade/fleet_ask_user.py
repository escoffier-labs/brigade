"""Bounded operator questions using online audit, optional desktop, and inbox waits."""

from __future__ import annotations

import html
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import unicodedata
from time import monotonic, sleep
from typing import Any, Callable

from . import fleet_client, fleet_client_messages as messages, fleet_hub_questions as questions
from .fleet_hub_messages import BODY_LIMIT, MAX_WAIT_MS
from .untrusted import scan_untrusted, wrap_untrusted

# The existing long-poll transport reserves five seconds beyond its wait.
# All requests share this single grace budget, including final audit reads
# and process cleanup. A local deadline never manufactures a hub timeout.
TRANSPORT_GRACE = fleet_client.FLUSH_TIMEOUT_SECONDS


class AskUserError(fleet_client.FleetClientError):
    def __init__(self, *, request_id: str, question_id: str | None = None, code: str = "question_unavailable") -> None:
        super().__init__("Question could not be completed. Retry with the same request_id.")
        self.request_id = request_id
        self.question_id = question_id
        self.code = code


def _plain(text: str) -> str:
    return "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def _process(argv: list[str], *, deadline: float, stop: Callable[[], bool]) -> str | None:
    remaining = deadline - monotonic()
    if remaining <= 0:
        return None
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                )
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return None
                    try:
                        process.wait(timeout=min(1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        if stop():
                            return None
                if process.returncode != 0:
                    return None
                output.seek(0)
                data = output.read(BODY_LIMIT + 1)
                if len(data) > BODY_LIMIT:
                    return None
                answer = data.decode("utf-8").rstrip("\r\n")
                return answer if answer.strip() else None
            finally:
                if process is not None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        elif process.poll() is None:  # pragma: no cover - Windows
                            process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=0.2)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


def _desktop(question: str, record: dict[str, Any], *, deadline: float, stop: Callable[[], bool]) -> str | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    text = _plain(f"{record['caller']} | Question {record['question_id']}\nAnswers are recorded.\n\n{question}")
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog") if not zenity else None
    if zenity:
        # Entry labels always run through g_strcompress and GTK mnemonic parsing,
        # even with --no-markup. Encode only this backend's native text syntax.
        encoded = text.replace("\\", "\\\\").replace("_", "__")
        answer = _process(
            [zenity, "--entry", "--no-markup", "--title", "Brigade operator question", "--text", encoded],
            deadline=deadline,
            stop=stop,
        )
    elif kdialog:
        answer = _process(
            [kdialog, "--title", "Brigade operator question", "--inputbox", html.escape(text)],
            deadline=deadline,
            stop=stop,
        )
    else:
        answer = None
    if answer is not None:
        return answer
    if stop():
        return None
    notice = shutil.which("notify-send")
    if notice:
        _process(
            [notice, "--", "Brigade operator question", "A question is waiting in the configured operator inbox."],
            deadline=min(deadline, monotonic() + 1),
            stop=stop,
        )
    return None


def _terminal(record: dict[str, Any]) -> dict[str, Any] | None:
    if record["status"] not in {"answered", "timeout"}:
        return None
    result = {key: record[key] for key in ("status", "question_id", "request_id", "message_id", "deadline")}
    if record["status"] == "answered":
        answer = record["answer"]
        result.update(
            answer=wrap_untrusted(answer, source_kind="peer-message"),
            source=record["source"],
            answered_by=record["answered_by"],
            answer_id=record["answer_id"],
            answered_at=record["answered_at"],
            scan={"flagged": scan_untrusted(answer).flagged},
        )
    return result


class AskUser:
    """At most two simultaneous asks per MCP station; retries share hub identity."""

    def __init__(self) -> None:
        self._slots = threading.BoundedSemaphore(2)

    def ask(
        self,
        question: str,
        *,
        timeout_secs: int = 60,
        request_id: str | None = None,
        authority: messages.MessageAuthority,
    ) -> dict[str, Any]:
        started = monotonic()
        request_id = secrets.token_hex(16) if request_id is None else request_id
        questions.validate_create({"question": question, "timeout_secs": timeout_secs, "request_id": request_id})
        if not self._slots.acquire(blocking=False):
            raise AskUserError(request_id=request_id, code="question_station_busy")
        question_id: str | None = None
        try:
            deadline = started + timeout_secs
            hard_deadline = deadline + TRANSPORT_GRACE

            def online(body: dict[str, Any]) -> dict[str, Any]:
                remaining = hard_deadline - monotonic()
                if remaining <= 0:
                    raise AskUserError(request_id=request_id, question_id=question_id)
                return messages._online(body, authority=authority, timeout=min(TRANSPORT_GRACE, remaining))

            record = online(
                {
                    "action": "question_create",
                    "question": question,
                    "timeout_secs": timeout_secs,
                    "request_id": request_id,
                }
            )
            question_id = record["question_id"]
            terminal = _terminal(record)
            if terminal is not None:
                return terminal
            if record["created"]:

                def desktop_done() -> bool:
                    nonlocal record
                    if record["status"] not in {"answered", "timeout"}:
                        record = online({"action": "question_status", "question_id": question_id})
                    return record["status"] in {"answered", "timeout"}

                answer = _desktop(question, record, deadline=deadline, stop=desktop_done)
                terminal = _terminal(record)
                if terminal is not None:
                    return terminal
                if answer is not None:
                    record = online({"action": "question_desktop", "question_id": question_id, "answer": answer})
                    terminal = _terminal(record)
                    if terminal is not None:
                        return terminal
            cursor = int(record["message_id"])
            delay = 0.25
            while monotonic() < hard_deadline:
                record = online({"action": "question_status", "question_id": question_id})
                terminal = _terminal(record)
                if terminal is not None:
                    return terminal
                remaining = deadline - monotonic()
                if remaining <= 0:
                    # Creation may have reached the hub after our local start.
                    sleep(min(delay, max(0, hard_deadline - monotonic())))
                    delay = min(delay * 2, 2)
                    continue
                wait_ms = int(min(remaining, MAX_WAIT_MS / 1000) * 1000)
                before = monotonic()
                capped = False
                try:
                    page = messages.list_messages(
                        after=cursor,
                        unacked=False,
                        wait_ms=wait_ms,
                        authority=authority,
                        timeout=max(0.001, hard_deadline - monotonic()),
                    )
                    cursor = page["next_cursor"]
                except messages.FleetMessageHubError as exc:
                    if exc.code != "message_wait_capped":
                        raise
                    capped = True
                # The trigger records answers even if another consumer acks them.
                record = online({"action": "question_status", "question_id": question_id})
                terminal = _terminal(record)
                if terminal is not None:
                    return terminal
                if capped or monotonic() - before < max(0.01, wait_ms / 2000):
                    sleep(min(delay, max(0, deadline - monotonic())))
                    delay = min(delay * 2, 2)
            raise AskUserError(request_id=request_id, question_id=question_id)
        except AskUserError:
            raise
        except Exception as exc:
            # Never leak upstream errors, credential fingerprints, or a local
            # answer that failed to reach the audit. Preserve resume identifiers.
            code = exc.code if isinstance(exc, messages.FleetMessageHubError) else None
            safe = (
                code
                if code
                in {
                    "question_request_conflict",
                    "question_not_owned",
                    "message_recipient_not_permitted",
                    "message_sender_inactive",
                }
                else "question_unavailable"
            )
            raise AskUserError(request_id=request_id, question_id=question_id, code=safe) from None
        finally:
            self._slots.release()
