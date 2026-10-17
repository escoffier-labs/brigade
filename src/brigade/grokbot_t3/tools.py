"""t3-dispatch tool handlers: allowlists, limits, host pick, fallback seat, projection."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from . import contracts as c
from .fleet import FleetCallError, FleetProtocolError, FleetTimeout, RevisionError
from .ledger import Ledger
from .runtime_config import Runtime

DISPATCH_HEADER = (
    'Dispatched by Grok Bot "{caller}" through Brigade t3-dispatch.\n'
    "Owner-authorized: implement, test, commit, push a branch, and open a pull request.\n"
    "Do not merge. Stop and ask only for spending, new external accounts, publication outside "
    "the GitHub PR flow, destructive data loss, or credentials.\n\n"
)
STATUS_KEYS = ("request_id", "host", "state", "needs_attention", "thread_id", "updated_at", "pending_request")
FALLBACK_CODE = "policy_rejected"
FALLBACK_REASON = "model-admission-denied"
_REOPEN_ATTEMPTS = 3


def compose_prompt(caller: str, prompt: str) -> str:
    return DISPATCH_HEADER.format(caller=caller) + prompt


def project_status(result: dict[str, Any]) -> dict[str, Any]:
    projected = {key: result[key] for key in STATUS_KEYS if key in result}
    admission = result.get("admission")
    if isinstance(admission, dict) and isinstance(admission.get("seat"), str):
        projected["seat"] = admission["seat"]
    return projected


def _fleet_error(exc: FleetCallError) -> c.T3DispatchError:
    return c.T3DispatchError("fleet-error", fleet_code=exc.code, fleet_reason=exc.reason, request_id=exc.request_id)


class T3DispatchTools:
    def __init__(
        self,
        *,
        runtime: Runtime,
        runner: Any,
        ledger: Ledger,
        now: Callable[[], datetime] | None = None,
    ):
        self._dispatch_lock = threading.Lock()
        self.runtime = runtime
        self.runner = runner
        self.ledger = ledger
        self.now = now or (lambda: datetime.now(timezone.utc))

    def inventory(self) -> list[str]:
        return sorted(c.TOOLS)

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "t3_approve": self.approve,
            "t3_answer": self.answer,
            "t3_capacity": self.capacity,
            "t3_followup": self.followup,
            "t3_interrupt": self.interrupt,
            "t3_recent": self.recent,
            "t3_status": self.status,
            "t3_submit": self.submit,
            "t3_wait": self.wait,
        }
        try:
            if name not in handlers:
                raise c.T3DispatchError("invalid_request")
            result = handlers[name](c.PARSERS[name](arguments))
            if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > c.PUBLIC_RESULT_LIMIT:
                raise c.T3DispatchError("unavailable")
            return result
        except c.T3DispatchError as exc:
            return exc.public_error()

    def _require_enabled(self) -> None:
        if not self.runtime.enabled:
            raise c.T3DispatchError("dispatch-disabled")

    def _known(self, request_id: str) -> dict[str, Any]:
        row = self.ledger.get(request_id)
        if row is None:
            raise c.T3DispatchError("unknown-request")
        return row

    def _fleet(self, fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        try:
            return fn(*args)
        except FleetCallError as exc:
            raise _fleet_error(exc) from exc
        except FleetTimeout as exc:
            raise c.T3DispatchError("unavailable") from exc

    def _record_state(
        self,
        request_id: str,
        result: dict[str, Any],
        expected_revision: int | None = None,
        *,
        owner_write: bool = False,
    ) -> None:
        """Write the reported state; with expected_revision, only if no other actor changed the row meanwhile."""
        state = result.get("state")
        if isinstance(state, str):
            self.ledger.update_state(
                request_id, state, self.now(), expected_revision=expected_revision, owner_write=owner_write
            )

    def capacity(self, _args: dict[str, Any]) -> dict[str, Any]:
        result = self._fleet(self.runner.capacity)
        hosts = []
        for entry in result.get("hosts", []):
            if isinstance(entry, dict) and entry.get("host") in self.runtime.hosts:
                hosts.append({key: entry[key] for key in ("host", "state", "active", "limit") if key in entry})
        return {"hosts": hosts}

    def _choose_host(self, requested: str) -> str:
        if requested != c.HOST_AUTO:
            return requested
        result = self._fleet(self.runner.capacity)
        best: tuple[int, int, str] | None = None
        for entry in result.get("hosts", []):
            if not isinstance(entry, dict):
                continue
            host = entry.get("host")
            active = entry.get("active")
            limit = entry.get("limit")
            if host not in self.runtime.hosts or entry.get("state") != "eligible":
                continue
            if not isinstance(active, int) or not isinstance(limit, int) or limit - active <= 0:
                continue
            rank = (limit - active, -self.runtime.hosts.index(host), host)
            if best is None or rank > best:
                best = rank
        if best is None:
            raise c.T3DispatchError("no-eligible-host")
        return best[2]

    def _submit_once(
        self, *, host: str, args: dict[str, Any], revision: str, title: str, seat: str, prompt: str
    ) -> dict[str, Any]:
        self._check_daily_limit()
        self.ledger.record_attempt(self.now())
        override = f"grokbot t3-dispatch seat {seat} for {args['caller']}" if self.runtime.fleet_enrolled else None
        return self.runner.submit(
            host=host,
            repository=args["repository"],
            source_revision=revision,
            title=title,
            seat=seat,
            prompt=prompt,
            interaction_mode=args["interaction_mode"],
            override_reason=override,
        )

    def _check_daily_limit(self) -> None:
        midnight = self.now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.ledger.attempts_since(midnight) >= self.runtime.daily_limit:
            raise c.T3DispatchError("daily-limit")

    def submit(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._dispatch_lock.acquire(timeout=5):
            raise c.T3DispatchError("unavailable")
        try:
            return self._submit_locked(args)
        finally:
            self._dispatch_lock.release()

    def _submit_locked(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        runtime = self.runtime
        repo = runtime.repositories.get(args["repository"])
        if repo is None:
            raise c.T3DispatchError("repository-not-allowed")
        if args["host"] != c.HOST_AUTO and args["host"] not in runtime.hosts:
            raise c.T3DispatchError("host-not-allowed")
        seat: str = args["seat"] or runtime.default_seat
        if seat not in runtime.seats:
            raise c.T3DispatchError("seat-not-allowed")
        if len(args["prompt"].encode("utf-8")) > runtime.prompt_max_bytes:
            raise c.T3DispatchError("prompt-too-large")
        if self.ledger.open_count(self.now()) >= runtime.max_open:
            raise c.T3DispatchError("open-limit")
        now = self.now()
        self._check_daily_limit()
        try:
            revision = self.runner.resolve_revision(repo.remote, args["base_ref"] or repo.default_branch)
        except RevisionError as exc:
            raise c.T3DispatchError("base-ref-unresolved") from exc
        host = self._choose_host(args["host"])
        title = f"[grokbot:{args['caller']}] {args['title']}"[: c.FLEET_TITLE_MAX].rstrip()
        prompt = compose_prompt(args["caller"], args["prompt"])
        fallback_used = False
        # Reserve the slot before t3-fleet can launch anything; release it only once the outcome is durable or refused.
        reservation = self.ledger.record_in_doubt(args["caller"], now)
        try:
            try:
                result = self._submit_once(
                    host=host, args=args, revision=revision, title=title, seat=seat, prompt=prompt
                )
            except FleetCallError as exc:
                can_fall_back = (
                    exc.code == FALLBACK_CODE
                    and exc.request_id is None
                    and exc.reason == FALLBACK_REASON
                    and args["seat"] is None
                    and runtime.fallback_seat is not None
                )
                if not can_fall_back or not isinstance(runtime.fallback_seat, str):
                    raise
                seat = runtime.fallback_seat
                fallback_used = True
                result = self._submit_once(
                    host=host, args=args, revision=revision, title=title, seat=seat, prompt=prompt
                )
        except FleetCallError as exc:
            if not isinstance(exc.request_id, str) or not c.HANDLE_RE.fullmatch(exc.request_id):
                if isinstance(exc, FleetProtocolError):
                    raise c.T3DispatchError("submit-unknown") from exc
                # A definite refusal launched nothing, so the slot goes back.
                self.ledger.release_in_doubt(reservation)
                raise _fleet_error(exc) from exc
            result = {"request_id": exc.request_id, "state": "unknown"}
        except FleetTimeout as exc:
            # The launch may have happened with no request id to track, so the reservation stays until it expires.
            raise c.T3DispatchError("submit-unknown") from exc
        except c.T3DispatchError:
            # Raised before a launch (for example the daily limit stopping the fallback attempt): nothing to hold.
            self.ledger.release_in_doubt(reservation)
            raise
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not c.HANDLE_RE.fullmatch(request_id):
            # A success envelope without a usable id may still mean a launch happened; keep the reservation.
            raise c.T3DispatchError("submit-unknown")
        raw_state = result.get("state")
        known_states = c.TERMINAL_STATES | {
            "queued",
            "preflight",
            "dispatching",
            "running",
            "needs_attention",
            "blocked_offline",
        }
        state = raw_state if isinstance(raw_state, str) and raw_state in known_states else "unknown"
        self.ledger.record_submit(
            request_id=request_id,
            caller=args["caller"],
            repository=args["repository"],
            host=str(result.get("host") or host),
            seat=seat,
            title=title,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            state=state,
            now=now,
            interaction_mode=args["interaction_mode"],
            release_reservation=reservation,
        )
        if state == "unknown":
            raise c.T3DispatchError("submit-unknown", request_id=request_id)
        return {
            "request_id": request_id,
            "host": str(result.get("host") or host),
            "seat": seat,
            "state": state,
            "fallback_used": fallback_used,
        }

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        row = self._known(args["request_id"])
        result = self._fleet(self.runner.status, args["request_id"])
        self._record_state(args["request_id"], result, expected_revision=row["state_revision"])
        return project_status(result)

    def wait(self, args: dict[str, Any]) -> dict[str, Any]:
        row = self._known(args["request_id"])
        result = self._fleet(self.runner.wait, args["request_id"], args["timeout_s"])
        self._record_state(args["request_id"], result, expected_revision=row["state_revision"])
        return project_status(result)

    def followup(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._dispatch_lock.acquire(timeout=5):
            raise c.T3DispatchError("unavailable")
        try:
            return self._followup_locked(args)
        finally:
            self._dispatch_lock.release()

    def _followup_locked(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        row = self._known(args["request_id"])
        if row["last_state"] in c.TERMINAL_STATES and self.ledger.open_count(self.now()) >= self.runtime.max_open:
            raise c.T3DispatchError("open-limit")
        if len(args["message"].encode("utf-8")) > self.runtime.prompt_max_bytes:
            raise c.T3DispatchError("prompt-too-large")
        self._check_daily_limit()
        self.ledger.record_attempt(self.now())
        request_id = args["request_id"]
        prior = row["last_state"]
        notified = row["notified_state"]
        revision = row["state_revision"]
        # Mark the row provisional before t3-fleet accepts the turn, from any prior state: a crash cannot hide
        # the new turn, and a poll that records the previous turn (running -> succeeded) cannot overwrite it.
        # A concurrent poll can win the compare-and-set first; retry against the fresh revision, and never
        # launch without the marker.
        for _ in range(_REOPEN_ATTEMPTS):
            reopened = self.ledger.update_state(
                request_id, c.REOPENING_STATE, self.now(), expected_revision=revision, owner_write=True
            )
            current = self._known(request_id)
            revision = current["state_revision"]
            if reopened:
                break
            prior, notified = current["last_state"], current["notified_state"]
        else:
            raise c.T3DispatchError("unavailable")
        try:
            result = self.runner.followup(request_id, args["message"], row["interaction_mode"])
        except (FleetTimeout, FleetProtocolError) as exc:
            # t3-fleet may have accepted the turn, so the request is open again until a status call says otherwise.
            self.ledger.update_state(request_id, "unknown", self.now(), expected_revision=revision, owner_write=True)
            raise c.T3DispatchError("submit-unknown", request_id=request_id) from exc
        except FleetCallError as exc:
            # A definite refusal launched nothing, so restore the prior state and its delivered wake.
            self.ledger.restore_state(request_id, prior, notified, self.now(), expected_revision=revision)
            raise _fleet_error(exc) from exc
        if not isinstance(result.get("state"), str):
            # Accepted but state-less: the request may have reopened, so keep it open until a status call says otherwise.
            self.ledger.update_state(request_id, "unknown", self.now(), expected_revision=revision, owner_write=True)
            raise c.T3DispatchError("submit-unknown", request_id=request_id)
        self._record_state(request_id, result, expected_revision=revision, owner_write=True)
        return project_status(result)

    def answer(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        row = self._known(args["request_id"])
        result = self._fleet(self.runner.answer, args["request_id"], args["input_id"], args["answers"])
        self._record_state(args["request_id"], result, expected_revision=row["state_revision"])
        return project_status(result)

    def approve(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        row = self._known(args["request_id"])
        result = self._fleet(self.runner.approve, args["request_id"], args["approval_id"], args["decision"])
        self._record_state(args["request_id"], result, expected_revision=row["state_revision"])
        return project_status(result)

    def interrupt(self, args: dict[str, Any]) -> dict[str, Any]:
        row = self._known(args["request_id"])
        result = self._fleet(self.runner.interrupt, args["request_id"])
        self._record_state(args["request_id"], result, expected_revision=row["state_revision"])
        return project_status(result)

    def recent(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"requests": self.ledger.recent(args["caller"], args["limit"])}
