"""Opt-in renewal of last-published local sessions with a proven live owner."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Callable, Sequence

from . import dirfd, fleet_client
from .claude_hooks import anchored_io
from .fleet_openclaw_observer import ObservationError, _remaining, _require_node_auth
from .fleet_session_heartbeat_state import LOCK_SECONDS, SUFFIX, read_record, snapshot_from_record
from .fleet_session_presence import SessionSnapshot, _hub_url_configured
from .fleet_session_process import ProcessTable
from .wiring import resolve_wired_target

SESSION_LIMIT = 200
PUBLISH_LIMIT = 50
DIRECTORY_LIMIT = 2048
TARGET_LIMIT = 32
OBSERVATION_BUDGET_SECONDS = 15
Publisher = Callable[[SessionSnapshot], fleet_client.SessionWriteResult]


def observe(
    targets: Sequence[Path],
    *,
    publish: bool = False,
    processes: ProcessTable | None = None,
    publisher: Publisher | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + OBSERVATION_BUDGET_SECONDS
    report: dict[str, object] = {
        "ok": True,
        "mode": "publish" if publish else "inspect",
        "observed": 0,
        "live": 0,
        "published": 0,
        "skipped": 0,
        "truncated": False,
        "session_limit": SESSION_LIMIT,
        "publish_limit": PUBLISH_LIMIT,
        "budget_seconds": OBSERVATION_BUDGET_SECONDS,
    }
    if publish:
        _require_node_auth()
    if not _hub_url_configured():
        return {**report, "ok": False, "error": "hub_unconfigured"}
    table = processes or ProcessTable()
    send = publisher or fleet_client.publish_session
    observed = live = published = attempted = 0
    seen: set[Path] = set()
    try:
        for target_index, raw_target in enumerate(targets):
            _remaining(deadline, cap=1)
            if target_index >= TARGET_LIMIT:
                report["truncated"] = True
                break
            target = raw_target.expanduser().resolve()
            if target in seen:
                continue
            seen.add(target)
            try:
                with anchored_io.session_directory(target) as anchor:
                    names = dirfd.child_names(anchor.fd, DIRECTORY_LIMIT + 1)
                    if len(names) > DIRECTORY_LIMIT:
                        report["truncated"] = True
                    for name in names[:DIRECTORY_LIMIT]:
                        _remaining(deadline, cap=1)
                        if not name.endswith(SUFFIX):
                            continue
                        if observed >= SESSION_LIMIT or (publish and attempted >= PUBLISH_LIMIT):
                            report["truncated"] = True
                            break
                        observed += 1
                        try:
                            with anchor.held_lock(name + ".lock", deadline_seconds=LOCK_SECONDS):
                                record = read_record(anchor, name)
                                if record is None:
                                    continue
                                snapshot = snapshot_from_record(record, target, name)
                                if (
                                    snapshot is None
                                    or resolve_wired_target(str(target), harness=snapshot.harness) != target
                                    or not table.alive(record["process"], snapshot.harness)
                                ):
                                    continue
                                live += 1
                                if publish:
                                    _remaining(deadline, cap=1, reserve=fleet_client.REPORT_TIMEOUT_SECONDS)
                                    anchor.validate()
                                    attempted += 1
                                    if not send(snapshot).ok:
                                        raise ObservationError("publication_failed")
                                    published += 1
                        except ObservationError:
                            raise
                        except (OSError, ValueError, KeyError, TypeError):
                            continue
            except FileNotFoundError:
                continue
            if observed >= SESSION_LIMIT or (publish and attempted >= PUBLISH_LIMIT):
                if target_index + 1 < len(targets):
                    report["truncated"] = True
                break
    except ObservationError as exc:
        report.update(ok=False, error=str(exc))
        if str(exc) == "observation_timeout":
            report["truncated"] = True
    except Exception:
        report.update(ok=False, error="observation_failed")
    report.update(observed=observed, live=live, published=published, skipped=observed - published)
    report["ok"] = not (report.get("error") or report["truncated"])
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, action="append", required=True, help="Wired checkout (repeatable)")
    parser.add_argument("--publish", action="store_true", help="Renew through this enrolled node")
    args = parser.parse_args(argv)
    try:
        report = observe(args.target, publish=args.publish)
    except ObservationError as exc:
        report = {"ok": False, "error": str(exc), "observed": 0, "published": 0, "skipped": 0}
    except Exception:
        report = {"ok": False, "error": "observation_failed", "observed": 0, "published": 0, "skipped": 0}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2 if report.get("truncated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
