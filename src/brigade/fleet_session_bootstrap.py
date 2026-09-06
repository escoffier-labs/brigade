"""Per-launch Fleet policy prepare/apply/ack for enrolled sessions.

The python -m entry is the supported enrolled interactive launcher for
codex, claude, and opencode. Raw app launches that bypass it are
unsupported until a harness has a real verified hook. Codex/OpenCode
managed AGENTS pointers are not verified runtime enforcement. Claude
SessionStart additionalContext is not a consumer acknowledgement.
Existing sessions need a restart through this launcher unless a harness
exposes a verified reload boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import fleet_client_cloud, fleet_client_policy, fleet_model_roster, fleet_session_presence
from .fleet_client import resolve_node_id
from .fleet_client_policy import FleetPolicyClientError

CONSUMER_BRIGADE_RUN = "brigade-run"
LAUNCHER_HARNESSES = frozenset({"codex", "claude", "opencode"})
UNSUPPORTED_CLOUD_PREFIXES = ("codex-cloud", "cursor-cloud", "jules-cloud")
RECEIPT_SCHEMA = "brigade.fleet_session_policy.v1"
MAX_RECEIPT_BYTES = 64 * 1024
_RESERVED_LAUNCHER_FLAGS = frozenset(
    {
        "--model",
        "-m",
        "--prompt",
        "-p",
        "--project",
        "--resume",
        "--config",
        "--append-system-prompt",
        "--cwd",
        "--directory",
    }
)
_CONTEXT: ContextVar["LaunchContext | None"] = ContextVar("brigade_fleet_launch_context", default=None)


class PreflightDenial(Exception):
    """Structured refusal before provider invocation."""

    def __init__(self, code: str, message: str, **fields: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = dict(fields)

    def payload(self) -> dict[str, Any]:
        body = {"ok": False, "code": self.code, "message": self.message, **self.fields}
        return body


@dataclass
class LaunchContext:
    session_id: str
    consumer: str
    provider: str
    model: str
    instance_id: str
    version: int
    digest: str
    instructions: str
    sources: dict[str, Any]
    loaded_at: str
    repo_identity: str | None = None
    origin: str = "local"
    receipt_path: Path | None = None
    applied: bool = False
    started: bool = False
    status: str = "loaded"
    error: str | None = None
    selected: dict[str, Any] = field(default_factory=dict)
    lease_id: str | None = None
    lease_holder: str | None = None
    context_hash: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_context() -> LaunchContext | None:
    return _CONTEXT.get()


def bind_context(ctx: LaunchContext | None) -> Token:
    return _CONTEXT.set(ctx)


def reset_context(token: Token) -> None:
    _CONTEXT.reset(token)


@contextmanager
def scoped_context(ctx: LaunchContext | None) -> Iterator[LaunchContext | None]:
    token = bind_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


def session_id_for(*, run_key: str, seat: str, attempt: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"brigade:fleet-session:v1:{run_key}:{seat}:{attempt}"))


def classify_enrollment(snapshot: Mapping[str, Any] | None) -> str:
    """Return enrolled, unenrolled, or denied. Errors are never unenrolled."""
    from . import fleet_model_admission

    if not isinstance(snapshot, Mapping):
        return "denied"
    state = snapshot.get("state")
    if state == "unconfigured":
        return "unenrolled"
    if state in {
        "auth-failed",
        "unavailable",
        "malformed-policy",
        "unsupported-schema",
        "node-token-required",
        "enrollment-downgrade",
    }:
        return "denied"
    if state != "authoritative":
        return "denied"
    meta, error = fleet_model_roster.parse_fleet_policy_authority(snapshot.get("fleet_policy"))
    if error is not None:
        return "denied"
    if meta is None:
        if fleet_model_admission.enrollment_activation_observed():
            return "denied"
        return "unenrolled"
    if meta.get("active") is True:
        return "enrolled"
    if fleet_model_admission.enrollment_activation_observed():
        return "denied"
    return "unenrolled"


def cloud_provider_unsupported(cli_ref: str) -> bool:
    base = cli_ref.split(":", 1)[0]
    return base.startswith(UNSUPPORTED_CLOUD_PREFIXES) or cli_ref.startswith(UNSUPPORTED_CLOUD_PREFIXES)


def _scrub_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    blocked = ("token", "secret", "password", "authorization", "bearer", "credential")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(item in lowered for item in blocked):
            continue
        if isinstance(value, dict):
            clean[key] = _scrub_receipt(value)
        else:
            clean[key] = value
    return clean


def _receipt_payload(ctx: LaunchContext) -> dict[str, Any]:
    return _scrub_receipt(
        {
            "schema": RECEIPT_SCHEMA,
            "session_id": ctx.session_id,
            "consumer": ctx.consumer,
            "provider": ctx.provider,
            "model": ctx.model,
            "instance_id": ctx.instance_id,
            "repo_identity": ctx.repo_identity,
            "origin": ctx.origin,
            "version": ctx.version,
            "digest": ctx.digest,
            "sources": ctx.sources,
            "loaded_at": ctx.loaded_at,
            "status": ctx.status,
            "applied": ctx.applied,
            "started": ctx.started,
            "error": ctx.error,
            "selected": ctx.selected,
            "context_hash": ctx.context_hash,
        }
    )


def persist_receipt(ctx: LaunchContext) -> None:
    if ctx.receipt_path is None:
        return
    payload = _receipt_payload(ctx)
    raw = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise PreflightDenial("receipt-oversized", "fleet session policy receipt exceeded the size limit")
    ctx.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="fleet-session-", suffix=".json", dir=str(ctx.receipt_path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp_name, ctx.receipt_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def receipt_dir_for(*, output_dir: Path | None, cwd: Path | None) -> Path | None:
    if output_dir is not None:
        return output_dir / "fleet-session"
    if cwd is not None:
        return cwd / ".brigade" / "fleet-session"
    return None


def _repo_identity(cwd: Path | None, explicit: str | None) -> str | None:
    if explicit:
        return fleet_session_presence.validate_repo_identity(explicit) or explicit
    if cwd is None:
        return None
    identity = fleet_session_presence.repository_identity(cwd)
    return identity.value


def _expected_launch_model(selected: Mapping[str, Any]) -> str | None:
    launch = selected.get("launch_model")
    if isinstance(launch, str) and launch:
        return launch
    canonical = selected.get("model")
    return canonical if isinstance(canonical, str) and canonical else None


def _verify_selected(prepared: Mapping[str, Any], *, provider: str, model: str, instance_id: str) -> None:
    selected = prepared.get("selected")
    if not isinstance(selected, Mapping):
        raise PreflightDenial("seat-unresolved", "fleet session prepare did not return a selected seat")
    if selected.get("provider") != provider:
        raise PreflightDenial("seat-unresolved", "selected provider does not match the launch identity")
    if selected.get("instance_id") != instance_id:
        raise PreflightDenial("seat-unresolved", "selected instance does not match the launch identity")
    if not isinstance(selected.get("seat"), str) or not selected.get("seat"):
        raise PreflightDenial("seat-unresolved", "selected seat is missing")
    selected_model = selected.get("model")
    if not isinstance(selected_model, str) or not selected_model:
        raise PreflightDenial("seat-unresolved", "selected model is missing")
    expected = _expected_launch_model(selected)
    if expected is None or model != expected:
        raise PreflightDenial("seat-unresolved", "selected model does not match the launch identity")


def _receipt_name(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"


def _require_cwd_repo(cwd: Path, repo: str, project: str | None = None) -> str:
    supplied = fleet_session_presence.validate_repo_identity(repo)
    if supplied is None:
        raise PreflightDenial("repo-unverified", "supplied repository identity is unverifiable")
    identity = fleet_session_presence.repository_identity(cwd)
    if identity.scope != "fleet" or not identity.value:
        raise PreflightDenial("repo-unverified", "checkout repository identity is unverifiable")
    if identity.value != supplied:
        raise PreflightDenial("repo-mismatch", "checkout repository does not match supplied identity")
    if project:
        try:
            project_path = Path(project).expanduser().resolve()
            checkout = cwd.expanduser().resolve()
        except OSError as exc:
            raise PreflightDenial("repo-unverified", "project path is unverifiable") from exc
        try:
            project_path.relative_to(checkout)
        except ValueError as exc:
            raise PreflightDenial("repo-mismatch", "project path is outside the verified checkout") from exc
    return supplied


def default_origin() -> str:
    """This machine's authenticated fleet identity for a policy receipt.

    Normal enrolled routing must not make the operator restate their machine,
    and must not send a literal ``"local"`` the Hub cannot map to a node. The
    configured node identity is used when one exists; a standalone checkout
    with no identity keeps the historical ``"local"`` placeholder. An explicit
    caller-supplied origin is a separate, receipted override and always wins.
    """
    node_id = resolve_node_id()
    return node_id if node_id and node_id != "unknown" else "local"


def _reject_reserved_extras(extra: Sequence[str]) -> None:
    for item in extra:
        if item == "--":
            continue
        name = item.split("=", 1)[0]
        if name in _RESERVED_LAUNCHER_FLAGS:
            raise PreflightDenial(
                "reserved-option",
                f"launcher extra {name} conflicts with the approved launch context",
            )


def prepare_session_launch(
    *,
    consumer: str,
    provider: str,
    model: str,
    instance_id: str,
    repo: str | None,
    origin: str | None,
    session_id: str,
    cwd: Path | None = None,
    output_dir: Path | None = None,
    snapshot: Mapping[str, Any] | None = None,
    explicit_enrollment: bool = False,
) -> LaunchContext:
    """Fetch effective policy, persist loaded receipt, do not mark started."""
    origin = origin or default_origin()
    if not explicit_enrollment:
        enrollment = classify_enrollment(
            snapshot if snapshot is not None else fleet_client_cloud.load_model_policy_snapshot()
        )
        if enrollment == "unenrolled":
            raise PreflightDenial("unenrolled", "fleet session is not enrolled")
        if enrollment == "denied":
            raise PreflightDenial("policy-unavailable", "enrolled fleet policy is unavailable or malformed")
    if consumer == CONSUMER_BRIGADE_RUN and not instance_id:
        raise PreflightDenial("seat-unresolved", "brigade-run requires exact bindings.brigade.cli")
    try:
        prepared = fleet_client_policy.prepare_session(
            consumer=consumer,
            repo=repo,
            session_id=session_id,
            origin=origin,
            provider=provider,
            model=model,
            instance_id=instance_id,
        )
    except FleetPolicyClientError as exc:
        raise PreflightDenial(exc.code, exc.message) from exc
    _verify_selected(prepared, provider=provider, model=model, instance_id=instance_id)
    version = prepared.get("version")
    digest = prepared.get("digest")
    instructions = prepared.get("instructions")
    if type(version) is not int or version <= 0 or not isinstance(instructions, str):
        raise PreflightDenial("invalid-request", "fleet session prepare returned an unusable policy envelope")
    if not isinstance(digest, str) or fleet_model_roster.SHA256_DIGEST_PATTERN.fullmatch(digest) is None:
        raise PreflightDenial("invalid-request", "fleet session prepare returned an unusable policy envelope")
    context_hash = prepared.get("context_hash")
    if not isinstance(context_hash, str) or fleet_model_roster.SHA256_DIGEST_PATTERN.fullmatch(context_hash) is None:
        raise PreflightDenial("invalid-request", "fleet session prepare returned an unusable policy envelope")
    raw_sources = prepared.get("sources")
    sources = dict(raw_sources) if isinstance(raw_sources, dict) else {}
    selected_raw = prepared.get("selected")
    selected = dict(selected_raw) if isinstance(selected_raw, dict) else {}
    directory = receipt_dir_for(output_dir=output_dir, cwd=cwd)
    receipt_path = directory / _receipt_name(session_id) if directory is not None else None
    ctx = LaunchContext(
        session_id=session_id,
        consumer=consumer,
        provider=provider,
        model=model,
        instance_id=instance_id,
        version=version,
        digest=digest,
        instructions=instructions,
        sources=sources,
        loaded_at=utc_now(),
        repo_identity=repo,
        origin=origin,
        receipt_path=receipt_path,
        selected=selected,
        status="loaded",
        context_hash=context_hash,
    )
    persist_receipt(ctx)
    return ctx


def apply_instructions(prompt: str, ctx: LaunchContext) -> str:
    block = ctx.instructions.strip()
    if not block:
        return prompt
    if block in prompt:
        return prompt
    if not prompt:
        return block
    return f"{prompt.rstrip()}\n\n{block}"


def _ack_matches(body: Mapping[str, Any], ctx: LaunchContext) -> bool:
    from . import fleet_policy

    if body.get("schema") != fleet_policy.POLICY_SESSION_SCHEMA:
        return False
    if body.get("applied") is not True:
        return False
    if body.get("state") != "current":
        return False
    if body.get("consumer") != ctx.consumer:
        return False
    if body.get("session_id") != ctx.session_id:
        return False
    if body.get("version") != ctx.version:
        return False
    if body.get("digest") != ctx.digest:
        return False
    if ctx.repo_identity is not None and body.get("repo_identity") != ctx.repo_identity:
        return False
    from .fleet_hub_policy_api import ACK_PUBLIC_KEYS

    unknown = set(body) - set(ACK_PUBLIC_KEYS)
    if unknown:
        return False
    if ctx.context_hash is not None:
        if body.get("context_hash") != ctx.context_hash:
            return False
    return True


def acknowledge_launch(ctx: LaunchContext) -> None:
    if ctx.applied:
        return
    try:
        body = fleet_client_policy.acknowledge_session(
            session_id=ctx.session_id,
            consumer=ctx.consumer,
            repo=ctx.repo_identity,
            version=ctx.version,
            digest=ctx.digest,
            status="applied",
            context_hash=ctx.context_hash,
        )
    except FleetPolicyClientError as exc:
        ctx.status = "failed"
        ctx.error = exc.message
        persist_receipt(ctx)
        raise PreflightDenial(exc.code, exc.message) from exc
    if not isinstance(body, Mapping) or not _ack_matches(body, ctx):
        ctx.status = "failed"
        ctx.error = "acknowledgement receipt did not match the prepared session"
        persist_receipt(ctx)
        raise PreflightDenial("invalid-request", ctx.error)
    ctx.applied = True
    ctx.status = "applied"
    persist_receipt(ctx)


def authorize_launch(ctx: LaunchContext, *, cli_ref: str, model: str) -> None:
    """Admit phase=launch and acquire the model lease after acknowledgement."""
    from . import fleet_client, fleet_model_admission

    if ctx.lease_id:
        return
    if not ctx.applied:
        raise PreflightDenial("not-applied", "fleet session cannot lease before acknowledgement")
    if not ctx.context_hash:
        raise PreflightDenial("not-applied", "fleet session cannot lease without an acknowledged context hash")
    seat = str(ctx.selected.get("seat") or "")
    if not seat:
        raise PreflightDenial("seat-unresolved", "acknowledged launch is missing the selected seat")
    canonical = str(ctx.selected.get("model") or "")
    if not canonical:
        raise PreflightDenial("seat-unresolved", "acknowledged launch is missing the canonical model")
    expected_launch = _expected_launch_model(ctx.selected)
    if expected_launch is not None and model != expected_launch and model != canonical:
        raise PreflightDenial("seat-unresolved", "lease model does not match the acknowledged launch identity")
    decision = fleet_model_admission.admit_model(
        consumer=ctx.consumer,
        request_id=ctx.session_id,
        phase="launch",
        seat=seat,
        policy_session_id=ctx.session_id,
        policy_version=ctx.version,
        policy_digest=ctx.digest,
        repo_identity=ctx.repo_identity,
        policy_context_hash=ctx.context_hash,
        allow_lkg=False,
    )
    if not decision.ok:
        raise PreflightDenial(decision.reason, str(decision.payload.get("error") or decision.reason))
    lease = fleet_client.acquire_model_lease(
        seat,
        ctx.provider,
        canonical,
        policy_session_id=ctx.session_id,
        policy_version=ctx.version,
        policy_digest=ctx.digest,
        request_id=ctx.session_id,
        repo_identity=ctx.repo_identity,
        launch_model=expected_launch if expected_launch and expected_launch != canonical else None,
        policy_context_hash=ctx.context_hash,
        consumer=ctx.consumer,
    )
    if not lease.granted:
        raise PreflightDenial("lease-denied", lease.reason)
    ctx.lease_id = lease.lease_id
    ctx.lease_holder = lease.holder
    _ = cli_ref


def mark_started(ctx: LaunchContext) -> None:
    if not ctx.applied:
        raise PreflightDenial("not-applied", "fleet session cannot start before acknowledgement")
    ctx.started = True
    ctx.status = "started"
    persist_receipt(ctx)


def mark_failed(ctx: LaunchContext, message: str) -> None:
    ctx.status = "failed"
    ctx.started = False
    ctx.error = message
    persist_receipt(ctx)


def ensure_prompt(
    prompt: str,
    *,
    cli_ref: str,
    model: str | None,
    cwd: Path | None,
    consumer: str = CONSUMER_BRIGADE_RUN,
    origin: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
    output_dir: Path | None = None,
) -> str:
    """Apply enrolled policy to a concrete prompt. Unenrolled is a no-network no-op."""
    ctx = current_context()
    if ctx is not None:
        if cloud_provider_unsupported(cli_ref):
            raise PreflightDenial(
                "unsupported",
                f"enrolled fleet sessions do not support {cli_ref}; exact model injection is not guaranteed",
            )
        from . import agents as agents_mod

        mapped = agents_mod.model_policy_provider(cli_ref)
        if mapped != ctx.provider and cli_ref != ctx.instance_id:
            raise PreflightDenial(
                "seat-unresolved",
                "bound launch context does not match the selected CLI identity",
            )
        expected = _expected_launch_model(ctx.selected)
        if model and expected is not None and model != expected and model != ctx.selected.get("model"):
            raise PreflightDenial(
                "seat-unresolved",
                "bound launch context does not match the selected model identity",
            )
        applied = apply_instructions(prompt, ctx)
        acknowledge_launch(ctx)
        authorize_launch(ctx, cli_ref=cli_ref, model=model or expected or ctx.model)
        return applied
    loaded = snapshot if snapshot is not None else fleet_client_cloud.load_model_policy_snapshot()
    enrollment = classify_enrollment(loaded)
    if enrollment == "unenrolled":
        return prompt
    if enrollment == "denied":
        raise PreflightDenial("policy-unavailable", "enrolled fleet policy is unavailable or malformed")
    if cloud_provider_unsupported(cli_ref):
        raise PreflightDenial(
            "unsupported",
            f"enrolled fleet sessions do not support {cli_ref}; exact model injection is not guaranteed",
        )
    provider = cli_ref.split(":", 1)[0]
    instance_id = cli_ref
    launch_model = model or ""
    if not launch_model:
        raise PreflightDenial("seat-unresolved", "enrolled launch requires an exact model identity")
    repo = _repo_identity(cwd, None)
    session_id = str(uuid4())
    prepared = prepare_session_launch(
        consumer=consumer,
        provider=provider,
        model=launch_model,
        instance_id=instance_id,
        repo=repo,
        origin=origin,
        session_id=session_id,
        cwd=cwd,
        output_dir=output_dir,
        snapshot=loaded,
    )
    applied = apply_instructions(prompt, prepared)
    acknowledge_launch(prepared)
    authorize_launch(prepared, cli_ref=cli_ref, model=launch_model)
    return applied


def prepare_bound_launch(
    *,
    agent: Any,
    snapshot: Mapping[str, Any] | None,
    cwd: Path | None,
    output_dir: Path | None,
    run_key: str,
    attempt: int,
    origin: str | None = None,
) -> LaunchContext | None:
    enrollment = classify_enrollment(snapshot)
    if enrollment == "unenrolled":
        return None
    if enrollment == "denied":
        raise PreflightDenial("policy-unavailable", "enrolled fleet policy is unavailable or malformed")
    cli_ref = str(getattr(agent, "cli", None) or "")
    if cloud_provider_unsupported(cli_ref):
        raise PreflightDenial(
            "unsupported",
            f"enrolled fleet sessions do not support {cli_ref}; exact model injection is not guaranteed",
        )
    from . import agents as agents_mod

    provider = agents_mod.model_policy_provider(cli_ref)
    model = str(getattr(agent, "model", None) or "")
    instance_id = cli_ref
    if not model or not instance_id:
        raise PreflightDenial("seat-unresolved", "enrolled launch requires exact provider/model/instance identity")
    repo = _repo_identity(cwd, None)
    if cwd is not None:
        if repo is None:
            raise PreflightDenial("repo-unverified", "checkout repository identity is unverifiable")
        repo = _require_cwd_repo(cwd, repo)
    session_id = session_id_for(run_key=run_key, seat=str(getattr(agent, "name", "seat")), attempt=attempt)
    return prepare_session_launch(
        consumer=CONSUMER_BRIGADE_RUN,
        provider=provider,
        model=model,
        instance_id=instance_id,
        repo=repo,
        origin=origin,
        session_id=session_id,
        cwd=cwd,
        output_dir=output_dir,
        snapshot=snapshot,
    )


def _launcher_argv(harness: str, *, prompt: str, model: str, extra: Sequence[str], project: str | None) -> list[str]:
    extra_args = [item for item in extra if item != "--"]
    if harness == "codex":
        argv = ["codex"]
        if model:
            argv.extend(["--model", model])
        argv.extend(extra_args)
        if prompt:
            argv.append(prompt)
        return argv
    if harness == "claude":
        argv = ["claude"]
        if prompt:
            argv.extend(["--append-system-prompt", prompt])
        if model:
            argv.extend(["--model", model])
        argv.extend(extra_args)
        return argv
    argv = ["opencode"]
    if project:
        argv.append(project)
    if model:
        argv.extend(["--model", model])
    if prompt:
        argv.extend(["--prompt", prompt])
    argv.extend(extra_args)
    return argv


def launch_enrolled(
    *,
    harness: str,
    consumer: str,
    provider: str,
    model: str,
    instance_id: str,
    repo: str,
    origin: str | None,
    prompt: str = "",
    extra: Sequence[str] = (),
    cwd: Path | None = None,
    project: str | None = None,
    session_id: str | None = None,
    spawn: Any = subprocess.Popen,
) -> int:
    if harness not in LAUNCHER_HARNESSES:
        raise PreflightDenial("unsupported", f"enrolled launcher supports {sorted(LAUNCHER_HARNESSES)}, not {harness}")
    from . import agents as agents_mod

    mapped_provider = agents_mod.model_policy_provider(harness)
    if mapped_provider != provider and harness != instance_id:
        raise PreflightDenial(
            "seat-unresolved",
            "launcher harness does not match the registered provider identity",
        )
    cwd = cwd.expanduser().resolve() if cwd is not None else Path.cwd()
    _reject_reserved_extras(extra)
    repo_id = _require_cwd_repo(cwd, repo, project)
    ctx = prepare_session_launch(
        consumer=consumer,
        provider=provider,
        model=model,
        instance_id=instance_id,
        repo=repo_id,
        origin=origin,
        session_id=session_id or str(uuid4()),
        cwd=cwd,
        explicit_enrollment=True,
    )
    if ctx.provider != provider or ctx.instance_id != instance_id:
        raise PreflightDenial("seat-unresolved", "prepared launch identity does not match the launcher")
    expected = _expected_launch_model(ctx.selected)
    if expected is not None and model != expected:
        raise PreflightDenial("seat-unresolved", "prepared launch model does not match the launcher")
    applied_prompt = apply_instructions(prompt, ctx)
    acknowledge_launch(ctx)
    authorize_launch(ctx, cli_ref=instance_id, model=model)
    argv = _launcher_argv(harness, prompt=applied_prompt, model=model, extra=extra, project=project or str(cwd))
    try:
        proc = spawn(argv, cwd=str(cwd))
    except Exception as exc:
        mark_failed(ctx, f"spawn failed: {exc}")
        raise PreflightDenial("spawn-failed", f"provider process failed to start: {exc}") from exc
    mark_started(ctx)
    return int(proc.wait()) if hasattr(proc, "wait") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brigade.fleet_session_bootstrap")
    parser.add_argument("--harness", required=True, choices=sorted(LAUNCHER_HARNESSES))
    parser.add_argument("--consumer", default=CONSUMER_BRIGADE_RUN)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--origin", default=None)
    parser.add_argument("--cwd", type=Path, default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--prompt", default="")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        return launch_enrolled(
            harness=args.harness,
            consumer=args.consumer,
            provider=args.provider,
            model=args.model,
            instance_id=args.instance_id,
            repo=args.repo,
            origin=args.origin,
            prompt=args.prompt,
            extra=args.extra,
            cwd=args.cwd,
            project=args.project,
            session_id=args.session_id,
        )
    except PreflightDenial as exc:
        print(json.dumps(exc.payload(), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
