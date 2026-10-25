# Grok Bot t3-dispatch pack implementation plan

**Goal:** Ship a first-party Grok Bot connector pack, `t3-dispatch`, that lets any Grok Bot launch and steer T3 threads on the operator's fleet hosts through `t3-fleet`, and wakes the calling bot when a thread finishes or needs input.

**Architecture:** A new package `src/brigade/grokbot_t3/` follows the `operations-relay` pack pattern (bearer-gated Streamable HTTP MCP listener, closed tool set, owner-only config). Tool handlers call one subprocess boundary (`fleet.py`) that talks to the local `t3-fleet` client with JSON on stdin, so prompts never reach argv. A pack-owned SQLite ledger records each launched request; a one-minute timer runs a watcher that polls open requests and POSTs a wake to the bot's webhook routine.

**Key technology:** Python 3.12, `mcp>=2` (Streamable HTTP), SQLite, systemd user units, Cloudflare Tunnel and Access, `t3-fleet` request schema `t3-fleet.request.v1`.

**Spec:** `docs/superpowers/specs/2026-10-16-grokbot-t3-dispatch-design.md` (approved 2026-10-16).

Execute the tasks in order and keep the checkboxes current. Every behavior change starts with a failing test. Run every check through Brigade: `brigade work verify run --target . --command "<check>" --capture brigade-work`. In the commands below, `V` means that prefix; write it out in full when you run them.

## File map

| File | Responsibility |
|---|---|
| `src/brigade/grokbot_t3/__init__.py` | Public names the registry and CLI import. |
| `src/brigade/grokbot_t3/contracts.py` | Tool names, limits, error type, argument parsers. |
| `src/brigade/grokbot_t3/runtime_config.py` | Owner-only runtime JSON loading and validation. |
| `src/brigade/grokbot_t3/fleet.py` | The only `t3-fleet` and `git ls-remote` subprocess boundary. |
| `src/brigade/grokbot_t3/ledger.py` | SQLite ledger of launched requests. |
| `src/brigade/grokbot_t3/tools.py` | Tool handlers: allowlists, limits, host pick, fallback seat, projection. |
| `src/brigade/grokbot_t3/watch.py` | Wake config, wake POST, one watcher pass. |
| `src/brigade/grokbot_t3/lifecycle.py` | Listener config, request gate, ASGI app, doctor, canary, unit rendering. |
| `src/brigade/grokbot_t3/__main__.py` | `python -m brigade.grokbot_t3 watch|install-watch`. |
| `src/brigade/grokbot_packs.py` | Register the pack: bind 8778, tools, instance key `runtime_path`, dispatch branches. |
| `src/brigade/cli/run_cloud.py` | `serve --pack t3-dispatch` branch and error mapping. |
| `tests/test_grokbot_t3_*.py` | One test module per source module. |
| `tests/test_grokbot_packs.py` | Registry expectations for the new pack. |
| `docs/grokbot-t3-dispatch.md` | Operator runbook. |
| `docs/grokbot-operating-guide.md` | Pack table row. |
| `CHANGELOG.md` | Unreleased entry. |

## demi

Actual ask: Grok Bot bots launch T3 threads on three hosts across Codex, Claude Code, and OpenCode seats, and get woken when work needs them.

Smallest useful slice: one connector pack that wraps the existing `t3-fleet` client plus a polling watcher. No new queue, no hub schema, no T3 changes.

Existing pattern: `src/brigade/grokbot_operations_relay.py` for the listener, gate, unit rendering, doctor, and canary.

Cut from scope: hub enrollment, the `t3-fleet` local queue and `tick`, merge automation, Dot bridge changes.

## Pinned decisions

- Pack id `t3-dispatch`, default bind `127.0.0.1:8778`, `public_route` `""`. The registry rejects overlapping routes and `obsidian-operator` owns `/mcp`; this pack gets its own hostname, `t3-dispatch.brigade.tools`.
- Instance config adds exactly one key, `runtime_path`. Everything else lives in the runtime JSON so `pack setup` needs no new flags (it already has `--runtime-path`).
- `t3-fleet` is always called in machine mode: argv is exactly `[t3_fleet_bin, <action>]`, stdin is one JSON object with `"schema": "t3-fleet.request.v1"` and `"action": <action>`. Prompts travel as the `prompt` key on stdin.
- Submits and follow-ups always send `"runtime_mode": "full-access"`.
- Default seat comes from runtime `default_seat` (`dsflash`). When the bot did not name a seat and `t3-fleet` answers `policy_rejected` with reason `model-admission-denied`, the pack retries once on `fallback_seat` (`glmflash`) and returns `"fallback_used": true`. A seat the bot named never falls back.
- Unenrolled controller (`fleet_enrolled: false`): the pack always sends an explicit host and seat, never `override_reason`. With host `auto`, it picks the allowlisted host with state `eligible` and the most free slots (`limit - active`); ties go to the earlier host in the `hosts` list. Enrolled (`true`): host `auto` passes through and every submit carries `override_reason` `grokbot t3-dispatch seat <seat> for <caller>`.
- Wake states: `needs_attention`, `unknown`, `succeeded`, `failed`, `interrupted`. A state change clears `notified_state`, so a second question after a running stretch wakes again.
- A wake counts as delivered only when every target for the caller returns HTTP 200.
- The pack never stores or logs prompt text. The ledger keeps the SHA-256 of the composed prompt.
- `fleet.py` stays free of Grok-specific assumptions (no caller names, wake logic, or Grok headers) because the phase 4 `dot` pack imports it. Grok-specific behavior lives in `tools.py` and `watch.py`.

---

### Task 0: Branch and environment

**Files:** none.

- [x] Create the implementation worktree from the spec branch:

```bash
cd <brigade-checkout>
git worktree add -b feat/grokbot-t3-dispatch <worktree-dir> origin/main
cd <worktree-dir>
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev,grokbot]"
```

- [x] Baseline: `V "./.venv/bin/pytest -q tests/test_grokbot_packs.py tests/test_grokbot_operations_relay.py"`. Expect all pass. If anything fails before your change, stop and report it.

---

### Task 1: Contracts

**Files:**
- Create: `src/brigade/grokbot_t3/contracts.py`
- Create: `src/brigade/grokbot_t3/__init__.py` (temporary one-line docstring; Task 7 fills it)
- Test: `tests/test_grokbot_t3_contracts.py`

- [x] Create `src/brigade/grokbot_t3/__init__.py` with only `"""First-party t3-dispatch Grok Bot connector pack."""`.
- [x] Write the failing test `tests/test_grokbot_t3_contracts.py`:

```python
"""t3-dispatch closed tool contracts."""

from __future__ import annotations

import pytest

from brigade.grokbot_t3 import contracts as c


def _submit(**overrides):
    base = {"repository": "brigade", "prompt": "Fix the flaky test.", "title": "Fix flake", "caller": "Builder"}
    base.update(overrides)
    return base


def test_tool_set_is_closed():
    assert c.TOOLS == {
        "t3_approve", "t3_answer", "t3_capacity", "t3_followup", "t3_interrupt",
        "t3_recent", "t3_status", "t3_submit", "t3_wait",
    }
    assert set(c.PARSERS) == c.TOOLS


def test_submit_defaults():
    parsed = c.parse_submit(_submit())
    assert parsed == {
        "repository": "brigade", "prompt": "Fix the flaky test.", "title": "Fix flake", "caller": "Builder",
        "host": "auto", "seat": None, "base_ref": None, "interaction_mode": "default",
    }


def test_submit_optional_fields():
    parsed = c.parse_submit(_submit(host="host-c", seat="sol61", base_ref="release/1.2", interaction_mode="plan"))
    assert (parsed["host"], parsed["seat"], parsed["base_ref"], parsed["interaction_mode"]) == (
        "host-c", "sol61", "release/1.2", "plan",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"extra": "x"},
        {"caller": ""},
        {"caller": "bad\nname"},
        {"title": "two\nlines"},
        {"title": "x" * 121},
        {"prompt": "   "},
        {"prompt": "nul\x00byte"},
        {"repository": "../etc"},
        {"base_ref": "a/../b"},
        {"base_ref": "main.lock"},
        {"interaction_mode": "yolo"},
        {"host": ""},
    ],
)
def test_submit_rejects(overrides):
    with pytest.raises(c.T3DispatchError) as info:
        c.parse_submit(_submit(**overrides))
    assert info.value.code == "invalid_request"


def test_submit_rejects_missing_and_non_object():
    with pytest.raises(c.T3DispatchError):
        c.parse_submit({"repository": "brigade"})
    with pytest.raises(c.T3DispatchError):
        c.parse_submit(["not", "an", "object"])


def test_wait_bounds():
    assert c.parse_wait({"request_id": "r-1"}) == {"request_id": "r-1", "timeout_s": 30}
    assert c.parse_wait({"request_id": "r-1", "timeout_s": 60})["timeout_s"] == 60
    for bad in (0, 61, True, 2.5):
        with pytest.raises(c.T3DispatchError):
            c.parse_wait({"request_id": "r-1", "timeout_s": bad})


def test_answer_and_approve():
    assert c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"pick": "B"}}) == {
        "request_id": "r-1", "input_id": "q-1", "answers": {"pick": "B"},
    }
    assert c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"many": ["a", "b"]}})["answers"] == {
        "many": ["a", "b"],
    }
    with pytest.raises(c.T3DispatchError):
        c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"k": 3}})
    with pytest.raises(c.T3DispatchError):
        c.parse_answer({"request_id": "r-1", "input_id": "q-1", "answers": {"k": "x" * 9000}})
    assert c.parse_approve({"request_id": "r-1", "approval_id": "a-1", "decision": "accept"})["decision"] == "accept"
    with pytest.raises(c.T3DispatchError):
        c.parse_approve({"request_id": "r-1", "approval_id": "a-1", "decision": "yes"})


def test_recent_defaults_and_bounds():
    assert c.parse_recent({}) == {"caller": None, "limit": 10}
    assert c.parse_recent(None) == {"caller": None, "limit": 10}
    assert c.parse_recent({"caller": "Builder", "limit": 20}) == {"caller": "Builder", "limit": 20}
    with pytest.raises(c.T3DispatchError):
        c.parse_recent({"limit": 21})


def test_public_error_shape():
    error = c.T3DispatchError("fleet-error", fleet_code="policy_rejected", fleet_reason="x", request_id="r-1")
    assert error.public_error() == {
        "error": {
            "code": "fleet-error",
            "message": c.ERROR_MESSAGES["fleet-error"],
            "fleet_code": "policy_rejected",
            "fleet_reason": "x",
            "request_id": "r-1",
        }
    }
    assert c.T3DispatchError("not-a-code").code == "unavailable"
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_contracts.py"`. Expect `ImportError` / `AttributeError` on `contracts`.
- [x] Implement `src/brigade/grokbot_t3/contracts.py`:

