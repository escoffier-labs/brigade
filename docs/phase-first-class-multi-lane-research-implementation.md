# First-Class Multi-Lane Research Implementation Plan

> Approved design: `docs/phase-first-class-multi-lane-research.md`
>
> Planning baseline: `origin/main` at `eb7e82cc61db`

## Goal

Move `brigade research` onto Brigade's standard run lifecycle, then execute a
domain-defined research graph in which Brigade gathers evidence, Gemini browser
synthesizes by default, and Luna plans, extracts, reviews, and provides the
recorded synthesis fallback.

The implementation keeps `brigade research` as the operator command. It reuses
the standard worker transport for individual model calls, stores new work in
`.brigade/runs/<run-id>/`, and preserves read access to legacy
`.brigade/research/<run-id>/` records. Center UI work is excluded. The three
versioned JSON projections required by the later Center Research page ship and
are tested here.

Execute this plan task by task, checking each box only after the named command
returns the expected result. Run every counted test through
`brigade work verify run`, capture each outcome, and commit at each task
boundary. Before the first code edit, sync Brigade Code and attach the returned
impact set to the implementation run.

## Fixed contracts

- New runs use `run.json` schema `brigade.run.v1` with `kind: "research"`.
- Runs without `kind` project as `kind: "work"`.
- Research sidecars use `brigade.research.v1`, `brigade.research.sources.v1`,
  `brigade.research.findings.v1`, and
  `brigade.research.citation-audit.v1`.
- Research phases are `planning`, `discovery`, `extraction`, `synthesis`,
  `review`, `repair`, and `publishing`.
- Exit codes are `0` for success, `1` for runtime or doctor failure, `2` for
  usage, configuration, or admission failure, and `130` for cancellation.
- Capability names are `research.plan`, `research.extract`,
  `research.synthesize`, `research.review`, and `research.browser-discover`.
- Legacy `role = "researcher"` maps to `research.general`, which may plan,
  extract, or synthesize. It may not review or perform browser discovery.
- The default `grounded` profile uses Brigade discovery, Gemini browser
  synthesis with Luna fallback, and Luna review.
- Gemini browser concurrency is one per process.
- Model receipts contain `requested_model` and `observed_model`. An unavailable
  observation is stored as `"unverified"`.
- A report publishes only after citation audit and independent review accept it.
  One repair attempt is allowed. A second rejection fails closed.
- Browser-AI discovery activates only with `--browser-ai-research` or the
  explicit `browser-ai` profile.
- `research init` writes `.brigade/research.toml` only when absent. It never
  installs software, edits the roster, or touches browser profiles.

## File map

| Path | Responsibility |
|---|---|
| `src/brigade/roster.py` | Parse seat capabilities and resolve capability matches deterministically. |
| `src/brigade/run_seat.py` | Invoke one exact-prompt seat through `run_transport.dispatch`. |
| `src/brigade/run_receipts.py` | Serialize each phase call into standard worker logs and receipt payloads. |
| `src/brigade/aboyeur.py` | Start and update standard run receipts with a preserved run kind. |
| `src/brigade/run_projector.py` | Preserve `kind` across lifecycle projection. |
| `src/brigade/receipt_schema.py` | Stamp the three versioned research sidecar schemas. |
| `src/brigade/research/types.py` | Define research profiles, phases, sources, findings, reviews, and typed failures. |
| `src/brigade/research/config.py` | Parse profiles, lane preferences, and dispatch caps. |
| `src/brigade/research/registry.py` | Store new standard runs, atomically update sidecars, and dual-read legacy runs. |
| `src/brigade/research/provenance.py` | Build source envelopes, digests, citation tokens, and citation audits. |
| `src/brigade/research/llm.py` | Expose phase-bound seat backends and turn failed worker results into typed errors. |
| `src/brigade/research/extract.py` | Extract findings with source identities and preserve the untrusted boundary. |
| `src/brigade/research/engine.py` | Execute the fixed multi-lane graph, review, repair, fallback, and durable phase callbacks. |
| `src/brigade/research/sources/browser_ai.py` | Convert Gemini browser discovery JSON into untrusted stored sources. |
| `src/brigade/research/doctor.py` | Report lane configuration, executable, auth, model, safety, and concurrency health. |
| `src/brigade/research_cmd.py` | Admit, run, cancel, resume, render, and project research runs. |
| `src/brigade/cli/research.py` | Register profiles, lane overrides, browser discovery, init, doctor, and status flags. |
| `src/brigade/work_cmd/ledger.py` | Accept completed standard research runs as trusted planning inputs. |
| `tests/test_roster.py` | Cover capability parsing and compatibility behavior. |
| `tests/test_run_seat.py` | Cover exact prompt dispatch, controls, receipts, and failures. |
| `tests/test_run_projector.py` | Cover kind preservation and legacy defaulting. |
| `tests/test_research_registry.py` | Cover standard storage, atomic sidecars, and dual-read behavior. |
| `tests/test_research_provenance.py` | Cover source digests, trust labels, and citation audits. |
| `tests/test_research_llm.py` | Cover phase seat resolution and typed failures. |
| `tests/test_research_engine.py` | Cover graph order, fallback, review separation, repair, and injection containment. |
| `tests/test_research_browser_ai.py` | Cover strict Gemini discovery parsing and provenance. |
| `tests/test_research_doctor.py` | Cover lane-specific doctor results and redaction. |
| `tests/test_research_cmd.py` | Cover CLI status, exit codes, cancellation, resume, and legacy records. |
| `tests/test_research_acceptance.py` | Exercise the real CLI with a real roster and hermetic Oracle stub. |
| `tests/test_work_cmd_ledger.py` | Cover plan creation from a completed standard research run. |
| `docs/runbooks/research-live-acceptance.md` | Pin the live Oracle, grounded, and browser-discovery acceptance procedure. |
| `docs/phase-first-class-multi-lane-research.md` | Link this execution plan while retaining deferred Phase 5. |

## Approved-spec coverage

| Approved requirement | Implemented by |
|---|---|
| Standard run lifecycle and `kind = "research"` | Tasks 3 and 7 |
| Shared single-seat execution without recursive runs | Task 2 |
| Capability-based planner, extractor, synthesizer, and reviewer lanes | Tasks 1 and 5 |
| Brigade discovery by default | Tasks 5 and 6 |
| Gemini browser synthesis with truthful Luna fallback | Tasks 2, 5, and 9 |
| Independent Luna review and one bounded repair | Tasks 5 and 9 |
| Explicit browser-AI discovery | Tasks 6 and 9 |
| Provenance, trust labels, stored evidence, and resolved citations | Tasks 4, 5, and 7 |
| Requested versus observed model reporting | Tasks 2, 6, and 8 |
| Failure, cancellation, budget, and resume parity | Tasks 2, 7, and 8 |
| Oracle concurrency limit of one | Task 2 |
| Lane-specific doctor and no automatic dependency installation | Task 8 |
| Legacy dual-read and legacy resume | Tasks 3 and 7 |
| Standard research input for work-plan creation | Task 8 |
| Hermetic and live Oracle acceptance | Task 9 |
| Versioned Center-facing CLI JSON | Task 8 |
| Center Research page retained but deferred | Deferred Phase 5 contract below |

### Task 1: Capability-aware roster and profile resolution

**Files:**

- Modify: `src/brigade/roster.py:31-61,346-563`
- Modify: `src/brigade/aboyeur.py:2910-2950`
- Modify: `src/brigade/research/types.py:1-51`
- Modify: `src/brigade/research/config.py:1-34`
- Test: `tests/test_roster.py`
- Test: `tests/test_research_config.py`

- [x] Sync the code graph and record the exact blast radius before editing:

```bash
brigade code sync .
brigade code affected src/brigade/roster.py
brigade code affected src/brigade/research/types.py
brigade code affected src/brigade/research/config.py
```

Expect all three commands to succeed. Copy the reported symbol and test names
into the active Brigade run's code-graph brief before changing a file.

- [x] Add failing roster and config tests:

```python
def test_load_roster_parses_research_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "roster.toml"
    path.write_text(
        """
[orchestrator]
name = "chef"

[agents.chef]
cli = "codex"
role = "orchestrator"

[agents.luna]
cli = "codex"
role = "researcher"
capabilities = ["research.plan", "research.extract", "research.review"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    roster = load_roster(path)

    assert roster.agents["luna"].capabilities == (
        "research.plan",
        "research.extract",
        "research.review",
    )
    assert [agent.name for agent in roster.find_capability("research.review")] == ["luna"]


def test_legacy_researcher_has_general_capability_only() -> None:
    agent = Agent(name="legacy", cli="codex", role="researcher")

    assert agent.effective_capabilities() == ("research.general",)
    assert agent.supports("research.plan") is True
    assert agent.supports("research.extract") is True
    assert agent.supports("research.synthesize") is True
    assert agent.supports("research.review") is False
    assert agent.supports("research.browser-discover") is False
```

```python
def test_profile_defaults_and_explicit_lane_order(tmp_path: Path) -> None:
    path = tmp_path / ".brigade" / "research.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """
[research]
default_profile = "grounded"

[profiles.grounded]
discovery = ["brigade"]
planner = ["luna"]
extractor = ["luna"]
synthesizer = ["gemini_browser", "luna"]
reviewer = ["luna"]
allow_synthesis_fallback = true
browser_ai_research = false
""".strip()
        + "\n",
        encoding="utf-8",
    )

    cfg = load(tmp_path)
    profile = cfg.profile(None)

    assert profile.name == "grounded"
    assert profile.synthesizer == ("gemini_browser", "luna")
    assert profile.browser_ai_research is False
```

- [x] Run the tests and confirm the new APIs are absent:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_roster.py","tests/test_research_config.py"]' --capture brigade-work --capture-kind skill
```

Expect failure naming `Agent.capabilities`, `Roster.find_capability`, or
`ResearchConfig.profile`.

- [x] Add these concrete contracts to `src/brigade/roster.py`:

```python
RESEARCH_GENERAL_CAPABILITIES = frozenset(
    {"research.plan", "research.extract", "research.synthesize"}
)


