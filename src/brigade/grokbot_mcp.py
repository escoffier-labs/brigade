"""Role-scoped Streamable HTTP adapter for the private Grok Bot queue.

The adapter deliberately delegates all queue authority to :mod:`grokbot_jobs`.
It contains no queue persistence and never accepts a caller-selected worker
identity, target, or role. A claim may request a lease length, but only inside
the queue's own bound, and the listener's configured lease is the default.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import cloud_tracker, fleet_client, grokbot_job_validation, grokbot_jobs


DEFAULT_BIND = "127.0.0.1:8766"
MAX_REQUEST_BYTES = 80_000
# A Grok Bot routine that spawns a cloud agent routinely goes minutes between
# tool calls, so the default lease has to outlast one agent step (issue #1383).
# The bound is the queue's own, so a listener can never hand out a lease the
# queue or the hub would refuse.
DEFAULT_LEASE_SECONDS = 900
MIN_LEASE_SECONDS = grokbot_job_validation.LEASE_SECONDS_MIN
MAX_LEASE_SECONDS = grokbot_job_validation.LEASE_SECONDS_MAX
LEASE_SECONDS_ENV = "BRIGADE_GROKBOT_LEASE_SECONDS"
MAX_LISTED_JOBS = 100
MAX_LIST_LIMIT = 100
LIST_ARGUMENT_KEYS = ("include_all", "limit", "role", "state")
JOURNAL_LOGGER_NAME = __name__
_JOURNAL_HANDLER_NAME = "brigade-grokbot-journal"
_JOURNAL = logging.getLogger(JOURNAL_LOGGER_NAME)
SDK_LOOPBACK_ALLOWED_HOSTS = ("localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "::1", "[::1]:*")
INSTANCES = frozenset({"operator", "repository-scout", "implementation-worker"})
_FLEET_HOLDER_DOMAIN = b"brigade.grokbot.fleet-holder"
_FLEET_SESSION_DOMAIN = b"brigade.grokbot.fleet-session"
_FLEET_BEST_EFFORT = frozenset({"no-hub", "no-identity", "hub-unavailable"})
ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_DIRECT_QUEUE_HUB_TOKEN_FILE_ENV = "BRIGADE_GROKBOT_HUB_TOKEN_FILE"
_DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES = 4098
_FEED_HUB_TOKEN_FILE_ENV = "BRIGADE_GROKBOT_FEED_HUB_TOKEN_FILE"

OPERATOR_TOOLS = frozenset(
    {
        "grokbot_queue_list",
        "grokbot_queue_status",
        "grokbot_queue_cancel",
        "grokbot_queue_expire",
        "grokbot_queue_report",
    }
)
WORKER_TOOLS = frozenset(
    {
        "grokbot_queue_list",
        "grokbot_queue_status",
        "grokbot_queue_claim",
        "grokbot_queue_renew",
        "grokbot_queue_start",
        "grokbot_queue_complete",
        "grokbot_queue_fail",
        "grokbot_queue_ack_cancel",
    }
)


class ConfigurationError(ValueError):
    """Configuration was unsafe or incomplete. Details never reach clients."""


class OptionalDependencyError(RuntimeError):
    """The listener's optional MCP runtime is not installed."""