```python
"""Closed t3-dispatch tool contracts: names, limits, errors, and argument parsers."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

PACK_ID = "t3-dispatch"
DEFAULT_BIND = "127.0.0.1:8778"
SERVICE_NAME = "grokbot-t3-dispatch"
TOOLS = frozenset(
    {
        "t3_approve",
        "t3_answer",
        "t3_capacity",
        "t3_followup",
        "t3_interrupt",
        "t3_recent",
        "t3_status",
        "t3_submit",
        "t3_wait",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "interrupted"})
NOTIFY_STATES = TERMINAL_STATES | frozenset({"needs_attention", "unknown"})
APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
INTERACTION_MODES = frozenset({"default", "plan"})
HOST_AUTO = "auto"
MAX_REQUEST_BYTES = 65_536
PUBLIC_RESULT_LIMIT = 131_072
MAX_ANSWERS_BYTES = 8_192
WAIT_DEFAULT_SECONDS = 30
WAIT_MAX_SECONDS = 60
RECENT_DEFAULT = 10
RECENT_MAX = 20
TITLE_MAX = 120
CALLER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
ERROR_MESSAGES = {
    "invalid_request": "Tool input failed validation",
    "dispatch-disabled": "Dispatch is turned off",
    "repository-not-allowed": "Repository is not on the dispatch allowlist",
    "host-not-allowed": "Host is not on the dispatch allowlist",
    "seat-not-allowed": "Seat is not on the dispatch allowlist",
    "prompt-too-large": "Prompt exceeds the dispatch size limit",
    "open-limit": "Too many dispatched requests are still open",
    "daily-limit": "Daily dispatch limit reached",
    "unknown-request": "Request was not launched through this connector",
    "base-ref-unresolved": "Base ref did not resolve to a commit",
    "no-eligible-host": "No allowlisted host has free capacity",
    "fleet-error": "t3-fleet refused the request",
    "submit-unknown": "Submit outcome is unknown; check t3_recent before retrying",
    "unavailable": "Dispatch is unavailable",
}


class T3DispatchError(ValueError):
    """Stable public failure. Messages never include prompts, paths, or secrets."""

    def __init__(
        self,
        code: str,
        *,
        fleet_code: str | None = None,
        fleet_reason: str | None = None,
        request_id: str | None = None,
    ):
        if code not in ERROR_MESSAGES:
            code = "unavailable"
        self.code = code
        self.fleet_code = fleet_code
        self.fleet_reason = fleet_reason
        self.request_id = request_id
        super().__init__(ERROR_MESSAGES[code])

    def public_error(self) -> dict[str, dict[str, str]]:
        error = {"code": self.code, "message": ERROR_MESSAGES[self.code]}
        if self.fleet_code is not None:
            error["fleet_code"] = self.fleet_code
        if self.fleet_reason is not None:
            error["fleet_reason"] = self.fleet_reason
        if self.request_id is not None:
            error["request_id"] = self.request_id
        return {"error": error}

    def __repr__(self) -> str:
        return f"T3DispatchError({self.code!r})"


def _invalid() -> T3DispatchError:
    return T3DispatchError("invalid_request")


def _object(raw: object, required: frozenset[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _invalid()
    keys = set(raw)
    if not required <= keys or keys - required - optional:
        raise _invalid()
    if any(raw[key] is None for key in required):
        raise _invalid()
    return {key: value for key, value in raw.items() if value is not None}


def _match(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _invalid()
    return value


def _handle(value: object) -> str:
    return _match(value, HANDLE_RE)


def _name(value: object) -> str:
    return _match(value, NAME_RE)


def _caller(value: object) -> str:
    return _match(value, CALLER_RE)


def _ref(value: object) -> str:
    ref = _match(value, REF_RE)
    if ".." in ref or ref.endswith("/") or ref.endswith(".lock") or "//" in ref:
        raise _invalid()
    return ref


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _invalid()
    return value


def _title(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > TITLE_MAX:
        raise _invalid()
    if any(ch in value for ch in "\r\n\x00"):
        raise _invalid()
    return value


def _bounded_int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid()
    return value


def parse_capacity(raw: object) -> dict[str, Any]:
    _object(raw, frozenset())
    return {}


def parse_submit(raw: object) -> dict[str, Any]:
    data = _object(
        raw,
        frozenset({"repository", "prompt", "title", "caller"}),
        frozenset({"host", "seat", "base_ref", "interaction_mode"}),
    )
    parsed: dict[str, Any] = {
        "repository": _name(data["repository"]),
        "prompt": _text(data["prompt"]),
        "title": _title(data["title"]),
        "caller": _caller(data["caller"]),
        "host": HOST_AUTO,
        "seat": None,
        "base_ref": None,
        "interaction_mode": "default",
    }
    if "host" in data:
        parsed["host"] = HOST_AUTO if data["host"] == HOST_AUTO else _name(data["host"])
    if "seat" in data:
        parsed["seat"] = _name(data["seat"])
    if "base_ref" in data:
        parsed["base_ref"] = _ref(data["base_ref"])
    if "interaction_mode" in data:
        if data["interaction_mode"] not in INTERACTION_MODES:
            raise _invalid()
        parsed["interaction_mode"] = data["interaction_mode"]
    return parsed


def parse_request_only(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id"}))
    return {"request_id": _handle(data["request_id"])}


def parse_wait(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id"}), frozenset({"timeout_s"}))
    timeout = data.get("timeout_s", WAIT_DEFAULT_SECONDS)
    return {"request_id": _handle(data["request_id"]), "timeout_s": _bounded_int(timeout, 1, WAIT_MAX_SECONDS)}


def parse_followup(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "message"}))
    return {"request_id": _handle(data["request_id"]), "message": _text(data["message"])}


def parse_answer(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "input_id", "answers"}))
    answers = data["answers"]
    if not isinstance(answers, dict) or not answers:
        raise _invalid()
    for key, value in answers.items():
        _handle(key)
        if isinstance(value, str):
            continue
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            continue
        raise _invalid()
    if len(json.dumps(answers, separators=(",", ":")).encode("utf-8")) > MAX_ANSWERS_BYTES:
        raise _invalid()
    return {"request_id": _handle(data["request_id"]), "input_id": _handle(data["input_id"]), "answers": answers}


def parse_approve(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset({"request_id", "approval_id", "decision"}))
    if data["decision"] not in APPROVAL_DECISIONS:
        raise _invalid()
    return {
        "request_id": _handle(data["request_id"]),
        "approval_id": _handle(data["approval_id"]),
        "decision": data["decision"],
    }


def parse_recent(raw: object) -> dict[str, Any]:
    data = _object(raw, frozenset(), frozenset({"caller", "limit"}))
    caller = _caller(data["caller"]) if "caller" in data else None
    limit = _bounded_int(data.get("limit", RECENT_DEFAULT), 1, RECENT_MAX)
    return {"caller": caller, "limit": limit}


PARSERS: dict[str, Callable[[object], dict[str, Any]]] = {
    "t3_approve": parse_approve,
    "t3_answer": parse_answer,
    "t3_capacity": parse_capacity,
    "t3_followup": parse_followup,
    "t3_interrupt": parse_request_only,
    "t3_recent": parse_recent,
    "t3_status": parse_request_only,
    "t3_submit": parse_submit,
    "t3_wait": parse_wait,
}
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3 tests/test_grokbot_t3_contracts.py && git commit -m "feat(grokbot): t3-dispatch tool contracts"`

---

### Task 2: Runtime config

**Files:**
- Create: `src/brigade/grokbot_t3/runtime_config.py`
- Test: `tests/test_grokbot_t3_runtime.py`

- [x] Write the failing test:

```python
"""t3-dispatch owner-only runtime JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_t3 import contracts as c
from brigade.grokbot_t3 import runtime_config as rc


def runtime_payload(tmp_path: Path, **overrides):
    payload = {
        "schema": rc.RUNTIME_SCHEMA,
        "enabled": True,
        "t3_fleet_bin": "/usr/bin/t3-fleet",
        "git_bin": "/usr/bin/git",
        "repositories": {
            "brigade": {"remote": "https://github.com/escoffier-labs/brigade.git", "default_branch": "main"},
        },
        "hosts": ["host-a", "host-b", "host-c"],
        "seats": ["dsflash", "glmflash", "sol61", "luna6", "opus55", "sonnet55"],
        "default_seat": "dsflash",
        "fallback_seat": "glmflash",
        "fleet_enrolled": False,
        "max_open": 6,
        "daily_limit": 30,
        "prompt_max_bytes": 16384,
        "ledger_path": str(tmp_path / "state" / "ledger.sqlite3"),
        "wake_path": str(tmp_path / "state" / "wake.json"),
        "writable_paths": [],
    }
    payload.update(overrides)
    return payload


def write_runtime(tmp_path: Path, mode: int = 0o600, **overrides) -> Path:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(runtime_payload(tmp_path, **overrides)), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_loads_valid_runtime(tmp_path):
    runtime = rc.load_runtime(write_runtime(tmp_path))
    assert runtime.enabled is True
    assert runtime.default_seat == "dsflash"
    assert runtime.fallback_seat == "glmflash"
    assert runtime.hosts == ("host-a", "host-b", "host-c")
    assert runtime.repositories["brigade"].default_branch == "main"
    assert runtime.ledger_path == tmp_path / "state" / "ledger.sqlite3"


def test_rejects_group_readable_file(tmp_path):
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(write_runtime(tmp_path, mode=0o640))


def test_rejects_symlink(tmp_path):
    real = write_runtime(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(link)


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_seat": "nope"},
        {"fallback_seat": "dsflash"},
        {"fallback_seat": "missing"},
        {"max_open": 0},
        {"daily_limit": 201},
        {"prompt_max_bytes": 65536},
        {"hosts": []},
        {"seats": ["dsflash", "dsflash"]},
        {"t3_fleet_bin": "relative/t3-fleet"},
        {"repositories": {"brigade": {"remote": "git@github.com:x/y.git", "default_branch": "main"}}},
        {"repositories": {"brigade": {"remote": "https://github.com/x/y.git", "default_branch": "a/../b"}}},
        {"writable_paths": ["relative"]},
        {"extra": 1},
    ],
)
def test_rejects_invalid_runtime(tmp_path, overrides):
    with pytest.raises(c.T3DispatchError):
        rc.load_runtime(write_runtime(tmp_path, **overrides))


def test_fallback_seat_may_be_null(tmp_path):
    assert rc.load_runtime(write_runtime(tmp_path, fallback_seat=None)).fallback_seat is None


def test_validate_runtime_reference_returns_path(tmp_path):
    path = write_runtime(tmp_path)
    assert rc.validate_runtime_reference(str(path)) == str(path)
    with pytest.raises(c.T3DispatchError):
        rc.validate_runtime_reference("relative.json")
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_runtime.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/runtime_config.py`:

```python
"""Owner-only t3-dispatch runtime JSON."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import NAME_RE, REF_RE, T3DispatchError

RUNTIME_SCHEMA = "brigade.grokbot.t3-dispatch-runtime.v1"
RUNTIME_KEYS = frozenset(
    {
        "schema",
        "enabled",
        "t3_fleet_bin",
        "git_bin",
        "repositories",
        "hosts",
        "seats",
        "default_seat",
        "fallback_seat",
        "fleet_enrolled",
        "max_open",
        "daily_limit",
        "prompt_max_bytes",
        "ledger_path",
        "wake_path",
        "writable_paths",
    }
)
MAX_RUNTIME_BYTES = 65_536
MAX_REPOSITORIES = 50
MAX_WRITABLE_PATHS = 8
REMOTE_RE = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+\.git$")


@dataclass(frozen=True)
class Repository:
    remote: str
    default_branch: str


@dataclass(frozen=True)
class Runtime:
    enabled: bool
    t3_fleet_bin: str
    git_bin: str
    repositories: Mapping[str, Repository]
    hosts: tuple[str, ...]
    seats: tuple[str, ...]
    default_seat: str
    fallback_seat: str | None
    fleet_enrolled: bool
    max_open: int
    daily_limit: int
    prompt_max_bytes: int
    ledger_path: Path
    wake_path: Path
    writable_paths: tuple[str, ...]


def _bad() -> T3DispatchError:
    return T3DispatchError("invalid_request")


def read_owner_file(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular, owner-only, non-symlink file of bounded size."""
    if os.name != "posix":
        raise T3DispatchError("unavailable")
    if not path.is_absolute():
        raise _bad()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _bad() from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _bad()
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > max_bytes:
            raise _bad()
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise _bad()
    return data


def _abs(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _bad()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _bad()
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _bad()
    return value


def _int(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad()
    return value


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _bad()
    if any(not isinstance(item, str) or not NAME_RE.fullmatch(item) for item in value):
        raise _bad()
    if len(set(value)) != len(value):
        raise _bad()
    return tuple(value)


def _repositories(value: object) -> Mapping[str, Repository]:
    if not isinstance(value, dict) or not value or len(value) > MAX_REPOSITORIES:
        raise _bad()
    parsed: dict[str, Repository] = {}
    for repo_id, entry in value.items():
        if not isinstance(repo_id, str) or not NAME_RE.fullmatch(repo_id):
            raise _bad()
        if not isinstance(entry, dict) or set(entry) != {"remote", "default_branch"}:
            raise _bad()
        remote = entry["remote"]
        branch = entry["default_branch"]
        if not isinstance(remote, str) or not REMOTE_RE.fullmatch(remote):
            raise _bad()
        if not isinstance(branch, str) or not REF_RE.fullmatch(branch) or ".." in branch:
            raise _bad()
        parsed[repo_id] = Repository(remote=remote, default_branch=branch)
    return MappingProxyType(parsed)


def parse_runtime(payload: object) -> Runtime:
    if not isinstance(payload, dict) or set(payload) != RUNTIME_KEYS:
        raise _bad()
    if payload["schema"] != RUNTIME_SCHEMA:
        raise _bad()
    seats = _names(payload["seats"])
    default_seat = payload["default_seat"]
    if default_seat not in seats:
        raise _bad()
    fallback_seat = payload["fallback_seat"]
    if fallback_seat is not None and (fallback_seat not in seats or fallback_seat == default_seat):
        raise _bad()
    writable = payload["writable_paths"]
    if not isinstance(writable, list) or len(writable) > MAX_WRITABLE_PATHS:
        raise _bad()
    return Runtime(
        enabled=_bool(payload["enabled"]),
        t3_fleet_bin=_abs(payload["t3_fleet_bin"]),
        git_bin=_abs(payload["git_bin"]),
        repositories=_repositories(payload["repositories"]),
        hosts=_names(payload["hosts"]),
        seats=seats,
        default_seat=default_seat,
        fallback_seat=fallback_seat,
        fleet_enrolled=_bool(payload["fleet_enrolled"]),
        max_open=_int(payload["max_open"], 1, 20),
        daily_limit=_int(payload["daily_limit"], 1, 200),
        prompt_max_bytes=_int(payload["prompt_max_bytes"], 1024, 32768),
        ledger_path=Path(_abs(payload["ledger_path"])),
        wake_path=Path(_abs(payload["wake_path"])),
        writable_paths=tuple(_abs(item) for item in writable),
    )


def load_runtime(path: Path) -> Runtime:
    try:
        payload: Any = json.loads(read_owner_file(path, max_bytes=MAX_RUNTIME_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _bad() from exc
    return parse_runtime(payload)


def validate_runtime_reference(value: object) -> str:
    """Validate an instance-config `runtime_path` reference and return it unchanged."""
    reference = _abs(value)
    load_runtime(Path(reference))
    return reference
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3/runtime_config.py tests/test_grokbot_t3_runtime.py && git commit -m "feat(grokbot): t3-dispatch runtime config"`