@dataclass(frozen=True)
class Agent:
    name: str
    cli: str | None
    role: str
    timeout_seconds: float | None = None
    endpoint: str | None = None
    model: str | None = None
    headers: dict | None = None
    reasoning: str | None = None
    transport: str = "direct"
    transport_version: str | None = None
    env: dict[str, str] | None = None
    invalid_final_fallback: str | None = None
    read_only_capable: bool = True
    purpose: str | None = None
    requires: dict[str, str] | None = None
    fallback: tuple[str, ...] = ()
    stats: dict[str, str] | None = None
    caveats: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()

    def effective_capabilities(self) -> tuple[str, ...]:
        if self.capabilities:
            return self.capabilities
        if self.role == "researcher":
            return ("research.general",)
        return ()

    def supports(self, capability: str) -> bool:
        effective = self.effective_capabilities()
        return capability in effective or (
            "research.general" in effective and capability in RESEARCH_GENERAL_CAPABILITIES
        )
```

Add `Roster.find_capability` in roster order:

```python
def find_capability(self, capability: str) -> tuple[Agent, ...]:
    return tuple(agent for agent in self.agents.values() if agent.supports(capability))
```

Parse `capabilities` with the existing string-list validator used by
`fallback` and `caveats`. Reject duplicates and values that do not match
`[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+`. Keep the field generic so later Brigade
domains can use the same roster contract. Pass the parsed tuple through the
named `capabilities=` constructor argument and include it in the persisted
roster payload in `aboyeur.py`.

- [x] Add these types and built-in profiles to `src/brigade/research/types.py`:

```python
ResearchPhase = Literal[
    "planning", "discovery", "extraction", "synthesis", "review", "repair", "publishing"
]


@dataclass(frozen=True)
class ResearchProfile:
    name: str
    discovery: tuple[str, ...]
    planner: tuple[str, ...]
    extractor: tuple[str, ...]
    synthesizer: tuple[str, ...]
    reviewer: tuple[str, ...]
    allow_synthesis_fallback: bool
    browser_ai_research: bool


BUILTIN_PROFILES: Dict[str, ResearchProfile] = {
    "grounded": ResearchProfile(
        name="grounded",
        discovery=("brigade",),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=True,
        browser_ai_research=False,
    ),
    "browser-ai": ResearchProfile(
        name="browser-ai",
        discovery=("brigade", "browser-ai"),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=True,
        browser_ai_research=True,
    ),
    "local-only": ResearchProfile(
        name="local-only",
        discovery=("local", "repository"),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=False,
        browser_ai_research=False,
    ),
    "luna-only": ResearchProfile(
        name="luna-only",
        discovery=("brigade",),
        planner=(),
        extractor=(),
        synthesizer=(),
        reviewer=(),
        allow_synthesis_fallback=False,
        browser_ai_research=False,
    ),
}
```

Implement `ResearchConfig.profile(name)` by overlaying `[profiles.<name>]` on
the matching built-in profile, with `grounded` as the default. Parse all lane
lists as ordered tuples. Reject an unknown profile, an empty configured lane
list, and any browser discovery value other than explicit `true` or `false`.

- [x] Run the focused tests to green and capture the outcome:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_roster.py","tests/test_research_config.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect both files to pass.

- [x] Commit only Task 1 files:

```bash
git add src/brigade/roster.py src/brigade/aboyeur.py src/brigade/research/types.py src/brigade/research/config.py tests/test_roster.py tests/test_research_config.py
git commit -m "feat: resolve research seats by capability"
```

### Task 2: Shared exact-prompt seat invocation

**Files:**

- Create: `src/brigade/run_seat.py`
- Modify: `src/brigade/run_receipts.py:1-260`
- Modify: `src/brigade/research/llm.py:1-76`
- Test: `tests/test_run_seat.py`
- Test: `tests/test_research_llm.py`

- [ ] Add failing tests that exercise the public invoker, not
`agents.run_agent` directly:

```python
def test_seat_invoker_forwards_controls(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_dispatch(assignments, roster, **kwargs):
        captured["assignments"] = assignments
        captured["roster"] = roster
        captured.update(kwargs)
        return [
            WorkerResult(
                worker="luna",
                task="research.plan",
                text='{"sub_questions": []}',
                ok=True,
                detail="",
            )
        ]

    monkeypatch.setattr("brigade.run_seat.run_transport.dispatch", fake_dispatch)
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=tmp_path / ".brigade" / "runs" / "research-1",
    )

    result = invoker.invoke(
        seat="luna",
        phase="research.plan",
        prompt="Return strict JSON",
        timeout=91,
    )

    assert result.text == '{"sub_questions": []}'
    assert captured["read_only"] is True
    assert captured["direct"] is True
    assert captured["run_id"] == "research-1"
    assert captured["cwd"] == tmp_path
    selected = captured["roster"].agents["luna"]
    assert selected.model == "gpt-5.6-luna"
    assert selected.reasoning == "medium"
    assert selected.timeout_seconds == 91.0
    assignment = captured["assignments"][0]
    assert assignment.worker == "luna"
    assert assignment.task == "research.plan"
```

Add this test-local roster helper above the test:

```python
def _research_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                reasoning="medium",
                capabilities=("research.plan",),
            ),
        },
    )
```

```python
def test_phase_backend_raises_typed_worker_failure() -> None:
    invoker = FakeInvoker(
        SeatResult(
            seat="gemini",
            attempt_id="research-1:0001",
            text="",
            ok=False,
            failure_phase="transport",
            failure_kind="browser-auth",
            detail="browser profile is not authenticated",
            requested_model="gemini-3.1-pro",
            observed_model="unverified",
            attempts=(),
        )
    )
    backend = PhaseBackend(invoker=invoker, seat="gemini", phase="research.synthesize")

    with pytest.raises(ResearchSeatError) as caught:
        backend.complete([{"role": "user", "content": "write"}])

    assert caught.value.failure_kind == "browser-auth"
    assert caught.value.seat == "gemini"
```

Define the test-local invoker used above:

```python
class FakeInvoker:
    def __init__(self, result: SeatResult) -> None:
        self.result = result

    def invoke(self, **_kwargs: object) -> SeatResult:
        return self.result
```

Add this second invoker test:

```python
def test_seat_invoker_does_not_persist_raw_browser_auth_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "brigade.run_seat.run_transport.dispatch",
        lambda *_args, **_kwargs: [
            WorkerResult(
                worker="luna",
                task="research.synthesize",
                text="",
                ok=False,
                detail="browser authentication failed; run research doctor",
                failure_phase="provider-preflight",
                failure_kind="browser-auth",
                stderr="browser session expired for /secret/profile",
            )
        ],
    )
    output_dir = tmp_path / ".brigade" / "runs" / "research-1"
    invoker = SeatInvoker(
        roster=_research_roster(),
        cwd=tmp_path,
        run_id="research-1",
        process_registry=ProcessRegistry(),
        output_dir=output_dir,
    )

    invoker.invoke(seat="luna", phase="research.synthesize", prompt="write", timeout=91)

    stderr_log = next((output_dir / "logs").glob("*.stderr.log"))
    receipt = next((output_dir / "workers").glob("*.json")).read_text(encoding="utf-8")
    assert stderr_log.read_text(encoding="utf-8") == ""
    assert "/secret/profile" not in receipt
    assert "browser session expired" not in receipt
```

- [ ] Run and observe import failures:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_run_seat.py","tests/test_research_llm.py"]' --capture brigade-work --capture-kind skill
```

Expect failure importing `brigade.run_seat` or `PhaseBackend`.

- [ ] Create `src/brigade/run_seat.py` with this public surface:

```python
from __future__ import annotations

from pathlib import Path
from contextlib import nullcontext
from dataclasses import dataclass, replace
from threading import BoundedSemaphore, Lock
from typing import Callable

from . import localio, proc, run_receipts, run_transport
from .roster import Roster


_ORACLE_GATE = BoundedSemaphore(1)


@dataclass(frozen=True)
class SeatResult:
    seat: str
    attempt_id: str
    text: str
    ok: bool
    failure_phase: str | None
    failure_kind: str | None
    detail: str
    requested_model: str | None
    observed_model: str
    attempts: tuple[object, ...]


class SeatInvoker:
    def __init__(
        self,
        *,
        roster: Roster,
        cwd: Path,
        run_id: str,
        process_registry: proc.ProcessRegistry,
        output_dir: Path,
        events_dir: Path | None = None,
        on_dispatch_requested: Callable | None = None,
        on_dispatch_observed: Callable | None = None,
        on_dispatch_completed: Callable | None = None,
        on_dispatch_failed: Callable | None = None,
    ) -> None:
        self.roster = roster
        self.cwd = cwd
        self.run_id = run_id
        self.process_registry = process_registry
        self.output_dir = output_dir
        self.events_dir = events_dir
        self._receipt_lock = Lock()
        self._receipt_sequence = 0
        self.budget_callbacks = {
            "on_dispatch_requested": on_dispatch_requested,
            "on_dispatch_observed": on_dispatch_observed,
            "on_dispatch_completed": on_dispatch_completed,
            "on_dispatch_failed": on_dispatch_failed,
        }

    def invoke(self, *, seat: str, phase: str, prompt: str, timeout: int) -> SeatResult:
        agent = self.roster.agents[seat]
        effective_timeout = max(float(timeout), agent.timeout_seconds or 0.0)
        effective = replace(agent, timeout_seconds=effective_timeout)
        roster = replace(self.roster, agents={**self.roster.agents, seat: effective})
        assignment = run_transport.Assignment(worker=seat, task=phase, capabilities=(phase,))
        gate = _ORACLE_GATE if agent.cli == "oracle" else nullcontext()
        with gate:
            results = run_transport.dispatch(
                [assignment],
                roster,
                build_prompt=lambda _agent, _assignment, **_context: prompt,
                run_appserver_worker=_unsupported_appserver,
                event_writer=_no_events,
                cwd=self.cwd,
                read_only=True,
                direct=True,
                fail_fast=True,
                process_registry=self.process_registry,
                events_dir=self.events_dir,
                run_id=self.run_id,
                **{key: value for key, value in self.budget_callbacks.items() if value is not None},
            )
        worker = results[0]
        if worker.failure_kind == "browser-auth":
            worker = replace(worker, stdout="", stderr="")
        recorded = run_receipts.write_worker_logs(self.output_dir, [worker])[0]
        with self._receipt_lock:
            self._receipt_sequence += 1
            attempt_id = f"{self.run_id}:{self._receipt_sequence:04d}"
            receipt_path = self.output_dir / "workers" / (
                f"{self._receipt_sequence:04d}-{phase.replace('.', '-')}-{seat}.json"
            )
            receipt = run_receipts.worker_payload_one(recorded)
            receipt["attempt_id"] = attempt_id
            localio.write_json(receipt_path, receipt)
        return SeatResult(
            seat=seat,
            attempt_id=attempt_id,
            text=worker.text,
            ok=worker.ok,
            failure_phase=worker.failure_phase,
            failure_kind=worker.failure_kind,
            detail=worker.detail,
            requested_model=recorded.requested_model or agent.model,
            observed_model=recorded.effective_model or "unverified",
            attempts=tuple(recorded.attempts),
        )
```