class AdapterError(ValueError):
    """Stable public error for every invalid or unauthorized tool request.

    ``reason`` is only ever one of this module's own bounded argument-shape
    sentences. Queue authority, job existence, and role refusals stay on the
    generic message so a hidden tool cannot be told apart from a real one.
    """

    def __init__(self, reason: str | None = None):
        super().__init__(reason or "invalid")
        self.reason = reason

    def public_error(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": "invalid_request", "message": self.reason or "Tool input failed validation"}}


@dataclass(frozen=True)
class ListenerConfig:
    """Non-secret listener configuration plus the already-resolved bearer."""

    target: Path
    instance: str
    bind_host: str
    bind_port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    bearer: str
    hub_token: str | None = None
    lease_seconds: int = DEFAULT_LEASE_SECONDS

    def validate(self) -> None:
        if self.instance not in INSTANCES or not self.target.is_dir():
            raise ConfigurationError("invalid")
        if not _within_lease_bound(self.lease_seconds):
            raise ConfigurationError("invalid")
        if not isinstance(self.bearer, str):
            raise ConfigurationError("invalid")
        _validate_bearer(self.bearer)
        if not 1 <= self.bind_port <= 65_535:
            raise ConfigurationError("invalid")
        if any(not _valid_host(host) for host in self.allowed_hosts):
            raise ConfigurationError("invalid")
        if any(not _valid_origin(origin) for origin in self.allowed_origins):
            raise ConfigurationError("invalid")
        if not _is_loopback(self.bind_host) and (not self.allowed_hosts or not self.allowed_origins):
            raise ConfigurationError("invalid")

    @property
    def bot_id(self) -> str:
        """The identity is deployment-fixed and never taken from tool input."""
        return f"grokbot-{self.instance}"


def tools_for_instance(instance: str) -> frozenset[str]:
    """Return the exact runtime allowlist for one packaged queue role."""
    if instance == "operator":
        return OPERATOR_TOOLS
    if instance in INSTANCES:
        return WORKER_TOOLS
    raise ConfigurationError("invalid")


def parse_bind(value: str) -> tuple[str, int]:
    """Parse a deliberately small host:port surface without IPv6 ambiguity."""
    if not isinstance(value, str) or value.count(":") != 1:
        raise ConfigurationError("invalid")
    host, port_text = value.rsplit(":", 1)
    if not host or not port_text.isdecimal():
        raise ConfigurationError("invalid")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise ConfigurationError("invalid")
    return host, port


def load_bearer(*, bearer_file: Path | None, bearer_env: str | None) -> str:
    """Resolve exactly one bearer reference without exposing its value or path."""
    if (bearer_file is None) == (bearer_env is None):
        raise ConfigurationError("invalid")
    if bearer_env is not None:
        if not ENVIRONMENT_NAME_RE.fullmatch(bearer_env):
            raise ConfigurationError("invalid")
        value = os.environ.get(bearer_env)
        if value is None:
            raise ConfigurationError("invalid")
        return _validate_bearer(value)

    assert bearer_file is not None
    return _read_mode600_secret(bearer_file)


def load_hub_token(*, instance: str) -> str | None:
    """Load this listener's Fleet Hub node token from a mode-0600 file path.

    The path comes from a narrowly scoped environment variable, never argv or
    public MCP arguments. Ordinary Brigade processes keep using fleet.toml.
    """
    names = (
        f"BRIGADE_GROKBOT_{instance.replace('-', '_').upper()}_HUB_TOKEN_FILE",
        _DIRECT_QUEUE_HUB_TOKEN_FILE_ENV,
    )
    path_text = next((os.environ.get(name) for name in names if os.environ.get(name)), None)
    if path_text is None:
        return None
    return _read_mode600_secret(Path(path_text))


def load_lease_seconds(*, instance: str) -> int:
    """Resolve one listener's lease length, per instance and then fleet-wide.

    ``BRIGADE_GROKBOT_<INSTANCE>_LEASE_SECONDS`` sets the length for a single
    packaged role; ``BRIGADE_GROKBOT_LEASE_SECONDS`` sets it for every role on
    the host. An unset pair means the 15-minute default, and anything outside
    the queue's own bound is a configuration failure rather than a clamp.
    """
    names = (f"BRIGADE_GROKBOT_{instance.replace('-', '_').upper()}_LEASE_SECONDS", LEASE_SECONDS_ENV)
    text = next((os.environ[name] for name in names if os.environ.get(name)), None)
    if text is None:
        return DEFAULT_LEASE_SECONDS
    seconds = int(text) if text.strip().isdecimal() else None
    if seconds is None or not _within_lease_bound(seconds):
        raise ConfigurationError("invalid")
    return seconds


def _within_lease_bound(value: object) -> bool:
    """The listener never widens the queue's own lease bound."""
    return type(value) is int and MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS


def load_direct_queue_listener_token() -> str | None:
    """Load only the generic direct-queue listener token from a safe file."""
    path_text = os.environ.get(_DIRECT_QUEUE_HUB_TOKEN_FILE_ENV)
    if path_text is None:
        return None
    return _read_descriptor_safe_token_file(Path(path_text))


def load_feed_hub_token() -> str | None:
    """Load the feed actor's dedicated Fleet Hub token without fallback."""
    path_text = os.environ.get(_FEED_HUB_TOKEN_FILE_ENV)
    if path_text is None:
        return None
    return _read_descriptor_safe_token_file(Path(path_text))


def load_hub_token_file(path: Path) -> str:
    """Load one configured Fleet Hub listener token from a safe file."""
    return _read_descriptor_safe_token_file(path)


def _read_descriptor_safe_token_file(path: Path) -> str:
    """Read one owner-private token through a no-follow descriptor."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    owner = getattr(os, "getuid", None)
    if not path.is_absolute() or not isinstance(no_follow, int) or no_follow == 0 or not callable(owner):
        raise ConfigurationError("invalid")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES
        ):
            raise ConfigurationError("invalid")
        chunks: list[bytes] = []
        remaining = _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES:
            raise ConfigurationError("invalid")
        return _validate_bearer(data.decode("utf-8").rstrip("\r\n"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise ConfigurationError("invalid") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise ConfigurationError("invalid") from None


def _read_mode600_secret(path: Path) -> str:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigurationError("invalid")
        return _validate_bearer(path.read_text(encoding="utf-8").rstrip("\r\n"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigurationError("invalid") from exc


def _validate_bearer(value: str) -> str:
    if not value or len(value) > 4096 or "\x00" in value or not value.isascii():
        raise ConfigurationError("invalid")
    return value


class GrokbotAdapter:
    """Thin role boundary over the Brigade-owned private queue."""

    def __init__(self, config: ListenerConfig):
        config.validate()
        self.config = config
        self._hub_actor_verified = False

    def ensure_hub_actor(self) -> None:
        """Fail closed when the listener's hub credential does not match this instance."""
        if self._hub_actor_verified:
            return
        if not grokbot_jobs.hub_authority(self.config.target):
            self._hub_actor_verified = True
            return
        from . import fleet_client_grokbot

        if not self.config.hub_token:
            raise ConfigurationError("invalid")
        with fleet_client_grokbot.listener_identity(self.config.hub_token):
            decision = fleet_client_grokbot.whoami()
        job = decision.job if decision.granted else None
        kind = job.get("actor_kind") if isinstance(job, dict) else None
        role = job.get("role") if isinstance(job, dict) else None
        if kind != self.config.instance:
            raise ConfigurationError("invalid")
        if self.config.instance != "operator" and role not in (None, self.config.instance):
            raise ConfigurationError("invalid")
        self._hub_actor_verified = True

    def tool_inventory(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": tool_description(name, self.config.instance, self.config.lease_seconds)}
            for name in sorted(self._tools())
        ]

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        bearer = authorization[7:]
        if not bearer.isascii():
            return False
        return hmac.compare_digest(bearer, self.config.bearer)

    def health_payload(self) -> dict[str, object]:
        return {"ok": True, "service": "grokbot-mcp", "role": self.config.instance}

    def call_tool(self, name: str, arguments: object) -> dict[str, Any]:
        """Execute one exposed tool with fixed authority and safe failures."""
        if name not in self._tools() or not isinstance(arguments, dict):
            journal_tool_call(name, arguments, "refused", None)
            raise AdapterError()
        arguments = _without_null_optionals(name, arguments)
        try:
            result = self._call(name, arguments)
        except AdapterError as exc:
            journal_tool_call(name, arguments, "refused", exc.reason)
            raise AdapterError(exc.reason) from None
        except (grokbot_jobs.GrokbotJobError, ValueError, OSError):
            journal_tool_call(name, arguments, "refused", None)
            raise AdapterError() from None
        journal_tool_call(name, arguments, "ok", None)
        return result

    def _tools(self) -> frozenset[str]:
        return tools_for_instance(self.config.instance)

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if grokbot_jobs.hub_authority(self.config.target):
            from . import fleet_client_grokbot

            self.ensure_hub_actor()
            with fleet_client_grokbot.listener_identity(self.config.hub_token):
                return self._dispatch(name, arguments)
        return self._dispatch(name, arguments)

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "grokbot_queue_list":
            options = list_options(arguments, self.config.instance)
            jobs = grokbot_jobs.status(self.config.target)["jobs"]
            if self.config.instance != "operator":
                jobs = [job for job in jobs if job["role"] == self.config.instance]
            if not options.include_all:
                jobs = [job for job in jobs if job["state"] not in grokbot_jobs.TERMINAL_STATES]
            if options.state is not None:
                jobs = [job for job in jobs if job["state"] == options.state]
            return {"jobs": jobs[: options.limit]}
        if name == "grokbot_queue_status":
            job = self._eligible_job(arguments)
            return job
        if name in {"grokbot_queue_cancel", "grokbot_queue_expire"}:
            job_id = _job_id(arguments)
            operation = grokbot_jobs.cancel if name.endswith("cancel") else grokbot_jobs.expire
            return operation(self.config.target, job_id)
        if name == "grokbot_queue_report":
            job_id = _job_id(arguments)
            return grokbot_jobs.read_report(self.config.target, job_id)
        if name == "grokbot_queue_claim":
            job = self._eligible_job(arguments, allowed={"job_id"}, optional={"lease_id", "lease_seconds"})
            lease_id = _lease_id(arguments, mint=True)
            lease_seconds = self._requested_lease_seconds(arguments)
            if grokbot_jobs.hub_authority(self.config.target):
                return self._granted_lease(
                    name,
                    self._hub_lifecycle(
                        grokbot_jobs.claim_execution_context,
                        job["job_id"],
                        self.config.bot_id,
                        lease_id,
                        lease_seconds,
                    ),
                    lease_id,
                    lease_seconds,
                )
            holder, session = fleet_holder(job["job_id"], lease_id), fleet_session(job["job_id"], lease_id)
            cloud_decision = self._admit_hub_lease_decision(job, lease_id, lease_seconds)
            if _cloud_refused(cloud_decision):
                raise AdapterError()
            decision = self._fleet_acquire(job["job_id"], holder, session, lease_seconds)
            if _fleet_refused(decision):
                if cloud_decision is not None and cloud_decision.granted:
                    self._release_hub_lease(job["job_id"], lease_id, "released")
                raise AdapterError()
            try:
                result = grokbot_jobs.claim_execution_context(
                    self.config.target, job["job_id"], self.config.bot_id, lease_id, lease_seconds
                )
            except Exception:
                self._release_hub_lease(job["job_id"], lease_id, "released")
                if decision.granted:
                    self._fleet_release(holder)
                raise
            self._bind_hub_lease(job["job_id"], lease_id)
            self._fleet_event(job["job_id"], session, "external.claimed")
            return self._granted_lease(name, result, lease_id, lease_seconds)
        if name == "grokbot_queue_renew":
            job = self._eligible_job(arguments, allowed={"job_id", "lease_id"})
            lease_id = _lease_id(arguments)
            job_id = job["job_id"]
            lease_seconds = self.config.lease_seconds
            if grokbot_jobs.hub_authority(self.config.target):
                return self._granted_lease(
                    name,
                    self._lease_call(
                        job_id,
                        self._hub_lifecycle,
                        grokbot_jobs.renew,
                        job_id,
                        self.config.bot_id,
                        lease_id,
                        lease_seconds,
                    ),
                    None,
                    lease_seconds,
                )
            holder, session = fleet_holder(job_id, lease_id), fleet_session(job_id, lease_id)
            decision = self._fleet_renew(holder, lease_seconds)
            if not decision.granted and decision.reason == "missing":
                decision = self._fleet_acquire(job_id, holder, session, lease_seconds)
            if _fleet_refused(decision):
                raise AdapterError()
            try:
                result = self._lease_call(
                    job_id, grokbot_jobs.renew, self.config.target, job_id, self.config.bot_id, lease_id, lease_seconds
                )
            except Exception:
                if decision.granted:
                    self._fleet_release(holder)
                raise
            self._renew_or_reconcile_hub_lease(result, lease_id)
            self._fleet_event(job_id, session, "external.heartbeat")
            return self._granted_lease(name, result, None, lease_seconds)
        if name in {"grokbot_queue_start", "grokbot_queue_fail", "grokbot_queue_ack_cancel"}:
            job_id, lease_id = _job_id(arguments, {"job_id", "lease_id"}), _lease_id(arguments)
            if grokbot_jobs.hub_authority(self.config.target):
                if name.endswith("start"):
                    return self._hub_lifecycle(grokbot_jobs.transition, job_id, self.config.bot_id, lease_id, "running")
                if name.endswith("fail"):
                    return self._lease_call(
                        job_id,
                        self._hub_lifecycle,
                        grokbot_jobs.transition,
                        job_id,
                        self.config.bot_id,
                        lease_id,
                        "failed",
                    )
                return self._hub_lifecycle(grokbot_jobs.acknowledge_cancel, job_id, self.config.bot_id, lease_id)
            holder, session = fleet_holder(job_id, lease_id), fleet_session(job_id, lease_id)
            if name.endswith("start"):
                result = grokbot_jobs.transition(self.config.target, job_id, self.config.bot_id, lease_id, "running")
                self._renew_or_reconcile_hub_lease(result, lease_id)
                self._fleet_event(job_id, session, "external.running")
                return result
            if name.endswith("fail"):
                result = self._lease_call(
                    job_id,
                    grokbot_jobs.transition,
                    self.config.target,
                    job_id,
                    self.config.bot_id,
                    lease_id,
                    "failed",
                )
                self._fleet_release(holder)
                self._fleet_event(job_id, session, "external.failed")
                self._release_hub_lease(job_id, lease_id, result["state"])
                return result
            result = grokbot_jobs.acknowledge_cancel(self.config.target, job_id, self.config.bot_id, lease_id)
            self._fleet_release(holder)
            self._fleet_event(job_id, session, "external.canceled")
            self._release_hub_lease(job_id, lease_id, result["state"])
            return result
        if name == "grokbot_queue_complete":
            allowed = {"job_id", "lease_id", "artifact"}
            has_report = "report_text" in arguments
            if has_report:
                allowed = allowed | {"report_text"}
            job = self._eligible_job(arguments, allowed=allowed)
            artifact = arguments.get("artifact")
            if not isinstance(artifact, dict):
                raise AdapterError()
            job_id = job["job_id"]
            lease_id = _lease_id(arguments)
            report_text = arguments.get("report_text")
            if has_report and not isinstance(report_text, str):
                raise AdapterError()
            if grokbot_jobs.hub_authority(self.config.target):
                if has_report:
                    assert isinstance(report_text, str)
                    return self._hub_lifecycle(
                        grokbot_jobs.complete_report,
                        job_id,
                        self.config.bot_id,
                        lease_id,
                        artifact,
                        report_text,
                    )
                return self._hub_lifecycle(
                    grokbot_jobs.transition,
                    job_id,
                    self.config.bot_id,
                    lease_id,
                    "completed",
                    artifact=artifact,
                )
            holder, session = fleet_holder(job_id, lease_id), fleet_session(job_id, lease_id)
            if has_report:
                assert isinstance(report_text, str)
                result = grokbot_jobs.complete_report(
                    self.config.target,
                    job_id,
                    self.config.bot_id,
                    lease_id,
                    artifact,
                    report_text,
                )
            else:
                result = grokbot_jobs.transition(
                    self.config.target,
                    job_id,
                    self.config.bot_id,
                    lease_id,
                    "completed",
                    artifact=artifact,
                )
            self._fleet_event(job_id, session, "external.completed")
            self._fleet_release(holder)
            self._release_hub_lease(job_id, lease_id, result["state"])
            return result
        raise AdapterError()

    def _hub_lifecycle(self, operation: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Run one hub-authoritative queue mutation with this listener's node token."""
        return operation(self.config.target, *args, **kwargs)

    def _requested_lease_seconds(self, arguments: Mapping[str, Any]) -> int:
        """Grant the caller's requested lease, or this listener's configured one."""
        requested = arguments.get("lease_seconds")
        if requested is None:
            return self.config.lease_seconds
        if not _within_lease_bound(requested):
            raise AdapterError(f"lease_seconds must be an integer between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}")
        assert isinstance(requested, int)
        return requested

    def _granted_lease(
        self, tool: str, result: dict[str, Any], lease_id: str | None, lease_seconds: int
    ) -> dict[str, Any]:
        """Answer with the granted lease and journal the window it opened."""
        granted = {**result, "lease_seconds": lease_seconds}
        if lease_id is not None:
            granted["lease_id"] = lease_id
        journal_lease(tool, lease_seconds, granted.get("lease_expires_at"))
        return granted

    def _lease_call(self, job_id: str, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one lease-holder mutation, naming an expired lease as such.

        A lapsed lease is an ordinary, recoverable state, not a malformed
        request. Folding it into the generic validation error is what makes a
        Bot read the listener as a broken adapter (issue #1383).
        """
        try:
            return operation(*args, **kwargs)
        except grokbot_jobs.GrokbotJobError as exc:
            if self._lease_lapsed(job_id, exc.reason):
                raise AdapterError("lease-expired") from None
            raise

    def _lease_lapsed(self, job_id: str, reason: str | None) -> bool:
        """True when the queue refused because this job's lease deadline passed.

        Hub authority answers every holder refusal with ``lease-conflict``, so
        an expiry is confirmed against the job's own deadline rather than by
        widening the hub's error contract. A job another worker has since
        re-claimed carries a live deadline and stays a conflict.
        """
        if reason == "lease-expired":
            return True
        if reason != "lease-conflict":
            return False
        try:
            deadline = grokbot_jobs.get_job(self.config.target, job_id).get("lease_expires_at")
            return isinstance(deadline, str) and _parse_deadline(deadline) <= datetime.now(timezone.utc)
        except Exception:
            return False

    def _admit_hub_lease_decision(
        self, job: Mapping[str, Any], holder: str, lease_seconds: int | None = None
    ) -> fleet_client.CloudDecision | None:
        """Ask the hub for one deterministic GrokBot cloud lease."""
        task_hash = job.get("task_hash")
        repository = job.get("repository")
        job_id = job.get("job_id")
        if not isinstance(task_hash, str) or not isinstance(repository, str) or not isinstance(job_id, str):
            return fleet_client.CloudDecision(False, "invalid")
        prompt_hash = task_hash.removeprefix("sha256:")
        return self._hub_call(
            fleet_client.admit_cloud,
            "grokbot-cloud",
            repo=repository,
            label=cloud_tracker.lease_label("grokbot-cloud", repository, prompt_hash),
            prompt_hash=prompt_hash,
            conductor=self.config.instance,
            ttl_seconds=lease_seconds if lease_seconds is not None else self.config.lease_seconds,
            lease_id=job_id,
            holder=holder,
        )

    def _admit_hub_lease(self, job: Mapping[str, Any], holder: str) -> bool:
        """True only when the hub granted the separate GrokBot cloud lease."""
        decision = self._admit_hub_lease_decision(job, holder, self.config.lease_seconds)
        return bool(decision is not None and decision.granted)

    def _bind_hub_lease(self, job_id: str, holder: str) -> None:
        """Associate the deterministic fleet lease with its GrokBot job."""
        self._hub_call(fleet_client.bind_cloud, job_id, provider_task_id=job_id, holder=holder)

    def _renew_or_reconcile_hub_lease(self, job: Mapping[str, Any], holder: str) -> None:
        """Mirror a live local lease, recreating only a definitively refused lease."""
        job_id = job.get("job_id")
        if not isinstance(job_id, str):
            return
        decision = self._hub_call(
            fleet_client.renew_cloud, job_id, ttl_seconds=self.config.lease_seconds, holder=holder
        )
        if decision is None or decision.granted or decision.reason != "refused":
            return
        if self._admit_hub_lease(job, holder):
            self._bind_hub_lease(job_id, holder)

    def _release_hub_lease(self, job_id: str, holder: str, state: str) -> None:
        """Best-effort terminal mirror that cannot roll back a queue transition."""
        self._hub_call(fleet_client.release_cloud, job_id, state=state, holder=holder)

    @staticmethod
    def _hub_call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> fleet_client.CloudDecision | None:
        """Contain hub failures at the mirror boundary without retaining details."""
        try:
            result = operation(*args, **kwargs)
        except Exception:
            return None
        return result if isinstance(result, fleet_client.CloudDecision) else None

    def _fleet_target(self) -> str:
        return fleet_client.resolve_claim_target(self.config.target)

    def _fleet_acquire(
        self, job_id: str, holder: str, session: str, lease_seconds: int | None = None
    ) -> fleet_client.ClaimDecision:
        try:
            return fleet_client.acquire_claim(
                self._fleet_target(),
                holder=holder,
                ttl_seconds=lease_seconds if lease_seconds is not None else self.config.lease_seconds,
                harness="grokbot",
                role=self.config.instance,
                job=job_id,
                session=session,
            )
        except Exception:
            return fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder=holder)

    def _fleet_renew(self, holder: str, lease_seconds: int | None = None) -> fleet_client.ClaimDecision:
        try:
            ttl = lease_seconds if lease_seconds is not None else self.config.lease_seconds
            return fleet_client.renew_claim(self._fleet_target(), holder=holder, ttl_seconds=ttl)
        except Exception:
            return fleet_client.ClaimDecision(granted=False, reason="hub-unavailable", holder=holder)

    def _fleet_release(self, holder: str) -> None:
        try:
            fleet_client.release_claim(self._fleet_target(), holder=holder)
        except Exception:
            return

    def _fleet_event(self, job_id: str, session: str, state: str) -> None:
        try:
            fleet_client.report_external_event(
                target=self._fleet_target(),
                harness="grokbot",
                role=self.config.instance,
                job=job_id,
                session=session,
                state=state,
            )
        except Exception:
            return

    def _eligible_job(
        self,
        arguments: dict[str, Any],
        allowed: set[str] | None = None,
        optional: set[str] | None = None,
    ) -> dict[str, Any]:
        job_id = _job_id(arguments, allowed or {"job_id"}, optional)
        job = grokbot_jobs.get_job(self.config.target, job_id)
        if self.config.instance != "operator" and job["role"] != self.config.instance:
            raise AdapterError()
        return job


class RequestGate:
    """Header/body admission checks for the HTTP adapter."""

    def __init__(self, config: ListenerConfig, *, max_request_bytes: int = MAX_REQUEST_BYTES):
        config.validate()
        if not 1 <= max_request_bytes <= MAX_REQUEST_BYTES:
            raise ConfigurationError("invalid")
        self.config = config
        self.max_request_bytes = max_request_bytes

    def reject_reason(self, headers: Mapping[str, str], body_size: int) -> str | None:
        if body_size < 0 or body_size > self.max_request_bytes:
            return "too-large"
        host = headers.get("host", "").split(":", 1)[0].casefold()
        if not self._host_allowed(host):
            return "forbidden"
        origin = headers.get("origin")
        if origin is not None and origin not in self.config.allowed_origins:
            return "forbidden"
        if not GrokbotAdapter(self.config).authorized(headers.get("authorization")):
            return "unauthorized"
        return None

    def _host_allowed(self, host: str) -> bool:
        if _is_loopback(self.config.bind_host) and host in {"localhost", "127.0.0.1", "::1", ""}:
            return True
        return host in {entry.casefold().split(":", 1)[0] for entry in self.config.allowed_hosts}


def build_listener_config(
    *,
    target: Path,
    instance: str,
    bind: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    bearer_file: Path | None,
    bearer_env: str | None,
    lease_seconds: int | None = None,
) -> ListenerConfig:
    host, port = parse_bind(bind)
    config = ListenerConfig(
        target=target.expanduser().resolve(),
        instance=instance,
        bind_host=host,
        bind_port=port,
        allowed_hosts=tuple(allowed_hosts),
        allowed_origins=tuple(allowed_origins),
        bearer=load_bearer(bearer_file=bearer_file, bearer_env=bearer_env),
        hub_token=load_hub_token(instance=instance),
        lease_seconds=lease_seconds if lease_seconds is not None else load_lease_seconds(instance=instance),
    )
    config.validate()
    return config


def build_app(config: ListenerConfig) -> Callable[..., Any]:
    """Build the separate Streamable HTTP process app with a strict edge gate."""
    MCPServer, JSONResponse, _, TransportSecuritySettings = _load_mcp()
    adapter = GrokbotAdapter(config)
    server = MCPServer("brigade-grokbot", version="1")

    @server.custom_route("/health", methods=["GET"])
    async def health(_: Any) -> Any:
        return JSONResponse(adapter.health_payload())

    for tool in adapter.tool_inventory():
        _register_tool(server, adapter, tool["name"])

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        host=config.bind_host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=_transport_allowed_hosts(config),
            allowed_origins=list(config.allowed_origins),
        ),
    )
    return _GateASGI(app, RequestGate(config), adapter)


def run_listener(config: ListenerConfig) -> None:
    """Run the remote adapter in its own process, never inside fleet serve."""
    _, _, _, _ = _load_mcp()
    configure_journal()
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - bundled with mcp at runtime.
        raise OptionalDependencyError() from exc
    uvicorn.run(build_app(config), host=config.bind_host, port=config.bind_port, access_log=False, log_level="warning")


def _register_tool(server: Any, adapter: GrokbotAdapter, name: str) -> None:
    """Register a name-specific typed wrapper so the SDK inventory stays exact."""
    handler = _tool_handler(adapter, name)
    server.tool(name=name, description=handler.__doc__)(handler)


def _tool_handler(adapter: GrokbotAdapter, name: str) -> Callable[..., Any]:
    """Build the typed wrapper whose signature becomes the advertised inputSchema."""
    description = tool_description(name, adapter.config.instance, adapter.config.lease_seconds)

    if name == "grokbot_queue_list":

        def invoke_list(
            state: str | None = None,
            include_all: bool | None = None,
            limit: int | None = None,
            role: str | None = None,
        ) -> dict[str, Any]:
            supplied = {"state": state, "include_all": include_all, "limit": limit, "role": role}
            return _invoke_adapter(adapter, name, {key: value for key, value in supplied.items() if value is not None})

        handler: Callable[..., Any] = invoke_list
    elif name == "grokbot_queue_claim":

        def invoke_claim(job_id: str, lease_id: str | None = None, lease_seconds: int | None = None) -> dict[str, Any]:
            payload: dict[str, Any] = {"job_id": job_id}
            if lease_id is not None:
                payload["lease_id"] = lease_id
            if lease_seconds is not None:
                payload["lease_seconds"] = lease_seconds
            return _invoke_adapter(adapter, name, payload)

        handler = invoke_claim
    elif name in {"grokbot_queue_status", "grokbot_queue_cancel", "grokbot_queue_expire", "grokbot_queue_report"}:

        def invoke_job_id(job_id: str) -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {"job_id": job_id})

        handler = invoke_job_id
    elif name == "grokbot_queue_complete":

        def invoke_complete(
            job_id: str,
            lease_id: str,
            artifact: dict[str, Any],
            report_text: str | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {"job_id": job_id, "lease_id": lease_id, "artifact": artifact}
            if report_text is not None:
                payload["report_text"] = report_text
            return _invoke_adapter(adapter, name, payload)

        handler = invoke_complete
    else:

        def invoke_lease(job_id: str, lease_id: str) -> dict[str, Any]:
            return _invoke_adapter(adapter, name, {"job_id": job_id, "lease_id": lease_id})

        handler = invoke_lease

    handler.__name__ = name
    handler.__doc__ = description
    return handler


def _invoke_adapter(adapter: GrokbotAdapter, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return adapter.call_tool(name, arguments)
    except AdapterError as exc:
        return exc.public_error()


class _GateASGI:
    """Reject untrusted requests before the SDK sees headers or a large body."""

    def __init__(self, app: Callable[..., Any], gate: RequestGate, adapter: GrokbotAdapter):
        self.app = app
        self.gate = gate
        self.adapter = adapter

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        content_length = headers.get("content-length")
        if content_length is not None and (
            not content_length.isdecimal() or int(content_length) > self.gate.max_request_bytes
        ):
            await _reject_http(send, 413, "Request body is too large")
            return
        reason = self.gate.reject_reason(headers, 0)
        if reason is not None:
            await _reject_http(
                send,
                401 if reason == "unauthorized" else 403,
                "Unauthorized" if reason == "unauthorized" else "Forbidden",
            )
            return
        body, messages, too_large = await _read_bounded_body(receive, self.gate.max_request_bytes)
        if too_large:
            await _reject_http(send, 413, "Request body is too large")
            return
        refusal = _tool_request_refusal(body, self.adapter) if scope.get("path") == "/mcp" else None
        if refusal is not None:
            await _reject_tool(send, body, refusal)
            return
        await self.app(scope, _replay_messages(messages, receive), send)


async def _read_bounded_body(receive: Callable[..., Any], maximum: int) -> tuple[bytes, list[dict[str, Any]], bool]:
    body = bytearray()
    messages: list[dict[str, Any]] = []
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            return bytes(body), messages, False
        body.extend(message.get("body", b""))
        if len(body) > maximum:
            return b"", messages, True
        if not message.get("more_body", False):
            return bytes(body), messages, False


def _replay_messages(messages: list[dict[str, Any]], receive: Callable[..., Any]) -> Callable[..., Any]:
    pending = iter(messages)

    async def replay() -> dict[str, Any]:
        try:
            return next(pending)
        except StopIteration:
            return await receive()

    return replay


def _tool_request_refusal(body: bytes, adapter: GrokbotAdapter) -> str | None:
    """Return the public refusal message for one raw tools/call, or None to admit it.

    Argument-shape refusals carry this module's bounded reason so a caller can
    correct itself. Everything else keeps the generic message, because the tool
    allowlist must not become a discovery surface.
    """
    try:
        request = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict):
        journal_tool_call(None, None, "refused", None)
        return "Invalid request"
    name, arguments = params.get("name"), params.get("arguments")
    if name not in adapter._tools():
        journal_tool_call(name, arguments, "refused", None)
        return "Invalid request"
    if name == "grokbot_queue_list" and (arguments is None or isinstance(arguments, dict)):
        try:
            list_options(arguments, adapter.config.instance)
        except AdapterError as exc:
            journal_tool_call(name, arguments, "refused", exc.reason)
            return exc.reason or "Invalid request"
    if _valid_tool_arguments(name, arguments):
        return None
    journal_tool_call(name, arguments, "refused", None)
    return "Invalid request"


async def _reject_http(send: Callable[..., Any], status: int, message: str) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}}).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def _reject_tool(send: Callable[..., Any], body: bytes, message: str = "Invalid request") -> None:
    request = json.loads(body)
    request_id = request.get("id") if isinstance(request, dict) else None
    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": message}}
    await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode("utf-8")})


def _load_mcp() -> tuple[Any, Any, Any, Any]:
    try:
        from mcp.server import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:
        raise OptionalDependencyError() from exc
    return MCPServer, JSONResponse, Request, TransportSecuritySettings


def _require_keys(arguments: dict[str, Any], expected: set[str], optional: set[str] | None = None) -> None:
    keys = set(arguments)
    if not keys.issuperset(expected) or not keys.issubset(expected | (optional or set())):
        raise AdapterError()


def _job_id(arguments: dict[str, Any], expected: set[str] | None = None, optional: set[str] | None = None) -> str:
    _require_keys(arguments, expected or {"job_id"}, optional)
    job_id = arguments.get("job_id")
    if not isinstance(job_id, str):
        raise AdapterError()
    return job_id


def _lease_id(arguments: dict[str, Any], *, mint: bool = False) -> str:
    """Return the caller's lease, or mint one when the tool allows omission."""
    if mint and arguments.get("lease_id") is None:
        return uuid.uuid4().hex
    lease_id = arguments.get("lease_id")
    if not isinstance(lease_id, str):
        raise AdapterError()
    return lease_id


@dataclass(frozen=True)
class ListOptions:
    """The validated, role-pinned projection filters for one list call."""

    state: str | None
    include_all: bool
    limit: int


def list_options(arguments: Mapping[str, Any] | None, instance: str) -> ListOptions:
    """Validate the list filters, refusing with a bounded, value-free reason."""
    arguments = {} if arguments is None else arguments
    if not isinstance(arguments, Mapping) or not set(arguments).issubset(LIST_ARGUMENT_KEYS):
        raise AdapterError(
            f"grokbot_queue_list accepts only these arguments: {', '.join(LIST_ARGUMENT_KEYS)}",
        )
    arguments = _without_null_optionals("grokbot_queue_list", arguments)
    state = arguments.get("state")
    if state is not None and (not isinstance(state, str) or state not in grokbot_jobs.JOB_STATES):
        raise AdapterError(f"state must be one of: {', '.join(sorted(grokbot_jobs.JOB_STATES))}")
    include_all = arguments.get("include_all", True)
    if type(include_all) is not bool:
        raise AdapterError("include_all must be a boolean")
    limit = arguments.get("limit", MAX_LISTED_JOBS)
    if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
        raise AdapterError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
    if "role" in arguments and arguments["role"] != instance:
        raise AdapterError(f"role is fixed by this listener and must be omitted or equal to: {instance}")
    if not include_all and state in grokbot_jobs.TERMINAL_STATES:
        # The two filters intersect, so this pair can only ever answer with an
        # empty list. Say so instead of returning the silence a Bot reads as a
        # broken queue. `state` is already pinned to the closed state set.
        raise AdapterError(f"state {state} requires include_all")
    return ListOptions(state=state, include_all=include_all, limit=limit)


def journal_tool_call(name: object, arguments: object, decision: str, reason: str | None) -> None:
    """Write one bounded INFO journal line per tool call, never an argument value."""
    tool = name if isinstance(name, str) and name in _TOOL_ARGUMENT_TYPES else "unknown"
    required, optional = _TOOL_ARGUMENT_TYPES.get(tool, ({}, {}))
    accepted = set(required) | set(optional)
    keys = [str(key) for key in arguments] if isinstance(arguments, Mapping) else []
    known = sorted(key for key in keys if key in accepted)
    _JOURNAL.info(
        "grokbot tool=%s args=%s unknown=%d decision=%s reason=%s",
        tool,
        ",".join(known) or "-",
        sum(1 for key in keys if key not in accepted),
        decision,
        reason or ("-" if decision == "ok" else "invalid-request"),
    )


def journal_lease(tool: str, lease_seconds: int, deadline: object) -> None:
    """Journal the lease window a claim or renew opened, never the job or lease.

    The deadline is the queue's own answer, so a lapsed lease can be read out
    of the journal instead of inferred from a later refusal.
    """
    _JOURNAL.info(
        "grokbot tool=%s lease_seconds=%d deadline=%s",
        tool if tool in _TOOL_ARGUMENT_TYPES else "unknown",
        lease_seconds,
        deadline if isinstance(deadline, str) else "-",
    )


def configure_journal() -> None:
    """Send this module's per-call journal lines to stderr at INFO, once.

    Propagation is switched off so an ancestor handler (root logging under a
    server framework, for example) cannot emit the same line a second time;
    the one-line-per-call contract holds regardless of the host's logging
    configuration.
    """
    _JOURNAL.setLevel(logging.INFO)
    _JOURNAL.propagate = False
    if any(getattr(handler, "name", None) == _JOURNAL_HANDLER_NAME for handler in _JOURNAL.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = _JOURNAL_HANDLER_NAME
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _JOURNAL.addHandler(handler)


def _fleet_derivation_message(job_id: str, lease_id: str) -> bytes:
    """Length-prefixed so job_id and lease_id cannot collide across concatenations."""
    return f"{len(job_id)}:{job_id}:{len(lease_id)}:{lease_id}".encode("utf-8")


def fleet_holder(job_id: str, lease_id: str) -> str:
    """Full fencing token derived from the job-scoped queue lease, never the lease itself."""
    return hmac.new(_FLEET_HOLDER_DOMAIN, _fleet_derivation_message(job_id, lease_id), hashlib.sha256).hexdigest()


def fleet_session(job_id: str, lease_id: str) -> str:
    """Opaque non-capability session label, domain-separated from the holder."""
    return hmac.new(_FLEET_SESSION_DOMAIN, _fleet_derivation_message(job_id, lease_id), hashlib.sha256).hexdigest()[:32]


def _fleet_refused(decision: fleet_client.ClaimDecision) -> bool:
    return not decision.granted and (_fleet_hub_configured() or decision.reason not in _FLEET_BEST_EFFORT)


def _cloud_refused(decision: fleet_client.CloudDecision | None) -> bool:
    """Only an absent hub permits queue work without a hub admission."""
    if decision is None:
        return _fleet_hub_configured()
    return not decision.granted and (_fleet_hub_configured() or decision.reason not in _FLEET_BEST_EFFORT)


def _fleet_hub_configured() -> bool:
    """Configured hub failures deny claims instead of becoming local-only work."""
    try:
        return bool(fleet_client.load_fleet_config().get("hub_url"))
    except Exception:
        return True


# Required and optional argument types per tool. The registered SDK signatures,
# the raw edge gate, and the journal all read this one table.
_TOOL_ARGUMENT_TYPES: dict[str, tuple[dict[str, type[object]], dict[str, type[object]]]] = {
    "grokbot_queue_list": ({}, {"state": str, "include_all": bool, "limit": int, "role": str}),
    "grokbot_queue_status": ({"job_id": str}, {}),
    "grokbot_queue_cancel": ({"job_id": str}, {}),
    "grokbot_queue_expire": ({"job_id": str}, {}),
    "grokbot_queue_report": ({"job_id": str}, {}),
    "grokbot_queue_claim": ({"job_id": str}, {"lease_id": str, "lease_seconds": int}),
    "grokbot_queue_renew": ({"job_id": str, "lease_id": str}, {}),
    "grokbot_queue_start": ({"job_id": str, "lease_id": str}, {}),
    "grokbot_queue_complete": ({"job_id": str, "lease_id": str, "artifact": dict}, {"report_text": str}),
    "grokbot_queue_fail": ({"job_id": str, "lease_id": str}, {}),
    "grokbot_queue_ack_cancel": ({"job_id": str, "lease_id": str}, {}),
}


def _without_null_optionals(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Drop optional arguments sent as null, which the schema itself permits.

    Every optional parameter is advertised as ``X | None``, so a caller that
    fills its optionals with null is inside the contract and must land on the
    same behaviour as omitting them.
    """
    optional = _TOOL_ARGUMENT_TYPES.get(name, ({}, {}))[1]
    return {key: value for key, value in arguments.items() if not (key in optional and value is None)}


def _typed(value: object, expected: type[object]) -> bool:
    """Exact typing for bool and int so a boolean cannot pass as a limit."""
    if expected in (bool, int):
        return type(value) is expected
    return isinstance(value, expected)


def _valid_tool_arguments(name: object, arguments: object) -> bool:
    if not isinstance(name, str) or name not in _TOOL_ARGUMENT_TYPES:
        return False
    required, optional = _TOOL_ARGUMENT_TYPES[name]
    if arguments is None:
        return not required
    if not isinstance(arguments, dict):
        return False
    arguments = _without_null_optionals(name, arguments)
    keys = set(arguments)
    if not keys.issuperset(required) or not keys.issubset(set(required) | set(optional)):
        return False
    return all(
        _typed(arguments[key], value_type) for key, value_type in {**required, **optional}.items() if key in keys
    )


def _transport_allowed_hosts(config: ListenerConfig) -> list[str]:
    if _is_loopback(config.bind_host):
        return list(dict.fromkeys((*SDK_LOOPBACK_ALLOWED_HOSTS, *config.allowed_hosts)))
    return list(config.allowed_hosts)


def _parse_deadline(value: str) -> datetime:
    """Read one queue timestamp as an aware instant."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _valid_host(value: str) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 253 and all(part for part in value.split("."))


def _valid_origin(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?", value))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_LIST_DESCRIPTION = (
    "List safe projections for this Grok Bot queue role. All arguments are optional: "
    "state (one of {states}), include_all (boolean, default true; false hides terminal jobs, so it "
    "cannot be combined with a terminal state), limit (integer 1..{limit}, default {default}), and "
    "role (must be omitted or equal to this listener's fixed role, {role}). An optional argument "
    "sent as null means the same as omitting it. The role is fixed server-side and no argument can "
    "widen it. Any other argument is refused."
)
_CLAIM_DESCRIPTION = (
    "Claim one queued job for this listener's fixed worker identity. Arguments: job_id (required), "
    "lease_id (optional), and lease_seconds (optional, integer {lease_min}..{lease_max}); null means "
    "omitted for either optional. When lease_id is omitted the listener mints one and returns it as "
    "lease_id; carry that value on every later call for this job. When lease_seconds is omitted the "
    "listener grants its configured lease ({lease_default} seconds here). The result carries the "
    "granted lease_seconds and the lease_expires_at deadline; the job's own deadline can shorten it. "
    "The routine sequence is grokbot_queue_list, grokbot_queue_claim, grokbot_queue_start, "
    "grokbot_queue_renew before lease_expires_at, then grokbot_queue_complete or grokbot_queue_fail, "
    "and grokbot_queue_ack_cancel when the operator requests cancellation. A renew or fail sent after "
    "the deadline is refused with lease-expired, which means the lease lapsed, not that the call was "
    "malformed. The role and worker identity are fixed server-side."
)


_TOOL_DESCRIPTIONS = {
    "grokbot_queue_list": _LIST_DESCRIPTION,
    "grokbot_queue_status": "Read one safe Grok Bot queue projection.",
    "grokbot_queue_cancel": "Cancel a Grok Bot queue job.",
    "grokbot_queue_expire": "Expire an elapsed Grok Bot queue job.",
    "grokbot_queue_report": "Read one verified Repository Scout report snapshot.",
    "grokbot_queue_claim": _CLAIM_DESCRIPTION,
    "grokbot_queue_renew": "Renew this worker's current queue lease with the claim's lease_id.",
    "grokbot_queue_start": "Mark this worker's claimed job as running.",
    "grokbot_queue_complete": "Complete this worker's running job.",
    "grokbot_queue_fail": "Fail this worker's live job.",
    "grokbot_queue_ack_cancel": "Acknowledge an operator cancellation request.",
}


def tool_description(name: str, instance: str, lease_seconds: int | None = None) -> str:
    """Describe one tool for a listener whose role and lease are already fixed."""
    return _TOOL_DESCRIPTIONS[name].format(
        states=", ".join(sorted(grokbot_jobs.JOB_STATES)),
        limit=MAX_LIST_LIMIT,
        default=MAX_LISTED_JOBS,
        role=instance,
        lease_min=MIN_LEASE_SECONDS,
        lease_max=MAX_LEASE_SECONDS,
        lease_default=lease_seconds if lease_seconds is not None else DEFAULT_LEASE_SECONDS,
    )