---

### Task 3: t3-fleet and git boundary

**Files:**
- Create: `src/brigade/grokbot_t3/fleet.py`
- Test: `tests/test_grokbot_t3_fleet.py`

- [x] Write the failing test:

```python
"""t3-dispatch subprocess boundary: machine-mode stdin JSON, error envelopes, git rev resolution."""

from __future__ import annotations

import json
import subprocess

import pytest

from brigade.grokbot_t3 import fleet

SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRun:
    def __init__(self, stdout="", returncode=0, raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


def _ok(**fields):
    return json.dumps({"schema": "t3-fleet.result.v1", **fields})


def test_submit_sends_prompt_on_stdin_only():
    run = FakeRun(_ok(request_id="host-a-r1", state="dispatching", host="host-a"))
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    result = runner.submit(
        host="host-a", repository="brigade", source_revision=SHA, title="[grokbot:Builder] Fix",
        seat="dsflash", prompt="SECRET PROMPT TEXT", interaction_mode="default", override_reason=None,
    )
    assert result["request_id"] == "host-a-r1"
    argv, kwargs = run.calls[0]
    assert argv == ["/bin/t3-fleet", "submit"]
    assert "SECRET PROMPT TEXT" not in " ".join(argv)
    body = json.loads(kwargs["input"])
    assert body["schema"] == "t3-fleet.request.v1"
    assert body["action"] == "submit"
    assert body["prompt"] == "SECRET PROMPT TEXT"
    assert body["runtime_mode"] == "full-access"
    assert "override_reason" not in body
    assert kwargs["timeout"] == fleet.SUBMIT_TIMEOUT
    assert set(kwargs["env"]) <= set(fleet.ENV_KEYS)


def test_override_reason_is_sent_when_given():
    run = FakeRun(_ok(request_id="r1", state="queued"))
    fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).submit(
        host="auto", repository="brigade", source_revision=SHA, title="t", seat="dsflash",
        prompt="p", interaction_mode="plan", override_reason="why",
    )
    body = json.loads(run.calls[0][1]["input"])
    assert body["override_reason"] == "why"
    assert body["interaction_mode"] == "plan"


def test_error_envelope_raises_fleet_call_error():
    stdout = json.dumps(
        {
            "schema": "t3-fleet.error.v1",
            "request_id": "r9",
            "error": {"code": "policy_rejected", "reason": "model-admission-denied", "message": "m"},
        }
    )
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun(stdout, returncode=2))
    with pytest.raises(fleet.FleetCallError) as info:
        runner.status("r9")
    assert (info.value.code, info.value.reason, info.value.request_id) == ("policy_rejected", "model-admission-denied", "r9")


def test_garbage_output_is_internal_error():
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun("not json"))
    with pytest.raises(fleet.FleetCallError) as info:
        runner.capacity()
    assert info.value.code == "internal_error"


def test_timeout_raises_fleet_timeout():
    run = FakeRun(raises=subprocess.TimeoutExpired(["x"], 1))
    with pytest.raises(fleet.FleetTimeout):
        fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).status("r1")


def test_wait_passes_timeout_and_extends_subprocess_timeout():
    run = FakeRun(_ok(request_id="r1", state="running"))
    fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run).wait("r1", 45)
    argv, kwargs = run.calls[0]
    assert json.loads(kwargs["input"])["timeout"] == 45.0
    assert kwargs["timeout"] == 45 + fleet.CALL_TIMEOUT


def test_answer_approve_followup_interrupt_payloads():
    run = FakeRun(_ok(request_id="r1", state="running"))
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    runner.answer("r1", "q1", {"pick": "B"})
    runner.approve("r1", "a1", "accept")
    runner.followup("r1", "next step")
    runner.interrupt("r1")
    bodies = [json.loads(call[1]["input"]) for call in run.calls]
    assert bodies[0] == {"schema": "t3-fleet.request.v1", "action": "answer", "request_id": "r1", "input_id": "q1", "answers": {"pick": "B"}}
    assert bodies[1] == {"schema": "t3-fleet.request.v1", "action": "approve", "request_id": "r1", "approval_id": "a1", "decision": "accept"}
    assert bodies[2]["prompt"] == "next step" and bodies[2]["runtime_mode"] == "full-access"
    assert bodies[3] == {"schema": "t3-fleet.request.v1", "action": "interrupt", "request_id": "r1"}


def test_resolve_revision():
    run = FakeRun(f"{SHA}\trefs/heads/main\n")
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=run)
    assert runner.resolve_revision("https://github.com/x/y.git", "main") == SHA
    assert run.calls[0][0] == ["/usr/bin/git", "ls-remote", "--exit-code", "https://github.com/x/y.git", "refs/heads/main"]


@pytest.mark.parametrize("stdout,code", [("", 2), ("abc\trefs/heads/main\n", 0), (f"{SHA}\tx\n{SHA}\ty\n", 0)])
def test_resolve_revision_failures(stdout, code):
    runner = fleet.FleetRunner("/bin/t3-fleet", "/usr/bin/git", run=FakeRun(stdout, returncode=code))
    with pytest.raises(fleet.RevisionError):
        runner.resolve_revision("https://github.com/x/y.git", "main")
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_fleet.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/fleet.py`:

```python
"""The only t3-fleet and git subprocess boundary for t3-dispatch.

t3-fleet runs in machine mode: argv is the binary plus the action, and the
request is one JSON object on stdin. Prompts never appear on argv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable

REQUEST_SCHEMA = "t3-fleet.request.v1"
RESULT_SCHEMA = "t3-fleet.result.v1"
ERROR_SCHEMA = "t3-fleet.error.v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SUBMIT_TIMEOUT = 60
CALL_TIMEOUT = 30
GIT_TIMEOUT = 20
MAX_OUTPUT_BYTES = 1_048_576
ENV_KEYS = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "USER",
    "LOGNAME",
    "SSH_AUTH_SOCK",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "T3_FLEET_STATE",
)

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class FleetCallError(Exception):
    def __init__(self, code: str, reason: str | None = None, request_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.request_id = request_id


class FleetTimeout(Exception):
    pass


class RevisionError(Exception):
    pass


def child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ENV_KEYS if key in os.environ}


class FleetRunner:
    def __init__(self, t3_fleet_bin: str, git_bin: str, *, run: Run = subprocess.run):
        self.t3_fleet_bin = t3_fleet_bin
        self.git_bin = git_bin
        self._run = run

    def call(self, action: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps({"schema": REQUEST_SCHEMA, "action": action, **payload}, separators=(",", ":"))
        try:
            proc = self._run(
                [self.t3_fleet_bin, action],
                input=body,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FleetTimeout(action) from exc
        except OSError as exc:
            raise FleetCallError("internal_error") from exc
        output = proc.stdout or proc.stderr or ""
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise FleetCallError("internal_error")
        try:
            result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise FleetCallError("internal_error") from exc
        if not isinstance(result, dict):
            raise FleetCallError("internal_error")
        if result.get("schema") == ERROR_SCHEMA:
            raw_error = result.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            raw_code = error.get("code")
            raw_reason = error.get("reason")
            request_id = result.get("request_id") or error.get("request_id")
            raise FleetCallError(
                raw_code if isinstance(raw_code, str) else "internal_error",
                raw_reason if isinstance(raw_reason, str) else None,
                request_id if isinstance(request_id, str) else None,
            )
        if result.get("schema") != RESULT_SCHEMA or proc.returncode != 0:
            raise FleetCallError("internal_error")
        return result

    def capacity(self) -> dict[str, Any]:
        return self.call("capacity", {}, timeout=CALL_TIMEOUT)

    def submit(
        self,
        *,
        host: str,
        repository: str,
        source_revision: str,
        title: str,
        seat: str,
        prompt: str,
        interaction_mode: str,
        override_reason: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host": host,
            "repository": repository,
            "source_revision": source_revision,
            "title": title,
            "seat": seat,
            "prompt": prompt,
            "runtime_mode": "full-access",
            "interaction_mode": interaction_mode,
        }
        if override_reason is not None:
            payload["override_reason"] = override_reason
        return self.call("submit", payload, timeout=SUBMIT_TIMEOUT)

    def status(self, request_id: str) -> dict[str, Any]:
        return self.call("status", {"request_id": request_id}, timeout=CALL_TIMEOUT)

    def wait(self, request_id: str, timeout_s: int) -> dict[str, Any]:
        return self.call("wait", {"request_id": request_id, "timeout": float(timeout_s)}, timeout=timeout_s + CALL_TIMEOUT)

    def followup(self, request_id: str, prompt: str) -> dict[str, Any]:
        payload = {"request_id": request_id, "prompt": prompt, "runtime_mode": "full-access", "interaction_mode": "default"}
        return self.call("followup", payload, timeout=SUBMIT_TIMEOUT)

    def answer(self, request_id: str, input_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        payload = {"request_id": request_id, "input_id": input_id, "answers": answers}
        return self.call("answer", payload, timeout=CALL_TIMEOUT)

    def approve(self, request_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        payload = {"request_id": request_id, "approval_id": approval_id, "decision": decision}
        return self.call("approve", payload, timeout=CALL_TIMEOUT)

    def interrupt(self, request_id: str) -> dict[str, Any]:
        return self.call("interrupt", {"request_id": request_id}, timeout=CALL_TIMEOUT)

    def resolve_revision(self, remote: str, branch: str) -> str:
        try:
            proc = self._run(
                [self.git_bin, "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                env=child_env(),
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RevisionError(branch) from exc
        if proc.returncode != 0:
            raise RevisionError(branch)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if len(lines) != 1:
            raise RevisionError(branch)
        sha = lines[0].split("\t", 1)[0].strip()
        if not SHA_RE.fullmatch(sha):
            raise RevisionError(branch)
        return sha
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3/fleet.py tests/test_grokbot_t3_fleet.py && git commit -m "feat(grokbot): t3-dispatch t3-fleet boundary"`