Define `_unsupported_appserver` to raise `RuntimeError("single-seat research uses direct transport")`
and `_no_events` to return `None`. The invoker must never call `aboyeur.run`.
Add this public wrapper to `run_receipts.py`:

```python
def worker_payload_one(result: WorkerResult) -> dict[str, object]:
    return worker_payload([result])[0]
```

The `workers/` receipt and any `logs/` references are written for successful
and failed calls.

- [ ] Replace the CLI-only backend in `research/llm.py` with a phase backend:

```python
class ResearchSeatError(RuntimeError):
    def __init__(self, result: SeatResult) -> None:
        super().__init__(result.detail or result.failure_kind or "research seat failed")
        self.seat = result.seat
        self.failure_phase = result.failure_phase or "dispatch"
        self.failure_kind = result.failure_kind or "worker-failed"
        self.result = result


class PhaseBackend:
    def __init__(self, *, invoker: SeatInvoker, seat: str, phase: str) -> None:
        self.invoker = invoker
        self.seat = seat
        self.phase = phase
        self.requested_model = invoker.roster.agents[seat].model
        self.observed_model = "unverified"
        self.last_attempt_id: str | None = None

    def complete(self, messages, *, max_tokens=2048, temperature=0.3, timeout=60) -> str:
        del max_tokens, temperature
        result = self.invoker.invoke(
            seat=self.seat,
            phase=self.phase,
            prompt=_messages_to_prompt(messages),
            timeout=timeout,
        )
        if not result.ok:
            raise ResearchSeatError(result)
        self.observed_model = result.observed_model
        self.last_attempt_id = result.attempt_id
        return result.text
```

Remove `_run_cli` and `CliBackend`. Keep `HttpBackend` for compatibility with
legacy research resume only. Do not catch `ResearchSeatError` in this module.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_run_seat.py","tests/test_research_llm.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect both files to pass.

- [ ] Commit Task 2:

```bash
git add src/brigade/run_seat.py src/brigade/run_receipts.py src/brigade/research/llm.py tests/test_run_seat.py tests/test_research_llm.py
git commit -m "refactor: share exact research seat dispatch"
```

### Task 3: Standard run container and dual-read registry

**Files:**

- Modify: `src/brigade/receipt_schema.py`
- Modify: `src/brigade/aboyeur.py:2951-3175`
- Modify: `src/brigade/run_projector.py:44-110,384-390`
- Rewrite: `src/brigade/research/registry.py`
- Test: `tests/test_run_projector.py`
- Test: `tests/test_research_registry.py`

- [ ] Add failing tests for kind preservation and the sidecar store:

```python
def test_projector_preserves_research_kind() -> None:
    base = _minimal_base_snapshot()
    base["kind"] = "research"

    projected = project_run_snapshot(base, [], journal_present=False).snapshot

    assert projected["kind"] == "research"


def test_projector_defaults_missing_kind_to_work() -> None:
    projected = project_run_snapshot(
        _minimal_base_snapshot(), [], journal_present=False
    ).snapshot

    assert projected["kind"] == "work"
```

```python
def test_create_standard_research_run_and_dual_read(tmp_path: Path) -> None:
    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
    )
    record = registry.create_standard_run(
        tmp_path,
        run_id="r-new",
        question="What changed?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 12},
        roster=roster,
    )
    legacy = tmp_path / ".brigade" / "research" / "r-old"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text(
        json.dumps({"run_id": "r-old", "question": "Old", "status": "done"}),
        encoding="utf-8",
    )

    assert record["schema"] == "brigade.research.v1"
    assert json.loads((tmp_path / ".brigade/runs/r-new/run.json").read_text())["kind"] == "research"
    listed = registry.list_runs(tmp_path)
    assert [(item["run_id"], item["legacy"]) for item in listed] == [
        ("r-new", False),
        ("r-old", True),
    ]
```

- [ ] Run and confirm failure:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_run_projector.py","tests/test_research_registry.py"]' --capture brigade-work --capture-kind skill
```

Expect failure because `kind` is not projected and `create_standard_run` does
not exist.

- [ ] Add schema constants and stamp helpers in `receipt_schema.py`:

```python
RESEARCH_SIDECAR_SCHEMA = "brigade.research.v1"
RESEARCH_SIDECAR_VERSION = 1
RESEARCH_SOURCES_SCHEMA = "brigade.research.sources.v1"
RESEARCH_SOURCES_VERSION = 1
RESEARCH_FINDINGS_SCHEMA = "brigade.research.findings.v1"
RESEARCH_FINDINGS_VERSION = 1
RESEARCH_CITATION_AUDIT_SCHEMA = "brigade.research.citation-audit.v1"
RESEARCH_CITATION_AUDIT_VERSION = 1


