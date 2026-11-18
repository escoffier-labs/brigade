"""Bounded, local work-hook observations for automatic lesson review."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from . import dirfd, guard, lesson_auth, lesson_io as io, localio, receipt_signing
from .component_paths import cache_root
from .handoff_ingest_rules import sha256_hex
from .guard.types import ScanOptions
from .lesson_io import held_file_lock
from .untrusted import scan_untrusted

ROOT = ".brigade/lesson-review"
ORIGIN = "brigade.lesson-review.v1"
MAX_EVENTS = 96
MAX_TEXT = 2048


def root(target: Path) -> Path:
    path = target / ROOT
    with io.directory(path, create=True, private=True):
        pass
    return path


def config(target: Path) -> dict[str, Any]:
    raw = io.text(target / ".brigade/lesson-review.json", absent=True, limit=4096)
    value = json.loads(raw) if raw is not None else {}
    if not isinstance(value, dict):
        raise ValueError("lesson review config must be an object")
    result = {"enabled": True, "threshold": 20, "idle_seconds": 300, "seat": "researcher", **value}
    if type(result["enabled"]) is not bool or not isinstance(result["seat"], str) or not result["seat"]:
        raise ValueError("invalid lesson review config")
    for key, maximum in (("threshold", 10000), ("idle_seconds", 86400)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(f"invalid lesson review {key}")
    return result


def load(target: Path) -> dict[str, Any]:
    raw = io.text(target / ROOT / "state.json", absent=True, limit=512 * 1024, private=True)
    if raw is None:
        return {"sequence": 0, "reviewed": 0, "last_activity": 0, "events": []}
    state = json.loads(raw)
    if (
        not isinstance(state, dict)
        or type(state.get("sequence")) is not int
        or type(state.get("reviewed")) is not int
        or not 0 <= state["reviewed"] <= state["sequence"]
        or not isinstance(state.get("events"), list)
        or len(state["events"]) > MAX_EVENTS
        or not isinstance(state.get("drained_prompt_markers", []), list)
        or len(state.get("drained_prompt_markers", [])) > MAX_EVENTS
        or any(not isinstance(e, dict) for e in state.get("drained_prompt_markers", []))
        or type(state.get("last_activity")) not in (int, float)
    ):
        raise ValueError("invalid lesson activity state")
    return state


def save(target: Path, state: dict[str, Any]) -> None:
    io.write(root(target) / "state.json", io.json_bytes(state))


def scanned(text: str) -> str:
    if scan_untrusted(text).flagged:
        raise ValueError("lesson text contains untrusted instructions")
    result = guard.scan_text(text, options=ScanOptions(honor_allow_comments=False, scan_frontmatter=True))
    if result.blocked or result.changed:
        raise ValueError("lesson text failed content scan")
    return text


def record_event(target: Path, event: str, payload: dict[str, Any], *, now: float | None = None) -> str | None:
    if event not in {"PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "PreToolUse"}:
        return None
    if not config(target)["enabled"]:
        return None
    # Review workers do not feed their own activity back into the trigger.
    if os.environ.get("BRIGADE_RUN_ID", "").startswith("lesson-"):
        return None
    from . import lesson_admission  # local import: lesson_admission imports this module

    if not lesson_admission.AUTOMATIC_LESSON_ADMISSION:
        # Nothing consumes observations until hub-anchored admission lands (#2620),
        # so hooks record nothing and keep no prompt text.
        discard_prompts(target)
        return "lesson_admission_awaits_hub_anchor"
    if receipt_signing.load_workspace_key(target) is None:
        discard_prompts(target)
        return "lesson_review_needs_key"
    if event == "UserPromptSubmit":
        try:
            prompt = scanned(str(payload.get("prompt", "")).encode()[:MAX_TEXT].decode(errors="ignore"))
        except ValueError:
            prompt = "[observation withheld by scanner]"
        spool = prompt_path(target)
        with held_file_lock(spool.with_suffix(".lock"), deadline_seconds=1):
            prompts = _read_prompts(spool)
            prompts.append(
                {
                    "prompt": prompt,
                    "prompt_id": uuid.uuid4().hex,
                    "at": time.time() if now is None else now,
                    **_session_metadata(target, payload),
                }
            )
            io.write(spool, io.json_bytes(prompts[-MAX_EVENTS:]))
        return None
    fields = ("tool_input", "tool_response", "tool_output", "error")
    values = [str(payload[key]) for key in fields if key in payload]
    budget = (MAX_TEXT - len(values) * 6) // max(1, len(values))
    text = "\n".join(
        value
        if len(value.encode()) <= budget
        else value.encode()[: budget // 2].decode(errors="ignore")
        + "\n...\n"
        + value.encode()[-budget // 2 :].decode(errors="ignore")
        for value in values
    )
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    text = text.encode()[:MAX_TEXT].decode(errors="ignore")
    try:
        text = scanned(text)
    except ValueError:
        text = "[observation withheld by scanner]"
    with held_file_lock(root(target) / "state.lock", deadline_seconds=1):
        state = load(target)
        persisted_ids = {e["id"] for e in state["events"]}
        drain_prompts(target, state, acknowledge=False)
        # Pre-tool activity invalidates an in-flight review without counting twice.
        at = time.time() if now is None else now
        metadata: dict[str, Any] = {}
        command = payload.get("tool_input")
        command = command.get("command", command.get("cmd")) if isinstance(command, dict) else command
        if event in {"PostToolUse", "PostToolUseFailure"} and isinstance(command, str):
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                argv = list(lexer)
                if any(token in {";", "&&", "||", "|", "&", ">", "<", "(", ")"} for token in argv):
                    argv = []
            except ValueError:
                argv = []
            if argv[:4] == ["brigade", "work", "verify", "run"]:
                metadata["verify_command"] = True
                run_id = _verify_run_id(payload)
                if run_id:
                    metadata["verify_run_id"] = run_id
        metadata.update(_session_metadata(target, payload))
        _, evicted = append_event(target, state, event, text, at, **metadata)
        _retain_evicted_prompts(target, state, [e for e in evicted if e["id"] in persisted_ids])
        if event in {"PostToolUse", "PostToolUseFailure"}:
            state["calls"] = state.get("calls", 0) + 1
        save(target, state)
        _acknowledge_saved_prompts(target)
    return None


def _session_metadata(target: Path, payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    session = payload.get("session_id")
    # An empty id would hash to a non-empty value and pass the same-session check.
    if isinstance(session, str) and session.strip():
        metadata["session_id"] = sha256_hex(session.encode())
        metadata["harness_session_fingerprint"] = localio.stable_hash({"claude_session_id": session})
    work_session = io.text(target / ".brigade/work/current", absent=True, limit=4096)
    if work_session and work_session.strip():
        metadata["work_session_id"] = work_session.strip()
    return metadata


def on_event(target: Path, event: str, payload: dict[str, Any]) -> None:
    """Optional hook work must never affect the primary hook response."""
    try:
        record_event(target, event, payload)
    except Exception:
        pass


def prompt_path(target: Path) -> Path:
    return Path(cache_root()) / "brigade/lesson-prompts" / (sha256_hex(str(target.resolve()).encode()) + ".json")


def _verify_run_id(payload: dict[str, Any]) -> str | None:
    """Read the coordinator's run field from output, never from command text."""
    output = payload.get("tool_response", payload.get("tool_output", payload.get("error", "")))
    if isinstance(output, dict):
        output = output.get("stdout", output.get("output", ""))
    if not isinstance(output, str) or len(output.encode()) > 256 * 1024:
        return None
    try:
        value = json.loads(output)
    except ValueError:
        # Multiple run headers are ambiguous, including output from nested runs.
        runs = re.findall(r"^run: ([A-Za-z0-9_-]+)\s*$", output, re.M)
        return runs[0] if len(runs) == 1 else None
    run_id = value.get("run_id") if isinstance(value, dict) else None
    return run_id if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", run_id) else None