---

### Task 4: Ledger

**Files:**
- Create: `src/brigade/grokbot_t3/ledger.py`
- Test: `tests/test_grokbot_t3_ledger.py`

- [x] Write the failing test:

```python
"""t3-dispatch SQLite ledger."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

from brigade.grokbot_t3.ledger import Ledger

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _record(ledger, request_id, *, caller="Builder", state="dispatching", at=T0):
    ledger.record_submit(
        request_id=request_id, caller=caller, repository="brigade", host="host-a", seat="dsflash",
        title="[grokbot:Builder] t", prompt_sha256="a" * 64, state=state, now=at,
    )


def test_file_is_owner_only_and_round_trips(tmp_path):
    path = tmp_path / "state" / "ledger.sqlite3"
    ledger = Ledger(path)
    _record(ledger, "r1")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    row = ledger.get("r1")
    assert row["caller"] == "Builder" and row["last_state"] == "dispatching" and row["notified_state"] is None
    assert ledger.get("missing") is None


def test_open_count_and_daily_count(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1")
    _record(ledger, "r2", state="succeeded")
    _record(ledger, "r3", at=T0 - timedelta(days=1))
    assert ledger.open_count() == 2
    assert ledger.submits_since(T0.replace(hour=0)) == 2


def test_state_change_clears_notification(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1", state="needs_attention")
    ledger.mark_notified("r1", "needs_attention", T0)
    assert ledger.get("r1")["notified_state"] == "needs_attention"
    ledger.update_state("r1", "needs_attention", T0)
    assert ledger.get("r1")["notified_state"] == "needs_attention"
    ledger.update_state("r1", "running", T0)
    assert ledger.get("r1")["notified_state"] is None


def test_pending_notifications(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "running", state="running")
    _record(ledger, "done-unnotified", state="succeeded")
    _record(ledger, "done-notified", state="failed")
    ledger.mark_notified("done-notified", "failed", T0)
    ids = [row["request_id"] for row in ledger.pending_notifications()]
    assert ids == ["running", "done-unnotified"]


def test_recent_projection(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    _record(ledger, "r1", caller="Builder", at=T0)
    _record(ledger, "r2", caller="Other", at=T0 + timedelta(minutes=1))
    rows = ledger.recent(None, 10)
    assert [row["request_id"] for row in rows] == ["r2", "r1"]
    assert set(rows[0]) == {"request_id", "caller", "repository", "host", "seat", "title", "state", "created_at", "updated_at"}
    assert [row["request_id"] for row in ledger.recent("Builder", 10)] == ["r1"]
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_ledger.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/ledger.py`:

```python
"""Pack-owned SQLite ledger of dispatched requests. Prompts are stored only as SHA-256."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import TERMINAL_STATES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    repository TEXT NOT NULL,
    host TEXT NOT NULL,
    seat TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_state TEXT NOT NULL,
    notified_state TEXT,
    notified_at TEXT
)
"""
_TERMINAL = tuple(sorted(TERMINAL_STATES))
_PUBLIC = "request_id, caller, repository, host, seat, title, last_state AS state, created_at, updated_at"


def _ts(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def record_submit(
        self,
        *,
        request_id: str,
        caller: str,
        repository: str,
        host: str,
        seat: str,
        title: str,
        prompt_sha256: str,
        state: str,
        now: datetime,
    ) -> None:
        ts = _ts(now)
        self._conn.execute(
            "INSERT INTO requests (request_id, caller, repository, host, seat, title, prompt_sha256, "
            "created_at, updated_at, last_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, caller, repository, host, seat, title, prompt_sha256, ts, ts, state),
        )

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_state(self, request_id: str, state: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE requests SET notified_state = CASE WHEN last_state = ? THEN notified_state ELSE NULL END, "
            "last_state = ?, updated_at = ? WHERE request_id = ?",
            (state, state, _ts(now), request_id),
        )

    def mark_notified(self, request_id: str, state: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE requests SET notified_state = ?, notified_at = ? WHERE request_id = ?",
            (state, _ts(now), request_id),
        )

    def open_count(self) -> int:
        placeholders = ", ".join("?" for _ in _TERMINAL)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE last_state NOT IN ({placeholders})", _TERMINAL
        ).fetchone()
        return int(row[0])

    def submits_since(self, since: datetime) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM requests WHERE created_at >= ?", (_ts(since),)).fetchone()
        return int(row[0])

    def recent(self, caller: str | None, limit: int) -> list[dict[str, Any]]:
        if caller is None:
            rows = self._conn.execute(
                f"SELECT {_PUBLIC} FROM requests ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_PUBLIC} FROM requests WHERE caller = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (caller, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_notifications(self) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in _TERMINAL)
        rows = self._conn.execute(
            f"SELECT * FROM requests WHERE last_state NOT IN ({placeholders}) "
            "OR notified_state IS NULL OR notified_state != last_state ORDER BY created_at, rowid",
            _TERMINAL,
        ).fetchall()
        return [dict(row) for row in rows]
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3/ledger.py tests/test_grokbot_t3_ledger.py && git commit -m "feat(grokbot): t3-dispatch request ledger"`

---

### Task 5: Tool handlers

**Files:**
- Create: `src/brigade/grokbot_t3/tools.py`
- Test: `tests/test_grokbot_t3_tools.py`

- [x] Write the failing test:

```python
"""t3-dispatch tool handlers against a fake runner."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from brigade.grokbot_t3 import runtime_config as rc
from brigade.grokbot_t3.fleet import FleetCallError, FleetTimeout, RevisionError
from brigade.grokbot_t3.ledger import Ledger
from brigade.grokbot_t3.tools import DISPATCH_HEADER, T3DispatchTools

from tests.test_grokbot_t3_runtime import runtime_payload

SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(self):
        self.capacity_result = {
            "hosts": [
                {"host": "host-a", "state": "eligible", "active": 10, "limit": 16},
                {"host": "host-b", "state": "eligible", "active": 1, "limit": 12},
                {"host": "host-c", "state": "blocked_offline", "active": 0, "limit": 12},
                {"host": "elsewhere", "state": "eligible", "active": 0, "limit": 99},
            ]
        }
        self.submits = []
        self.submit_errors = []
        self.status_result = {
            "request_id": "r1",
            "host": "host-b",
            "state": "running",
            "needs_attention": False,
            "thread_id": "t1",
            "admission": {"seat": "dsflash", "model": "x"},
            "extra": "dropped",
        }
        self.revision_error = False

    def capacity(self):
        return self.capacity_result

    def resolve_revision(self, remote, branch):
        if self.revision_error:
            raise RevisionError(branch)
        self.last_branch = branch
        return SHA

    def submit(self, **kwargs):
        self.submits.append(kwargs)
        if self.submit_errors:
            raise self.submit_errors.pop(0)
        return {"request_id": f"r{len(self.submits)}", "host": kwargs["host"], "state": "dispatching"}

    def status(self, request_id):
        return dict(self.status_result, request_id=request_id)

    def wait(self, request_id, timeout_s):
        self.waited = timeout_s
        return dict(self.status_result, request_id=request_id)

    def followup(self, request_id, prompt):
        self.followed = prompt
        return dict(self.status_result, request_id=request_id)

    def answer(self, request_id, input_id, answers):
        return dict(self.status_result, request_id=request_id)

    def approve(self, request_id, approval_id, decision):
        return dict(self.status_result, request_id=request_id)

    def interrupt(self, request_id):
        return dict(self.status_result, request_id=request_id, state="interrupted")


@pytest.fixture
def make(tmp_path):
    def _make(**overrides):
        runtime = rc.parse_runtime(runtime_payload(tmp_path, **overrides))
        runner = FakeRunner()
        ledger = Ledger(runtime.ledger_path)
        return T3DispatchTools(runtime=runtime, runner=runner, ledger=ledger, now=lambda: NOW), runner, ledger

    return _make


def _submit(tools, **overrides):
    args = {"repository": "brigade", "prompt": "Do the thing.", "title": "Thing", "caller": "Builder"}
    args.update(overrides)
    return tools.call_tool("t3_submit", args)


def test_submit_picks_host_default_seat_and_records(make):
    tools, runner, ledger = make()
    result = _submit(tools)
    assert result == {
        "request_id": "r1",
        "host": "host-b",
        "seat": "dsflash",
        "state": "dispatching",
        "fallback_used": False,
    }
    sent = runner.submits[0]
    assert sent["host"] == "host-b"
    assert sent["seat"] == "dsflash"
    assert sent["source_revision"] == SHA
    assert sent["title"] == "[grokbot:Builder] Thing"
    assert sent["override_reason"] is None
    assert sent["prompt"] == DISPATCH_HEADER.format(caller="Builder") + "Do the thing."
    row = ledger.get("r1")
    assert row["prompt_sha256"] == hashlib.sha256(sent["prompt"].encode("utf-8")).hexdigest()
    assert "Do the thing." not in str(row)
    assert runner.last_branch == "main"


def test_explicit_host_and_base_ref(make):
    tools, runner, _ = make()
    _submit(tools, host="host-c", base_ref="release/1")
    assert runner.submits[0]["host"] == "host-c"
    assert runner.last_branch == "release/1"


def test_enrolled_passes_auto_and_override_reason(make):
    tools, runner, _ = make(fleet_enrolled=True)
    _submit(tools)
    assert runner.submits[0]["host"] == "auto"
    assert runner.submits[0]["override_reason"] == "grokbot t3-dispatch seat dsflash for Builder"


def test_fallback_seat_on_admission_denied(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    result = _submit(tools)
    assert result["seat"] == "glmflash" and result["fallback_used"] is True
    assert [s["seat"] for s in runner.submits] == ["dsflash", "glmflash"]


def test_named_seat_never_falls_back(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetCallError("policy_rejected", "model-admission-denied")]
    out = _submit(tools, seat="dsflash")
    assert out["error"]["code"] == "fleet-error"
    assert out["error"]["fleet_reason"] == "model-admission-denied"
    assert len(runner.submits) == 1


@pytest.mark.parametrize(
    "overrides,runtime_overrides,code",
    [
        ({"repository": "other"}, {}, "repository-not-allowed"),
        ({"host": "elsewhere"}, {}, "host-not-allowed"),
        ({"seat": "muse13"}, {}, "seat-not-allowed"),
        ({"prompt": "x" * 2000}, {"prompt_max_bytes": 1024}, "prompt-too-large"),
        ({}, {"enabled": False}, "dispatch-disabled"),
    ],
)
def test_submit_refusals(make, overrides, runtime_overrides, code):
    tools, runner, _ = make(**runtime_overrides)
    out = _submit(tools, **overrides)
    assert out["error"]["code"] == code
    assert runner.submits == []


def test_open_and_daily_limits(make):
    tools, runner, _ = make(max_open=1)
    _submit(tools)
    assert _submit(tools)["error"]["code"] == "open-limit"
    tools, runner, ledger = make(daily_limit=1, max_open=5)
    ledger.update_state("r1", "succeeded", NOW)
    assert _submit(tools)["error"]["code"] == "daily-limit"


def test_no_eligible_host_and_unresolved_ref(make):
    tools, runner, _ = make()
    runner.capacity_result = {"hosts": [{"host": "host-a", "state": "eligible", "active": 16, "limit": 16}]}
    assert _submit(tools)["error"]["code"] == "no-eligible-host"
    tools, runner, _ = make()
    runner.revision_error = True
    assert _submit(tools)["error"]["code"] == "base-ref-unresolved"


def test_submit_timeout_is_unknown(make):
    tools, runner, _ = make()
    runner.submit_errors = [FleetTimeout("submit")]
    assert _submit(tools)["error"]["code"] == "submit-unknown"


def test_status_requires_ledger_entry_and_projects(make):
    tools, runner, ledger = make()
    assert tools.call_tool("t3_status", {"request_id": "nope"})["error"]["code"] == "unknown-request"
    _submit(tools)
    out = tools.call_tool("t3_status", {"request_id": "r1"})
    assert out == {
        "request_id": "r1",
        "host": "host-b",
        "state": "running",
        "needs_attention": False,
        "thread_id": "t1",
        "seat": "dsflash",
    }
    assert ledger.get("r1")["last_state"] == "running"


def test_followup_answer_approve_interrupt_wait_recent(make):
    tools, runner, ledger = make()
    _submit(tools)
    assert tools.call_tool("t3_followup", {"request_id": "r1", "message": "more"})["state"] == "running"
    assert runner.followed == "more"
    assert (
        tools.call_tool("t3_answer", {"request_id": "r1", "input_id": "q", "answers": {"a": "b"}})["state"] == "running"
    )
    assert (
        tools.call_tool("t3_approve", {"request_id": "r1", "approval_id": "a", "decision": "accept"})["state"]
        == "running"
    )
    assert tools.call_tool("t3_wait", {"request_id": "r1", "timeout_s": 5})["state"] == "running"
    assert runner.waited == 5
    assert tools.call_tool("t3_interrupt", {"request_id": "r1"})["state"] == "interrupted"
    assert ledger.get("r1")["last_state"] == "interrupted"
    recent = tools.call_tool("t3_recent", {"caller": "Builder"})
    assert [row["request_id"] for row in recent["requests"]] == ["r1"]


def test_capacity_filters_to_allowlist(make):
    tools, _, _ = make()
    hosts = tools.call_tool("t3_capacity", {})["hosts"]
    assert [h["host"] for h in hosts] == ["host-a", "host-b", "host-c"]


def test_unknown_tool_and_bad_args(make):
    tools, _, _ = make()
    assert tools.call_tool("t3_nope", {})["error"]["code"] == "invalid_request"
    assert tools.call_tool("t3_status", {"request_id": "bad id"})["error"]["code"] == "invalid_request"
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_tools.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/tools.py`. `call_tool` returns the public error dict instead of raising, so the adapter stays thin:

