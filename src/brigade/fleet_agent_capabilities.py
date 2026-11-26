"""Fleet Hub messaging, memory, and work capabilities by agent kind."""

from dataclasses import dataclass
from typing import Literal

from .dot.cloud_runtime import CLOUD_AGENT_PRINCIPAL
from .dot.profile import PROFILES, runtime_profile
from .dot.runtime_config import DUET_PRINCIPAL
from .fleet_inbox_delivery import TOOL_POLL_INTERVAL_SECONDS

Wake = Literal["next-event", "next-start", "hosted-subscription"]


@dataclass(frozen=True)
class AgentCapability:
    kind: str
    # "node", or the hub principal the connector runtime enforces.
    principal_source: str
    connector_profile: str | None
    send: str | None
    read: str | None
    ack: str | None
    reply: str | None
    status: str | None
    live_delivery: frozenset[str] | None
    wake: Wake
    limits: str
    memory_principal: str
    memory_read: tuple[str, ...]
    handoff_write: str | None
    work_view: tuple[str, ...]
    memory_status: str
    work_status: str
    evidence: str | None = None


def _local(kind: str, evidence: str) -> AgentCapability:
    return AgentCapability(
        kind=kind,
        principal_source="node",
        connector_profile=None,
        send="fleet_send",
        read="fleet_inbox",
        ack="fleet_ack",
        reply="fleet_reply",
        status="fleet_sent",
        live_delivery=frozenset({"UserPromptSubmit", "PostToolUse"}),
        wake="next-event",
        limits=f"Tool-event polls are at least {TOOL_POLL_INTERVAL_SECONDS:g} seconds apart.",
        memory_principal="node",
        memory_read=("brigade memory search",),
        handoff_write="brigade handoff draft",
        work_view=("brigade fleet work list", "brigade fleet sessions", "brigade fleet claims"),
        memory_status="Hub search requires `[hub]` read or sync mode. Handoff drafts go to a local inbox.",
        work_status="",
        evidence=evidence,
    )


def _connector(
    kind: str,
    principal: str,
    wake: Wake,
    limits: str,
    evidence: str | None = None,
    *,
    memory_status: str = "",
    work_status: str = "",
) -> AgentCapability:
    profile = runtime_profile(PROFILES[kind], dispatch_enabled=True, messaging_enabled=True)
    tools = {name: name for name in profile.tools}
    if profile.send_limit_per_hour is not None:
        limits += f" Send and reply share {profile.send_limit_per_hour} messages per hour."
    return AgentCapability(
        kind=kind,
        principal_source=principal,
        connector_profile=profile.pack_id,
        send=tools.get("message_send"),
        read=tools.get("inbox_list"),
        ack=tools.get("inbox_ack"),
        reply=tools.get("inbox_reply"),
        status=tools.get("message_status"),
        live_delivery=None,
        wake=wake,
        limits=limits,
        memory_principal=principal if profile.principal_memory else "host [memory_hub] read credential",
        memory_read=tuple(name for name in ("memory_search", "memory_card") if name in tools),
        handoff_write=tools.get("handoff_write"),
        work_view=tuple(name for name in ("fleet_work_list", "fleet_sessions", "fleet_overview") if name in tools),
        memory_status=memory_status,
        work_status=work_status,
        evidence=evidence,
    )


AGENT_CAPABILITIES = (
    _local(
        "local-claude-code",
        "https://github.com/escoffier-labs/brigadestack/issues/1887#issuecomment-7179649896",
    ),
    _local(
        "local-codex",
        "https://github.com/escoffier-labs/brigadestack/issues/1887#issuecomment-7179839383",
    ),
    _connector(
        "duet",
        DUET_PRINCIPAL,
        "next-start",
        "`messaging_enabled` required for send and reply.",
        "https://github.com/escoffier-labs/brigadestack/issues/2592#issuecomment-7180093019",
    ),
    _connector(
        "dot",
        "agent:miller",
        "hosted-subscription",
        "Send follows `messaging_enabled`, or `dispatch_enabled` when unset. Subscription requires `events_enabled`.",
    ),
    _connector(
        "cloud-agent",
        CLOUD_AGENT_PRINCIPAL,
        "next-start",
        "`messaging_enabled: true` required for all messaging tools. "
        "All cloud workers share one principal, so the Hub cannot tell one cloud run from another. "
        "The deployed `tool_grants` deny `handoff_write`.",
        memory_status=(
            "not yet: the connector has no memory scope granted. Read access to the shared fleet scope only "
            "(never private scopes) is a pending decision"
        ),
        work_status="not yet: the cloud-agent profile has no work view",
    ),
)


def _code_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "-"


def render_markdown() -> str:
    """Render the Capability table section in docs/fleet-inbox-delivery.md."""
    columns = (
        "Agent kind",
        "Principal source",
        "Connector profile",
        "Send",
        "Read",
        "Ack",
        "Reply",
        "Status",
        "Live delivery hook events",
        "Wake",
        "Limits",
        "Evidence",
    )
    lines = [
        "## Capability table",
        "",
        "### Messaging",
        "",
        "Connector tools depend on runtime settings and grants. For a ChatGPT dot, "
        "`hosted-subscription` means an enabled [MCP Events](fleet-mcp-events.md) subscription can "
        "wake a task that checks its inbox. `next-start` means the agent can check its inbox when "
        "a task starts.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in AGENT_CAPABILITIES:
        cells = (
            *(
                f"`{value}`" if value else "-"
                for value in (
                    row.kind,
                    row.principal_source,
                    row.connector_profile,
                    row.send,
                    row.read,
                    row.ack,
                    row.reply,
                    row.status,
                )
            ),
            _code_list(tuple(sorted(row.live_delivery or ()))),
            f"`{row.wake}`",
            row.limits,
            f"[Observed]({row.evidence})" if row.evidence else "-",
        )
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend(
        (
            "",
            "### Memory and work visibility",
            "",
            "The connector columns list tools the profile can expose. The status and limits cells describe "
            "current access. Dot and Duet write handoff files to the host inbox without a Hub principal. "
            "Local memory search uses the node credential by default and supports a memory hub override.",
            "",
            "| Agent kind | Memory principal | Memory read | Handoff write | Work view | Memory status | Work status |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        )
    )
    for row in AGENT_CAPABILITIES:
        cells = (
            f"`{row.kind}`",
            f"`{row.memory_principal}`",
            _code_list(row.memory_read),
            f"`{row.handoff_write}`" if row.handoff_write else "-",
            _code_list(row.work_view),
            row.memory_status or "-",
            row.work_status or "-",
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
