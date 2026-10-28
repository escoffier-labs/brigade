"""Synthetic MCP reports for three fake machines. Fake server names, no real hosts, paths, or values."""

from __future__ import annotations

from datetime import datetime, timezone

SCOPE = {"claude": "project", "cursor": "project", "codex": "user", "gemini": "user"}


def server(name, harness, status="current", *, transport="stdio", enabled=True, managed=True, host=None):
    return {
        "name": name,
        "harness": harness,
        "scope": SCOPE[harness],
        "transport": transport,
        "enabled": enabled,
        "status": status,
        "managed": managed,
        "remote_host": host,
    }


def harness(name, found=True, readable=True):
    return {"harness": name, "scope": SCOPE[name], "config_found": found, "readable": readable}


def report(observed, servers, harnesses, *, version="0.31.0", catalog=True):
    return {
        "schema_version": 1,
        "brigade_version": version,
        "observed_at": observed,
        "catalog_present": catalog,
        "truncated": False,
        "servers": servers,
        "harnesses": harnesses,
    }


def reports(now: datetime | None = None, *, hub_version: str = "0.31.0") -> dict[str, dict]:
    """Keys are the machine slot: linux1, linux2, windows1."""
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    linux1 = report(
        stamp,
        [
            server("sample-memory", "claude"),
            server("sample-memory", "codex"),
            server("sample-memory", "gemini"),
            server("sample-search", "claude", "stale"),
            server("sample-search", "codex"),
            server("sample-docs", "claude", transport="http", host="docs.sample.example"),
            server("sample-browser", "claude", "disabled", enabled=False),
        ],
        [harness("claude"), harness("codex"), harness("cursor", found=False), harness("gemini")],
        version=hub_version,
    )
    linux2 = report(
        stamp,
        [
            server("sample-memory", "claude"),
            server("sample-memory", "codex"),
            server("sample-search", "claude"),
            server("sample-docs", "claude", transport="http", host="docs.sample.example"),
            server(
                "sample-browser", "cursor", "unmanaged", managed=False, transport="http", host="browser.sample.example"
            ),
        ],
        [harness("claude"), harness("codex"), harness("cursor"), harness("gemini", found=False)],
        version=hub_version,
    )
    windows1 = report(
        stamp,
        [
            server("sample-memory", "claude"),
            server("sample-memory", "codex"),
            server("sample-search", "claude"),
            server("sample-docs", "claude", "missing", transport="http", host="docs.sample.example"),
        ],
        [harness("claude"), harness("codex"), harness("cursor", found=False), harness("gemini", readable=False)],
        version=hub_version,
    )
    return {"linux1": linux1, "linux2": linux2, "windows1": windows1}


def account_row(name, status, host="mail.sample.example"):
    return {
        "name": name,
        "harness": "claude",
        "scope": "account",
        "transport": "http",
        "enabled": True,
        "status": status,
        "managed": False,
        "remote_host": host,
    }


def with_account_connectors(report: dict, *, extra=()) -> dict:
    """A copy of *report* that also lists fake claude.ai account connectors (opt-in data)."""
    rows = [
        account_row("claude.ai Sample Mail", "connected"),
        account_row("claude.ai Sample Drive", "needs_auth", "drive.sample.example"),
        *extra,
    ]
    marker = {"harness": "claude", "scope": "account", "config_found": True, "readable": True}
    return {**report, "servers": [*report["servers"], *rows], "harnesses": [*report["harnesses"], marker]}