def stamp_research_sidecar(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": RESEARCH_SIDECAR_SCHEMA, "schema_version": RESEARCH_SIDECAR_VERSION, **payload}


def stamp_research_sources(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": RESEARCH_SOURCES_SCHEMA, "schema_version": RESEARCH_SOURCES_VERSION, **payload}


def stamp_research_findings(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": RESEARCH_FINDINGS_SCHEMA, "schema_version": RESEARCH_FINDINGS_VERSION, **payload}


def stamp_research_citation_audit(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESEARCH_CITATION_AUDIT_SCHEMA,
        "schema_version": RESEARCH_CITATION_AUDIT_VERSION,
        **payload,
    }
```

- [ ] Add `kind: str = "work"` to `_run_payload` and `record_run_start`, write it
into the payload, and pass it through every internal call. Add `kind` to
`PRESERVED_FIELDS` and increment `PROJECTOR_VERSION` from 5 to 6. When the input
lacks `kind`, the projector must set `projected["kind"] = "work"` after
preserved fields are copied.

Add a public atomic updater beside `record_run_start`:

```python
def update_run_receipt(output_dir: Path, **fields: object) -> dict[str, object]:
    path = output_dir.expanduser().resolve() / "run.json"
    current = _read_json_dict(path)
    if current is None:
        raise FileNotFoundError(path)
    current.update(fields)
    _write_json(path, current)
    return current
```

Use the existing guarded JSON reader used by `record_run_start`. Do not add a
second permissive parser.

- [ ] Rewrite `research/registry.py` around a `RunRecord` projection. New writes
use `localio.write_json` and `localio.write_text_atomic`. The exact paths are:

```python
def standard_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "runs" / run_id


def legacy_run_dir(target: Path, run_id: str) -> Path:
    return target / ".brigade" / "research" / run_id


def run_dir(target: Path, run_id: str) -> Path:
    standard = standard_run_dir(target, run_id)
    return standard if standard.is_dir() else legacy_run_dir(target, run_id)
```

`create_standard_run` must call the standard start function with these named
arguments:

```python
aboyeur.record_run_start(
    standard_run_dir(target, run_id),
    task=question,
    cwd=target,
    roster=roster,
    read_only=True,
    kind="research",
    run_budget_payload=caps,
)
```

It then writes the initial stamped `research.json` and returns the projected
record.
`update_research` must read, merge, stamp, and atomically write `research.json`.
`write_sources`, `write_findings`, and `write_citation_audit` stamp their
schemas. `show_run`
checks standard first, then legacy. `list_runs` merges both stores, de-duplicates
by run ID in favor of standard, adds `legacy: bool`, and sorts descending by
`created_at`, then `run_id`.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_run_projector.py","tests/test_research_registry.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect both files to pass and no existing projector snapshot to change except
for the new `kind: "work"` field.

- [ ] Commit Task 3:

```bash
git add src/brigade/receipt_schema.py src/brigade/aboyeur.py src/brigade/run_projector.py src/brigade/research/registry.py tests/test_run_projector.py tests/test_research_registry.py
git commit -m "feat: store research in standard runs"
```

### Task 4: Provenance envelopes and citation audit

**Files:**

- Create: `src/brigade/research/provenance.py`
- Modify: `src/brigade/research/types.py`
- Modify: `src/brigade/research/extract.py`
- Test: `tests/test_research_provenance.py`
- Test: `tests/test_research_extract.py`

- [ ] Add failing tests for deterministic source IDs, wrapped synthesis input,
and unresolved citations:

```python
def test_source_envelope_has_stable_digest_and_id() -> None:
    first = SourceEnvelope.build(
        origin="web",
        provider="playwright",
        uri="https://example.test/a",
        content="same content",
        trust="web",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    second = SourceEnvelope.build(
        origin="web",
        provider="playwright",
        uri="https://example.test/a",
        content="same content",
        trust="web",
        acquired_at="2026-08-13T12:01:00+00:00",
    )

    assert first.source_id == second.source_id
    assert first.content_digest == second.content_digest
    assert first.source_id.startswith("src-")


def test_citation_audit_rejects_unknown_source() -> None:
    audit = audit_citations(
        "Supported [source:src-1111111111111111]. Unsupported [source:src-2222222222222222].",
        findings=[finding_for("src-1111111111111111")],
    )

    assert audit.accepted is False
    assert audit.unresolved == ("src-2222222222222222",)
    assert [item.status for item in audit.citations] == ["accepted", "unresolved"]
```

Define `finding_for` in the same test file:

```python
def finding_for(source_id: str) -> Finding:
    return Finding(
        source_ids=(source_id,),
        title="Known source",
        summary="Supported fact",
        evidence="Evidence",
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:02:00+00:00",
        parent_source_ids=(),
    )
```

```python
def test_extract_finding_keeps_source_identity(fake_llm) -> None:
    source = SourceEnvelope.build(
        origin="browser-ai",
        provider="oracle",
        uri="oracle://session/abc/result/1",
        content="Evidence text",
        trust="browser-ai",
        acquired_at="2026-08-13T12:00:00+00:00",
        producing_lane="gemini_browser",
        requested_model="gemini-3.1-pro",
        observed_model="unverified",
    )

    finding = extract_finding(fake_llm, goal="goal", source=source)

    assert finding is not None
    assert finding.source_ids == (source.source_id,)
    assert finding.extraction_lane == "luna"
```

- [ ] Run and observe missing provenance types:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_provenance.py","tests/test_research_extract.py"]' --capture brigade-work --capture-kind skill
```

Expect import or signature failures.

- [ ] Add immutable provenance types. `SourceEnvelope.build` computes
`sha256(content)` and `source_id = "src-" + sha256(origin + "\0" + provider +
"\0" + uri + "\0" + content_digest)[:16]`. Store these fields:

```python
Origin = Literal["local", "repository", "indexed-cli", "web", "browser-ai"]


@dataclass(frozen=True)
class SourceEnvelope:
    source_id: str
    origin: Origin
    provider: str
    uri: str
    content: str
    content_digest: str
    acquired_at: str
    trust: Trust
    producing_lane: str | None = None
    requested_model: str | None = None
    observed_model: str | None = None
    parent_source_ids: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        origin: Origin,
        provider: str,
        uri: str,
        content: str,
        trust: Trust,
        acquired_at: str,
        producing_lane: str | None = None,
        requested_model: str | None = None,
        observed_model: str | None = None,
        parent_source_ids: tuple[str, ...] = (),
    ) -> "SourceEnvelope":
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = "\0".join((origin, provider, uri, content_digest))
        source_id = "src-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return cls(
            source_id=source_id,
            origin=origin,
            provider=provider,
            uri=uri,
            content=content,
            content_digest=content_digest,
            acquired_at=acquired_at,
            trust=trust,
            producing_lane=producing_lane,
            requested_model=requested_model,
            observed_model=observed_model,
            parent_source_ids=parent_source_ids,
        )


@dataclass(frozen=True)
class Finding:
    source_ids: tuple[str, ...]
    title: str
    summary: str
    evidence: str
    trust: Trust
    extraction_lane: str
    extracted_at: str
    parent_source_ids: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.source_ids[0]


@dataclass(frozen=True)
class CitationRecord:
    token: str
    source_ids: tuple[str, ...]
    status: Literal["accepted", "rejected", "unresolved", "repaired"]


@dataclass(frozen=True)
class CitationAudit:
    accepted: bool
    citations: tuple[CitationRecord, ...]
    unresolved: tuple[str, ...]
```

Extend `Trust` with `"browser-ai"`. Replace `Finding.source` with
`source_ids: tuple[str, ...]`, `extraction_lane: str`, `extracted_at: str`, and
`parent_source_ids: tuple[str, ...]`. Keep a read-only `source` property that
returns the first source ID so legacy report rendering continues during this
task.

- [ ] Implement citation tokens and audit in `provenance.py`:

```python
CITATION_PATTERN = re.compile(r"\[source:(src-[a-f0-9]{16})\]")


def citation_token(source_id: str) -> str:
    return f"[source:{source_id}]"


def audit_citations(report: str, findings: Sequence[Finding]) -> CitationAudit:
    known = {source_id for finding in findings for source_id in finding.source_ids}
    seen = tuple(dict.fromkeys(CITATION_PATTERN.findall(report)))
    citations = tuple(
        CitationRecord(
            token=citation_token(source_id),
            source_ids=(source_id,),
            status="accepted" if source_id in known else "unresolved",
        )
        for source_id in seen
    )
    unresolved = tuple(source_id for source_id in seen if source_id not in known)
    return CitationAudit(accepted=bool(seen) and not unresolved, citations=citations, unresolved=unresolved)
```

The audit rejects a report with no citations. `CitationRecord.status` allows
`accepted`, `rejected`, `unresolved`, or `repaired`.

- [ ] Change `extract_finding` to accept `source: SourceEnvelope` and
`extraction_lane: str = "luna"`. Pass `source.content` through
`wrap_untrusted`, label `browser-ai` as `web`, and return a finding linked to
the source ID and content digest. Never pass source text or a model summary to a
later prompt without `wrap_untrusted`.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_provenance.py","tests/test_research_extract.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect both files to pass.

- [ ] Commit Task 4:

```bash
git add src/brigade/research/provenance.py src/brigade/research/types.py src/brigade/research/extract.py tests/test_research_provenance.py tests/test_research_extract.py
git commit -m "feat: preserve research evidence provenance"
```

### Task 5: Multi-lane research graph, review, and fallback

**Files:**

- Rewrite: `src/brigade/research/engine.py`
- Modify: `src/brigade/research/llm.py`
- Test: `tests/test_research_engine.py`
- Test: `tests/test_research_llm.py`

- [ ] Add failing graph tests with distinct fake backends:

```python
def test_grounded_graph_uses_gemini_synthesis_and_luna_review() -> None:
    calls: list[tuple[str, str]] = []
    lanes = fake_lanes(calls, gemini_report=f"Answer [source:{FIXTURE_SOURCE_ID}]", review="accepted")
    engine = ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1))

    result = engine.run("question")

    assert [phase for phase, _seat in calls] == [
        "planning",
        "extraction",
        "synthesis",
        "review",
    ]
    assert calls[-2] == ("synthesis", "gemini_browser")
    assert calls[-1] == ("review", "luna")
    assert result.review.accepted is True
    assert result.synthesis_attempt_id != result.review.attempt_id
    assert result.fallbacks == ()


def test_gemini_failure_falls_back_to_luna_and_records_it() -> None:
    lanes = fake_lanes(
        [],
        gemini_error=ResearchSeatError(browser_auth_result()),
        luna_report=f"Fallback [source:{FIXTURE_SOURCE_ID}]",
        review="accepted",
    )

    result = ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1)).run("q")

    assert result.synthesis_seat == "luna"
    assert result.fallbacks[0].from_seat == "gemini_browser"
    assert result.fallbacks[0].to_seat == "luna"
    assert result.fallbacks[0].failure_kind == "browser-auth"


def test_second_review_rejection_fails_closed() -> None:
    lanes = fake_lanes(
        [],
        gemini_report="Bad [source:src-2222222222222222]",
        repair_report="Still unsupported [source:src-2222222222222222]",
        review="rejected",
    )

    with pytest.raises(ResearchRunError) as caught:
        ResearchEngine(lanes=lanes, sources=[fixed_source_provider()], caps=Caps(max_rounds=1)).run("q")

    assert caught.value.failure_phase == "review"
    assert caught.value.failure_kind == "review-rejected"
```

Use these test-local doubles. `ScriptedBackend` exposes the same `complete`
method as `PhaseBackend`, so the engine tests do not patch internal methods:

```python
class ScriptedBackend:
    def __init__(
        self,
        *,
        seat: str,
        phase: str,
        calls: list[tuple[str, str]],
        responses: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.seat = seat
        self.phase = phase
        self.calls = calls
        self.responses = list(responses or [])
        self.error = error

    def complete(self, messages, **_kwargs) -> str:
        self.calls.append((self.phase, self.seat))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


FIXTURE_SOURCE_ID = SourceEnvelope.build(
    origin="web",
    provider="fixture-web",
    uri="https://example.test/fact",
    content="Verified fact",
    trust="web",
    acquired_at="2026-08-13T12:00:00+00:00",
).source_id


class FixedSourceProvider:
    trust = "web"
    source_id = "fixture-web"
    source_type = "web"

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        del query, limit
        return [{"url": "https://example.test/fact", "title": "Fact", "trust": "web"}]

    def fetch(self, url: str) -> dict[str, object]:
        return {"success": True, "content": "Verified fact", "title": "Fact", "url": url}


def fixed_source_provider() -> FixedSourceProvider:
    return FixedSourceProvider()


def browser_auth_result() -> SeatResult:
    return SeatResult(
        seat="gemini_browser",
        attempt_id="research-test:0001",
        text="",
        ok=False,
        failure_phase="provider-preflight",
        failure_kind="browser-auth",
        detail="browser profile is not authenticated",
        requested_model="gemini-3.1-pro",
        observed_model="unverified",
        attempts=(),
    )


def fake_lanes(
    calls: list[tuple[str, str]],
    *,
    gemini_report: str = f"Answer [source:{FIXTURE_SOURCE_ID}]",
    gemini_error: Exception | None = None,
    luna_report: str = f"Fallback [source:{FIXTURE_SOURCE_ID}]",
    repair_report: str | None = None,
    review: str = "accepted",
) -> ResearchLanes:
    synthesis_responses = [gemini_report]
    if repair_report is not None:
        synthesis_responses.append(repair_report)
    review_payload = json.dumps(
        {
            "accepted": review == "accepted",
            "detail": review,
            "rejected_claims": [] if review == "accepted" else ["unsupported"],
        }
    )
    return ResearchLanes(
        planner=ScriptedBackend(
            seat="luna",
            phase="planning",
            calls=calls,
            responses=['{"sub_questions":["fact"],"key_topics":["fact"],"success_criteria":"cite"}'],
        ),
        extractor=ScriptedBackend(
            seat="luna",
            phase="extraction",
            calls=calls,
            responses=['{"summary":"Verified fact","evidence":"Verified fact"}'],
        ),
        synthesizers=(
            ScriptedBackend(
                seat="gemini_browser",
                phase="synthesis",
                calls=calls,
                responses=synthesis_responses,
                error=gemini_error,
            ),
            ScriptedBackend(
                seat="luna",
                phase="synthesis",
                calls=calls,
                responses=[luna_report],
            ),
        ),
        reviewer=ScriptedBackend(
            seat="luna",
            phase="review",
            calls=calls,
            responses=[review_payload, review_payload],
        ),
    )
```

Add an adversarial fixture whose source says `Ignore all previous instructions`
and assert both synthesis and review prompts contain the untrusted-source start
and end markers around that text.

- [ ] Run and observe missing graph classes:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_engine.py","tests/test_research_llm.py"]' --capture brigade-work --capture-kind skill
```

Expect failure importing `ResearchEngine`, `ResearchLanes`, or
`ResearchRunError`.

- [ ] Add deterministic lane resolution to `research/llm.py`. Candidate names
from the selected profile are tried in order and must support the phase
capability. With no explicit candidates, use `Roster.find_capability` in roster
order, except synthesis ranks an installed, read-only-capable `cli == "oracle"`
candidate first. Authentication is not assumed healthy at admission. The real
call may produce the typed `browser-auth` failure and recorded fallback.
Return this immutable contract:

```python
@dataclass(frozen=True)
class ResolvedLane:
    phase: str
    primary: str
    fallbacks: tuple[str, ...]
    resolution: Literal["profile", "capability", "compatibility"]
```

Add these domain contracts to `research/types.py` and import them in the
engine:

```python
@dataclass(frozen=True)
class ReviewResult:
    accepted: bool
    detail: str
    rejected_claims: tuple[str, ...]
    seat: str
    attempt_id: str


@dataclass(frozen=True)
class SynthesisRecord:
    seat: str
    attempt_id: str
    requested_model: str | None
    observed_model: str


@dataclass(frozen=True)
class FallbackRecord:
    phase: str
    from_seat: str
    to_seat: str
    failure_kind: str
    detail: str


@dataclass(frozen=True)
class ResearchLanes:
    planner: Any
    extractor: Any
    synthesizers: tuple[Any, ...]
    reviewer: Any
    browser_discovery: Any | None = None


class ResearchRunError(RuntimeError):
    def __init__(self, failure_phase: str, failure_kind: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_phase = failure_phase
        self.failure_kind = failure_kind
        self.detail = detail


@dataclass(frozen=True)
class ResearchResult:
    report: str
    findings: tuple[Finding, ...]
    sources: tuple[SourceEnvelope, ...]
    citation_audit: CitationAudit
    review: ReviewResult
    synthesis_seat: str
    synthesis_attempt_id: str
    fallbacks: tuple[FallbackRecord, ...]
    stats: dict[str, Any]
```

Reject a review whose attempt ID equals the synthesis attempt ID. A Luna
fallback synthesis may use the same named seat as the reviewer only when review
uses a new `SeatInvoker.invoke` call and a distinct attempt ID recorded in the
result. The reviewer receives only the cited artifact packet, never a continued
model session.

- [ ] Rewrite the engine as explicit phase methods. Discovery providers run in
a `ThreadPoolExecutor(max_workers=min(len(providers), 4))`. Preserve provider
result order by sorting completed batches back to the provider's original
index. The main sequence is exact:

```python
def run(self, question: str, *, resume: ResumeState | None = None) -> ResearchResult:
    plan = self._resume_or_plan(question, resume)
    sources = self._resume_or_discover(question, plan, resume)
    findings = self._resume_or_extract(question, sources, resume)
    report, synthesis = self._synthesize_with_fallback(question, findings)
    audit = audit_citations(report, findings)
    review = self._review(question, report, findings, audit)
    if not audit.accepted or not review.accepted:
        report = self._repair_once(question, report, findings, audit, review)
        audit = audit_citations(report, findings)
        review = self._review(question, report, findings, audit)
    if not audit.accepted or not review.accepted:
        raise ResearchRunError("review", "review-rejected", review.detail)
    return ResearchResult(
        report=report,
        findings=findings,
        sources=sources,
        citation_audit=audit,
        review=review,
        synthesis_seat=synthesis.seat,
        synthesis_attempt_id=synthesis.attempt_id,
        fallbacks=tuple(self.fallbacks),
        stats=self._stats(),
    )
```

Every phase calls `on_phase_started`, then `on_phase_completed` with artifact
references and digests. `ResearchSeatError` and invalid JSON become
`ResearchRunError` with the current phase and original failure kind. Remove all
catch-and-return-old-report behavior from `_safe_plan`, `_synthesize`, and
`_final`.

Build synthesis and review packets from stored findings. Each summary and
evidence excerpt is separately passed through `wrap_untrusted` and labeled with
its `[source:<id>]` token. Review returns strict JSON:

```json
{"accepted": true, "detail": "citations resolve", "rejected_claims": []}
```

The review prompt begins with `You are an independent research reviewer.` so
the acceptance stub and production contract use one stable discriminator.

Repair receives only the report, citation audit, review result, and bounded
stored finding packet. It does not invoke discovery again.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_engine.py","tests/test_research_llm.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect all graph, fallback, repair, and injection tests to pass.

- [ ] Commit Task 5:

```bash
git add src/brigade/research/engine.py src/brigade/research/llm.py tests/test_research_engine.py tests/test_research_llm.py
git commit -m "feat: orchestrate multi-lane research"
```

### Task 6: Explicit Gemini browser discovery

**Files:**

- Create: `src/brigade/research/sources/browser_ai.py`
- Modify: `src/brigade/research_cmd.py:24-151`
- Modify: `src/brigade/cli/research.py:15-35,142-158`
- Test: `tests/test_research_browser_ai.py`
- Test: `tests/test_research_cmd.py`

- [ ] Add failing provider and CLI opt-in tests:

```python
def test_browser_ai_provider_returns_untrusted_sources() -> None:
    backend = FakeBackend(
        '[{"url":"https://example.test/a","title":"A","snippet":"Claim A"}]'
    )
    provider = BrowserAiProvider(backend=backend, lane="gemini_browser")

    hits = provider.search("query", limit=3)
    page = provider.fetch(hits[0]["url"])

    assert hits[0]["trust"] == "browser-ai"
    assert page == {"success": True, "content": "Claim A", "title": "A"}
    assert provider.requested_model == "gemini-3.1-pro"
    assert provider.observed_model == "unverified"
```

Use this test backend:

```python
class FakeBackend:
    requested_model = "gemini-3.1-pro"
    observed_model = "unverified"

    def __init__(self, response: str) -> None:
        self.response = response

    def complete(self, _messages, **_kwargs) -> str:
        return self.response
```

```python
def test_run_omits_browser_ai_discovery_by_default(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=["docs/*.md"],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=False,
        json_output=True,
    )

    assert code == 0
    assert captured["browser_ai_research"] is False


def test_explicit_browser_ai_flag_adds_provider(monkeypatch, tmp_path: Path) -> None:
    captured = capture_run(monkeypatch)

    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={"max_rounds": 1},
        browser_ai_research=True,
        json_output=True,
    )

    assert code == 0
    assert captured["browser_ai_research"] is True
```

Define the capture helper in `tests/test_research_cmd.py`:

```python
def capture_run(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> str:
        captured.update(kwargs)
        return "captured-run"

    monkeypatch.setattr(research_cmd, "run", fake_run)
    monkeypatch.setattr(
        registry,
        "show_run",
        lambda _target, _run_id: {"run_id": "captured-run", "status": "completed"},
    )
    return captured
```

- [ ] Run and observe the missing provider and argument:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_browser_ai.py","tests/test_research_cmd.py"]' --capture brigade-work --capture-kind skill
```

Expect import or unexpected-keyword failures.

- [ ] Implement `BrowserAiProvider` with a strict JSON-array prompt. Reject
non-HTTPS URLs, non-list JSON, more than `limit` items, entries without URL,
title, or snippet, and snippets longer than 12,000 characters. Cache accepted
snippets by URL so `fetch` performs no second browser call. Set
`trust = "browser-ai"` and `source_type = "browser-ai"`. Copy the backend's
requested and observed model fields, defaulting observed to `"unverified"`.

Use this implementation surface in `browser_ai.py`:

```python
DISCOVERY_PROMPT = """Use browser research to find sources for this query.
Query: {query}
Return ONLY a JSON array with at most {limit} objects.
Each object must contain https url, title, and snippet strings."""


class BrowserAiDiscoveryError(RuntimeError):
    pass


class BrowserAiProvider:
    trust = "browser-ai"
    source_type = "browser-ai"
    source_id = "gemini-browser"

    def __init__(self, *, backend: Any, lane: str) -> None:
        self.backend = backend
        self.lane = lane
        self.requested_model = getattr(backend, "requested_model", None)
        self.observed_model = getattr(backend, "observed_model", None) or "unverified"
        self._pages: dict[str, dict[str, Any]] = {}

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        raw = self.backend.complete(
            [{"role": "user", "content": DISCOVERY_PROMPT.format(query=query, limit=limit)}],
            max_tokens=4096,
            timeout=180,
        )
        self.observed_model = getattr(self.backend, "observed_model", None) or "unverified"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrowserAiDiscoveryError("browser AI discovery returned invalid JSON") from exc
        if not isinstance(parsed, list) or len(parsed) > limit:
            raise BrowserAiDiscoveryError("browser AI discovery returned an invalid result count")
        hits: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                raise BrowserAiDiscoveryError("browser AI discovery result must be an object")
            url = item.get("url")
            title = item.get("title")
            snippet = item.get("snippet")
            if not all(isinstance(value, str) and value.strip() for value in (url, title, snippet)):
                raise BrowserAiDiscoveryError("browser AI discovery result is missing url, title, or snippet")
            if urlparse(url).scheme != "https":
                raise BrowserAiDiscoveryError("browser AI discovery URL must use https")
            if len(snippet) > 12_000:
                raise BrowserAiDiscoveryError("browser AI discovery snippet exceeds 12000 characters")
            self._pages[url] = {"success": True, "content": snippet, "title": title}
            hits.append({"url": url, "title": title, "trust": self.trust})
        return hits

    def fetch(self, url: str) -> dict[str, Any]:
        return dict(self._pages.get(url, {"success": False, "content": "", "title": ""}))
```

- [ ] Add `--browser-ai-research` to `research run`, `--profile` with built-in
choices, and `--synthesizer` plus `--reviewer` seat-name overrides. Pass all
four values to `cli_run`. The flag is false by default. A selected `browser-ai`
profile sets it true even when the flag is absent. No other profile may activate
it implicitly. Forward the existing `--category` value and persist it as
`category` in `research.json`. The current parser accepts it but the command
drops it.

In `research_cmd.run`, create `BrowserAiProvider` only after admission resolves
a `research.browser-discover` seat. Add it to the provider list and manifest
with `origin: "browser-ai"`. A run with no local, CLI, web, or browser-AI route
fails admission with `failure_kind: "no-source-route"` and exit code 2.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_browser_ai.py","tests/test_research_cmd.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect both files to pass.

- [ ] Commit Task 6:

```bash
git add src/brigade/research/sources/browser_ai.py src/brigade/research_cmd.py src/brigade/cli/research.py tests/test_research_browser_ai.py tests/test_research_cmd.py
git commit -m "feat: add explicit browser AI discovery"
```

### Task 7: Durable budgets, cancellation, resume, and publication

**Files:**

- Modify: `src/brigade/research/types.py`
- Modify: `src/brigade/research/registry.py`
- Modify: `src/brigade/research/engine.py`
- Modify: `src/brigade/research_cmd.py`
- Test: `tests/test_research_engine.py`
- Test: `tests/test_research_cmd.py`

- [ ] Add failing tests for active cancellation and phase-bound resume:

```python
def test_cancellation_watcher_stops_registered_process(tmp_path: Path) -> None:
    process_registry = RecordingProcessRegistry()
    registry.create_standard_run(
        tmp_path,
        run_id="cancel-me",
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    cancelled = Event()
    watcher = CancellationWatcher(
        target=tmp_path,
        run_id="cancel-me",
        cancelled=cancelled,
        process_registry=process_registry,
        poll_seconds=0.01,
    )
    watcher.start()

    registry.request_cancel(tmp_path, "cancel-me", requested_by="operator")

    assert cancelled.wait(timeout=1)
    assert process_registry.cancel_calls == 1
    watcher.stop()
    watcher.join(timeout=1)


def test_resume_reuses_findings_when_digest_matches(monkeypatch, tmp_path: Path) -> None:
    run_id = seed_cancelled_run_after_extraction(tmp_path)
    discovery = Mock(side_effect=AssertionError("discovery must not repeat"))
    extraction = Mock(side_effect=AssertionError("extraction must not repeat"))
    monkeypatch.setattr(ResearchEngine, "_discover", discovery)
    monkeypatch.setattr(ResearchEngine, "_extract", extraction)

    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)

    assert code == 0
    assert registry.show_run(tmp_path, run_id)["status"] == "completed"
```

Add these test helpers in `tests/test_research_cmd.py`:

```python
class RecordingProcessRegistry:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


def minimal_research_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
    )


def seed_cancelled_run_after_extraction(target: Path) -> str:
    run_id = "resume-after-extraction"
    registry.create_standard_run(
        target,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=minimal_research_roster(),
    )
    source = SourceEnvelope.build(
        origin="local",
        provider="local",
        uri="docs/fact.md",
        content="Verified fact",
        trust="local",
        acquired_at="2026-08-13T12:00:00+00:00",
    )
    finding = Finding(
        source_ids=(source.source_id,),
        title="Fact",
        summary="Verified fact",
        evidence="Verified fact",
        trust="local",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:01:00+00:00",
        parent_source_ids=(),
    )
    source_ref = registry.write_sources(target, run_id, [source])
    finding_ref = registry.write_findings(target, run_id, [finding])
    registry.update_research(
        target,
        run_id,
        current_phase="synthesis",
        phases={
            "planning": {"status": "completed", "value": "plan"},
            "discovery": {"status": "completed", "artifact": source_ref},
            "extraction": {"status": "completed", "artifact": finding_ref},
            "synthesis": {"status": "cancelled"},
            "review": {"status": "pending"},
            "repair": {"status": "pending"},
            "publishing": {"status": "pending"},
        },
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(target, run_id), status="cancelled")
    return run_id
```

Add this budget test:

```python
def test_second_research_dispatch_exhausts_budget() -> None:
    events: list[tuple[str, dict[str, object], str]] = []

    def append_event(event_type: str, payload: dict[str, object], key: str) -> None:
        events.append((event_type, payload, key))

    coordinator = BudgetCoordinator(
        declaration=RunBudgetDeclaration(worker_dispatch_count=1),
        append_event=append_event,
    )
    callbacks = ResearchBudgetCallbacks(coordinator=coordinator, append_event=append_event)
    luna = Agent(name="luna", cli="codex", role="researcher")

    assert callbacks.on_dispatch_requested(luna) == 1
    with pytest.raises(BudgetPolicyError) as caught:
        callbacks.on_dispatch_requested(luna)

    assert caught.value.code == "budget_exhausted"
```

- [ ] Run and observe behavioral failures:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_engine.py","tests/test_research_cmd.py"]' --capture brigade-work --capture-kind skill
```

Expect cancellation not to terminate the active process and resume to repeat
discovery.

- [ ] Add `Caps.max_dispatches: int = 24`. At run admission, create
`RunBudgetDeclaration(wall_clock_seconds=caps.max_time,
worker_dispatch_count=caps.max_dispatches)` and a `BudgetCoordinator`. Add this
public adapter in `research_cmd.py`. It uses only the coordinator's public
`reserve_and_record_dispatch` method:

```python
@dataclass
class ResearchBudgetCallbacks:
    coordinator: BudgetCoordinator
    append_event: Callable[[str, dict[str, object], str], object]
    attempts: dict[str, int] = field(default_factory=dict)

    def _next_attempt(self, seat: str) -> int:
        attempt = self.attempts.get(seat, 0) + 1
        self.attempts[seat] = attempt
        return attempt

    def _record(self, event_type: str, seat: str, attempt: int) -> int:
        self.append_event(
            event_type,
            {"seat": seat, "attempt": attempt},
            f"{event_type}:{seat}:{attempt}",
        )
        return attempt

    def on_dispatch_requested(self, agent: Agent) -> int | None:
        return self.coordinator.reserve_and_record_dispatch(
            seat=agent.name,
            allocate_attempt=lambda: self._next_attempt(agent.name),
            record_requested=lambda attempt: self._record(
                "run.dispatch.requested", agent.name, attempt
            ),
        )

    def on_dispatch_observed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.observed", agent.name, attempt)

    def on_dispatch_completed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.completed", agent.name, attempt)

    def on_dispatch_failed(self, agent: Agent, attempt: int) -> None:
        self._record("run.dispatch.failed", agent.name, attempt)
```

Pass these four methods into `SeatInvoker`. Persist the budget declaration and
observed projection in `run.json` and `research.json` after every phase
transition. Normalize coordinator code `budget_exhausted` to research
`failure_kind: "budget-exhausted"` and mark the active phase failed.

Wrap the active run body in
`runguard.run_lock(target, run_dir=registry.standard_run_dir(target, run_id))`.
This makes `runguard.has_active_run_owner(target, run_dir)` the sole ownership
check for cancel and resume. Do not introduce a research-specific PID file.

- [ ] Start one daemon cancellation watcher per active run. It polls the
stamped sidecar every 250 milliseconds. When `cancel_requested_at` appears, it
sets the engine cancellation event and calls the shared `ProcessRegistry.cancel`
once. Stop and join the watcher in a `finally` block. The engine checks the
event before every phase, before each provider batch, and after each seat call.

Implement it in `research_cmd.py` as a `Thread` subclass:

```python
class CancellationWatcher(Thread):
    def __init__(
        self,
        *,
        target: Path,
        run_id: str,
        cancelled: Event,
        process_registry: proc.ProcessRegistry,
        poll_seconds: float = 0.25,
    ) -> None:
        super().__init__(name=f"research-cancel-{run_id}", daemon=True)
        self.target = target
        self.run_id = run_id
        self.cancelled = cancelled
        self.process_registry = process_registry
        self.poll_seconds = poll_seconds
        self.stopped = Event()

    def stop(self) -> None:
        self.stopped.set()

    def run(self) -> None:
        while not self.stopped.wait(self.poll_seconds):
            sidecar = registry.read_research(self.target, self.run_id)
            if sidecar.get("cancel_requested_at"):
                self.cancelled.set()
                self.process_registry.cancel()
                return
```

`cancel` atomically writes `cancel_requested_at` and `cancel_requested_by:
"operator"`. It sets terminal status immediately only when no live runner owns
the run, as reported by `runguard.has_active_run_owner`. The live runner writes
the final `cancelled` lifecycle event and exit code 130.

- [ ] Define durable resume boundaries from stamped artifact digests:

```python
@dataclass(frozen=True)
class ResumeState:
    plan: str | None = None
    sources: tuple[SourceEnvelope, ...] | None = None
    findings: tuple[Finding, ...] | None = None
    report: str | None = None
    audit: CitationAudit | None = None


def resume_state(target: Path, run_id: str) -> ResumeState:
    sidecar = registry.read_research(target, run_id)
    phases = sidecar["phases"]
    sources = registry.read_verified_artifact(target, run_id, phases["discovery"].get("artifact"))
    findings = registry.read_verified_artifact(target, run_id, phases["extraction"].get("artifact"))
    report = registry.read_verified_artifact(target, run_id, phases["synthesis"].get("artifact"))
    audit = registry.read_verified_artifact(target, run_id, phases["review"].get("artifact"))
    return ResumeState(
        plan=phases["planning"].get("value") if phases["planning"]["status"] == "completed" else None,
        sources=sources if phases["discovery"]["status"] == "completed" else None,
        findings=findings if phases["extraction"]["status"] == "completed" else None,
        report=report if phases["synthesis"]["status"] == "completed" else None,
        audit=audit if phases["review"]["status"] == "completed" else None,
    )
```

`read_verified_artifact` recomputes SHA-256 and returns `None` on a mismatch.
Resume is permitted only from `failed`, `cancelled`, or an incomplete active
record with no live owner. A completed run returns exit code 2. Legacy resume
continues through a renamed `resume_legacy` path using the old checkpoint.

- [ ] Persist `sources.json` atomically when discovery completes, including the
source envelopes and bounded source content required for extraction replay.
Do not include `sources.json` content in `status --json` or `show --json`.
Publish `report.md`, `report.html`, `handoff.md`, `findings.json`, and
`citation-audit.json` atomically only after the accepted review. Store relative
paths and digests in `research.json`. Update `run.json` to `status: "completed"`.
On failure, keep phase artifacts but do not write a final report. Persist
`failure_phase`, `failure_kind`, and an operator-safe detail in both receipts.
Use `aboyeur.record_run_termination` for `failed` and `cancelled` outcomes so
the standard terminal lifecycle event and finished timestamp are preserved.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_engine.py","tests/test_research_cmd.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect cancellation, resume, budget, publication, and failure tests to pass.

- [ ] Commit Task 7:

```bash
git add src/brigade/research/types.py src/brigade/research/registry.py src/brigade/research/engine.py src/brigade/research_cmd.py tests/test_research_engine.py tests/test_research_cmd.py
git commit -m "feat: make research runs durable"
```

### Task 8: Doctor, init, versioned JSON, and work-plan integration

**Files:**

- Create: `src/brigade/research/doctor.py`
- Modify: `src/brigade/research_cmd.py`
- Modify: `src/brigade/cli/research.py`
- Modify: `src/brigade/work_cmd/ledger.py:4328-4395`
- Test: `tests/test_research_doctor.py`
- Test: `tests/test_research_cmd.py`
- Test: `tests/test_work_cmd_ledger.py`

- [ ] Add failing contract tests:

```python
def test_doctor_reports_each_lane_without_profile_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("brigade.research.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    payload = doctor_payload(
        tmp_path,
        roster=doctor_roster(),
        probe=lambda _agent: {
            "auth_status": "authenticated",
            "detail": "profile=/home/alice/.config/browser secret-cookie-value Bearer abcdef",
        },
    )

    assert payload["schema"] == "brigade.research.doctor.v1"
    assert [lane["capability"] for lane in payload["lanes"]] == [
        "research.plan",
        "research.extract",
        "research.synthesize",
        "research.review",
        "research.browser-discover",
    ]
    gemini = next(lane for lane in payload["lanes"] if lane["capability"] == "research.browser-discover")
    assert gemini["concurrency"] == 1
    assert gemini["read_only"] is True
    assert "/home/" not in json.dumps(payload)
    assert "secret-cookie-value" not in json.dumps(payload)
    assert "Bearer " not in json.dumps(payload)


def test_status_all_json_is_center_safe(tmp_path: Path, capsys) -> None:
    seed_standard_and_legacy_runs(tmp_path)

    code = cli_status(target=tmp_path, run_id=None, all_runs=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["schema"] == "brigade.research.status.v1"
    assert payload["schema_version"] == 1
    assert {run["legacy"] for run in payload["runs"]} == {True, False}
    assert all("browser_profile" not in json.dumps(run) for run in payload["runs"])
```

```python
def test_plan_artifact_accepts_completed_standard_research_run(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path)
    task_id = _plan_task_id(tmp_path, capsys)
    run_id = seed_completed_standard_research_run(tmp_path)

    code = work_cmd.task_plan(
        target=tmp_path,
        task_id=task_id[:12],
        write=True,
        from_research=run_id,
    )
    capsys.readouterr()
    json_path, _ = work_cmd._plan_paths(tmp_path, task_id)
    artifact = json.loads(json_path.read_text(encoding="utf-8"))["research_runs"][0]

    assert code == 0
    assert artifact["kind"] == "research"
    assert artifact["trust"] == "mixed-provenance"
    assert artifact["citation_audit"] == "accepted"
```

Add these exact helpers beside the relevant tests:

```python
def doctor_roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="orchestrator"),
            "luna": Agent(
                name="luna",
                cli="codex",
                role="researcher",
                model="gpt-5.6-luna",
                capabilities=(
                    "research.plan",
                    "research.extract",
                    "research.synthesize",
                    "research.review",
                ),
            ),
            "gemini_browser": Agent(
                name="gemini_browser",
                cli="oracle",
                role="browser researcher",
                model="gemini-3.1-pro",
                capabilities=("research.synthesize", "research.browser-discover"),
            ),
        },
    )


def seed_standard_and_legacy_runs(target: Path) -> None:
    registry.create_standard_run(
        target,
        run_id="standard-run",
        question="New",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    legacy = target / ".brigade" / "research" / "legacy-run"
    legacy.mkdir(parents=True)
    localio.write_json(
        legacy / "run.json",
        {"run_id": "legacy-run", "question": "Old", "status": "done"},
    )


def seed_completed_standard_research_run(target: Path) -> str:
    run_id = "completed-research"
    registry.create_standard_run(
        target,
        run_id=run_id,
        question="What is the loop?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 8},
        roster=doctor_roster(),
    )
    audit_ref = registry.write_citation_audit(
        target,
        run_id,
        CitationAudit(
            accepted=True,
            citations=(
                CitationRecord(
                    token="[source:src-1111111111111111]",
                    source_ids=("src-1111111111111111",),
                    status="accepted",
                ),
            ),
            unresolved=(),
        ),
    )
    localio.write_text_atomic(registry.standard_run_dir(target, run_id) / "report.md", "# Report\n")
    registry.update_research(
        target,
        run_id,
        review_result="accepted",
        artifacts={"report_md": "report.md", "citation_audit": audit_ref},
    )
    aboyeur.update_run_receipt(
        registry.standard_run_dir(target, run_id),
        status="completed",
        artifacts={"report_md": "report.md"},
    )
    return run_id
```

- [ ] Run and observe missing commands and schema fields:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_doctor.py","tests/test_research_cmd.py","tests/test_work_cmd_ledger.py"]' --capture brigade-work --capture-kind skill
```

Expect missing `doctor_payload`, `cli_status`, or new source metadata.

- [ ] Implement `doctor_payload` with schema
`brigade.research.doctor.v1`. For each fixed capability report:
`configured`, `seat`, `cli`, `executable`, `version`, `auth_status`,
`requested_model`, `model_attestation`, `read_only`, `timeout_seconds`,
`concurrency`, `last_live_smoke`, `status`, and `detail`. Oracle auth failures
retain `failure_kind: "browser-auth"`. Redact home paths, profile paths, header
values, environment values, cookies, tokens, and raw stderr. Overall status is
`fail` when the selected profile lacks a required planner, extractor,
synthesizer, reviewer, or source route. It is `warn` for an unavailable optional
fallback or browser-discovery lane.

Its signature is
`doctor_payload(target: Path, *, roster: Roster | None = None,
profile_name: str | None = None, probe: Callable[[Agent], dict[str, object]] =
probe_agent) -> dict[str, object]`. Tests inject `probe`. Production uses the
read-only executable and adapter health probes. Sanitize probe output before it
enters a lane record.

- [ ] Register and implement these exact CLI contracts:

```text
brigade research init [--profile grounded] [--json]
brigade research doctor [--profile grounded] [--json]
brigade research status <run-id> [--json]
brigade research status --all [--json]
```

Keep `research list` as a compatibility alias for `research status --all` for
one release window. It emits the same schema under `--json`.

`init` creates this file only when it is absent:

```toml
[research]
default_profile = "grounded"

[caps]
max_rounds = 6
max_time = 300
max_dispatches = 24
```

When `--profile` is supplied, substitute its validated built-in name for
`grounded` in `default_profile`. Every other line stays byte-identical.
If the file exists, return exit code 2 and do not modify it. JSON projections
use `brigade.research.status.v1`, `brigade.research.show.v1`, and
`brigade.research.doctor.v1`, each with `schema_version: 1`. `show --json`
contains `run`, `research`, `findings`, and `citation_audit`. Missing artifacts
are `null`. The projection sanitizes secrets and absolute browser-profile paths.

Change `cli_run` and `cli_resume` to return the pinned exit code from terminal
status and failure kind. Do not return zero for a failed record.

- [ ] Update `_write_plan_artifact` to require a completed research run and an
accepted citation audit. Read both legacy and standard runs through the
registry. Standard runs use `trust: "mixed-provenance"`. Legacy runs retain
`trust: "untrusted-web"` and `legacy: true`. Reject failed, active, cancelled,
or unresolved standard runs.

- [ ] Run focused tests and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_doctor.py","tests/test_research_cmd.py","tests/test_work_cmd_ledger.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect all files to pass.

- [ ] Commit Task 8:

```bash
git add src/brigade/research/doctor.py src/brigade/research_cmd.py src/brigade/cli/research.py src/brigade/work_cmd/ledger.py tests/test_research_doctor.py tests/test_research_cmd.py tests/test_work_cmd_ledger.py
git commit -m "feat: expose research operations and health"
```

### Task 9: Hermetic acceptance, live runbook, and release verification

**Files:**

- Create: `tests/test_research_acceptance.py`
- Create: `docs/runbooks/research-live-acceptance.md`
- Modify: `docs/phase-first-class-multi-lane-research.md:1-10`

- [ ] Add hermetic `oracle` and `codex` executables under `tmp_path/bin` in the
acceptance fixture. They read stdin. Oracle emits a cited report for synthesis,
strict JSON for browser discovery, and a `browser-auth` error when
`ORACLE_TEST_MODE=expired-cookie`. Codex emits strict planning, extraction,
review, and fallback responses based on the prompt. Do not patch
`research_cmd`, `SeatInvoker`, or `agents.run_agent` in these tests.

```python
def test_grounded_cli_creates_auditable_standard_run(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="success")

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    run_dir = workspace / ".brigade" / "runs" / payload["run_id"]
    run = json.loads((run_dir / "run.json").read_text())
    research = json.loads((run_dir / "research.json").read_text())
    audit = json.loads((run_dir / "citation-audit.json").read_text())
    assert run["kind"] == "research"
    assert run["status"] == "completed"
    assert (run_dir / "journal.jsonl").is_file()
    assert any((run_dir / "workers").iterdir())
    assert research["resolved_lanes"]["synthesis"]["primary"] == "gemini_browser"
    assert research["resolved_lanes"]["review"]["primary"] == "luna"
    assert audit["accepted"] is True
    assert audit["unresolved"] == []


def test_expired_cookie_fails_truthfully_without_report(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="expired-cookie", allow_fallback=False)

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    run_dir = workspace / ".brigade" / "runs" / payload["run_id"]
    assert payload["failure_kind"] == "browser-auth"
    assert payload["failure_phase"] == "synthesis"
    assert not (run_dir / "report.md").exists()
    assert "cookie" not in result.stderr.lower()


def test_oracle_failure_uses_luna_fallback(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="expired-cookie", allow_fallback=True)

    result = run_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )

    payload = json.loads(result.stdout)
    research = json.loads(
        (workspace / ".brigade" / "runs" / payload["run_id"] / "research.json").read_text()
    )
    assert result.returncode == 0
    assert research["synthesis"]["seat"] == "luna"
    assert research["fallbacks"][0]["from_seat"] == "gemini_browser"
    assert research["fallbacks"][0]["failure_kind"] == "browser-auth"


def test_browser_ai_discovery_requires_explicit_mode(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="success", include_source=False)

    denied = run_brigade(
        workspace,
        "research",
        "run",
        "Find browser agents",
        "--profile",
        "grounded",
        "--json",
    )
    allowed = run_brigade(
        workspace,
        "research",
        "run",
        "Find browser agents",
        "--profile",
        "browser-ai",
        "--browser-ai-research",
        "--json",
    )

    allowed_payload = json.loads(allowed.stdout)
    research = json.loads(
        (workspace / ".brigade" / "runs" / allowed_payload["run_id"] / "research.json").read_text()
    )
    assert denied.returncode == 2
    assert json.loads(denied.stdout)["failure_kind"] == "no-source-route"
    assert allowed.returncode == 0
    assert research["discovery_mode"] == "browser-ai"
    assert research["source_counts"]["browser-ai"] == 1


def test_cancel_active_synthesis_exits_130(tmp_path: Path) -> None:
    workspace = acceptance_workspace(tmp_path, oracle_mode="block")
    process = start_brigade(
        workspace,
        "research",
        "run",
        "What changed?",
        "--source",
        "docs/*.md",
        "--profile",
        "grounded",
        "--json",
    )
    run_id = wait_for_phase(workspace, "synthesis", timeout=5.0)

    cancelled = run_brigade(workspace, "research", "cancel", run_id, "--json")
    stdout, stderr = process.communicate(timeout=5)

    assert cancelled.returncode == 0
    assert process.returncode == 130, stderr
    assert json.loads(stdout)["status"] == "cancelled"
```

Add these complete acceptance helpers above the tests. The stubs inspect the
real adapter inputs and return prompt-specific responses:

```python
def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)


def acceptance_workspace(
    tmp_path: Path,
    *,
    oracle_mode: str,
    allow_fallback: bool = True,
    include_source: bool = True,
) -> Path:
    workspace = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    workspace.mkdir()
    bin_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / ".brigade").mkdir()
    (workspace / "docs").mkdir()
    if include_source:
        (workspace / "docs" / "fact.md").write_text("Verified fact\n", encoding="utf-8")
    (workspace / ".brigade" / "roster.toml").write_text(
        """
[orchestrator]
name = "chef"

[agents.chef]
cli = "codex"
role = "orchestrator"

[agents.luna]
cli = "codex"
role = "researcher"
model = "gpt-5.6-luna"
reasoning = "medium"
capabilities = ["research.plan", "research.extract", "research.synthesize", "research.review"]

[agents.gemini_browser]
cli = "oracle"
role = "browser researcher"
model = "gemini-3.1-pro"
capabilities = ["research.synthesize", "research.browser-discover"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (workspace / ".brigade" / "research.toml").write_text(
        (
            "[research]\n"
            'default_profile = "grounded"\n\n'
            "[profiles.grounded]\n"
            'discovery = ["brigade"]\n'
            'planner = ["luna"]\n'
            'extractor = ["luna"]\n'
            'synthesizer = ["gemini_browser", "luna"]\n'
            'reviewer = ["luna"]\n'
            f"allow_synthesis_fallback = {str(allow_fallback).lower()}\n"
            "browser_ai_research = false\n\n"
            "[profiles.browser-ai]\n"
            'discovery = ["browser-ai"]\n'
            'planner = ["luna"]\n'
            'extractor = ["luna"]\n'
            'synthesizer = ["gemini_browser", "luna"]\n'
            'reviewer = ["luna"]\n'
            "allow_synthesis_fallback = true\n"
            "browser_ai_research = true\n"
        ),
        encoding="utf-8",
    )
    _write_executable(
        bin_dir / "oracle",
        """import json, os, re, sys, time
prompt = sys.argv[sys.argv.index('-p') + 1]
mode = os.environ.get('ORACLE_TEST_MODE', 'success')
if mode == 'expired-cookie':
    print('browser session expired', file=sys.stderr)
    raise SystemExit(1)
if mode == 'block':
    time.sleep(30)
if 'Return ONLY a JSON array' in prompt:
    print(json.dumps([{'url': 'https://example.test/current', 'title': 'Current', 'snippet': 'Verified fact'}]))
else:
    source = re.search(r'\[source:(src-[a-f0-9]{16})\]', prompt)
    print('Oracle report ' + (source.group(0) if source else ''))
""",
    )
    _write_executable(
        bin_dir / "codex",
        """import json, re, sys
prompt = sys.stdin.read()
source = re.search(r'\[source:(src-[a-f0-9]{16})\]', prompt)
if 'research strategist' in prompt:
    print(json.dumps({'sub_questions': ['fact'], 'key_topics': ['fact'], 'success_criteria': 'cite'}))
elif 'extract only the information' in prompt:
    print(json.dumps({'summary': 'Verified fact', 'evidence': 'Verified fact'}))
elif 'independent research reviewer' in prompt:
    print(json.dumps({'accepted': True, 'detail': 'citations resolve', 'rejected_claims': []}))
else:
    print('Luna report ' + (source.group(0) if source else ''))
""",
    )
    localio.write_json(
        workspace / ".acceptance-env.json",
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "ORACLE_TEST_MODE": oracle_mode,
        },
    )
    return workspace


def _acceptance_env(workspace: Path) -> dict[str, str]:
    return {
        **os.environ,
        **json.loads((workspace / ".acceptance-env.json").read_text(encoding="utf-8")),
    }


def run_brigade(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "brigade", *args, "--target", str(workspace)],
        cwd=workspace,
        env=_acceptance_env(workspace),
        text=True,
        capture_output=True,
        timeout=15,
    )


def start_brigade(workspace: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "brigade", *args, "--target", str(workspace)],
        cwd=workspace,
        env=_acceptance_env(workspace),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_for_phase(workspace: Path, phase: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for run_dir in (workspace / ".brigade" / "runs").glob("*"):
            sidecar = run_dir / "research.json"
            if sidecar.is_file() and json.loads(sidecar.read_text())["current_phase"] == phase:
                return run_dir.name
        time.sleep(0.02)
    raise AssertionError(f"research run did not reach {phase}")
```

- [ ] Run the hermetic acceptance file and capture:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_acceptance.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect all acceptance cases to pass without network access or a real browser.

- [ ] Write `docs/runbooks/research-live-acceptance.md` with these non-counted,
operator-run checks and their required evidence:

```bash
brigade research doctor --profile grounded --json
brigade research run "Summarize the latest Brigade research changes" --source "docs/*.md" --profile grounded --json
brigade research run "Find current browser-agent research products" --profile browser-ai --browser-ai-research --json
```

The runbook requires a real `oracle --engine browser` session, records Oracle
version, requested model, observed model or `unverified`, run IDs, duration,
citation audit, fallback state, and redacted doctor output. It explicitly says
the hermetic stub does not prove live Oracle authentication or model identity.

- [ ] Change the design status from `Implementation: planned` to
`Implementation: complete`, retaining the link to this file. Leave
`Dashboard: explicitly deferred to Phase 5` unchanged.

- [ ] Run the focused research suite through Brigade:

```bash
brigade work verify run --target . --argv-json '["pytest","-q","tests/test_research_types.py","tests/test_research_config.py","tests/test_research_llm.py","tests/test_research_extract.py","tests/test_research_engine.py","tests/test_research_registry.py","tests/test_research_provenance.py","tests/test_research_browser_ai.py","tests/test_research_doctor.py","tests/test_research_cmd.py","tests/test_research_acceptance.py","tests/test_work_cmd_ledger.py","tests/test_run_seat.py","tests/test_run_projector.py","tests/test_agents_oracle.py"]' --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect zero failures.

- [ ] Run the repository definition of done and capture it:

```bash
brigade work verify run --target . --command "./scripts/verify" --capture brigade-work --capture-kind skill
brigade outcome capture brigade-work --run-id latest
```

Expect `./scripts/verify` to exit 0. If the command exposes an unrelated baseline
failure, record that failure receipt, run the smallest affected check again,
and do not mark this task complete until the baseline is green or the owner
accepts the named blocker.

- [ ] Create and lint the required Memory Handoff:

```bash
brigade work verify run --target . --command "brigade memory handoff create --target . --agent codex --summary 'Implemented first-class multi-lane research on the standard run lifecycle' --finding 'Research runs now use standard run receipts, provenance sidecars, Gemini browser synthesis, Luna review, and explicit browser-AI discovery.' --file docs/phase-first-class-multi-lane-research.md --next-step 'Run the live Oracle acceptance runbook and attach its evidence receipt.' && brigade memory handoff lint --target ." --capture brigade-handoffs --capture-kind skill
brigade outcome capture brigade-handoffs --run-id latest
```

Expect the handoff to be created and lint to exit 0.

- [ ] Commit Task 9:

```bash
git add tests/test_research_acceptance.py docs/runbooks/research-live-acceptance.md docs/phase-first-class-multi-lane-research.md .claude/memory-handoffs
git commit -m "test: verify first-class research lane"
```

## Deferred Phase 5 contract

No task above modifies `src/brigade/center_cmd/dashboard/`. After Phases 1
through 4 ship and the JSON fixtures have survived one compatibility window,
Center may add a read-only Research page by invoking only:

```bash
brigade research status --all --json
brigade research show <run-id> --json
brigade research doctor --json
```

The page must not read `.brigade/runs`, `.brigade/research`, Oracle sessions, or
browser profiles directly. Start, cancel, resume, and profile editing remain a
later dashboard action phase.
