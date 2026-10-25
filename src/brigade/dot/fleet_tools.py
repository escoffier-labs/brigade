"""Dot fleet tools over the shared t3-fleet machine-mode boundary."""

from __future__ import annotations

from dataclasses import dataclass
import os
import json
import subprocess
from pathlib import Path
import re
import stat
from typing import Any, cast

from brigade.grokbot_t3.fleet import (
    ERROR_SCHEMA,
    MAX_OUTPUT_BYTES,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    FleetCallError,
    FleetProtocolError,
    FleetRunner,
    FleetTimeout,
    child_env,
)

from .. import toml_compat as tomllib
from .contracts import MAX_PROMPT_BYTES, DotError, require
from .process import CommandRunner, child_environment, child_path
from .runtime_config import OwnerAuthorization, Runtime
from .safe_state import redact_credentials
from .state import BridgeState, Clock, utc_now

MAX_FLEET_CONFIG_BYTES = 262144
FLEET_SUBMIT_TIMEOUT = 150
FLEET_CONTROL_TIMEOUT = 120
FLEET_READ_TIMEOUT = 60
FLEET_DOCTOR_TIMEOUT = 90
GIT_TIMEOUT = 20


class _FleetErrorResult(FleetCallError):
    def __init__(self, error: dict[str, Any], exit_code: int, request_id: str | None):
        code = error.get("code")
        super().__init__(code if isinstance(code, str) else "internal_error", request_id=request_id)
        self.error, self.exit_code = error, exit_code