```python
"""t3-dispatch tool handlers: allowlists, limits, host pick, fallback seat, projection."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import contracts as c
from .fleet import FleetCallError, FleetTimeout, RevisionError
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

    def _record_state(self, request_id: str, result: dict[str, Any]) -> None:
        state = result.get("state")
        if isinstance(state, str):
            self.ledger.update_state(request_id, state, self.now())

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
        if self.runtime.fleet_enrolled:
            return c.HOST_AUTO
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

    def submit(self, args: dict[str, Any]) -> dict[str, Any]:
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
        if self.ledger.open_count() >= runtime.max_open:
            raise c.T3DispatchError("open-limit")
        now = self.now()
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.ledger.submits_since(midnight) >= runtime.daily_limit:
            raise c.T3DispatchError("daily-limit")
        try:
            revision = self.runner.resolve_revision(repo.remote, args["base_ref"] or repo.default_branch)
        except RevisionError as exc:
            raise c.T3DispatchError("base-ref-unresolved") from exc
        host = self._choose_host(args["host"])
        title = f"[grokbot:{args['caller']}] {args['title']}"
        prompt = compose_prompt(args["caller"], args["prompt"])
        fallback_used = False
        try:
            try:
                result = self._submit_once(
                    host=host, args=args, revision=revision, title=title, seat=seat, prompt=prompt
                )
            except FleetCallError as exc:
                can_fall_back = (
                    exc.code == FALLBACK_CODE
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
            raise _fleet_error(exc) from exc
        except FleetTimeout as exc:
            raise c.T3DispatchError("submit-unknown") from exc
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not c.HANDLE_RE.fullmatch(request_id):
            raise c.T3DispatchError("submit-unknown")
        raw_state = result.get("state")
        state = raw_state if isinstance(raw_state, str) else "unknown"
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
        )
        return {
            "request_id": request_id,
            "host": str(result.get("host") or host),
            "seat": seat,
            "state": state,
            "fallback_used": fallback_used,
        }

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        self._known(args["request_id"])
        result = self._fleet(self.runner.status, args["request_id"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def wait(self, args: dict[str, Any]) -> dict[str, Any]:
        self._known(args["request_id"])
        result = self._fleet(self.runner.wait, args["request_id"], args["timeout_s"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def followup(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        self._known(args["request_id"])
        if len(args["message"].encode("utf-8")) > self.runtime.prompt_max_bytes:
            raise c.T3DispatchError("prompt-too-large")
        result = self._fleet(self.runner.followup, args["request_id"], args["message"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def answer(self, args: dict[str, Any]) -> dict[str, Any]:
        self._known(args["request_id"])
        result = self._fleet(self.runner.answer, args["request_id"], args["input_id"], args["answers"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def approve(self, args: dict[str, Any]) -> dict[str, Any]:
        self._known(args["request_id"])
        result = self._fleet(self.runner.approve, args["request_id"], args["approval_id"], args["decision"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def interrupt(self, args: dict[str, Any]) -> dict[str, Any]:
        self._known(args["request_id"])
        result = self._fleet(self.runner.interrupt, args["request_id"])
        self._record_state(args["request_id"], result)
        return project_status(result)

    def recent(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"requests": self.ledger.recent(args["caller"], args["limit"])}
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3/tools.py tests/test_grokbot_t3_tools.py && git commit -m "feat(grokbot): t3-dispatch tool handlers"`

---

### Task 6: Wake watcher

**Files:**
- Create: `src/brigade/grokbot_t3/watch.py`
- Test: `tests/test_grokbot_t3_watch.py`

- [x] Write the failing test:

```python
"""t3-dispatch wake watcher."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from brigade.grokbot_t3 import contracts as c
from brigade.grokbot_t3 import watch
from brigade.grokbot_t3.fleet import FleetTimeout
from brigade.grokbot_t3.ledger import Ledger

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _owner_file(path, text):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def setup(tmp_path):
    key = _owner_file(tmp_path / "builder.key", "wake-key-value\n")
    wake = _owner_file(
        tmp_path / "wake.json",
        json.dumps({"schema": watch.WAKE_SCHEMA, "targets": {
            "Builder": [{"url": "https://api2.cursor.sh/automations/webhook/abc", "key_file": str(key)}],
            "default": [{"url": "https://api2.cursor.sh/automations/webhook/def", "key_file": str(key)}],
        }}),
    )
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    return tmp_path, watch.load_wake_config(wake), ledger


class Runner:
    def __init__(self, states):
        self.states = states

    def status(self, request_id):
        value = self.states[request_id]
        if isinstance(value, Exception):
            raise value
        return {"schema": "t3-fleet.result.v1", "request_id": request_id, "state": value}


class Post:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, url, key, body):
        self.calls.append((url, key, body))
        return self.codes.pop(0) if self.codes else 200


def _record(ledger, request_id, caller="Builder", state="running"):
    ledger.record_submit(request_id=request_id, caller=caller, repository="brigade", host="host-a",
                         seat="dsflash", title="[grokbot:Builder] t", prompt_sha256="a" * 64, state=state, now=NOW)


def test_wakes_on_terminal_state_once(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([200])
    summary = watch.run_watch_once(runner=Runner({"r1": "succeeded"}), ledger=ledger, wake_targets=targets,
                                   log_path=tmp_path / "wake-notify.jsonl", now=lambda: NOW, post=post)
    assert summary == {"checked": 1, "woken": 1, "failed": 0}
    url, key, body = post.calls[0]
    assert url.endswith("/abc") and key == "wake-key-value"
    assert body == {"request_id": "r1", "state": "succeeded", "repository": "brigade", "title": "[grokbot:Builder] t"}
    again = watch.run_watch_once(runner=Runner({"r1": "succeeded"}), ledger=ledger, wake_targets=targets,
                                 log_path=tmp_path / "wake-notify.jsonl", now=lambda: NOW, post=post)
    assert again == {"checked": 0, "woken": 0, "failed": 0}
    log = (tmp_path / "wake-notify.jsonl").read_text().splitlines()
    assert json.loads(log[0])["statuses"] == [200]


def test_no_wake_while_running(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([])
    summary = watch.run_watch_once(runner=Runner({"r1": "running"}), ledger=ledger, wake_targets=targets,
                                   log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post)
    assert summary == {"checked": 1, "woken": 0, "failed": 0}
    assert post.calls == []


def test_retry_after_non_200(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([500, 200])
    kwargs = dict(runner=Runner({"r1": "failed"}), ledger=ledger, wake_targets=targets,
                  log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post)
    assert watch.run_watch_once(**kwargs)["failed"] == 1
    assert watch.run_watch_once(**kwargs)["woken"] == 1
    assert ledger.get("r1")["notified_state"] == "failed"


def test_second_question_wakes_again(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1")
    post = Post([])
    states = {"r1": "needs_attention"}
    kwargs = dict(runner=Runner(states), ledger=ledger, wake_targets=targets,
                  log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post)
    watch.run_watch_once(**kwargs)
    states["r1"] = "running"
    watch.run_watch_once(**kwargs)
    states["r1"] = "needs_attention"
    watch.run_watch_once(**kwargs)
    assert [call[2]["state"] for call in post.calls] == ["needs_attention", "needs_attention"]


def test_unknown_caller_uses_default_and_status_failure_counts(setup):
    tmp_path, targets, ledger = setup
    _record(ledger, "r1", caller="Stranger")
    _record(ledger, "r2")
    post = Post([])
    summary = watch.run_watch_once(runner=Runner({"r1": "succeeded", "r2": FleetTimeout("status")}), ledger=ledger,
                                   wake_targets=targets, log_path=tmp_path / "w.jsonl", now=lambda: NOW, post=post)
    assert post.calls[0][0].endswith("/def")
    assert summary == {"checked": 2, "woken": 1, "failed": 1}


@pytest.mark.parametrize(
    "targets",
    [
        {"Builder": [{"url": "http://insecure.example/x", "key_file": "/k"}]},
        {"Builder": [{"url": "https://ok.example/x", "key_file": "relative"}]},
        {"bad name!": [{"url": "https://ok.example/x", "key_file": "/k"}]},
        {"Builder": []},
    ],
)
def test_rejects_bad_wake_config(tmp_path, targets):
    path = _owner_file(tmp_path / "wake.json", json.dumps({"schema": watch.WAKE_SCHEMA, "targets": targets}))
    with pytest.raises(c.T3DispatchError):
        watch.load_wake_config(path)
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_watch.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/watch.py`:

