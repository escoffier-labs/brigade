"""Runtime model-admission fencing for Brigade worker launches."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .. import fleet_model_admission
from ..roster import Roster
from . import artifacts, run_io

_EXACT_FIELDS = (
    "schema",
    "roster_revision",
    "roster_digest",
    "seat",
    "provider",
    "model",
    "reasoning",
    "binding",
)


@dataclass(frozen=True)
class RuntimeAdmissionResult:
    error: str | None
    records: tuple[dict[str, object], ...] = ()

    @property
    def target(self) -> dict[str, object] | None:
        return next((record for record in self.records if record.get("phase") == "target"), None)


@dataclass
class RuntimeAdmissionCoordinator:
    """Repeat Hub admission for each concrete launch using replay-stable IDs."""

    run_key: str
    attempts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def for_run(cls, output_dir: Path | None) -> RuntimeAdmissionCoordinator:
        from uuid import uuid4

        return cls(output_dir.name if output_dir is not None else str(uuid4()))

    @staticmethod
    def _matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
        return all(actual.get(name) == expected.get(name) for name in _EXACT_FIELDS)

    def admit(self, seat: str, receipt: dict[str, object]) -> RuntimeAdmissionResult:
        admissions = receipt.get("admissions")
        if not isinstance(admissions, list):
            return RuntimeAdmissionResult("controller admissions are missing")
        controller = next(
            (item for item in admissions if isinstance(item, Mapping) and item.get("seat") == seat),
            None,
        )
        if controller is None:
            return RuntimeAdmissionResult("controller admission is missing")
        revision = controller.get("roster_revision")
        digest = controller.get("roster_digest")
        if type(revision) is not int or not isinstance(digest, str):
            return RuntimeAdmissionResult("controller admission is malformed")
        runtime = receipt.setdefault("runtime_admissions", [])
        if not isinstance(runtime, list):
            return RuntimeAdmissionResult("runtime admission receipt is malformed")

        attempt = self.attempts.get(seat, 0) + 1
        self.attempts[seat] = attempt
        expected: Mapping[str, Any] = controller
        records: list[dict[str, object]] = []
        for phase in ("brigade-run", "target"):
            request_id = str(
                uuid5(NAMESPACE_URL, f"brigade:model-admission:v1:{self.run_key}:{seat}:{attempt}:{phase}")
            )
            decision = fleet_model_admission.admit_model(
                consumer="brigade-run",
                request_id=request_id,
                phase=phase,
                seat=seat,
                expect_revision=revision,
                expect_digest=digest,
                allow_lkg=phase == "brigade-run",
            )
            if not decision.ok:
                runtime.extend(records)
                return RuntimeAdmissionResult(decision.reason, tuple(records))
            if not self._matches(expected, decision.payload):
                runtime.extend(records)
                return RuntimeAdmissionResult(f"{phase} admission drift", tuple(records))
            expected = decision.payload
            record: dict[str, object] = {
                "request_id": request_id,
                "phase": phase,
                "model_admission": dict(decision.payload),
            }
            records.append(record)
        runtime.extend(records)
        return RuntimeAdmissionResult(None, tuple(records))

    @staticmethod
    def attach_target(roster: Roster, seat: str, target: Mapping[str, object]) -> Roster:
        routing = []
        for item in roster.seat_routing:
            updated = dict(item)
            if item.get("requested_seat") == seat and item.get("outcome") == "enabled":
                updated["target_model_admission"] = dict(target)
            routing.append(updated)
        return replace(roster, seat_routing=tuple(routing))


def roster_payload(roster: Roster, receipt: Mapping[str, Any]) -> dict[str, object]:
    payload = artifacts.roster_payload_with_admission(roster, receipt)
    runtime = receipt.get("runtime_admissions")
    if isinstance(runtime, list):
        payload["runtime_admissions"] = [dict(item) for item in runtime if isinstance(item, Mapping)]
    return payload


def persist(output_dir: Path, roster: Roster, receipt: Mapping[str, Any]) -> None:
    run_io._write_json(output_dir / "model-policy.json", receipt)
    run_io._write_json(output_dir / "roster.json", roster_payload(roster, receipt))
    if (output_dir / "run.json").is_file():
        artifacts.update_run_receipt(output_dir, seat_routing=[dict(item) for item in roster.seat_routing])
