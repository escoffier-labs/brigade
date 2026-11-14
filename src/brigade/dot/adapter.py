"""Dot tool validation, dispatch and composition without a transport dependency."""

from __future__ import annotations
import time
from typing import Any
from .contracts import (
    ALL_TOOL_SCHEMAS,
    DISPATCH_GATED_TOOLS,
    GATED_TOOLS,
    MAX_MESSAGE_BYTES,
    MAX_PROMPT_BYTES,
    MESSAGING_GATED_TOOLS,
    TOOLS,
    DotError,
    require,
)
from .fleet_tools import FleetTools
from .hub_inbox import HubInbox
from .hub_tools import HubTools
from .inbox_tools import InboxTools
from .memory_push import MAX_DIGEST_HITS, MAX_PUSH_HITS, MemoryPush, PushScan, digest_query, inbox_query
from .memory_tools import MemoryTools
from .process import CommandRunner, bound_result
from .profile import (
    DOT,
    DUET_EXCLUDED_TOOLS,
    DUET_DISPATCH_TOOLS,
    INBOX_TRUST_NOTICE as INBOX_TRUST_NOTICE,
    ConnectorProfile,
    runtime_profile,
)
from .runtime_config import Runtime
from .duet_dispatch import DuetFleetTools
from .safe_state import identity
from .state import BridgeState, Clock, summarize_arguments, utc_now

MAX_UNACKED = 1000
# Responses that carry memory hits, and how each derives its query from its own content.
PUSH_QUERIES = {"inbox_list": inbox_query, "fleet_digest": digest_query}
# The digest covers more ground than the next message, so it carries more hits.
PUSH_LIMITS = {"inbox_list": MAX_PUSH_HITS, "fleet_digest": MAX_DIGEST_HITS}
PUSH_SCAN_LABEL = "memory_push_scan"
SEND_WINDOW_SECONDS = 3600


def validate_arguments(name: str, arguments: dict[str, Any], *, tools: frozenset[str] = TOOLS) -> dict[str, Any]:
    """Exact per-tool validation. Unknown keys are refused; null means absent."""
    require(isinstance(name, str) and name in tools and name in ALL_TOOL_SCHEMAS and isinstance(arguments, dict))
    schema = ALL_TOOL_SCHEMAS[name]
    require(set(arguments) <= set(schema.fields))
    result = {}
    for key, spec in schema.fields.items():
        value = arguments.get(key)
        if value is not None:
            result[key] = spec.validator(value)
        elif spec.required:
            raise DotError("invalid_request")
        elif spec.default is not None:
            result[key] = spec.default
    return result


def argument_problem(name: object, arguments: object, *, tools: frozenset[str] = TOOLS) -> str:
    """Why a tools/call fails validate_arguments, in schema-owned words only.

    The text never repeats caller-supplied keys or values, so it is safe to return to the
    client and cheap to act on: a client can fix an out-of-range limit instead of retrying.
    """
    if not isinstance(name, str) or name not in tools or name not in ALL_TOOL_SCHEMAS:
        return "unknown tool"
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    fields = ALL_TOOL_SCHEMAS[name].fields
    unknown = sum(1 for key in arguments if key not in fields)
    if unknown:
        accepted = ", ".join(sorted(fields)) or "none"
        return f"{unknown} unsupported argument(s); accepted arguments: {accepted}"
    for key, spec in fields.items():
        value = arguments.get(key)
        if value is None:
            if spec.required:
                return f"missing required argument {key}"
            continue
        try:
            spec.validator(value)
        except DotError:
            describe = getattr(spec.validator, "describe", None)
            if callable(describe):
                return f"{key} must be {describe()}"
            return f"invalid value for {key}"
    return "invalid arguments"