def _read_prompts(path: Path) -> list[dict[str, Any]]:
    try:
        return _validated_prompts(path)
    except (ValueError, UnicodeError):
        # Caller holds the spool lock. Preserve the bad bytes privately without
        # reading an oversized file again or including its body in a diagnostic.
        name = f"{path.stem}-{uuid.uuid4().hex}.quarantine"
        with io.directory(path.parent, private=True) as parent:
            info = dirfd.stat_child(parent, path.name)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("lesson prompt spool must be regular and single-link") from None
            dirfd.rename_file_child(parent, path.name, parent, name)
            dirfd.fsync_directory(parent)
        io.write(path.parent / (name + ".json"), io.json_bytes({"reason": "invalid-prompt-spool"}))
        return []


def _validated_prompts(path: Path) -> list[dict[str, Any]]:
    raw = io.text(path, absent=True, private=True, limit=256 * 1024)
    value = json.loads(raw) if raw is not None else None
    # Read the earlier single-prompt spool format during upgrades.
    if isinstance(value, dict):
        value = [value]
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_EVENTS:
        raise ValueError("invalid lesson prompt spool")
    if any(
        not isinstance(p, dict) or not isinstance(p.get("prompt"), str) or type(p.get("at")) not in (int, float)
        for p in value
    ):
        raise ValueError("invalid lesson prompt observation")
    return value


