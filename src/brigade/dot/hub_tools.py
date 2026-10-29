"""Read-only Fleet Hub projections and concurrent task-start digest."""

from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
import time
from typing import Any
from .contracts import DotError
from .process import CommandRunner, child_environment, child_path, parse_json_bytes
from .runtime_config import Runtime
from .state import BridgeState
from .fleet_tools import FleetTools
from .inbox_tools import InboxTools

BRIDGE_VERSION = "6"
BRIGADE_TIMEOUT = 60
MAX_SMALL_CAPTURE = 2 * 1024 * 1024
MAX_FLEET_CAPTURE = 4 * 1024 * 1024
MAX_STATUS_ALL_CAPTURE = 48 * 1024 * 1024
DIGEST_BRIGADE_TIMEOUT = 15
DIGEST_GH_TIMEOUT = 20
OCTOPOOL_WHOAMI_TIMEOUT = 8
OCTOPOOL_ROUTE_TTL = 600
DIGEST_DEADLINE = 40
DIGEST_PR_LIMIT = 30


class HubTools:
    def __init__(
        self,
        runtime: Runtime,
        state: BridgeState,
        *,
        commands: CommandRunner,
        fleet: FleetTools | None,
        inbox: InboxTools,
    ) -> None:
        self.runtime, self.state = runtime, state
        self.commands, self.fleet, self.inbox = commands, fleet, inbox
        self.environment = child_environment(child_path(runtime.paths.bin))
        self._github_route: tuple[str, float] | None = None

    def _brigade_json(
        self, tail: list[str], *, timeout: float = BRIGADE_TIMEOUT, max_output: int = MAX_SMALL_CAPTURE
    ) -> tuple[Any, str | None]:
        result = self.commands.run(
            [self.runtime.paths.bin.brigade, *tail], timeout=timeout, max_output=max_output, env=self.environment
        )
        if result.timed_out:
            return None, "brigade_timeout"
        if result.overflow:
            return None, "output_too_large"
        value = parse_json_bytes(result.stdout)
        if value is None:
            return None, "brigade_unavailable"
        return value, None

    def fleet_overview(self, include_terminal: bool = False, timeout: float = BRIGADE_TIMEOUT) -> dict[str, Any]:
        overview: dict[str, Any] = {}
        status, error = self._brigade_json(
            ["fleet", "status", "--json"] + (["--all"] if include_terminal else []),
            max_output=MAX_STATUS_ALL_CAPTURE if include_terminal else MAX_SMALL_CAPTURE,
            timeout=timeout,
        )
        if error or not isinstance(status, dict) or not isinstance(status.get("runs"), list):
            overview["status"] = {"error": error or "brigade_unavailable"}
        else:
            runs = [run for run in status["runs"] if isinstance(run, dict)]
            counts: dict[str, int] = {}
            for run in runs:
                counts[str(run.get("state"))] = counts.get(str(run.get("state")), 0) + 1
            runs.sort(key=lambda run: str(run.get("received_at") or run.get("ts") or ""), reverse=True)
            keys: tuple[str, ...] = (
                "run_id",
                "node_id",
                "repo",
                "repo_identity",
                "seat",
                "harness",
                "state",
                "exit_status",
                "ts",
                "received_at",
            )
            overview["status"] = dict(
                include_terminal=include_terminal,
                run_count=len(runs),
                by_state=dict(sorted(counts.items(), key=lambda item: -item[1])[:20]),
                latest_runs=[{key: run.get(key) for key in keys} for run in runs[:25]],
                routing=status.get("preference"),
            )
        claims, error = self._brigade_json(["fleet", "claims", "--json"], timeout=timeout)
        if error or not isinstance(claims, dict) or not isinstance(claims.get("claims"), list):
            overview["claims"] = {"error": error or "brigade_unavailable"}
        else:
            keys = (
                "target",
                "harness",
                "role",
                "job",
                "session",
                "owner_node",
                "owner_conductor",
                "acquired_at",
                "renewed_at",
                "expires_at",
                "expired",
            )
            items = [claim for claim in claims["claims"] if isinstance(claim, dict)]
            overview["claims"] = dict(
                count=len(items), claims=[{key: claim.get(key) for key in keys} for claim in items[:50]]
            )
        return overview

    # -- bridge v4: read-only Fleet Hub views -------------------------------------------

    def _hub_read(
        self, tail: list[str], *, max_output: int = MAX_SMALL_CAPTURE, timeout: float = BRIGADE_TIMEOUT
    ) -> tuple[Any, dict[str, Any] | None]:
        """Run one read-only `brigade fleet ... --json`; return (dict, None) or (None, error dict)."""
        value, error = self._brigade_json(["fleet", *tail, "--json"], max_output=max_output, timeout=timeout)
        if error:
            return None, {"error": error}
        if isinstance(value, dict) and isinstance(value.get("error"), str) and len(value) <= 3:
            # Hub error envelope ({"code", "error"}): keep the code and a short reason.
            return None, {"error": "hub_error", "code": str(value.get("code"))[:64], "detail": value["error"][:300]}
        return value, None

    @staticmethod
    def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {key: item.get(key) for key in keys}

    @staticmethod
    def _tally(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            counts[str(item.get(key))] = counts.get(str(item.get(key)), 0) + 1
        return dict(sorted(counts.items(), key=lambda pair: -pair[1])[:30])

    WORK_KEYS = (
        "work_id",
        "title",
        "status",
        "kind",
        "priority",
        "burn_eligible",
        "burn_rank",
        "execution_mode",
        "attempt_count",
        "blocker",
        "ready_at",
        "review_after",
        "updated_at",
    )

    def _work_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        summary = self._pick(item, self.WORK_KEYS)
        links = item.get("links")
        links = links if isinstance(links, list) else []
        summary["links"] = [link.get("display_ref") for link in links[:5] if isinstance(link, dict)]
        return summary

    def fleet_report_session(self, report: dict[str, Any], publish: bool = False) -> dict[str, Any]:
        """One bounded stdin report, under runtime identity and the actual gate."""
        from ..fleet_dot import MAX_REPORT_BYTES, report_session
        from .contracts import require, task_report

        require(self.runtime.dispatch_enabled, "dispatch_disabled")
        task_report(report)
        projected = report_session(report)
        data = json.dumps(report, ensure_ascii=False).encode("utf-8")
        require(len(data) <= MAX_REPORT_BYTES)
        argv = [self.runtime.paths.bin.brigade, "fleet", "dot", "report", "--expected-node", self.runtime.hub.node_id]
        if publish:
            argv.append("--publish")
        result = self.commands.run(argv, stdin=data, timeout=BRIGADE_TIMEOUT, max_output=65536, env=self.environment)
        value = None if result.timed_out or result.overflow else parse_json_bytes(result.stdout)
        status = value.get("status") if isinstance(value, dict) else None
        if not publish and result.returncode == 0 and status == "dry-run":
            return projected
        projected["mode"] = "publish" if publish else "dry-run"
        projected["status"] = "unknown"
        projected["components"]["presence"] = "unknown:publication_unconfirmed"
        if publish and status == "refused":
            projected["status"] = "refused"
            projected["components"]["presence"] = "failed:publication_refused"
        elif (
            publish
            and result.returncode == 0
            and isinstance(value, dict)
            and status == "confirmed"
            and value.get("summaries_persisted") is True
            and value.get("confirmed_node") == self.runtime.hub.node_id
            and value.get("presence_payload") == projected["presence_payload"]
        ):
            projected.update(status="confirmed", summaries_persisted=True, confirmed_node=self.runtime.hub.node_id)
            projected["components"]["presence"] = "published"
        return projected

    def fleet_sessions(self, include_ended: bool = False, limit: int = 50) -> dict[str, Any]:
        value, error = self._hub_read(
            ["sessions"] + (["--all"] if include_ended else []),
            max_output=MAX_FLEET_CAPTURE if include_ended else MAX_SMALL_CAPTURE,
        )
        if error:
            return error
        if not isinstance(value, list):
            return {"error": "brigade_unavailable"}
        sessions = [item for item in value if isinstance(item, dict)]
        sessions.sort(key=lambda item: str(item.get("heartbeat_at") or item.get("started_at") or ""), reverse=True)
        keys = (
            "session_id",
            "node_id",
            "harness",
            "repo_label",
            "repo_identity",
            "branch",
            "checkout_path",
            "state",
            "started_at",
            "heartbeat_at",
            "ended_at",
            "expires_at",
            "cloud_context",
            "presence_state",
            "provider_lifecycle",
            "inventory_coverage",
            "evidence_verification",
        )
        shown = []
        for item in sessions[:limit]:
            entry = self._pick(item, keys)
            dirty = item.get("dirty_paths")
            entry["dirty_count"] = len(dirty) if isinstance(dirty, list) else None
            entry["dirty_truncated"] = item.get("dirty_truncated")
            shown.append(entry)
        return dict(
            include_ended=include_ended,
            session_count=len(sessions),
            by_state=self._tally(sessions, "state"),
            by_node=self._tally(sessions, "node_id"),
            by_harness=self._tally(sessions, "harness"),
            sessions=shown,
        )

    def fleet_work_list(self, source: str | None = None, status: str | None = None, limit: int = 25) -> dict[str, Any]:
        value, error = self._hub_read(["work", "list"] + (["--source", source] if source else []))
        if error:
            return error
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            return {"error": "brigade_unavailable"}
        items = [item for item in value["items"] if isinstance(item, dict)]
        matched = [item for item in items if status is None or item.get("status") == status]
        return dict(
            source=source,
            status=status,
            page_count=len(items),
            by_status=self._tally(items, "status"),
            match_count=len(matched),
            items=[self._work_summary(item) for item in matched[:limit]],
            more_on_hub=bool(value.get("next_cursor")),
            next="Read one item with fleet_work_show(work_id).",
        )

    def fleet_work_show(self, work_id: str) -> dict[str, Any]:
        value, error = self._hub_read(["work", "show", work_id])
        if error:
            return error
        if not isinstance(value, dict) or not isinstance(value.get("item"), dict):
            return {"error": "brigade_unavailable"}
        return value

    def fleet_work_next(
        self, consumer: str | None = None, workload: str | None = None, timeout: float = BRIGADE_TIMEOUT
    ) -> dict[str, Any]:
        # `brigade fleet work next` is a dry-run route: read-only and never reserves.
        tail = (
            ["work", "next"]
            + (["--consumer", consumer] if consumer else [])
            + (["--workload", workload] if workload else [])
        )
        value, error = self._hub_read(tail, timeout=timeout)
        if error:
            return error
        if not isinstance(value, dict):
            return {"error": "brigade_unavailable"}
        considered = [item for item in value.get("considered") or [] if isinstance(item, dict)]
        return dict(
            schema=value.get("schema"),
            work_id=value.get("work_id"),
            item=value.get("item"),
            route=value.get("route"),
            considered_count=len(considered),
            considered_by_bucket=self._tally(considered, "bucket"),
            considered=considered[:20],
            reserved=False,
        )

    def fleet_work_burn(self, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        value, error = self._hub_read(
            ["work", "burn", "--limit", str(limit)] + (["--cursor", cursor] if cursor else [])
        )
        if error:
            return error
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            return {"error": "brigade_unavailable"}
        items = [item for item in value["items"] if isinstance(item, dict)]
        return dict(
            exclusions=value.get("exclusions"),
            item_count=len(items),
            items=[self._work_summary(item) for item in items[:limit]],
            next_cursor=value.get("next_cursor"),
        )

    def fleet_models(self, consumer: str = "brigade-run") -> dict[str, Any]:
        value, error = self._hub_read(["models", "list", "--consumer", consumer])
        if error:
            return error
        if not isinstance(value, dict) or not isinstance(value.get("seats"), list):
            return {"error": "brigade_unavailable"}
        keys = ("seat", "binding", "provider", "model", "reasoning", "enabled", "limit")
        return dict(
            consumer=value.get("consumer"),
            roster_revision=value.get("roster_revision"),
            seats=[self._pick(seat, keys) for seat in value["seats"][:200] if isinstance(seat, dict)],
        )

    def fleet_cloud(self, include_released: bool = False, limit: int = 50) -> dict[str, Any]:
        value, error = self._hub_read(["cloud"] + (["--all"] if include_released else []))
        if error:
            return error
        if not isinstance(value, dict) or not isinstance(value.get("leases"), list):
            return {"error": "brigade_unavailable"}
        leases = [lease for lease in value["leases"] if isinstance(lease, dict)]
        leases.sort(key=lambda lease: str(lease.get("admitted_at") or ""), reverse=True)
        keys = (
            "lease_id",
            "provider",
            "label",
            "repo",
            "state",
            "admitted_at",
            "renewed_at",
            "released_at",
            "expires_at",
            "expired",
            "owner_node",
            "owner_conductor",
            "provider_task_id",
            "artifact_ref",
        )
        return dict(
            include_released=include_released,
            policy=value.get("policy"),
            lease_count=len(leases),
            by_state=self._tally(leases, "state"),
            leases=[self._pick(lease, keys) for lease in leases[:limit]],
        )

    def fleet_hub_policy(self) -> dict[str, Any]:
        # Only `policy show` and `policy status`; preview/save/rollback/route/observe are never reachable.
        result = {}
        show, error = self._hub_read(["policy", "show"])
        if error or not isinstance(show, dict):
            result["policy"] = error or {"error": "brigade_unavailable"}
        else:
            result["policy"] = self._pick(
                show, ("schema", "version", "digest", "created_at", "actor", "reason", "parent_revision", "document")
            )
        status, error = self._hub_read(["policy", "status"])
        result["status"] = (
            (error or {"error": "brigade_unavailable"}) if error or not isinstance(status, dict) else status
        )
        return result

    def fleet_digest(self, owner: str) -> dict[str, Any]:
        """One read-only call. Sections run concurrently, each with its own timeouts;
        a failing or slow section reports {"error": ...} and the others still return."""
        sections = {
            "fleet_overview": self._digest_overview,
            "fleet_work_next": self._digest_work_next,
            "inbox": self._digest_inbox,
            "subscriptions": lambda: {"count": self.state.subscription_count(owner)},
            "brigade_prs": self._digest_prs,
        }
        pool = ThreadPoolExecutor(max_workers=len(sections), thread_name_prefix="fleet-digest")
        try:
            futures = {name: pool.submit(section) for name, section in sections.items()}
            futures_wait(futures.values(), timeout=DIGEST_DEADLINE)
            digest: dict[str, Any] = {"bridge_version": BRIDGE_VERSION}
            for name, future in futures.items():
                if not future.done():
                    digest[name] = {"error": "section_timeout"}
                    continue
                try:
                    digest[name] = future.result()
                except DotError as error:
                    digest[name] = {"error": str(error)}
                except Exception:
                    digest[name] = {"error": "section_unavailable"}
            digest["section_errors"] = sorted(
                name for name in sections if isinstance(digest[name], dict) and "error" in digest[name]
            )
            return digest
        finally:
            # Unfinished children are bounded by their own subprocess timeouts.
            pool.shutdown(wait=False, cancel_futures=True)

    def _digest_overview(self) -> dict[str, Any]:
        overview = self.fleet_overview(timeout=DIGEST_BRIGADE_TIMEOUT)
        status, claims = overview.get("status", {}), overview.get("claims", {})
        if "error" not in status:
            run_keys = ("run_id", "node_id", "repo", "seat", "state", "exit_status", "received_at")
            status = dict(
                run_count=status.get("run_count"),
                by_state=status.get("by_state"),
                latest_runs=[self._pick(run, run_keys) for run in status.get("latest_runs", [])[:10]],
            )
        if "error" not in claims:
            claim_keys = ("target", "harness", "role", "owner_node", "expires_at", "expired")
            claims = dict(
                count=claims.get("count"),
                claims=[self._pick(claim, claim_keys) for claim in claims.get("claims", [])[:20]],
            )
        return {"status": status, "claims": claims}

    def _digest_work_next(self) -> dict[str, Any]:
        value = self.fleet_work_next(timeout=DIGEST_BRIGADE_TIMEOUT)
        if "error" in value:
            return value
        item = value.get("item") if isinstance(value.get("item"), dict) else {}
        return dict(
            work_id=value.get("work_id"),
            item=self._pick(item, ("title", "status", "kind", "priority", "burn_rank")) if item else None,
            route=value.get("route"),
            considered_count=value.get("considered_count"),
            considered_by_bucket=value.get("considered_by_bucket"),
            reserved=False,
        )

    def _digest_inbox(self) -> dict[str, Any]:
        listing = self.inbox.inbox_list(unacked_only=True, limit=5)
        keys = ("id", "created_at", "sender", "subject", "kind", "priority")
        return {
            "unacked": listing["unacked_total"],
            "oldest": [self._pick(message, keys) for message in listing["messages"]],
        }

    def _github_cli(self) -> str:
        """("octopool", argv0, env) when `octopool whoami` works (cached), else plain gh."""
        if self.runtime.paths.bin.octopool is None:
            return "gh"
        now = time.monotonic()
        cached = self._github_route
        if cached is None or now - cached[1] > OCTOPOOL_ROUTE_TTL:
            try:
                result = self.commands.run(
                    [self.runtime.paths.bin.octopool, "whoami"],
                    timeout=OCTOPOOL_WHOAMI_TIMEOUT,
                    max_output=65536,
                    env=self.environment,
                )
                route = "octopool" if result.returncode == 0 and not result.timed_out and not result.overflow else "gh"
            except DotError:
                route = "gh"
            self._github_route = cached = (route, now)
        return cached[0]

    def _gh_pr_list(self, via: str) -> tuple[Any, str | None]:
        tail = [
            "pr",
            "list",
            "--repo",
            self.runtime.digest_pr_repo,
            "--state",
            "open",
            "--limit",
            str(DIGEST_PR_LIMIT),
            "--json",
            "number,title,headRefOid,isDraft,mergeable,statusCheckRollup",
        ]
        if via == "octopool":
            assert self.runtime.paths.bin.octopool is not None
            argv, env = (
                [self.runtime.paths.bin.octopool, "gh", *tail],
                child_environment(child_path(self.runtime.paths.bin), {"OCTOPOOL_NO_FALLBACK": "1"}),
            )
        else:
            argv, env = [self.runtime.paths.bin.gh, *tail], self.environment
        result = self.commands.run(argv, timeout=DIGEST_GH_TIMEOUT, max_output=MAX_SMALL_CAPTURE, env=env)
        if result.timed_out:
            return None, "gh_timeout"
        if result.overflow:
            return None, "output_too_large"
        value = parse_json_bytes(result.stdout)
        if result.returncode != 0 or not isinstance(value, list):
            return None, "gh_unavailable"
        return value, None

    @staticmethod
    def _check_rollup(checks: Any) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}
        for check in checks if isinstance(checks, list) else []:
            if not isinstance(check, dict):
                continue
            if check.get("__typename") == "StatusContext":
                value = check.get("state") or "PENDING"
            elif check.get("status") not in (None, "COMPLETED"):
                value = "PENDING"
            else:
                value = check.get("conclusion") or "PENDING"
            value = str(value).upper()[:32]
            counts[value] = counts.get(value, 0) + 1
        failing = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
        passing = {"SUCCESS", "NEUTRAL", "SKIPPED"}
        if not counts:
            state = "none"
        elif failing & set(counts):
            state = "failure"
        elif set(counts) - passing:
            state = "pending"
        else:
            state = "success"
        return state, counts

    def _digest_prs(self) -> dict[str, Any]:
        via = self._github_cli()
        value, error = self._gh_pr_list(via)
        fallback_error = None
        if error and via == "octopool":
            fallback_error, via = error, "gh"
            value, error = self._gh_pr_list(via)
        if error:
            return {"error": error, "via": via}
        prs = []
        for pr in value[:DIGEST_PR_LIMIT]:
            if not isinstance(pr, dict):
                continue
            state, counts = self._check_rollup(pr.get("statusCheckRollup"))
            head = pr.get("headRefOid")
            prs.append(
                dict(
                    number=pr.get("number"),
                    title=str(pr.get("title") or "")[:120],
                    head_sha8=head[:8] if isinstance(head, str) else None,
                    draft=pr.get("isDraft"),
                    mergeable=pr.get("mergeable"),
                    checks=state,
                    check_counts=counts,
                )
            )
        result = {"repo": self.runtime.digest_pr_repo, "via": via, "count": len(prs), "prs": prs}
        if fallback_error:
            result["octopool_error"] = fallback_error
        return result