class _DotFleetRunner(FleetRunner):
    """Preserve Dot's error envelope without changing the shared dispatch runner."""

    def __init__(self, *args: Any, fleet_config: Path | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # t3-fleet must read the same controller config Dot validated against.
        self.fleet_config = fleet_config

    def _child_env(self) -> dict[str, str]:
        env = child_env()
        if self.fleet_config is not None:
            env["T3_FLEET_CONFIG"] = str(self.fleet_config)
        return env

    def call(self, action: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        body = json.dumps({"schema": REQUEST_SCHEMA, "action": action, **payload}, separators=(",", ":"))
        try:
            proc = self._run(
                [self.t3_fleet_bin, action],
                input=body,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FleetTimeout(action) from exc
        except OSError as exc:
            raise FleetCallError("internal_error") from exc
        except UnicodeDecodeError as exc:
            # Undecodable output may follow an accepted submit, so it is in doubt like any malformed result.
            raise FleetProtocolError("internal_error") from exc
        output = proc.stdout or proc.stderr or ""
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise FleetProtocolError("internal_error")
        try:
            result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise FleetProtocolError("internal_error") from exc
        if not isinstance(result, dict):
            raise FleetProtocolError("internal_error")
        if result.get("schema") == ERROR_SCHEMA:
            raw_error = result.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            request_id = result.get("request_id")
            raise _FleetErrorResult(error, proc.returncode, request_id if isinstance(request_id, str) else None)
        if result.get("schema") != RESULT_SCHEMA or proc.returncode != 0:
            request_id = result.get("request_id") if result.get("schema") == RESULT_SCHEMA else None
            raise FleetProtocolError("internal_error", None, request_id if isinstance(request_id, str) else None)
        nested = result.get("result")
        if isinstance(nested, dict):
            flattened = {key: value for key, value in result.items() if key != "result"}
            flattened.update(nested)
            return flattened
        return result


@dataclass(frozen=True)
class FleetConfig:
    controller_host: str
    enrolled: bool
    hosts: dict[str, bool]
    repositories: dict[str, Any]
    outbox: bool = False


def load_fleet_config(path: Path) -> FleetConfig:
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and not info.st_mode & 0o022
            and info.st_size <= MAX_FLEET_CONFIG_BYTES
        )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(MAX_FLEET_CONFIG_BYTES + 1)
        require(len(data) <= MAX_FLEET_CONFIG_BYTES)
        value = tomllib.loads(data.decode("utf-8"))
        controller = value.get("controller_host")
        require(isinstance(controller, str) and bool(controller))
        policy = value.get("policy") or {}
        require(isinstance(policy, dict))
        hosts = {}
        for host_id, host in (value.get("hosts") or {}).items():
            require(isinstance(host, dict))
            hosts[host_id] = host.get("enabled", True) is True
        repositories = {}
        for alias, repository in (value.get("repositories") or {}).items():
            require(isinstance(repository, dict))
            branch = repository.get("default_branch")
            paths = repository.get("paths") or {}
            require(isinstance(branch, str) and isinstance(paths, dict))
            repositories[alias] = {
                "default_branch": branch,
                "paths": {key: item for key, item in paths.items() if isinstance(item, str)},
            }
        outbox = value.get("outbox") or {}
        require(isinstance(outbox, dict))
        return FleetConfig(
            cast(str, controller),
            policy.get("enrollment") == "required",
            hosts,
            repositories,
            outbox.get("enabled") is True,
        )
    except Exception:
        raise DotError("fleet_config_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def authorization_prompt(prompt: str, owner: OwnerAuthorization | None, read_only: bool) -> str:
    if read_only:
        return prompt
    if owner is None:
        raise DotError("invalid_config")
    return prompt + "\n\n## Owner authorization [" + owner.version + "]\n" + owner.text


class FleetTools:
    """Tool handlers called after schema validation and the adapter's dispatch gate."""

    def __init__(
        self,
        runtime: Runtime,
        state: BridgeState,
        *,
        runner: FleetRunner | None = None,
        commands: CommandRunner | None = None,
        clock: Clock = utc_now,
    ):
        self.runtime, self.state, self.clock = runtime, state, clock
        t3_fleet, fleet_config = runtime.paths.bin.t3_fleet, runtime.paths.fleet_config
        if t3_fleet is None or fleet_config is None:
            # Only a dispatch profile's runtime configures the T3 fleet client.
            raise DotError("invalid_config")
        self.runner = (
            runner
            if runner is not None
            else _DotFleetRunner(
                t3_fleet,
                runtime.paths.bin.git,
                runtime_mode="full-access",
                fleet_config=fleet_config,
            )
        )
        self.commands = commands if commands is not None else CommandRunner()
        self.environment = child_environment(child_path(runtime.paths.bin))

    @staticmethod
    def _uncertain(request_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error": "fleet_uncertain",
            "next": "Do not resubmit. Check fleet_recent and fleet_status for the request.",
        }
        if request_id is not None:
            result["request_id"] = request_id
        return result

    def _fleet(self, action: str, payload: dict[str, Any], *, timeout: float, mutating: bool = False) -> dict[str, Any]:
        try:
            return {"ok": True, "result": self.runner.call(action, payload, timeout=timeout)}
        except FleetTimeout:
            if mutating:
                return self._uncertain(None)
            return {"error": "fleet_timeout"}
        except FleetProtocolError as error:
            # Untrusted output after a mutating call leaves its side effect unknown, like a timeout.
            if mutating:
                return self._uncertain(error.request_id)
            return {"error": "fleet_unavailable"}
        except _FleetErrorResult as error:
            result: dict[str, Any] = {"error": "fleet_error", "fleet": error.error, "exit_code": error.exit_code}
            if error.request_id is not None:
                result["request_id"] = error.request_id
            return result
        except FleetCallError as error:
            fleet = {"code": error.code}
            if error.reason is not None:
                fleet["reason"] = error.reason
            result = {"error": "fleet_error", "fleet": fleet}
            if error.request_id is not None:
                result["request_id"] = error.request_id
            return result

    def _fleet_config(self) -> FleetConfig:
        path = self.runtime.paths.fleet_config
        if path is None:
            raise DotError("fleet_config_unavailable")
        return load_fleet_config(path)

    @staticmethod
    def _controller_summary(config: FleetConfig) -> dict[str, Any]:
        return dict(
            controller_host=config.controller_host,
            enrollment="required" if config.enrolled else "off",
            enabled_hosts=sorted(host for host, enabled in config.hosts.items() if enabled),
            repositories={
                alias: sorted(repository["paths"]) for alias, repository in sorted(config.repositories.items())
            },
        )

    def fleet_capacity(self, host: str | None = None) -> dict[str, Any]:
        result = self._fleet("capacity", {"host": host} if host else {}, timeout=FLEET_READ_TIMEOUT)
        try:
            result["controller"] = self._controller_summary(self._fleet_config())
        except DotError as error:
            result["controller"] = {"error": error.code}
        return result

    def fleet_doctor(self, host: str | None = None, repository: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in (("host", host), ("repository", repository)) if value}
        return self._fleet("doctor", payload, timeout=FLEET_DOCTOR_TIMEOUT)

    def _resolve_revision(self, config: FleetConfig, repository: str) -> tuple[str, str]:
        entry = config.repositories[repository]
        branch, checkout = entry["default_branch"], entry["paths"].get(config.controller_host)
        require(
            isinstance(checkout, str)
            and checkout.startswith("/")
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) is not None
            and ".." not in branch,
            "source_revision_unavailable",
        )
        remote = self.commands.run(
            [self.runtime.paths.bin.git, "-C", checkout, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
            timeout=GIT_TIMEOUT,
            max_output=65536,
            env=self.environment,
        )
        if remote.returncode == 0 and not remote.timed_out and not remote.overflow:
            for line in remote.stdout.decode("utf-8", "replace").splitlines():
                match = re.fullmatch(r"([0-9a-f]{40})\trefs/heads/" + re.escape(branch), line)
                if match:
                    return match.group(1), f"origin/{branch} tip via git ls-remote"
        local = self.commands.run(
            [
                self.runtime.paths.bin.git,
                "-C",
                checkout,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{branch}^{{commit}}",
            ],
            timeout=GIT_TIMEOUT,
            max_output=4096,
            env=self.environment,
        )
        revision = local.stdout.decode("utf-8", "replace").strip()
        if local.returncode == 0 and not local.timed_out and re.fullmatch(r"[0-9a-f]{40}", revision):
            return revision, f"local refs/remotes/origin/{branch} (ls-remote failed; may be stale)"
        raise DotError("source_revision_unavailable")

    def fleet_submit(
        self,
        repository: str,
        title: str,
        prompt: str,
        host: str = "auto",
        seat: str | None = None,
        workload: str | None = None,
        override_reason: str | None = None,
        source_revision: str | None = None,
        allow_queue: bool = False,
        read_only: bool = False,
    ) -> dict[str, Any]:
        prompt = authorization_prompt(prompt, self.runtime.owner_authorization, read_only)
        require(len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES, "prompt_too_large")
        config = self._fleet_config()
        if repository not in config.repositories:
            return {"error": "unknown_repository", "repositories": sorted(config.repositories)}
        enabled = sorted(name for name, on in config.hosts.items() if on)
        if config.enrolled:
            if (host != "auto" or seat) and not override_reason:
                return {"error": "override_reason_required"}
        else:
            if host == "auto":
                return {"error": "explicit_host_required", "enrollment": "off", "enabled_hosts": enabled}
            if workload or override_reason:
                return {"error": "requires_enrollment", "enrollment": "off"}
            if not seat:
                return {"error": "seat_required", "enrollment": "off"}
        if host != "auto" and host not in enabled:
            return {"error": "unknown_host", "enabled_hosts": enabled}
        if allow_queue and not config.outbox:
            return {
                "error": "queue_unavailable",
                "next": "The controller's local outbox is off. Submit without allow_queue, "
                "and on target_busy wait and resubmit later.",
            }
        if source_revision is not None:
            basis = "caller supplied"
        else:
            source_revision, basis = self._resolve_revision(config, repository)
        payload: dict[str, Any] = {
            "host": host,
            "repository": repository,
            "source_revision": source_revision,
            "title": title,
            "prompt": prompt,
            "runtime_mode": "full-access",
            "interaction_mode": "default",
            "initiator_kind": "agent",
            "initiator_label": "dot",
        }
        for key, value in (("seat", seat), ("workload", workload), ("override_reason", override_reason)):
            if value:
                payload[key] = value
        if allow_queue:
            payload["allow_queue"] = True
        summary = dict(
            repository=repository,
            title=title,
            host=host,
            seat=seat,
            workload=workload,
            source_revision=source_revision,
            prompt_bytes=len(prompt.encode("utf-8")),
        )
        operation = self.state.begin_operation("fleet_submit", summary, clock=self.clock)
        result = self._fleet("submit", payload, timeout=FLEET_SUBMIT_TIMEOUT, mutating=True)
        request_id = result.get("request_id") or (result.get("result") or {}).get("request_id")
        request_id = request_id if isinstance(request_id, str) else None
        self.state.finish_operation(
            operation, request_id, self._outcome(result), error_message=self._error_message(result), clock=self.clock
        )
        return {
            **result,
            "request_id": request_id,
            "source_revision": source_revision,
            "source_revision_basis": basis,
            "operation_id": operation,
        }

    @staticmethod
    def _error_message(result: dict[str, Any]) -> str | None:
        fleet = result.get("fleet") if result.get("error") == "fleet_error" else None
        message = fleet.get("message") if isinstance(fleet, dict) else None
        return redact_credentials(message)[:300] if isinstance(message, str) and message else None

    @staticmethod
    def _outcome(result: dict[str, Any]) -> str:
        if result.get("ok"):
            state = result["result"].get("state")
            return f"ok:{state}" if isinstance(state, str) else "ok"
        if result.get("error") == "fleet_error" and isinstance(result.get("fleet"), dict):
            return f"fleet_error:{result['fleet'].get('code')}"
        return str(result.get("error"))

    def _control(self, action: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        operation = self.state.begin_operation("fleet_" + action, {"request_id": request_id}, clock=self.clock)
        result = self._fleet(
            action, {"request_id": request_id, **payload}, timeout=FLEET_CONTROL_TIMEOUT, mutating=True
        )
        self.state.finish_operation(
            operation, request_id, self._outcome(result), error_message=self._error_message(result), clock=self.clock
        )
        return {**result, "request_id": request_id, "operation_id": operation}

    def fleet_status(self, request_id: str, include_output: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"request_id": request_id}
        if include_output:
            payload["include_output"] = True
        return self._fleet("status", payload, timeout=FLEET_READ_TIMEOUT)

    def fleet_wait(self, request_id: str, timeout_seconds: int = 30, include_output: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"request_id": request_id, "timeout": float(timeout_seconds)}
        if include_output:
            payload["include_output"] = True
        return self._fleet("wait", payload, timeout=timeout_seconds + 45)

    def fleet_followup(self, request_id: str, message: str, read_only: bool = False) -> dict[str, Any]:
        message = authorization_prompt(message, self.runtime.owner_authorization, read_only)
        require(len(message.encode("utf-8")) <= MAX_PROMPT_BYTES, "prompt_too_large")
        return self._control(
            "followup",
            request_id,
            {
                "prompt": message,
                "runtime_mode": "full-access",
                "interaction_mode": "default",
            },
        )

    def fleet_answer(self, request_id: str, input_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        return self._control("answer", request_id, {"input_id": input_id, "answers": answers})

    def fleet_approve(self, request_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        return self._control("approve", request_id, {"approval_id": approval_id, "decision": decision})

    def fleet_interrupt(self, request_id: str) -> dict[str, Any]:
        return self._control("interrupt", request_id, {})

    def fleet_recent(self, limit: int = 20) -> dict[str, Any]:
        return {"operations": self.state.operations(limit)}
