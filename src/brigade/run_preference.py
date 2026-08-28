"""Fleet/local Brigade run preference overlay (#1223).

The hub pin is the fleet default. A machine caches the last fetched document
under ``~/.brigade/run-preference.toml`` and uses that cache when the hub is
down. ``--worker`` and a spoken seat name always win. The document names
existing roster seats only: no tokens, env values, or home paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import toml_compat

SCHEMA = "brigade.run_preference.v1"
CACHE_REL_PATH = Path(".brigade") / "run-preference.toml"
SEAT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLOWED_FIELDS = ("impl", "review", "chef", "notes")
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|credential|env|path|home)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(keepass://|/home/|/Users/|/run/user/|BEGIN [A-Z ]+PRIVATE KEY|CURSOR_API_KEY|BRIGADE_FLEET_)",
    re.IGNORECASE,
)


class RunPreferenceError(ValueError):
    """The preference document is invalid or contains forbidden material."""


@dataclass(frozen=True)
class RunPreference:
    impl: str | None = None
    review: str | None = None
    chef: str | None = None
    notes: str | None = None

    def payload(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for field in ALLOWED_FIELDS:
            value = getattr(self, field)
            if isinstance(value, str) and value:
                result[field] = value
        return result

    def planner_prefix(self) -> str:
        lines = [
            "Fleet run preference (explicit --worker or a named seat in the task wins):",
        ]
        if self.impl:
            lines.append(f"- default impl: {self.impl}")
        if self.review:
            lines.append(f"- default review: {self.review}")
        if self.chef:
            lines.append(f"- default chef: {self.chef}")
        if self.notes:
            lines.append(f"- notes: {self.notes}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines) + "\n\n"


def cache_path(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home) / CACHE_REL_PATH
    from .fleet_client import brigade_home

    return brigade_home() / CACHE_REL_PATH.name


def parse_preference(raw: Any) -> RunPreference:
    if raw is None:
        return RunPreference()
    if not isinstance(raw, dict):
        raise RunPreferenceError("preference must be a JSON/TOML object")
    for key in raw:
        if _SECRET_KEY_RE.search(str(key)):
            raise RunPreferenceError("preference must not carry secret or path fields")
    unknown = [key for key in raw if key not in ALLOWED_FIELDS]
    if unknown:
        raise RunPreferenceError(f"unknown preference field: {unknown[0]}")
    parsed: dict[str, str | None] = {}
    for field in ALLOWED_FIELDS:
        value = raw.get(field)
        if value is None or value == "":
            parsed[field] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise RunPreferenceError(f"preference field {field!r} must be a string")
        text = value.strip()
        if _SECRET_VALUE_RE.search(text):
            raise RunPreferenceError("preference must not contain tokens, env values, or home paths")
        if field != "notes" and not SEAT_NAME_RE.match(text):
            raise RunPreferenceError(f"preference field {field!r} must be a roster seat name")
        if field == "notes" and len(text) > 240:
            raise RunPreferenceError("preference notes must be at most 240 characters")
        parsed[field] = text
    return RunPreference(**parsed)


def load_cached(home: Path | None = None) -> RunPreference:
    path = cache_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RunPreference()
    except OSError:
        return RunPreference()
    try:
        return parse_preference(toml_compat.loads(text))
    except (RunPreferenceError, ValueError):
        return RunPreference()


def write_cached(preference: RunPreference, home: Path | None = None) -> Path:
    path = cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Local cache of the fleet run preference (#1223).",
        "# --worker and a spoken seat name always win.",
    ]
    payload = preference.payload()
    if payload:
        lines.extend(f"{key} = {toml_compat.format_toml_value(value)}" for key, value in payload.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def apply_to_roster(roster: Any, preference: RunPreference) -> Any:
    """Overlay default chef when that seat exists locally. Missing seats are ignored."""
    chef = preference.chef
    if not chef or chef not in getattr(roster, "agents", {}):
        return roster
    if chef == roster.orchestrator:
        return roster
    return replace(roster, orchestrator=chef)


def apply_to_task(task: str, preference: RunPreference, *, worker: str | None) -> str:
    """Prefix the planner-visible default. A direct ``--worker`` skips the overlay."""
    if worker:
        return task
    prefix = preference.planner_prefix()
    return f"{prefix}{task}" if prefix else task


def _spoken_seat_names(task: str, roster: Any) -> list[str]:
    agents = getattr(roster, "agents", {}) or {}
    found: list[str] = []
    for name in sorted(agents, key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9._-]){re.escape(str(name))}(?![A-Za-z0-9._-])"
        if re.search(pattern, task, re.IGNORECASE):
            found.append(str(name))
    return found


def resolve_worker(
    preference: RunPreference,
    roster: Any,
    *,
    worker: str | None,
    task: str,
) -> str | None:
    """Pick the default worker seat from the pin.

    ``--worker`` always wins. Without a pinned ``impl`` the answer is always
    ``None`` (the orchestrator plans, exactly as before the pin existed): a
    roster seat name appearing in task prose must never convert a planned run
    into a direct dispatch on its own. With a pin, any spoken roster seat name
    suppresses the pin and hands the task back to the orchestrator, which sees
    the named seat in the planner prefix; the pin also never resolves to the
    orchestrator seat.
    """
    if worker:
        return worker
    impl = preference.impl
    agents = getattr(roster, "agents", {}) or {}
    if not impl or impl not in agents:
        return None
    if impl == getattr(roster, "orchestrator", None):
        return None
    if _spoken_seat_names(task, roster):
        return None
    return impl


def refresh_cache(*, home: Path | None = None) -> RunPreference:
    """Best-effort hub pull. Never raises; hub down keeps the last cache."""
    cached = load_cached(home)
    try:
        from . import fleet_client

        remote = fleet_client.fetch_run_preference()
    except Exception:
        return cached
    try:
        parsed = parse_preference(remote)
    except RunPreferenceError:
        return cached
    write_cached(parsed, home)
    return parsed