class DotTools:
    def __init__(
        self,
        runtime: Runtime,
        state: BridgeState,
        *,
        fleet: FleetTools | None,
        hub: HubTools,
        memory: MemoryTools,
        inbox: InboxTools,
        clock: Clock = utc_now,
        profile: ConnectorProfile = DOT,
    ) -> None:
        self.runtime, self.state, self.clock, self.profile = (
            runtime,
            state,
            clock,
            runtime_profile(profile, runtime.dispatch_enabled, messaging_enabled=bool(runtime.messaging_enabled)),
        )
        self.fleet, self.hub, self.memory, self.inbox = fleet, hub, memory, inbox
        self.push = MemoryPush(
            runtime.paths.workspace,
            enabled=runtime.memory_push and self.profile.memory_push,
            hub=lambda query, limit: self.memory.push_search(query, limit),
        )

    def call(self, owner: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        error_code = None
        result: dict[str, Any] = {}
        try:
            identity(owner)
            values = validate_arguments(name, arguments, tools=self.profile.tools)
            if not self.profile.fleet_dispatch and name == "message_send":
                if self.profile.strict_recipients and values["to"] not in self.runtime.hub.recipients:
                    # Outside the allowlist, so refused like a hub send-grant denial and never forwarded.
                    raise DotError("recipient_not_permitted", "message_recipient_not_permitted")
                require(values["to"] in self.runtime.hub.recipients)
            if name in DISPATCH_GATED_TOOLS:
                require(self.runtime.dispatch_enabled, "dispatch_disabled")
            elif name in MESSAGING_GATED_TOOLS or (name == "inbox_reply" and not self.profile.fleet_dispatch):
                if self.runtime.messaging_enabled is None:
                    require(self.runtime.dispatch_enabled, "dispatch_disabled")
                else:
                    require(self.runtime.messaging_enabled, "messaging_disabled")
                if self.profile.send_limit_per_hour is not None:
                    require(
                        self.state.reserve_send(self.profile.send_limit_per_hour, SEND_WINDOW_SECONDS),
                        "send_limit_reached",
                    )
            if name == "dot_t3_capabilities":
                result = self.dot_t3_capabilities()
            elif name == "duet_capabilities":
                result = self.duet_capabilities()
            elif name == "fleet_digest":
                result = self.hub.fleet_digest(owner)
            else:
                for module in (self.fleet, self.hub, self.memory, self.inbox):
                    if module is None:
                        continue
                    handler = getattr(module, name, None)
                    if handler is not None:
                        result = handler(**values)
                        break
                else:
                    raise DotError("bridge_unavailable")
            if name in PUSH_QUERIES and isinstance(result, dict) and "error" not in result:
                scan = PushScan()
                pushed = self.push.hits(PUSH_QUERIES[name](result), scan, limit=PUSH_LIMITS[name])
                if pushed is not None:
                    result["memory_hits"] = pushed
                if scan.dropped or scan.error:
                    # Counts, rule ids and an error class only; the response shape does not change.
                    self.record_call(
                        PUSH_SCAN_LABEL, scan.error is None, 0, arg_summary=scan.summary(), error_code=scan.error
                    )
            # Bound report data before adding the connector-owned trust metadata.
            if name == "fleet_sessions":
                for entry in result.get("sessions", []):
                    if isinstance(entry, dict):
                        entry.pop("content_trust", None)
            result = bound_result(result)
            if name == "fleet_sessions":
                for entry in result.get("sessions", []):
                    if isinstance(entry, dict) and entry.get("cloud_context") is not None:
                        entry["content_trust"] = "untrusted"
            error_code = result.get("error")
            return result
        except DotError as error:
            error_code = error.code
            raise
        except Exception:
            error_code = "adapter_unavailable"
            raise
        finally:
            nested = result.get("result")
            request_id = arguments.get("request_id")
            if request_id is None:
                request_id = result.get("request_id") or (
                    nested.get("request_id") if isinstance(nested, dict) else None
                )
            self.record_call(
                name,
                error_code is None,
                (time.monotonic() - started) * 1000,
                request_id=request_id,
                arg_summary=summarize_arguments(arguments),
                error_code=error_code,
            )

    def record_call(
        self,
        tool: str,
        ok: bool,
        duration_ms: float,
        *,
        request_id: str | None = None,
        arg_summary: str | None = None,
        error_code: str | None = None,
        client_name: str | None = None,
    ) -> None:
        """A log failure never changes the tool outcome."""
        try:
            self.state.log_call(
                tool,
                ok,
                duration_ms,
                request_id=request_id,
                arg_summary=arg_summary,
                error_code=error_code,
                client_name=client_name,
                clock=self.clock,
            )
        except Exception:
            pass

    def dot_t3_capabilities(self) -> dict[str, Any]:
        """Static description only: no IO, no subprocess, no config read."""
        return dict(
            bridge_version="6",
            operating_instructions=self.profile.operating_instructions,
            dispatch_enabled=self.runtime.dispatch_enabled,
            events_enabled=self.runtime.events_enabled,
            tools=sorted(self.profile.tools),
            # With an explicit messaging_enabled, message_send has its own gate and is not dispatch-gated.
            dispatch_gated_tools=sorted(
                GATED_TOOLS if self.runtime.messaging_enabled is None else DISPATCH_GATED_TOOLS
            ),
            messaging_enabled=(
                self.runtime.dispatch_enabled
                if self.runtime.messaging_enabled is None
                else self.runtime.messaging_enabled
            ),
            fleet=dict(
                submit=(
                    "fleet_submit needs repository (t3-fleet alias), title, prompt. Call fleet_capacity "
                    "first: it reports controller enrollment, enabled hosts and repository aliases. "
                    "Enrollment off (legacy): pass an explicit host and seat (for example sol61); "
                    "workload and override_reason are refused. Enrollment required: host defaults to "
                    "auto; an explicit host or seat needs override_reason."
                ),
                source_revision=(
                    "Optional 40-hex commit. When omitted the bridge resolves the tip of the "
                    "repository's default branch on origin (git ls-remote from the controller "
                    "checkout; falls back to the local origin tracking ref and says so)."
                ),
                runtime_mode="full-access (trusted operator fleet; children do not raise T3 approval prompts)",
                max_prompt_bytes=MAX_PROMPT_BYTES,
                handles=(
                    "Keep the returned request_id. fleet_recent lists every mutating bridge call with its "
                    "request_id and outcome, including calls whose HTTP response was lost."
                ),
                wait="fleet_wait blocks at most 60 seconds; a timeout leaves the task running.",
            ),
            fleet_hub=dict(
                read_only=False,
                report_session="fleet_report_session: gated explicit v2 task reports, dry-run by default, reported-unverified; no lifecycle authority",
                start_of_task=(
                    "fleet_digest (one call: overview, work_next dry run, inbox, subscriptions, "
                    "open brigade PRs); fleet_overview + fleet_work_next for full detail"
                ),
                tools=dict(
                    fleet_digest=(
                        "compact read-only digest; each section reports its own error without failing the call"
                    ),
                    fleet_sessions="interactive sessions per node (include_ended adds ended/expired)",
                    fleet_work_list="Worklore items, filter by source and status",
                    fleet_work_show="one Worklore item by work_id (wl-...)",
                    fleet_work_next="first eligible burn item with a dry-run route",
                    fleet_work_burn="one page of the burn queue; pass next_cursor to page",
                    fleet_models="hub model roster for brigade-run or t3-fleet",
                    fleet_cloud="cloud lease snapshot (include_released adds released/expired)",
                    fleet_hub_policy="current control-plane policy revision and status projection",
                ),
                events=(
                    "No event-history tool: brigade fleet export reads only the hub host's local "
                    "database, which is not on the control host. Use fleet_overview and fleet_sessions."
                ),
            ),
            inbox=dict(
                usage=(
                    "The inbox is hub-backed. Call inbox_list to read pending messages oldest first, "
                    "inbox_reply to answer and inbox_ack after handling. message_send starts messages; "
                    "message_status checks sent messages. message_send accepts these names in to: "
                    + ", ".join(sorted(self.runtime.hub.recipients))
                    + ". "
                    "Fleet agents use python -m brigade.dot notify --from <agent> or brigade fleet msg send --to "
                    + self.runtime.hub.principal
                    + ". Hub history is retained for 90 days."
                ),
                trust=INBOX_TRUST_NOTICE,
                cli="brigade fleet msg send",
                max_message_bytes=MAX_MESSAGE_BYTES,
                max_unacked=MAX_UNACKED,
            ),
            memory=dict(
                workspace=str(self.runtime.paths.workspace),
                cards=str(self.memory.cards),
                handoff_inbox=str(self.memory.handoff_inbox),
                handoff_format=(
                    "handoff_write content must be a full Memory Handoff starting with "
                    "'# Memory Handoff' (see docs/claude-code-memory-handoff.md); it is linted "
                    "with brigade handoff lint before it lands and never overwrites."
                ),
            ),
        )

    def duet_capabilities(self) -> dict[str, Any]:
        """Static description only: no IO, no subprocess, no config read."""
        return dict(
            bridge_version="6",
            connector=self.profile.pack_id,
            operating_instructions=self.profile.operating_instructions,
            messaging_enabled=self.runtime.messaging_enabled is True,
            tools=sorted(self.profile.tools),
            absent_tools=sorted(DUET_EXCLUDED_TOOLS - self.profile.tools),
            dispatch=dict(
                enabled=self.runtime.dispatch_enabled,
                tools=sorted(self.profile.tools & DUET_DISPATCH_TOOLS),
                limits=(
                    dict(
                        repositories=sorted(self.runtime.dispatch_limits.repositories)
                        if self.runtime.dispatch_limits.repositories is not None
                        else None,
                        max_open=self.runtime.dispatch_limits.max_open,
                        daily_limit=self.runtime.dispatch_limits.daily_limit,
                        prompt_max_bytes=self.runtime.dispatch_limits.prompt_max_bytes,
                    )
                    if self.runtime.dispatch_limits is not None
                    else None
                ),
            ),
            fleet_hub=dict(
                read_only=True,
                start_of_task="fleet_digest, then fleet_overview and fleet_work_next for full detail",
                note="Fleet Hub views never reserve, claim or change hub state. fleet_work_next is a dry run.",
            ),
            inbox=dict(
                usage=(
                    "The inbox is hub-backed and addressed to "
                    + self.runtime.hub.principal
                    + ". Call inbox_list to read pending messages oldest first, inbox_reply to answer and "
                    "inbox_ack after handling. message_send starts messages; message_status checks sent messages. "
                    "message_send accepts these names in to: "
                    + ", ".join(sorted(self.runtime.hub.recipients))
                    + ". Do not auto-reply to agent messages that contain no new facts; ack them instead."
                ),
                trust=self.profile.trust_notice,
                max_message_bytes=MAX_MESSAGE_BYTES,
                message_send_per_hour=self.profile.send_limit_per_hour,
            ),
            memory=dict(
                handoff_format=(
                    "handoff_write content must be a full Memory Handoff starting with '# Memory Handoff'; "
                    "it is linted with brigade handoff lint before it lands, never overwrites, and is "
                    "ingested on the next ingest run."
                ),
            ),
            skills="skill_search and skill_get read the shared skill registry. Skills withheld by the owner "
            "report skill_not_found.",
        )


def build_tools(runtime: Runtime, profile: ConnectorProfile = DOT) -> tuple[DotTools, BridgeState, HubInbox]:
    require(runtime.schema == profile.runtime_schema, "invalid_config")
    profile = runtime_profile(profile, runtime.dispatch_enabled, messaging_enabled=bool(runtime.messaging_enabled))
    state = BridgeState(runtime.paths.state_dir)
    try:
        commands = CommandRunner()
        # Profiles without fleet dispatch never construct the T3 fleet client.
        fleet = FleetTools(runtime, state, commands=commands) if profile.fleet_dispatch else None
        if profile.pack_id == "duet" and runtime.dispatch_enabled:
            fleet = DuetFleetTools(runtime, state, commands=commands)
        hub_inbox = HubInbox(
            state,
            runtime.hub,
            profile=profile,
            messaging_enabled=profile.fleet_dispatch or runtime.messaging_enabled is not False,
        )
        inbox = InboxTools(runtime, state, hub_inbox)
        hub = HubTools(runtime, state, commands=commands, fleet=fleet, inbox=inbox)
        # A principal profile's memory calls use the credential the listener pinned at startup.
        memory = MemoryTools(runtime, commands=commands, profile=profile, authority=hub_inbox.capture)
        tools = DotTools(runtime, state, fleet=fleet, hub=hub, memory=memory, inbox=inbox, profile=profile)
        return tools, state, hub_inbox
    except Exception:
        state.close()
        raise