def discard_prompts(target: Path) -> None:
    """Discard queued observations when workspace authority is unavailable."""
    path = prompt_path(target)
    with held_file_lock(path.with_suffix(".lock"), deadline_seconds=1):
        io.write(path, b"null\n")


def _prompt_marker(target: Path, event: dict[str, Any]) -> dict[str, Any]:
    """Sign a compact dedup identity for a prompt that stays queued.

    The marker binds the saved event id and prompt digest with the workspace key,
    so eviction cannot turn an unsigned id list into review authority.
    """
    marker = {
        "id": event["id"],
        "append_sequence": event["append_sequence"],
        "kind": "DrainedPrompt",
        "at": event["at"],
        "earliest_unreviewed_at": event["earliest_unreviewed_at"],
        "prompt_id": event["prompt_id"],
        "prompt_digest": event["prompt_digest"],
    }
    lesson_auth.stamp(target, marker)
    return marker


def _prompt_marker_matches(target: Path, marker: dict[str, Any], prompt: dict[str, Any]) -> bool:
    return (
        isinstance(marker, dict)
        and marker.get("kind") == "DrainedPrompt"
        and marker.get("prompt_id") == prompt["prompt_id"]
        and marker.get("prompt_digest") == localio.canonical_json_digest(prompt["prompt"])
        and marker.get("at") == prompt["at"]
        and lesson_auth.authentic(target, marker)
    )


