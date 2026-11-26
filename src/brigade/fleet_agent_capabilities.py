"""Fleet Hub messaging capabilities by agent kind."""

from dataclasses import dataclass
from typing import Literal

from .dot.profile import PROFILES, runtime_profile
from .fleet_inbox_delivery import TOOL_POLL_INTERVAL_SECONDS

PrincipalSource = Literal["node", "agent:duet", "agent:miller", "agent:cloud-agent"]
Wake = Literal["next-event", "next-start", "hosted-subscription"]


@dataclass(frozen=True)
class AgentCapability:
    kind: str
    principal_source: PrincipalSource
    connector_profile: str | None
    send: str | None
    read: str | None
    ack: str | None
    reply: str | None
    status: str | None
    live_delivery: frozenset[str] | None
    wake: Wake
    limits: str
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
        evidence=evidence,
    )


def _connector(
    kind: str, principal: PrincipalSource, wake: Wake, limits: str, evidence: str | None = None
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
        "agent:duet",
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
        "agent:cloud-agent",
        "next-start",
        "`messaging_enabled: true` required for all messaging tools.",
    ),
)


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
        "Connector tools depend on runtime settings and grants. For a ChatGPT dot, "
        "`hosted-subscription` means an enabled [MCP Events](fleet-mcp-events.md) subscription can "
        "wake a task that checks its inbox. `next-start` means the agent can check its inbox when "
        "a task starts.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in AGENT_CAPABILITIES:
        delivery = ", ".join(f"`{event}`" for event in sorted(row.live_delivery or ()))
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
            delivery or "-",
            f"`{row.wake}`",
            row.limits,
            f"[Observed]({row.evidence})" if row.evidence else "-",
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