```python
"""One-shot wake watcher: poll open requests and POST a wake to the calling bot's routine."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import contracts as c
from .fleet import FleetCallError, FleetTimeout
from .ledger import Ledger
from .runtime_config import read_owner_file

WAKE_SCHEMA = "brigade.grokbot.t3-dispatch-wake.v1"
WAKE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9._~/%-]*$")
MAX_WAKE_BYTES = 16_384
MAX_KEY_BYTES = 1_024
MAX_TARGETS = 4
POST_TIMEOUT = 8
Post = Callable[[str, str, dict[str, Any]], int]


def _bad() -> c.T3DispatchError:
    return c.T3DispatchError("invalid_request")


def load_wake_config(path: Path) -> dict[str, list[dict[str, str]]]:
    try:
        payload = json.loads(read_owner_file(path, max_bytes=MAX_WAKE_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _bad() from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "targets"} or payload["schema"] != WAKE_SCHEMA:
        raise _bad()
    targets = payload["targets"]
    if not isinstance(targets, dict) or not targets:
        raise _bad()
    parsed: dict[str, list[dict[str, str]]] = {}
    for name, entries in targets.items():
        if not isinstance(name, str) or (name != "default" and not c.CALLER_RE.fullmatch(name)):
            raise _bad()
        if not isinstance(entries, list) or not entries or len(entries) > MAX_TARGETS:
            raise _bad()
        parsed_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"url", "key_file"}:
                raise _bad()
            url, key_file = entry["url"], entry["key_file"]
            if not isinstance(url, str) or not WAKE_URL_RE.fullmatch(url):
                raise _bad()
            if not isinstance(key_file, str) or not Path(key_file).is_absolute() or "\x00" in key_file:
                raise _bad()
            parsed_entries.append({"url": url, "key_file": key_file})
        parsed[name] = parsed_entries
    return parsed


def read_key(key_file: str) -> str:
    try:
        value = read_owner_file(Path(key_file), max_bytes=MAX_KEY_BYTES).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise _bad() from exc
    if not value or "\n" in value or "\r" in value:
        raise _bad()
    return value


def post_wake(url: str, key: str, body: dict[str, Any]) -> int:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "X-Automation-Key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _append_log(path: Path, record: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def run_watch_once(
    *,
    runner: Any,
    ledger: Ledger,
    wake_targets: dict[str, list[dict[str, str]]],
    log_path: Path,
    now: Callable[[], datetime] | None = None,
    post: Post = post_wake,
) -> dict[str, int]:
    clock = now or (lambda: datetime.now(timezone.utc))
    summary = {"checked": 0, "woken": 0, "failed": 0}
    for row in ledger.pending_notifications():
        state = row["last_state"]
        notified = row["notified_state"]
        if state not in c.TERMINAL_STATES:
            summary["checked"] += 1
            try:
                result = runner.status(row["request_id"])
            except (FleetCallError, FleetTimeout):
                summary["failed"] += 1
                continue
            new_state = result.get("state") if isinstance(result.get("state"), str) else "unknown"
            if new_state != state:
                ledger.update_state(row["request_id"], new_state, clock())
                state = new_state
                notified = None
        if state not in c.NOTIFY_STATES or notified == state:
            continue
        targets = wake_targets.get(row["caller"]) or wake_targets.get("default") or []
        body = {"request_id": row["request_id"], "state": state, "repository": row["repository"], "title": row["title"]}
        statuses: list[int] = []
        for target in targets:
            try:
                key = read_key(target["key_file"])
            except c.T3DispatchError:
                statuses.append(0)
                continue
            statuses.append(post(target["url"], key, body))
        _append_log(
            log_path,
            {"at": clock().isoformat(timespec="seconds"), "request_id": row["request_id"], "state": state, "statuses": statuses},
        )
        if statuses and all(code == 200 for code in statuses):
            ledger.mark_notified(row["request_id"], state, clock())
            summary["woken"] += 1
        else:
            summary["failed"] += 1
    return summary
```

