"""Bounded operator questions using online audit, optional desktop, and inbox waits."""

from __future__ import annotations

import math
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
        guidance = {
            "question_operator_invalid": (
                "Configure the hub operator recipient as a dedicated agent: message principal. "
                "Keep its token in the operator-only inbox client."
            ),
            "question_request_conflict": (
                "Request conflicts with an existing question. Resume with the same request_id and the original "
                "question and timeout_secs. Use a new request_id for a new question."
            ),
            "question_not_owned": (
                "Question is not owned by the current credential. Use the current credential and a new request_id "
                "to ask a new question."
            ),
        }
        super().__init__(guidance.get(code, "Question could not be completed. Retry with the same request_id."))
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
                    # Helpers need the desktop session, never the station's
                    # credentials or interpreter/plugin injection settings.
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key
                        in {
                            "DISPLAY",
                            "WAYLAND_DISPLAY",
                            "XAUTHORITY",
                            "DBUS_SESSION_BUS_ADDRESS",
                            "XDG_RUNTIME_DIR",
                            "HOME",
                            "LANG",
                            "LANGUAGE",
                            "LC_ALL",
                            "LC_CTYPE",
                            "LC_MESSAGES",
                            "LC_COLLATE",
                            "LC_NUMERIC",
                            "LC_TIME",
                            "LC_MONETARY",
                            "SystemRoot",
                            "SYSTEMROOT",
                            "WINDIR",
                            "TEMP",
                            "TMP",
                        }
                    },
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
    preview = "\n".join(_plain(question).splitlines()[:8])[:512]
    if preview != _plain(question):
        preview += "\n[truncated]"
    text = (
        f"{record['caller']} | Question {record['question_id']}\n"
        "Answers are recorded. The full question is in the operator's hub inbox.\n\n"
        f"{preview}"
    )
    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog") if not zenity else None
    if zenity:
        # Entry labels always run through g_strcompress and GTK mnemonic parsing,
        # even with --no-markup. Encode only this backend's native text syntax.
        encoded = text.replace("\\", "\\\\").replace("_", "__")
        answer = _process(
            [
                zenity,
                "--entry",
                "--no-markup",
                "--timeout",
                str(max(1, math.ceil(deadline - monotonic()))),
                "--title",
                "Brigade operator question",
                "--text",
                encoded,
            ],
            deadline=deadline,
            stop=stop,
        )
    elif kdialog:
        # Qt AutoText stops at the first newline. The hub-bound caller and ID
        # form a plain first line. QInputDialog's QLabel buddy consumes
        # ampersand mnemonics, for which Qt's native literal escape is &&.
        encoded = text.replace("&", "&&")
        answer = _process(
            [kdialog, "--title", "Brigade operator question", "--inputbox", encoded],
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
        # Only the hub's accepted reply from its configured operator carries
        # operator authority. A desktop report uses the asking credential and
        # cannot prove operator identity, so it has its own local-input fence.
        operator_authenticated = record["source"] == "operator_reply" and record["answered_by"] == record["operator"]
        desktop = record["source"] == "caller_reported_desktop"
        result.update(
            answer=answer
            if operator_authenticated
            else wrap_untrusted(answer, source_kind="desktop-local" if desktop else "peer-message"),
            operator=record["operator"],
            operator_authenticated=operator_authenticated,
            source="desktop" if desktop else record["source"],
            answered_by=record["answered_by"],
            answer_id=record["answer_id"],
            answered_at=record["answered_at"],
            scan={"flagged": scan_untrusted(answer).flagged},
        )
        if desktop:
            result["answered_at_local_desktop"] = True
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
                response = messages._online(body, authority=authority, timeout=min(TRANSPORT_GRACE, remaining))
                returned_id = response.get("question_id")
                if (
                    response.get("request_id") != request_id
                    or not isinstance(returned_id, str)
                    or questions._QUESTION_ID.fullmatch(returned_id) is None
                    or (question_id is not None and returned_id != question_id)
                ):
                    raise AskUserError(request_id=request_id, question_id=question_id)
                return response

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
                        try:
                            record = online({"action": "question_status", "question_id": question_id})
                        except messages.MessageAuthorityError:
                            raise
                        except messages.FleetMessageHubError as exc:
                            # A failed poll cannot cancel a still-live prompt.
                            # Authentication and other permanent refusals must
                            # stop it; transport retries keep the same deadline.
                            if not (
                                (exc.status is None and exc.code == "hub_unavailable")
                                or exc.status in {408, 425, 429}
                                or (exc.status is not None and exc.status >= 500)
                            ):
                                raise
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
                    "message_recipient_quota",
                    "message_sender_quota",
                    "message_sender_recipient_quota",
                    "question_station_busy",
                    "question_operator_invalid",
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
