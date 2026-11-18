"""Closed privacy projection for served run views and the run handoff note (#2046).

Local run files keep raw prompts, worker text, and absolute paths because resume
needs them. ``brigade runs list --json``, ``brigade runs show --json``, the
loopback Run View, its watch stream, and the memory handoff note are readable
surfaces, so they reuse the export rules from :mod:`run_export_projection`:
prompt and generated text becomes the keyed ``{digest, chars}`` object, and a
path-like string becomes a keyed digest instead of a filesystem location.

The digest key is the same per-host HMAC key the export uses, so a digest is not
portable across hosts and a short prompt cannot be reversed by guessing.

The closed field helpers at the bottom (``_clean_str``, ``_allowlisted`` and
friends) used to live in ``runs_cmd``. They live here so the frozen legacy
module does not grow: every string in a list, detail, or watch contract passes
through ``_clean_str``, and every fixed key set passes through ``_allowlisted``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from . import run_export_projection

TEXT_PREVIEW_CHARS = 80

# Values under these keys carry prompt or generated text. The export set already
# covers task, text, detail, final, error, output, result and content. The run
# views add the watch record's ``failure_detail`` spelling, verify ``command``
# text, and the roster ``role`` note that export treats as a prompt too.
TEXT_KEYS = run_export_projection.PROMPT_KEYS | frozenset({"failure_detail", "command", "role"})


@lru_cache(maxsize=64)
def _key_for_target(target: Path) -> bytes:
    return run_export_projection.require_digest_key(target)


def digest_key(run_dir: Path) -> bytes:
    """Return the per-host keyed digest key for the workspace that owns *run_dir*."""
    return _key_for_target(run_export_projection.digest_key_target(run_dir))


def digest_text(run_dir: Path, text: str) -> dict[str, Any]:
    """Return the closed ``{digest, chars}`` shape for one text value."""
    return run_export_projection.digest_prompt(digest_key(run_dir), text)


def digest_value(run_dir: Path, value: object) -> dict[str, Any] | None:
    """Return the digest shape for a text value, or None when it is absent."""
    if not isinstance(value, str) or not value:
        return None
    return digest_text(run_dir, value)


def task_preview(value: object) -> str | None:
    """Return a bounded, home-redacted, one-line task preview for ``--show-task``."""
    return _clean_str(value, TEXT_PREVIEW_CHARS)


def _project_text(value: Any, *, key: bytes) -> Any:
    if isinstance(value, str):
        return run_export_projection.digest_prompt(key, value)
    if isinstance(value, Mapping):
        if set(value) == {"digest", "chars"} and run_export_projection.is_valid_digest(value.get("digest")):
            return dict(value)
        return _project(value, key=key)
    if isinstance(value, list):
        return _project(value, key=key)
    return value


def _project(value: Any, *, key: bytes) -> Any:
    if isinstance(value, str):
        return run_export_projection.digest_prompt(key, value) if run_export_projection.is_path_like(value) else value
    if isinstance(value, list):
        return [_project(item, key=key) for item in value]
    if isinstance(value, Mapping):
        return {
            item_key: (_project_text(item, key=key) if item_key in TEXT_KEYS else _project(item, key=key))
            for item_key, item in value.items()
            if isinstance(item_key, str)
        }
    return value


def _assert_closed(value: Any, *, label: str, home: Path) -> None:
    """Fail closed when a projected view still holds raw text or a filesystem path."""
    if isinstance(value, str):
        if run_export_projection.is_path_like(value) or (bool(str(home)) and str(home) in value):
            raise run_export_projection.RunExportProjectionError(f"{label} still holds an absolute path")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_closed(item, label=f"{label}[{index}]", home=home)
        return
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise run_export_projection.RunExportProjectionError(f"{label} has a non-string JSON key")
            if item_key in TEXT_KEYS and isinstance(item, str):
                raise run_export_projection.RunExportProjectionError(f"{label}.{item_key} still holds raw text")
            if (
                item_key in TEXT_KEYS
                and isinstance(item, Mapping)
                and set(item) == {"digest", "chars"}
                and not run_export_projection.is_valid_digest(item.get("digest"))
            ):
                raise run_export_projection.RunExportProjectionError(f"{label}.{item_key} is not a keyed digest")
            _assert_closed(item, label=f"{label}.{item_key}", home=home)
        return


def project_value(value: Any, run_dir: Path) -> Any:
    """Project one served JSON value: text becomes a digest, paths become digests."""
    key = digest_key(run_dir)
    projected = _project(value, key=key)
    _assert_closed(projected, label="run-view", home=Path.home())
    return projected


def project_summary(
    payload: Mapping[str, Any], run_dir: Path, *, task: object = None, show_task: bool = False
) -> dict[str, Any]:
    """Project one ``brigade.runs-list.v1`` run summary."""
    projected = project_value(dict(payload), run_dir)
    if show_task:
        preview = task_preview(task)
        if preview:
            projected["task_preview"] = preview
    return projected


def project_detail(
    payload: Mapping[str, Any], run_dir: Path, *, task: object = None, show_task: bool = False
) -> dict[str, Any]:
    """Project one ``brigade.run-detail.v1`` payload."""
    projected = project_value(dict(payload), run_dir)
    if show_task:
        preview = task_preview(task)
        run_section = projected.get("run")
        if preview and isinstance(run_section, dict):
            run_section["task_preview"] = preview
    return projected


def watch_record_projector(run_dir: Path) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Return a projection callable for ``brigade.run-watch.v1`` records."""
    key = digest_key(run_dir)

    def project(record: Mapping[str, Any]) -> dict[str, Any]:
        projected = _project(dict(record), key=key)
        _assert_closed(projected, label="run-watch", home=Path.home())
        return projected

    return project