- [x] Run to green: same command. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3/watch.py tests/test_grokbot_t3_watch.py && git commit -m "feat(grokbot): t3-dispatch wake watcher"`

---

### Task 7: Listener lifecycle, package exports, module entry point

**Files:**
- Create: `src/brigade/grokbot_t3/lifecycle.py`
- Create: `src/brigade/grokbot_t3/__main__.py`
- Modify: `src/brigade/grokbot_t3/__init__.py`
- Test: `tests/test_grokbot_t3_lifecycle.py`

- [x] Write the failing test:

```python
# content-guard: allow bearer-token file
"""t3-dispatch listener gate, early tool rejection, and unit rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from brigade.grokbot_t3 import lifecycle

from tests.test_grokbot_t3_runtime import write_runtime

SECRET = "not-a-real-token-value-32chars!!"


def _config(**overrides):
    base = dict(target=Path("/tmp/x"), bind_host="127.0.0.1", bind_port=8778,
                allowed_hosts=("t3-dispatch.brigade.tools",), allowed_origins=(), bearer=SECRET, runtime_path="/r.json")
    base.update(overrides)
    return lifecycle.T3DispatchListenerConfig(**base)


def test_gate_requires_bearer_and_allowed_host():
    gate = lifecycle._T3RequestGate(_config())
    ok = {"host": "t3-dispatch.brigade.tools", "authorization": f"Bearer {SECRET}"}
    assert gate.reject_reason(ok, 0) is None
    assert gate.reject_reason({"host": "127.0.0.1"}, 0) == "unauthorized"
    assert gate.reject_reason({**ok, "host": "evil.example"}, 0) == "forbidden"
    assert gate.reject_reason({**ok, "origin": "https://evil.example"}, 0) == "forbidden"
    assert gate.reject_reason({**ok, "authorization": "Bearer wrong"}, 0) == "unauthorized"
    assert gate.reject_reason(ok, lifecycle.MAX_REQUEST_BYTES + 1) == "too-large"


def test_repr_hides_bearer():
    assert SECRET not in repr(_config())


@pytest.mark.parametrize(
    "body,invalid",
    [
        ({"method": "tools/list"}, False),
        ({"method": "tools/call", "params": {"name": "t3_capacity", "arguments": {}}}, False),
        ({"method": "tools/call", "params": {"name": "t3_status", "arguments": {"request_id": "r1"}}}, False),
        ({"method": "tools/call", "params": {"name": "t3_shell", "arguments": {}}}, True),
        ({"method": "tools/call", "params": {"name": "t3_status", "arguments": {"request_id": "r1", "x": 1}}}, True),
        ({"method": "tools/call", "params": "nope"}, True),
    ],
)
def test_invalid_tool_request(body, invalid):
    assert lifecycle._invalid_tool_request(json.dumps(body).encode()) is invalid


def test_render_units(tmp_path, monkeypatch):
    runtime = write_runtime(tmp_path, writable_paths=["/srv/t3/state/t3-fleet"])
    monkeypatch.setattr(lifecycle, "_load_validated_instance", lambda target: {
        "bind": "127.0.0.1:8778", "allowed_hosts": ["t3-dispatch.brigade.tools"], "allowed_origins": [],
        "bearer": {"kind": "file", "path": "/srv/t3/config/origin.bearer"}, "runtime_path": str(runtime),
    })
    unit = lifecycle.render_unit(tmp_path, python="/venv/bin/python")
    assert "--pack t3-dispatch" in unit
    assert "--allow-host t3-dispatch.brigade.tools" in unit
    assert "--bearer-file /srv/t3/config/origin.bearer" in unit
    assert f"ReadWritePaths={tmp_path / 'state'} /srv/t3/state/t3-fleet" in unit
    service, timer = lifecycle.render_watch_units(tmp_path, python="/venv/bin/python")
    assert "Type=oneshot" in service
    assert f"ExecStart=/venv/bin/python -m brigade.grokbot_t3 watch --target {tmp_path}" in service
    assert "OnUnitActiveSec=1min" in timer and f"Unit={lifecycle.WATCH_SERVICE}" in timer


def test_write_watch_units_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "render_watch_units", lambda target, python=None: ("svc\n", "tmr\n"))
    out = tmp_path / "units"
    out.mkdir()
    paths = lifecycle.write_watch_units(tmp_path, out)
    assert [p.name for p in paths] == [lifecycle.WATCH_SERVICE, lifecycle.WATCH_TIMER]
    assert lifecycle.write_watch_units(tmp_path, out) == paths
    monkeypatch.setattr(lifecycle, "render_watch_units", lambda target, python=None: ("svc2\n", "tmr\n"))
    from brigade import grokbot_ops
    with pytest.raises(grokbot_ops.ServiceRenderError):
        lifecycle.write_watch_units(tmp_path, out)
    assert lifecycle.write_watch_units(tmp_path, out, force=True) == paths
```

- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_lifecycle.py"`. Expect `ImportError`.
- [x] Implement `src/brigade/grokbot_t3/lifecycle.py`:

```python
"""t3-dispatch listener: config, request gate, ASGI app, doctor, canary, unit rendering."""

from __future__ import annotations

import hmac
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import grokbot_mcp, grokbot_ops
from . import contracts as c
from .fleet import FleetCallError, FleetRunner, FleetTimeout
from .ledger import Ledger
from .runtime_config import Runtime, load_runtime, validate_runtime_reference
from .tools import T3DispatchTools
from .watch import load_wake_config, run_watch_once

MAX_REQUEST_BYTES = c.MAX_REQUEST_BYTES
WATCH_SERVICE = "brigade-grokbot-t3-watch.service"
WATCH_TIMER = "brigade-grokbot-t3-watch.timer"
_TOOL_DESCRIPTIONS = {
    "t3_capacity": "Read per-host T3 fleet capacity for allowlisted hosts",
    "t3_submit": "Launch a T3 thread: repository, prompt, title, caller; optional host, seat, base_ref, interaction_mode",
    "t3_status": "Read the current state of a thread launched through this connector",
    "t3_wait": "Wait up to 60 seconds for a launched thread to change state",
    "t3_followup": "Send another turn to a launched thread",
    "t3_answer": "Answer a pending user-input request on a launched thread",
    "t3_approve": "Respond to a pending approval request on a launched thread",
    "t3_interrupt": "Interrupt the active turn of a launched thread",
    "t3_recent": "List recent threads launched through this connector",
}


@dataclass(frozen=True)
class T3DispatchListenerConfig:
    target: Path
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    runtime_path: str

    def __repr__(self) -> str:
        return f"T3DispatchListenerConfig(bind_host={self.bind_host!r}, bind_port={self.bind_port})"


def _bearer_ok(authorization: str | None, bearer: str) -> bool:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    candidate = authorization[7:]
    if not candidate.isascii():
        return False
    try:
        return hmac.compare_digest(candidate, bearer)
    except ValueError:
        return False


class _T3RequestGate:
    def __init__(self, config: T3DispatchListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        self.config = config
        self.max_request_bytes = max_request_bytes

    def reject_reason(self, headers: Mapping[str, str], body_size: int) -> str | None:
        if body_size < 0 or body_size > self.max_request_bytes:
            return "too-large"
        host = headers.get("host", "").split(":", 1)[0].casefold()
        if grokbot_mcp._is_loopback(self.config.bind_host) and host in {"localhost", "127.0.0.1", "::1", ""}:
            pass
        elif host not in {entry.casefold().split(":", 1)[0] for entry in self.config.allowed_hosts}:
            return "forbidden"
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            return "forbidden"
        if not _bearer_ok(headers.get("authorization"), self.config.bearer):
            return "unauthorized"
        return None


class _T3GateASGI:
    def __init__(self, app: Callable[..., Any], gate: _T3RequestGate):
        self.app = app
        self.gate = gate

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        content_length = headers.get("content-length")
        if content_length is not None and (
            not content_length.isdecimal() or int(content_length) > self.gate.max_request_bytes
        ):
            await grokbot_mcp._reject_http(send, 413, "Request body is too large")
            return
        reason = self.gate.reject_reason(headers, 0)
        if reason is not None:
            await grokbot_mcp._reject_http(
                send,
                401 if reason == "unauthorized" else 403,
                "Unauthorized" if reason == "unauthorized" else "Forbidden",
            )
            return
        body, messages, too_large = await grokbot_mcp._read_bounded_body(receive, self.gate.max_request_bytes)
        if too_large:
            await grokbot_mcp._reject_http(send, 413, "Request body is too large")
            return
        if scope.get("path") == "/mcp" and _invalid_tool_request(body):
            await grokbot_mcp._reject_tool(send, body)
            return
        await self.app(scope, grokbot_mcp._replay_messages(messages, receive), send)


def _invalid_tool_request(body: bytes) -> bool:
    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params")
    if not isinstance(params, dict):
        return True
    name = params.get("name")
    if name not in c.TOOLS:
        return True
    arguments = params.get("arguments")
    if isinstance(arguments, dict):
        arguments = {key: value for key, value in arguments.items() if value is not None}
    try:
        c.PARSERS[name](arguments if arguments is not None else {})
    except c.T3DispatchError:
        return True
    return False


def _drop_none(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _register_tools(server: Any, tools: T3DispatchTools) -> None:
    def t3_capacity() -> dict[str, Any]:
        return tools.call_tool("t3_capacity", {})

    def t3_submit(
        repository: str,
        prompt: str,
        title: str,
        caller: str,
        host: str | None = None,
        seat: str | None = None,
        base_ref: str | None = None,
        interaction_mode: str | None = None,
    ) -> dict[str, Any]:
        return tools.call_tool(
            "t3_submit",
            _drop_none(
                {
                    "repository": repository,
                    "prompt": prompt,
                    "title": title,
                    "caller": caller,
                    "host": host,
                    "seat": seat,
                    "base_ref": base_ref,
                    "interaction_mode": interaction_mode,
                }
            ),
        )

    def t3_status(request_id: str) -> dict[str, Any]:
        return tools.call_tool("t3_status", {"request_id": request_id})

    def t3_wait(request_id: str, timeout_s: int | None = None) -> dict[str, Any]:
        return tools.call_tool("t3_wait", _drop_none({"request_id": request_id, "timeout_s": timeout_s}))

    def t3_followup(request_id: str, message: str) -> dict[str, Any]:
        return tools.call_tool("t3_followup", {"request_id": request_id, "message": message})

    def t3_answer(request_id: str, input_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        return tools.call_tool("t3_answer", {"request_id": request_id, "input_id": input_id, "answers": answers})

    def t3_approve(request_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        return tools.call_tool(
            "t3_approve", {"request_id": request_id, "approval_id": approval_id, "decision": decision}
        )

    def t3_interrupt(request_id: str) -> dict[str, Any]:
        return tools.call_tool("t3_interrupt", {"request_id": request_id})

    def t3_recent(caller: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return tools.call_tool("t3_recent", _drop_none({"caller": caller, "limit": limit}))

    for handler in (
        t3_capacity,
        t3_submit,
        t3_status,
        t3_wait,
        t3_followup,
        t3_answer,
        t3_approve,
        t3_interrupt,
        t3_recent,
    ):
        name = handler.__name__
        handler.__doc__ = _TOOL_DESCRIPTIONS[name]
        server.tool(name=name, description=_TOOL_DESCRIPTIONS[name])(handler)


def build_app(config: T3DispatchListenerConfig, tools: T3DispatchTools) -> Callable[..., Any]:
    MCPServer, JSONResponse, _, TransportSecuritySettings = grokbot_mcp._load_mcp()
    server = MCPServer(c.SERVICE_NAME, version="1")

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Any) -> Any:
        return JSONResponse({"ok": True, "service": c.SERVICE_NAME})

    _register_tools(server, tools)
    allowed_hosts = list(dict.fromkeys((*grokbot_mcp.SDK_LOOPBACK_ALLOWED_HOSTS, *config.allowed_hosts)))
    if not grokbot_mcp._is_loopback(config.bind_host):
        allowed_hosts = list(config.allowed_hosts)
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        host=config.bind_host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=list(config.allowed_origins),
        ),
    )
    return _T3GateASGI(app, _T3RequestGate(config))


def run_listener(config: T3DispatchListenerConfig, tools: T3DispatchTools) -> None:
    grokbot_mcp._load_mcp()
    try:
        import uvicorn
    except ImportError as exc:
        raise grokbot_mcp.OptionalDependencyError() from exc
    uvicorn.run(
        build_app(config, tools), host=config.bind_host, port=config.bind_port, access_log=False, log_level="warning"
    )


def _require_isolated_bearer(bearer: str) -> str:
    if len(bearer) < 32:
        raise c.T3DispatchError("invalid_request")
    dispatch_token = os.environ.get("GROKBOT_DISPATCH_TOKEN")
    if dispatch_token is not None and bearer == dispatch_token:
        raise c.T3DispatchError("invalid_request")
    return bearer


def _load_validated_instance(target: Path) -> dict[str, Any]:
    from .. import grokbot_packs

    payload = grokbot_packs._load_instance_config(target, c.PACK_ID)
    grokbot_packs._parse_default_bind(str(payload["bind"]))
    grokbot_packs._validate_bearer_reference(payload["bearer"])
    validated = dict(payload)
    validated["runtime_path"] = validate_runtime_reference(payload["runtime_path"])
    return validated


def _tools_for(runtime: Runtime) -> T3DispatchTools:
    return T3DispatchTools(
        runtime=runtime,
        runner=FleetRunner(runtime.t3_fleet_bin, runtime.git_bin),
        ledger=Ledger(runtime.ledger_path),
    )


def build_listener_from_target(
    target: Path,
    *,
    bind: str | None,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
) -> tuple[T3DispatchListenerConfig, T3DispatchTools]:
    from .. import grokbot_packs

    instance = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(bind or instance["bind"])
    runtime = load_runtime(Path(instance["runtime_path"]))
    bearer = _require_isolated_bearer(grokbot_mcp.load_bearer(bearer_file=bearer_file, bearer_env=bearer_env))
    config = T3DispatchListenerConfig(
        target=target.expanduser().resolve(),
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=bearer,
        runtime_path=instance["runtime_path"],
    )
    return config, _tools_for(runtime)


def watch_once(target: Path) -> dict[str, int]:
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    tools = _tools_for(runtime)
    return run_watch_once(
        runner=tools.runner,
        ledger=tools.ledger,
        wake_targets=load_wake_config(runtime.wake_path),
        log_path=runtime.wake_path.parent / "wake-notify.jsonl",
    )


def _load_runtime_config(target: Path) -> tuple[T3DispatchListenerConfig, str, Runtime]:
    from .. import grokbot_packs

    instance = _load_validated_instance(target)
    host, port = grokbot_packs._parse_default_bind(str(instance["bind"]))
    bearer = _require_isolated_bearer(grokbot_ops._resolve_bearer(dict(instance["bearer"])))
    runtime = load_runtime(Path(instance["runtime_path"]))
    config = T3DispatchListenerConfig(
        target=target,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(instance["allowed_hosts"]),
        allowed_origins=tuple(instance["allowed_origins"]),
        bearer=bearer,
        runtime_path=instance["runtime_path"],
    )
    return config, bearer, runtime


def doctor(target: Path, *, timeout: int | None = None) -> list[dict[str, str]]:
    from .. import grokbot_packs

    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool) -> None:
        checks.append({"check": name, "status": "ok" if ok else "fail"})

    try:
        from mcp.server import MCPServer  # noqa: F401

        record("dependency", True)
    except ImportError:
        record("dependency", False)
    try:
        config, bearer, runtime = _load_runtime_config(target)
        record("config", True)
    except (c.T3DispatchError, grokbot_packs.PackError, grokbot_mcp.ConfigurationError, OSError, ValueError, KeyError):
        record("config", False)
        return checks
    parent = grokbot_packs.instance_config_path(target, c.PACK_ID).parent
    record("permissions", os.access(parent, os.W_OK) if parent.is_dir() else False)
    try:
        FleetRunner(runtime.t3_fleet_bin, runtime.git_bin).capacity()
        record("t3-fleet", True)
    except (FleetCallError, FleetTimeout):
        record("t3-fleet", False)
    wait = timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    payload = grokbot_ops._request_json(
        f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}/health", bearer, wait, method="GET"
    )
    record("endpoint", payload is not None and payload.get("ok") is True and payload.get("service") == c.SERVICE_NAME)
    return checks


def canary(target: Path, *, timeout: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "pack_id": c.PACK_ID}
    try:
        config, bearer, _ = _load_runtime_config(target)
    except Exception:
        result["reason"] = "config"
        return result
    wait = timeout or grokbot_ops.DEFAULT_TIMEOUT_SECONDS
    base = f"http://{grokbot_ops._connect_host(config.bind_host)}:{config.bind_port}"
    health = grokbot_ops._request_json(f"{base}/health", bearer, wait, method="GET")
    anonymous_status = grokbot_ops._anonymous_health_status(f"{base}/health", wait)
    tools_response = grokbot_ops._tools_list(base, bearer, wait)
    if health is None or health.get("ok") is not True or health.get("service") != c.SERVICE_NAME:
        result["reason"] = "health"
        return result
    if anonymous_status not in {401, 403}:
        result["reason"] = "auth"
        return result
    if tools_response is None:
        result["reason"] = "inventory-unreachable"
        return result
    names = {tool.get("name") for tool in tools_response if isinstance(tool, dict)}
    if names != set(c.TOOLS):
        result["reason"] = "inventory-mismatch"
        return result
    result.update({"ok": True, "health": health, "auth_rejected_without_bearer": True, "tools": sorted(c.TOOLS)})
    return result


def _writable(runtime: Runtime) -> str:
    paths = [str(runtime.ledger_path.parent), *runtime.writable_paths]
    if runtime.wake_path.parent != runtime.ledger_path.parent:
        paths.insert(1, str(runtime.wake_path.parent))
    return " ".join(grokbot_ops._systemd_quote(path) for path in dict.fromkeys(paths))


def _hardening(runtime: Runtime) -> str:
    return (
        "NoNewPrivileges=yes\n"
        "PrivateTmp=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=read-only\n"
        f"ReadWritePaths={_writable(runtime)}\n"
    )


def render_unit(target: Path, *, python: str | None = None) -> str:
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = [
        python or sys.executable,
        "-m",
        "brigade",
        "run",
        "cloud",
        "grokbot",
        "serve",
        "--pack",
        c.PACK_ID,
        "--target",
        str(target),
        "--bind",
        instance["bind"],
    ]
    for host in instance.get("allowed_hosts", []):
        args += ["--allow-host", host]
    for origin in instance.get("allowed_origins", []):
        args += ["--allow-origin", origin]
    reference = instance["bearer"]
    if reference["kind"] == "file":
        args += ["--bearer-file", reference["path"]]
    else:
        args += ["--bearer-env", reference["name"]]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    return (
        "# Generated by brigade run cloud grokbot pack install-service.\n"
        f"# Unit: {grokbot_ops.unit_name(c.PACK_ID)}\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot MCP listener (t3-dispatch)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=10s\n"
        f"{_hardening(runtime)}\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_watch_units(target: Path, *, python: str | None = None) -> tuple[str, str]:
    instance = _load_validated_instance(target)
    runtime = load_runtime(Path(instance["runtime_path"]))
    args = [python or sys.executable, "-m", "brigade.grokbot_t3", "watch", "--target", str(target)]
    exec_start = " ".join(grokbot_ops._systemd_quote(argument) for argument in args)
    service = (
        "# Generated by python -m brigade.grokbot_t3 install-watch.\n"
        "[Unit]\n"
        "Description=Brigade Grok Bot t3-dispatch wake watcher\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
        "TimeoutStartSec=300\n"
        f"{_hardening(runtime)}"
    )
    timer = (
        "# Generated by python -m brigade.grokbot_t3 install-watch.\n"
        "[Unit]\n"
        "Description=Run the t3-dispatch wake watcher every minute\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=1min\n"
        f"Unit={WATCH_SERVICE}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _write_rendered(path: Path, rendered: str, *, force: bool) -> Path:
    try:
        existing = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        if not force or not grokbot_ops._path_is_symlink(path):
            raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
        existing = None
    if existing == rendered:
        return path
    if existing is not None and not force:
        raise grokbot_ops.ServiceRenderError(f"refusing to overwrite existing unit: {path.name}")
    try:
        grokbot_ops._write_text_nofollow_atomic(
            path, rendered, mode=0o644, replace_symlink=force, replace=force or existing is not None
        )
    except OSError as exc:
        raise grokbot_ops.ServiceRenderError(f"refusing unsafe unit path: {path.name}") from exc
    return path


def write_unit(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> Path:
    return _write_rendered(out_dir / grokbot_ops.unit_name(c.PACK_ID), render_unit(target, python=python), force=force)


def write_watch_units(target: Path, out_dir: Path, *, force: bool = False, python: str | None = None) -> list[Path]:
    service, timer = render_watch_units(target, python=python)
    return [
        _write_rendered(out_dir / WATCH_SERVICE, service, force=force),
        _write_rendered(out_dir / WATCH_TIMER, timer, force=force),
    ]
```

- [x] Replace `src/brigade/grokbot_t3/__init__.py` with:

```python
"""First-party t3-dispatch Grok Bot connector pack."""

from __future__ import annotations

from .contracts import PACK_ID, TOOLS, T3DispatchError
from .lifecycle import (
    build_listener_from_target,
    canary,
    doctor,
    render_unit,
    run_listener,
    write_unit,
)
from .runtime_config import validate_runtime_reference

__all__ = [
    "PACK_ID",
    "TOOLS",
    "T3DispatchError",
    "build_listener_from_target",
    "canary",
    "doctor",
    "render_unit",
    "run_listener",
    "validate_runtime_reference",
    "write_unit",
]
```

- [x] Create `src/brigade/grokbot_t3/__main__.py`:

```python
"""python -m brigade.grokbot_t3 watch|install-watch"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import grokbot_ops
from .contracts import T3DispatchError
from . import lifecycle


