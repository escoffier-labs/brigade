"""Detailed delivery receipts using the fleet client's existing durable spool."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal

ReportResult = Literal["delivered", "queued", "rejected", "failed"]


_RECEIPT: ContextVar[list[ReportResult] | None] = ContextVar("fleet_event_receipt", default=None)


def report_event_detailed(event: dict[str, Any]) -> ReportResult:
    """Capture a receipt through report_event without changing its frozen API.

    The call-local channel carries no wire fields and cannot mix concurrent
    callers' receipts. Always reset it, including when an interrupt propagates.
    """
    from . import fleet_client as client

    receipt: list[ReportResult] = []
    token = _RECEIPT.set(receipt)
    try:
        if client.report_event(event):
            return "delivered"
        return receipt[-1] if receipt else "failed"
    finally:
        _RECEIPT.reset(token)


def report(event: dict[str, Any]) -> ReportResult:
    receipt = _RECEIPT.get()
    result = _report(event, detailed=receipt is not None)
    if receipt is not None:
        receipt.append(result)
    return result


def _report(event: dict[str, Any], *, detailed: bool) -> ReportResult:
    from . import fleet_client as client

    try:
        config = client.load_fleet_config()
        hub = config["hub_url"]
        if not hub:
            return "failed"
        resolved_node_id = client.resolve_node_id()
        if detailed and resolved_node_id == "unknown":
            return "failed"
        event = {**event, "node_id": resolved_node_id}
        token = config["token"]
        spool = client.spool_path(resolved_node_id)
    except Exception:
        return "failed"

    interrupted: BaseException | None = None
    delivered = False
    persisted = False

    def record_delivery(batch: list[dict[str, Any]]) -> None:
        nonlocal delivered
        # A successful HTTP acknowledgement remains delivery evidence even
        # when subsequent spool cleanup fails or another process flushes it.
        delivered = delivered or event in batch

    try:
        if not detailed and spool.is_file():
            with client._spool_lock(spool) as dir_fd:
                appended = client._spool_append_locked(spool, event, dir_fd=dir_fd)
                persisted = appended
                if appended:
                    try:
                        client._flush_locked(
                            spool,
                            hub,
                            token,
                            max_batches=1,
                            timeout=client.REPORT_TIMEOUT_SECONDS,
                            dir_fd=dir_fd,
                            preserve_poison=detailed,
                            on_delivered=record_delivery if detailed else None,
                        )
                    except (KeyboardInterrupt, SystemExit) as exc:
                        interrupted = exc
                if not detailed:
                    delivered = appended and not spool.is_file()
                persisted = appended and not delivered
        else:
            try:
                client._post_events(hub, token, event, timeout=client.REPORT_TIMEOUT_SECONDS)
                delivered = True
            except client._PoisonResponse:
                if detailed:
                    return "rejected"
            except (KeyboardInterrupt, SystemExit) as exc:
                interrupted = exc
            except Exception:
                pass
            if not delivered:
                with client._spool_lock(spool) as dir_fd:
                    persisted = client._spool_append_locked(spool, event, dir_fd=dir_fd)
                    if not persisted and interrupted is None:
                        return "failed"
    except (KeyboardInterrupt, SystemExit) as exc:
        # Preserve the event after interrupted spool bookkeeping, then propagate.
        interrupted = exc
        if not delivered:
            try:
                with client._spool_lock(spool) as dir_fd:
                    if event not in client._read_spool(spool, dir_fd=dir_fd):
                        if not client._spool_append_locked(spool, event, dir_fd=dir_fd):
                            client._LOG.warning("interrupted fleet event could not be persisted to the spool")
            except Exception as exc:
                client._LOG.warning("interrupted fleet event could not be made durable in the spool: %s", exc)
    except Exception as exc:
        client._LOG.warning("fleet event was not delivered and could not be spooled: %s", exc)
    if interrupted is not None:
        raise interrupted
    return "delivered" if delivered else "queued" if persisted else "failed"
