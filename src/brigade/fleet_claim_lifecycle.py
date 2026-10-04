"""Confirmed repository-claim authority, cleanup, and cancellation.

Runtime dependencies are resolved through the fleet client facade so callers
can replace its clock, threading namespace, transport, and interrupt hooks.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading


def _client() -> ModuleType:
    # Resolve after import initialization, including when this module loads first.
    from . import fleet_client

    return fleet_client


class _ConfirmedClaimLease:
    """Serialize local authority independently of in-flight hub requests.

    A deadline is request-start plus the exact integer TTL. LOST is sticky:
    a late success cannot restore authority, even before the watcher runs.
    Notification happens outside the Condition, after cancellation is set.
    """

    def __init__(
        self,
        request_start: float,
        ttl_seconds: int,
        *,
        cancel_event: threading.Event,
        abort: bool,
        on_loss: Callable[[str], None],
    ) -> None:
        self.condition = _client().threading.Condition()
        self.stopped = _client().threading.Event()
        self._state = "ACTIVE"
        self._loss_reason: str | None = None
        self._deadline = request_start + ttl_seconds
        self._ttl_seconds = ttl_seconds
        self._cancel_event = cancel_event
        self._abort = abort
        self._on_loss = on_loss

    def _lose_locked(self, reason: str, *, notify: bool) -> str | None:
        self._state = "LOST"
        self._loss_reason = reason
        if notify and self._abort:
            self._cancel_event.set()
        self.condition.notify_all()
        return reason if notify else None

    def _expire_locked(self, now: float) -> str | None:
        if self._state == "ACTIVE" and now >= self._deadline:
            return self._lose_locked("lease-expired", notify=True)
        return None

    def _publish(self, reason: str | None) -> None:
        # Only the winning ACTIVE -> LOST transition returns a reason. It
        # admits this one-shot notification under the authority lock, so a
        # later STOPPING cannot erase the obligation. Arbitrary callbacks
        # run outside the lock, independently of interrupt suppression.
        if reason is not None:
            self._on_loss(reason)

    def active(self, *, pending_orphan: threading.Event | None = None) -> bool:
        with self.condition:
            reason = self._expire_locked(_client().time.monotonic())
            active = self._state == "ACTIVE"
            if active and pending_orphan is not None:
                pending_orphan.set()
        self._publish(reason)
        return active

    def confirm(self, request_start: float, *, on_rejected: Callable[[], None] | None = None) -> bool:
        with self.condition:
            now = _client().time.monotonic()
            reason = self._expire_locked(now)
            candidate = request_start + self._ttl_seconds
            if self._state == "ACTIVE" and now >= candidate:
                reason = self._lose_locked("lease-expired", notify=True)
            accepted = self._state == "ACTIVE"
            if accepted:
                self._deadline = candidate
                self.condition.notify_all()
        try:
            if not accepted and on_rejected is not None:
                # A successful but rejected request may have extended the
                # hub row. Fence it off before notifying: the heartbeat can
                # itself discover expiry, and its callback may block.
                on_rejected()
        finally:
            self._publish(reason)
        return accepted

    def terminate(self, reason: str, *, notify: bool = False) -> bool:
        with self.condition:
            expiry = self._expire_locked(_client().time.monotonic())
            accepted = self._state == "ACTIVE"
            loss = self._lose_locked(reason, notify=notify) if accepted else expiry
        self._publish(loss)
        return accepted

    def watch(self) -> None:
        with self.condition:
            while self._state == "ACTIVE":
                reason = self._expire_locked(_client().time.monotonic())
                if reason is not None:
                    break
                self.condition.wait(timeout=max(0.0, self._deadline - _client().time.monotonic()))
            else:
                return
        self._publish(reason)

    def stop(self) -> None:
        with self.condition:
            # Shutdown control is separate from the terminal loss cause and
            # any notification already admitted by that loss transition.
            self._state = "STOPPING"
            self.stopped.set()
            self.condition.notify_all()

    def _dispatch_interrupt(self) -> None:
        # This bounded process signal and stop share the authority lock.
        # A false outer stop snapshot is insufficient: STOPPING may win
        # between that snapshot and dispatch into subsequent main-thread work.
        with self.condition:
            if self._state != "STOPPING":
                _client()._thread.interrupt_main()


def _schedule_orphan_release(
    target: str, *, node_id: str, holder: str, conductor: str | None, ttl_seconds: int
) -> None:
    """Best-effort background cleanup after a lost acquire response.

    The hub may have committed a row for this holder that we never saw; left
    alone it would block every other machine for the full TTL. A daemon
    thread retries ``release`` until the hub gives any definitive answer
    (released, already gone, or held by someone else) or the TTL window
    passes and expiry makes the point moot. The first attempt happens
    immediately (#1157): a short-lived ``brigade run`` that lost its acquire
    response must release before process exit kills this daemon thread.
    """

    def _loop() -> None:
        started = _client().time.monotonic()
        deadline = started + ttl_seconds
        while _client().time.monotonic() < deadline:
            outcome = _client().release_claim(target, node_id=node_id, holder=holder, conductor=conductor)
            if outcome.reason == "hub-unavailable":
                _client().time.sleep(
                    min(_client().ORPHAN_RELEASE_RETRY_SECONDS, max(0.0, deadline - _client().time.monotonic()))
                )
                continue
            if (
                outcome.reason == "missing"
                and _client().time.monotonic() - started < _client().ORPHAN_RELEASE_UNCERTAINTY_SECONDS
            ):
                # Not definitive yet (#1157): the acquire whose response we
                # lost may still commit its row after this "missing". Keep
                # retrying until the uncertainty window closes.
                _client().time.sleep(min(0.5, max(0.0, deadline - _client().time.monotonic())))
                continue
            return

    _client().threading.Thread(target=_loop, name="brigade-fleet-claim-orphan-release", daemon=True).start()


def _abort_claim_owner(
    target: str,
    reason: str,
    *,
    owner_thread: threading.Thread,
    cancel_event: threading.Event,
    on_claim_lost: Callable[[str | None], None] | None,
    stop_event: threading.Event | None = None,
    _interrupt_dispatcher: Callable[[], None] | None = None,
) -> None:
    """Fail-closed abort of a guarded block whose claim was lost (#1152).

    The interrupt is triggered independently of the caller's callback (#1157
    round 2): a watchdog daemon forces it once the callback exceeds
    ``CLAIM_LOST_CALLBACK_GRACE_SECONDS`` (a blocking callback delays
    notification, never the abort), and the callback runs under a
    ``BaseException`` catch so ``SystemExit`` raised inside it is logged
    instead of honored. The callback completes (or the grace expires)
    *before* the interrupt fires, so callers can record their state and have
    it observed by the time the interrupt lands. When the claim's owner is
    not the process main thread, ``_thread.interrupt_main()`` would stab an
    unrelated thread while the owner kept working, so the interrupt is
    replaced by the cooperative channel: ``cancel_event`` is already set and
    the guarded block must unwind through ``ClaimDecision.cancel_event``.
    The watchdog's grace fire and the callback-finally fire share one
    lock-guarded check-and-set (#1157 round 3), so the grace boundary can
    never deliver two interrupts.
    """
    owner_is_main = owner_thread is _client().threading.main_thread()
    # Cooperative observers learn first, whatever happens to the callback.
    cancel_event.set()

    def _interrupt() -> None:
        if not owner_is_main:
            # Committed cooperative notification survives shutdown. Only
            # main-thread signaling needs stale-interrupt suppression.
            _client()._LOG.warning(
                "fleet claim on %s lost (%s): repo_claim was entered off the main thread; "
                "the guarded block must unwind via ClaimDecision.cancel_event",
                target,
                reason,
            )
            return
        if stop_event is not None and stop_event.is_set():
            return
        if _interrupt_dispatcher is None:
            _client()._thread.interrupt_main()
        else:
            _interrupt_dispatcher()

    if on_claim_lost is None:
        _interrupt()
        return
    done = _client().threading.Event()
    fired = _client().threading.Event()
    # One-shot gate (#1157 round 3): at the grace boundary the watchdog and
    # the callback's finally can both observe "not fired"; check-and-set is
    # atomic under this lock, so exactly one path interrupts.
    fired_gate = _client().threading.Lock()

    def _fire_once(*, forced: bool) -> None:
        with fired_gate:
            if fired.is_set():
                return
            fired.set()
        if forced:
            _client()._LOG.warning(
                "fleet claim-lost callback for %s exceeded %.1fs; forcing the abort",
                target,
                _client().CLAIM_LOST_CALLBACK_GRACE_SECONDS,
            )
        _interrupt()

    def _watchdog() -> None:
        if done.wait(_client().CLAIM_LOST_CALLBACK_GRACE_SECONDS) or fired.is_set():
            return
        _fire_once(forced=True)

    _client().threading.Thread(target=_watchdog, name="brigade-fleet-claim-abort", daemon=True).start()
    try:
        # repo_claim supplies the dispatcher only for a callback admitted by
        # its locked loss transition. That callback survives STOPPING, while
        # the final interrupt remains fenced by the dispatcher's lock.
        # Direct users without the dispatcher retain their stop behavior.
        if _interrupt_dispatcher is not None or stop_event is None or not stop_event.is_set():
            on_claim_lost(reason)
    except BaseException:
        _client()._LOG.warning("fleet claim-lost callback raised; interrupting anyway", exc_info=True)
    finally:
        done.set()
        _fire_once(forced=False)