def main(argv: list[str] | None = None) -> int:
    from .. import grokbot_packs

    parser = argparse.ArgumentParser(prog="python -m brigade.grokbot_t3")
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch", help="Run one wake-watcher pass.")
    watch.add_argument("--target", type=Path, required=True)
    install = sub.add_parser("install-watch", help="Write the watcher service and timer units.")
    install.add_argument("--target", type=Path, required=True)
    install.add_argument("--out-dir", type=Path, required=True)
    install.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "watch":
            summary = lifecycle.watch_once(args.target)
            print(json.dumps(summary, sort_keys=True))
            return 1 if summary["failed"] else 0
        paths = lifecycle.write_watch_units(args.target, args.out_dir, force=args.force)
        print(json.dumps({"units": [path.name for path in paths]}))
        return 0
    except (T3DispatchError, grokbot_packs.PackError, grokbot_ops.ServiceRenderError, OSError):
        print("error: t3-dispatch configuration is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] Run to green: `V "./.venv/bin/pytest -q tests/test_grokbot_t3_lifecycle.py tests/test_grokbot_t3_tools.py"`. Expect all pass.
- [x] Commit: `git add src/brigade/grokbot_t3 tests/test_grokbot_t3_lifecycle.py && git commit -m "feat(grokbot): t3-dispatch listener, watcher entry point, units"`

---

### Task 8: Register the pack

**Files:**
- Modify: `src/brigade/grokbot_packs.py` (imports, constants near lines 23-110, `_connector_tools` ~137, `doctor` ~336, `canary` ~362, `render_install_service` ~381, `apply_install_service` ~423, `_setup` ~485, `_validate_instance_config` ~968, `_instance_keys` ~1040)
- Modify: `src/brigade/cli/run_cloud.py` (serve `--pack` choices ~300, serve dispatch ~1185, error tuple ~1215)
- Modify: `tests/test_grokbot_packs.py`

- [x] Update `tests/test_grokbot_packs.py` first:
  - Add `"t3-dispatch",` to `PACK_IDS` between `"repository-scout"` and `"wazuh-triage"`.
  - Add near the other tool tuples: `T3_DISPATCH_TOOLS = ("t3_answer", "t3_approve", "t3_capacity", "t3_followup", "t3_interrupt", "t3_recent", "t3_status", "t3_submit", "t3_wait")`.
  - In the show loop, before the final `else:`, add:

```python
        elif pack["id"] == "t3-dispatch":
            assert pack["kind"] == "connector"
            assert shown["tools"] == list(T3_DISPATCH_TOOLS)
            assert shown["default_bind"] == "127.0.0.1:8778"
            assert shown["public_route"] == ""
```

  - In `test_first_party_queue_packs_keep_isolated_ports_tools_and_credentials`, after the operations-relay lines, add `assert grokbot_mcp.parse_bind(packs["t3-dispatch"]["default_bind"])[1] == 8778`.
  - Append this setup test:

```python
def test_t3_dispatch_setup_requires_runtime_path(tmp_path, monkeypatch):
    from tests.test_grokbot_t3_runtime import write_runtime

    monkeypatch.setenv("TEST_GROKBOT_BEARER", "x" * 40)
    runtime = write_runtime(tmp_path)
    with _reject("missing-path-reference"):
        grokbot_packs.preview_setup(tmp_path, "t3-dispatch", bearer_env="TEST_GROKBOT_BEARER")
    with _reject("unexpected-key"):
        grokbot_packs.preview_setup(
            tmp_path, "t3-dispatch", bearer_env="TEST_GROKBOT_BEARER", runtime_path=str(runtime), workdir=str(tmp_path)
        )
    result = grokbot_packs.apply_setup(
        tmp_path, "t3-dispatch", bearer_env="TEST_GROKBOT_BEARER", runtime_path=str(runtime),
        allowed_hosts=["t3-dispatch.brigade.tools"],
    )
    assert result["bind"] == "127.0.0.1:8778"
    config = json.loads(grokbot_packs.instance_config_path(tmp_path, "t3-dispatch").read_text())
    assert config["runtime_path"] == str(runtime)
    assert config["allowed_hosts"] == ["t3-dispatch.brigade.tools"]
```


- [x] Run it, watch it fail: `V "./.venv/bin/pytest -q tests/test_grokbot_packs.py"`. Expect failures on the missing `t3-dispatch` pack.
- [x] Edit `src/brigade/grokbot_packs.py`:
  - Add `grokbot_t3,` to the `from . import (...)` block, alphabetically after `grokbot_ops,`.
  - In `CONNECTOR_DEFAULT_BINDS` add `"t3-dispatch": "127.0.0.1:8778",` after `"operations-relay"`.
  - After `OPERATIONS_RELAY_PACK_ID = "operations-relay"` add `T3_DISPATCH_PACK_ID = "t3-dispatch"`.
  - After `OPERATIONS_RELAY_INSTANCE_KEYS = ...` add `T3_DISPATCH_INSTANCE_KEYS = INSTANCE_KEYS | frozenset({"runtime_path"})`.
  - In `_connector_tools`, before `if pack_id == "wazuh-triage":` add:

```python
    if pack_id == T3_DISPATCH_PACK_ID:
        return grokbot_t3.TOOLS
```

  - In `doctor`, before `else:` add `elif pack["id"] == T3_DISPATCH_PACK_ID: checks = grokbot_t3.doctor(target)`.
  - In `canary`, before the final `return grokbot_cerebro.canary(target)` add `if pack["id"] == T3_DISPATCH_PACK_ID: return grokbot_t3.canary(target)`.
  - In `render_install_service`, before `return grokbot_cerebro.render_unit(...)` add `if pack["id"] == T3_DISPATCH_PACK_ID: return grokbot_t3.render_unit(target, python=sys.executable)`; add `grokbot_t3.T3DispatchError,` to its error tuple.
  - In `apply_install_service`, before `else:` add `elif pack["id"] == T3_DISPATCH_PACK_ID: path = grokbot_t3.write_unit(target, out_dir, force=force)`; add `grokbot_t3.T3DispatchError,` to its error tuple.
  - In `_setup`, before the final `else:` add:

```python
    elif pack["id"] == T3_DISPATCH_PACK_ID:
        if (
            cli_executable is not None
            or workdir is not None
            or owner_workspace is not None
            or ledger_path is not None
            or action_state_path is not None
            or approval_dir is not None
            or any(path is not None for path in obsidian_paths)
            or any(field is not None for field in upstream_fields)
        ):
            raise PackError("unexpected-key")
        payload["runtime_path"] = _t3_runtime_reference(runtime_path)
```

  - In `_validate_instance_config`, before `elif pack["kind"] == "connector":` add:

```python
    elif pack["id"] == T3_DISPATCH_PACK_ID:
        _t3_runtime_reference(payload.get("runtime_path"))
```

  - In `_instance_keys`, before `if pack["id"] == N8N_PACK_ID:` add `if pack["id"] == T3_DISPATCH_PACK_ID: return T3_DISPATCH_INSTANCE_KEYS`.
  - After `_operations_relay_owner_reference`, add:

```python
def _t3_runtime_reference(runtime_path: object) -> str:
    if runtime_path is None:
        raise PackError("missing-path-reference")
    try:
        return grokbot_t3.validate_runtime_reference(str(runtime_path))
    except grokbot_t3.T3DispatchError as exc:
        raise PackError("unsafe-path") from exc
```

- [x] Edit `src/brigade/cli/run_cloud.py`:
  - Add `"t3-dispatch",` to the serve `--pack` `choices` tuple after `"operations-relay",`.
  - Before the `else:` that builds the cerebro listener, add:

```python
                elif args.pack == "t3-dispatch":
                    from .. import grokbot_t3

                    t3_config, t3_tools = grokbot_t3.build_listener_from_target(target, **listener_kwargs)
                    grokbot_t3.run_listener(t3_config, t3_tools)
```

  - In the `except Exception as exc:` block, add `grokbot_t3,` to the local import and `grokbot_t3.T3DispatchError,` to the `isinstance` tuple.
- [x] Run to green: `V "./.venv/bin/pytest -q tests/test_grokbot_packs.py tests/test_grokbot_t3_contracts.py tests/test_grokbot_t3_runtime.py tests/test_grokbot_t3_fleet.py tests/test_grokbot_t3_ledger.py tests/test_grokbot_t3_tools.py tests/test_grokbot_t3_watch.py tests/test_grokbot_t3_lifecycle.py tests/test_grokbot_operations_relay.py"`. Expect all pass.
- [x] Commit: `git add -A src/brigade/grokbot_packs.py src/brigade/cli/run_cloud.py tests/test_grokbot_packs.py && git commit -m "feat(grokbot): register t3-dispatch connector pack"`

---

### Task 9: Docs and changelog

**Files:**
- Create: `docs/grokbot-t3-dispatch.md`
- Modify: `docs/grokbot-operating-guide.md` (connector pack table near line 43)
- Modify: `CHANGELOG.md` (`## Unreleased`)

- [x] Add a row to the pack table in `docs/grokbot-operating-guide.md`, matching the existing column order: `| t3-dispatch | connector | 127.0.0.1:8778 | (none) | t3_answer, t3_approve, t3_capacity, t3_followup, t3_interrupt, t3_recent, t3_status, t3_submit, t3_wait |`.
- [x] Create `docs/grokbot-t3-dispatch.md` with these sections and content: **Purpose** (one paragraph from the spec Goal); **Runtime JSON** (the full key list from Task 2 with a generic example using `host-a`, `host-b`, `host-c` and `$HOME/.config/grokbot-t3/` paths); **Setup** (`brigade run cloud grokbot pack setup --id t3-dispatch --target <queue-workspace> --bearer-file <file> --runtime-path <file> --allow-host <public host> --apply`, then `pack install-service --id t3-dispatch --target <queue-workspace> --out ~/.config/systemd/user`, then `python -m brigade.grokbot_t3 install-watch --target <queue-workspace> --out-dir ~/.config/systemd/user`); **Wake config** (the schema from Task 6 with one generic `targets` example); **Tools** (the table from the spec); **Kill switch** (set `"enabled": false` in the runtime JSON, then `systemctl --user restart brigade-grokbot-t3-dispatch.service`, because the listener reads the runtime once at start; for an immediate hard stop, `systemctl --user stop brigade-grokbot-t3-dispatch.service cloudflared-grokbot-t3.service`); **Security** (the spec Security paragraph).
- [x] Under `## Unreleased` in `CHANGELOG.md` add: `- Grok Bot: new first-party connector pack \`t3-dispatch\` launches and steers T3 threads through \`t3-fleet\`, with a one-minute wake watcher.`
- [x] Commit: `git add docs/grokbot-t3-dispatch.md docs/grokbot-operating-guide.md CHANGELOG.md && git commit -m "docs(grokbot): t3-dispatch runbook"`

---

### Task 10: Full gate and pull request

- [ ] `V "./scripts/verify-fast"`. Expect exit 0. Re-run any parallel-only failure serially with `./.venv/bin/pytest -q <test>` before treating it as real.
- [ ] `V "./scripts/verify"`. Expect exit 0.
- [ ] Run the skillet `pass` skill on the branch.
- [ ] Push and open the PR: `git push -u origin feat/grokbot-t3-dispatch` then `gh pr create --repo escoffier-labs/brigade --base main --title "feat(grokbot): t3-dispatch connector pack" --body-file <body>` where the body links the spec and plan, lists the test modules, and ends with the attribution line required by the session. Link the PR with `link_pull_request`.
- [ ] Apply label `coderabbit-review` and comment `@coderabbitai full review`. Address every finding per `sendback`, then merge through the normal gates. Deployment (Tasks 11 to 14) runs from merged `main`, from the operator's private runbook.
---

### Tasks 11 to 14: deployment

Host seats, the controller listener and watcher units, the Cloudflare edge, the Grok Bot connector and wake routines, and live acceptance run from the operator's private deployment runbook, because they name private hosts, paths, and credentials. They start after this PR merges.