# --- closed field helpers (#631) moved out of the frozen runs_cmd module ---


# Windows alternative is first so `C:/Users/<name>` is not left as `C:~`.
# `/+` and `[/\\]+` accept doubled separators; `[^/\\]+` keeps spaces and apostrophes.
_HOME_PREFIX_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]+(?i:Users)[/\\]+|/(?:home|Users)/+)[^/\\]+",
)


def _redact_home_prefixes(text: str) -> str:
    """Rewrite POSIX and Windows home prefixes to `~` for JSON contracts."""
    return _HOME_PREFIX_RE.sub("~", text)


def _short(text: object, limit: int = 72) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _clean_str(value: object, limit: int | None = None) -> str | None:
    """Bound, then home-prefix-redact, every string that enters a JSON contract."""
    if not isinstance(value, str) or not value:
        return None
    cleaned = _short(value, limit) if limit is not None else value
    return _redact_home_prefixes(cleaned)


def _clean_str_list(value: object) -> list[str] | None:
    """Clean every string in a JSON-contract list; drop non-strings and empties."""
    if not isinstance(value, list):
        return None
    return [item for raw in value if (item := _clean_str(raw)) is not None]


def _clean_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _compact(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value is not None}


class _MergedSource(dict[str, object]):
    """Source overlay that records which keys passed through the cleaner."""

    cleaned_keys: frozenset[str]

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(data or {})
        self.cleaned_keys = frozenset()


def _merge_source(source: Mapping[str, Any] | None, cleaned: Mapping[str, object]) -> dict[str, object]:
    """Overlay cleaned fields on the source mapping so `_allowlisted` must drop extras."""
    payload = _MergedSource(source)
    payload.update(cleaned)
    payload.cleaned_keys = frozenset(cleaned)
    return payload


def _allowlisted(payload: Mapping[str, object], allowed: frozenset[str]) -> dict[str, object]:
    """Keep only explicitly allowlisted keys; drop None the same way as `_compact`."""
    if not isinstance(payload, _MergedSource):
        raise AssertionError("JSON contract allowlist requires _merge_source overlay")
    missing = allowed - payload.cleaned_keys
    if missing:
        raise AssertionError("allowlist keys have no cleaned counterpart: " + ", ".join(sorted(missing)))
    return {key: value for key, value in payload.items() if key in allowed and value is not None}
