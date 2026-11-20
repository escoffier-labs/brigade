"""The native child core is host neutral, and the T3 adapter binds it without changing a persisted byte."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import brigade
from brigade import native_child_admission as admission_core
from brigade import native_child_capacity as capacity_core
from brigade import native_child_journal as journal_core
from brigade import native_t3_admission, native_t3_dispatch, native_t3_lifecycle
from brigade import roster as roster_mod
from brigade.native_child_host import NativeChildHost, TargetUnresolved
from brigade.native_t3_host import HOST

SRC = Path(brigade.__file__).parent
CORE_MODULES = sorted(SRC.glob("native_child_*.py"))
HOST_WORDS = re.compile(r"\bt3\b|t3_|_t3|delegate_task|task_status|clientRequestId|native-t3", re.IGNORECASE)


def test_core_modules_exist() -> None:
    names = {path.stem for path in CORE_MODULES}
    assert {
        "native_child_host",
        "native_child_capacity",
        "native_child_admission",
        "native_child_journal",
        "native_child_claim",
        "native_child_lifecycle",
        "native_child_custody",
        "native_child_observations",
    } <= names


@pytest.mark.parametrize("path", CORE_MODULES, ids=lambda path: path.stem)
def test_core_module_imports_no_adapter_module(path: Path) -> None:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported = [node.module or "", *(alias.name for alias in node.names)]
        else:
            continue
        for name in imported:
            assert "native_t3" not in name, f"{path.name} imports {name}"


@pytest.mark.parametrize("path", CORE_MODULES, ids=lambda path: path.stem)
def test_core_module_names_no_host(path: Path) -> None:
    match = HOST_WORDS.search(path.read_text(encoding="utf-8"))
    assert match is None, f"{path.name} names a host: {match.group(0) if match else ''}"


def test_core_loads_without_pulling_in_the_adapter() -> None:
    names = [path.stem for path in CORE_MODULES]
    code = (
        "import importlib, sys\n"
        f"for name in {names!r}:\n"
        "    importlib.import_module('brigade.' + name)\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('brigade.native_t3'))\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_t3_host_keeps_every_persisted_spelling() -> None:
    assert HOST.run_kind == "native-t3"
    assert HOST.run_prefix == "t3-"
    assert HOST.roster_digest_domain == "brigade.native-t3.roster-digest.v1"
    assert HOST.idempotency_domain == "brigade.native-t3.idempotency.v1"
    assert HOST.seat_digest_domain == "brigade.t3.seat.v1"
    assert (HOST.ack_schema, HOST.finalize_schema, HOST.status_schema) == (
        "brigade.t3_ack.v1",
        "brigade.t3_finalize.v1",
        "brigade.t3_status.v1",
    )
    assert HOST.declarations_digest_key == "t3_targets"
    assert HOST.unsupported_code == "t3-unsupported"
    assert HOST.cancel_event == "task.native-cancel-requested"
    assert native_t3_dispatch.CLAIM_EVENT == "task.native-claimed"
    assert native_t3_dispatch.HELD_EVENT == "task.native-held"
    assert native_t3_dispatch.HOLD_RESOLVED_EVENT == "task.native-hold-resolved"
    assert capacity_core.CAPACITY_LOCK_NAME == "native-capacity.lock"


def _roster(
    declarations: dict[str, dict[str, object]], *, max_workers: int = 3, allow_models: tuple[str, ...] = ()
) -> roster_mod.Roster:
    agents = {
        "lead": roster_mod.Agent(name="lead", cli="claude", role="lead"),
        "coder": roster_mod.Agent(name="coder", cli="codex", role="coder", model="m1"),
    }
    return roster_mod.Roster(
        orchestrator="lead",
        agents=agents,
        max_workers=max_workers,
        allow_models=allow_models,
        t3_targets=declarations,
    )


def test_t3_digests_match_the_values_recorded_before_the_split() -> None:
    roster = _roster({"coder": {"model": "m1", "driver": "codex", "max_live": 2}}, allow_models=("m1",))
    # Computed from the pre-split module at aa3c508c3.
    assert native_t3_admission.roster_digest(roster) == (
        "sha256:26c93455c5becd56083818f6d293ed39d6e60643f669a44e1827e395e206ed8b"
    )
    assert native_t3_dispatch._key("held", "task-1", "a" * 64, "accept-conflict", "b" * 64) == (
        "native-held:43e923c565dee40701215e41c122126a049551ae973023c815f699e061019e93"
    )
    assert journal_core.digest({"seat": "coder"}, native_t3_lifecycle.SEAT_DOMAIN) == (
        "a1e6be7dafed23da02730eb33d836d892718f7d3ab3f50ecf438e47bbb251ef4"
    )


def _other_host(**overrides: object) -> NativeChildHost:
    fields: dict[str, object] = {
        "run_kind": "native-other",
        "run_prefix": "other-",
        "run_noun": "governed native other run",
        "roster_digest_domain": "brigade.native-other.roster-digest.v1",
        "idempotency_domain": "brigade.native-other.idempotency.v1",
        "seat_digest_domain": "brigade.other.seat.v1",
        "ack_schema": "brigade.other_ack.v1",
        "finalize_schema": "brigade.other_finalize.v1",
        "status_schema": "brigade.other_status.v1",
        "declarations_digest_key": "other_targets",
        "unsupported_code": "other-unsupported",
        "cancel_event": "task.native-cancel-requested",
        "structural_gaps": frozenset({"other-gap"}),
        "declarations": lambda roster: {"coder": {"max_live": 1}},
        "structural_gap": lambda target: None,
        "driver_adapter": lambda target: "codex",
        "resolve_target": lambda roster, seat, revision: SimpleNamespace(source_revision=revision),
    }
    fields.update(overrides)
    return NativeChildHost(**fields)  # type: ignore[arg-type]


def test_roster_digest_is_bound_to_the_hosts_domain_and_declarations() -> None:
    roster = _roster({"coder": {"max_live": 1}})
    assert admission_core.roster_digest(_other_host(), roster) != admission_core.roster_digest(HOST, roster)


def _retained() -> SimpleNamespace:
    return SimpleNamespace(source_revision="a" * 40, driver="anything", launch_model="m1")


def test_admission_resolves_the_target_through_the_host() -> None:
    roster = _roster({})
    retained = _retained()
    hub = admission_core.HubAuthority(state="unenrolled")

    def unresolved(missing: bool):
        def resolve(*_args: object) -> object:
            raise TargetUnresolved(missing=missing)

        return resolve

    gone = admission_core.evaluate(_other_host(resolve_target=unresolved(True)), roster, "coder", retained, hub=hub)
    assert (gone.code, gone.reason) == (admission_core.DENIED, admission_core.TARGET_MISSING)
    bad = admission_core.evaluate(_other_host(resolve_target=unresolved(False)), roster, "coder", retained, hub=hub)
    assert (bad.code, bad.reason) == (admission_core.DENIED, admission_core.TARGET_INVALID)
    changed = admission_core.evaluate(
        _other_host(resolve_target=lambda *_args: SimpleNamespace(source_revision="a" * 40, other=1)),
        roster,
        "coder",
        retained,
        hub=hub,
    )
    assert (changed.code, changed.reason) == (admission_core.DENIED, admission_core.TARGET_CHANGED)


def test_admission_takes_structural_gaps_and_the_driver_adapter_from_the_host() -> None:
    roster = _roster({})
    retained = _retained()
    hub = admission_core.HubAuthority(state="unenrolled")
    gap = admission_core.evaluate(
        _other_host(structural_gap=lambda target: "other-gap"), roster, "coder", retained, hub=hub
    )
    assert (gap.code, gap.reason) == (admission_core.DENIED, "other-gap")
    same = _other_host(resolve_target=lambda *_args: retained)
    unsupported = admission_core.evaluate(
        _other_host(resolve_target=same.resolve_target, driver_adapter=lambda target: None),
        roster,
        "coder",
        retained,
        hub=hub,
    )
    assert (unsupported.code, unsupported.reason) == (admission_core.DENIED, admission_core.DRIVER_UNSUPPORTED)
    admitted = admission_core.evaluate(same, roster, "coder", retained, hub=hub)
    assert (admitted.code, admitted.reason) == (admission_core.ADMITTED, "local-roster")


def test_capacity_limit_comes_from_the_hosts_declarations() -> None:
    roster = _roster({}, max_workers=5)
    occupancy = capacity_core.Occupancy(live=1, seats=(("coder", 1),), authoritative=True)
    # The roster declares nothing for the seat. Only the host's table limits it.
    decision = capacity_core.assess(_other_host(), roster, "coder", occupancy)
    assert (decision.code, decision.seat_free, decision.global_free) == (capacity_core.CAPACITY_DENIED, 0, 4)
    open_host = _other_host(declarations=lambda roster: {})
    assert capacity_core.assess(open_host, roster, "coder", occupancy).granted


@pytest.mark.skipif(os.name == "nt", reason="occupancy scans bound run directories")
def test_occupancy_scans_only_the_hosts_own_run_namespace(tmp_path: Path) -> None:
    runs = capacity_core.runs_root(tmp_path)
    foreign = runs / "t3-aaaa"
    foreign.mkdir(parents=True)
    (foreign / "run.json").write_text('{"kind": "native-t3"}', encoding="utf-8")
    # Another host's run is outside this host's namespace, so it is skipped.
    assert capacity_core.governed_occupancy(_other_host(), tmp_path).authoritative
    # The same run is native for its own host, and it names no workspace.
    assert not capacity_core.governed_occupancy(HOST, tmp_path).authoritative
    # A run in this host's namespace whose kind was rewritten is unverifiable.
    own = runs / "other-bbbb"
    own.mkdir()
    (own / "run.json").write_text('{"kind": "native-t3"}', encoding="utf-8")
    assert not capacity_core.governed_occupancy(_other_host(), tmp_path).authoritative