def _retain_evicted_prompts(target: Path, state: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Keep signed dedup markers for authentic prompt events that left the buffer."""
    markers = {
        e["prompt_id"]: e
        for e in state.get("drained_prompt_markers", [])
        if isinstance(e, dict) and isinstance(e.get("prompt_id"), str)
    }
    for event in events:
        if (
            event.get("kind") == "UserPromptSubmit"
            and isinstance(event.get("prompt_id"), str)
            and isinstance(event.get("prompt_digest"), str)
            and lesson_auth.authentic(target, event)
        ):
            markers[event["prompt_id"]] = _prompt_marker(target, event)
    if markers:
        state["drained_prompt_markers"] = list(markers.values())[-MAX_EVENTS:]
    else:
        state.pop("drained_prompt_markers", None)


def drain_prompts(target: Path, state: dict[str, Any], *, acknowledge: bool = True) -> None:
    """Caller holds state.lock. Prompt hooks acquire only the cache-side lock."""
    if receipt_signing.load_workspace_key(target) is None:
        discard_prompts(target)
        return
    path = prompt_path(target)
    with held_file_lock(path.with_suffix(".lock"), deadline_seconds=1):
        prompts = _read_prompts(path)
        if not prompts:
            state.pop("drained_prompt_markers", None)
            return
        # Upgrade legacy entries before recording them so retries have stable ids.
        if any(not isinstance(p.get("prompt_id"), str) or not p["prompt_id"] for p in prompts):
            for prompt in prompts:
                if not isinstance(prompt.get("prompt_id"), str) or not prompt["prompt_id"]:
                    prompt["prompt_id"] = uuid.uuid4().hex
            io.write(path, io.json_bytes(prompts))
        # Keep signed identities for authentic prompt events that were evicted
        # while their prompt stays queued after an acknowledgement failure. The
        # marker deduplicates the prompt, so an old timestamp cannot re-enter a
        # later review window.
        queued = {p["prompt_id"] for p in prompts}
        persisted_ids = {e["id"] for e in state["events"]}
        markers = {
            e["prompt_id"]: e
            for e in state.get("drained_prompt_markers", [])
            if isinstance(e, dict) and isinstance(e.get("prompt_id"), str) and e["prompt_id"] in queued
        }
        for prompt in prompts:
            matched = next(
                (e for e in state["events"] if lesson_auth.prompt_matches(target, e, prompt)),
                None,
            )
            if matched is not None:
                continue
            marker = markers.get(prompt["prompt_id"])
            if marker is not None and _prompt_marker_matches(target, marker, prompt):
                continue
            candidate = {
                **state,
                "events": list(state["events"]),
                "drained_prompt_markers": list(state.get("drained_prompt_markers", [])),
            }
            _, evicted = append_event(
                target,
                candidate,
                "UserPromptSubmit",
                prompt["prompt"],
                prompt["at"],
                prompt_id=prompt["prompt_id"],
                prompt_digest=localio.canonical_json_digest(prompt["prompt"]),
                **{
                    key: prompt[key]
                    for key in ("session_id", "harness_session_fingerprint", "work_session_id")
                    if key in prompt
                },
            )
            _retain_evicted_prompts(target, candidate, [e for e in evicted if e["id"] in persisted_ids])
            # A full protected buffer leaves the prompt queued without
            # changing the review digest or resetting its attempt budget.
            if any(
                e["id"] == candidate["sequence"] and lesson_auth.prompt_matches(target, e, prompt)
                for e in candidate["events"]
            ):
                state.update(candidate)
                markers = {
                    e["prompt_id"]: e
                    for e in state.get("drained_prompt_markers", [])
                    if isinstance(e, dict) and isinstance(e.get("prompt_id"), str)
                }
        state["drained_prompt_markers"] = list(markers.values())[-MAX_EVENTS:]
        if not state["drained_prompt_markers"]:
            state.pop("drained_prompt_markers", None)
        state["drained_prompt_ids"] = [p["prompt_id"] for p in prompts]
        save(target, state)
    if acknowledge:
        _acknowledge_saved_prompts(target)


def _acknowledge_saved_prompts(target: Path) -> None:
    """Keep prompts queued when the acknowledgement write fails.

    The signed saved event, not the spool write, suppresses a repeat drain, so a
    failed acknowledgement must not abort the caller after it has saved its own
    event.
    """
    try:
        acknowledge_prompts(target)
    except OSError:
        pass


def acknowledge_prompts(target: Path) -> None:
    """Caller holds state.lock and has completed every append and final save."""
    path = prompt_path(target)
    with held_file_lock(path.with_suffix(".lock"), deadline_seconds=1):
        prompts = _read_prompts(path)
        saved = load(target)
        remaining = [
            p
            for p in prompts
            if not any(lesson_auth.prompt_matches(target, e, p) for e in saved["events"])
            and not any(_prompt_marker_matches(target, e, p) for e in saved.get("drained_prompt_markers", []))
        ]
        io.write(path, io.json_bytes(remaining) if remaining else b"null\n")


def append_event(
    target: Path, state: dict[str, Any], kind: str, text: str, at: float, **metadata: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    authenticated = [e for e in state["events"] if lesson_auth.authentic(target, e)]
    # A forged counter can only skip ids or veto recording, never move an
    # authenticated append backwards relative to the retained history.
    if any(e["id"] > state["sequence"] for e in authenticated):
        raise ValueError("lesson sequence precedes authenticated history")
    append_sequence = max((e["append_sequence"] for e in authenticated), default=0) + 1
    state["sequence"] += 1
    boundary = min([at] + [e["earliest_unreviewed_at"] for e in authenticated if e["id"] > state["reviewed"]])
    state.setdefault("earliest_unreviewed_at", at)
    state["last_activity"] = max(state["last_activity"], at)
    observation = {
        "id": state["sequence"],
        "append_sequence": append_sequence,
        "kind": kind,
        "text": text,
        "at": at,
        **metadata,
        "earliest_unreviewed_at": boundary,
    }
    lesson_auth.stamp(target, observation)
    state["events"].append(observation)
    evicted: list[dict[str, Any]] = []
    # Keep the failure side of a fix even in long sessions. Other observations
    # are bounded to over twenty complete pre/post tool pairs plus prompts.
    while len(state["events"]) > MAX_EVENTS or len(io.json_bytes(state)) > 256 * 1024:
        reviewed = next((i for i, e in enumerate(state["events"]) if e["id"] <= state["reviewed"]), None)
        if reviewed is not None:
            evicted.append(state["events"].pop(reviewed))
            continue
        index = next(
            (
                i
                for i, e in enumerate(state["events"])
                if not e.get("verify_command") and e["kind"] != "PostToolUseFailure"
            ),
            None,
        )
        if index is None:
            index = next((i for i, e in enumerate(state["events"]) if not e.get("verify_command")), 0)
        evicted.append(state["events"].pop(index))
    return observation, evicted
