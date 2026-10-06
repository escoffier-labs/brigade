"""Bounded, pure configuration facts from caller-normalized literal definitions.

No YAML parser, reference fetching, credential access, probes or mutation. Caller
attestations are provenance, not independent verification of their truth.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .claude_actions_evidence import ActionRunEvidence, _repository, _sha, _timestamp

_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
_REF = re.compile(r"(?:[0-9a-fA-F]{40}|v[0-9]+(?:\.[0-9]+){0,2}(?:-[A-Za-z0-9.-]+)?)")
_REFERENCE = re.compile(r"\$\{\{\s*(secrets|inputs)\.[A-Za-z_][A-Za-z0-9_]{0,99}\s*\}\}")
_AUTHORITIES = {
    "caller-normalized-definition",
    "operator-report",
    "supported-authorized-metadata",
    "normalized-actions-run",
}
_STATES = {"confirmed", "operator-reported", "absent-in-complete-scope", "unknown", "unsupported", "access-denied"}
_OBSERVATIONS = {
    "app-installation": {"installed", "uninstalled"},
    "app-access": {"accessible", "inaccessible"},
    "workflow-enabled": {"enabled", "disabled"},
    "managed-review": {"enabled", "disabled"},
}
_EVENTS = {
    "branch_protection_rule",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "deployment",
    "deployment_status",
    "discussion",
    "discussion_comment",
    "fork",
    "gollum",
    "issue_comment",
    "issues",
    "label",
    "merge_group",
    "milestone",
    "page_build",
    "public",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_target",
    "push",
    "registry_package",
    "release",
    "repository_dispatch",
    "schedule",
    "status",
    "watch",
    "workflow_call",
    "workflow_dispatch",
    "workflow_run",
}
_PERMISSIONS = {
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "security-events",
    "statuses",
}
_AUTH_INPUTS = {
    "anthropic_api_key": "api-key",
    "claude_code_oauth_token": "subscription-oauth",
    "anthropic_federation_rule_id": "federation",
    "use_bedrock": "bedrock",
    "use_vertex": "vertex",
    "use_foundry": "foundry",
}


def _path(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 255
        and bool(_PATH.fullmatch(value))
        and not any(part in {".", ".."} for part in value.split("/"))
    )


@dataclass(frozen=True)
class Scope:
    repository: str
    path: str
    revision: str

    def __post_init__(self) -> None:
        repo, revision = _repository(self.repository), _sha(self.revision)
        if repo is None or revision is None or not _path(self.path):
            raise ValueError("invalid diagnostic scope")
        object.__setattr__(self, "repository", repo)
        object.__setattr__(self, "revision", revision)


@dataclass(frozen=True)
class Provenance:
    authority: str
    scope: Scope
    source_url: str
    observed_at: str

    def __post_init__(self) -> None:
        stamp = _timestamp(self.observed_at)
        if not isinstance(self.scope, Scope) or stamp is None or self.authority not in _AUTHORITIES:
            raise ValueError("invalid diagnostic provenance")
        repo = self.scope.repository
        allowed = {
            f"https://github.com/{repo}",
            f"https://github.com/{repo}/blob/{self.scope.revision}/{self.scope.path}",
            f"https://github.com/{repo}/tree/{self.scope.revision}/{self.scope.path}",
            f"https://api.github.com/repos/{repo}/installation",
            f"https://api.github.com/repos/{repo}/actions/workflows",
        }
        object_link = (
            re.fullmatch(
                rf"https://github\.com/{re.escape(repo)}/(?:pull|issues|actions/runs)/[1-9][0-9]{{0,18}}",
                self.source_url,
            )
            if isinstance(self.source_url, str)
            else None
        )
        if not isinstance(self.source_url, str) or (self.source_url not in allowed and object_link is None):
            raise ValueError("invalid diagnostic provenance URL")
        object.__setattr__(self, "observed_at", stamp)


@dataclass(frozen=True)
class Observation:
    """One independently supplied supported metadata or operator observation."""

    facet: str
    state: str
    value: str | None
    provenance: Provenance
    complete_scope: bool = False

    def __post_init__(self) -> None:
        if self.facet not in _OBSERVATIONS or self.state not in _STATES:
            raise ValueError("invalid diagnostic observation")
        if self.value is not None and self.value not in _OBSERVATIONS[self.facet]:
            raise ValueError("invalid diagnostic observation value")
        authority = self.provenance.authority
        if self.state == "operator-reported":
            if authority != "operator-report" or self.value is None:
                raise ValueError("invalid operator observation authority")
        elif authority != "supported-authorized-metadata":
            raise ValueError("invalid supported observation authority")
        if self.state == "confirmed" and self.value is None:
            raise ValueError("confirmed observation requires a value")
        if self.state == "absent-in-complete-scope" and self.complete_scope is not True:
            raise ValueError("absence requires explicit complete scope")
        if self.state not in {"confirmed", "operator-reported"} and self.value is not None:
            raise ValueError("uncertain observation cannot carry a confirmed value")


@dataclass(frozen=True)
class Definition:
    """Literal mapping attested by caller, never YAML text or an evaluated AST.

    ``complete`` certifies that normalization omitted no uses/jobs/steps. Raw
    YAML, aliases, duplicate keys and unresolved merges must use complete=False
    or a non-mapping body. External definitions carry their own exact revision.
    Enabled state is a separate supported metadata Observation.
    """

    provenance: Provenance
    body: object
    kind: str = "workflow"
    complete: bool = True
    http_status: int | None = None

    def __post_init__(self) -> None:
        if self.http_status is not None and (type(self.http_status) is not int or not 100 <= self.http_status <= 599):
            raise ValueError("invalid definition HTTP status")
        if self.kind not in {"workflow", "composite"} or self.provenance.authority not in {
            "caller-normalized-definition",
            "supported-authorized-metadata",
        }:
            raise ValueError("invalid definition provenance or kind")


@dataclass(frozen=True)
class Inventory:
    """Bounded .github/workflows inventory for one repository and commit."""

    provenance: Provenance
    definitions: Sequence[Definition] = ()
    complete: bool = False

    def __post_init__(self) -> None:
        if self.provenance.scope.path != ".github/workflows" or self.provenance.authority not in {
            "caller-normalized-definition",
            "supported-authorized-metadata",
        }:
            raise ValueError("invalid inventory provenance")


@dataclass(frozen=True)
class Bounds:
    files: int = 20
    depth: int = 4
    nodes: int = 512

    def __post_init__(self) -> None:
        for value, ceiling, minimum in ((self.files, 20, 1), (self.depth, 4, 0), (self.nodes, 512, 1)):
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError("invalid diagnostic bounds")


def _fact(facet: str, state: str, provenance: Provenance, value: object = None) -> dict[str, Any]:
    return {"facet": facet, "state": state, "value": value, "provenance": asdict(provenance)}


def _permissions(value: object) -> dict[str, str] | str | None:
    if isinstance(value, str) and value in {"read-all", "write-all"}:
        return value
    if not isinstance(value, Mapping) or len(value) > len(_PERMISSIONS):
        return None
    if any(
        key not in _PERMISSIONS or not isinstance(level, str) or level not in {"read", "write", "none"}
        for key, level in value.items()
    ):
        return None
    return dict(value)


def _auth(inputs: object, provenance: Provenance) -> list[dict[str, Any]]:
    modes: list[str] = []
    references: dict[str, str] = {}
    uncertain = not isinstance(inputs, Mapping)
    if isinstance(inputs, Mapping):
        for name, mode in _AUTH_INPUTS.items():
            value = inputs.get(name)
            if value is None or value == "" or value is False or value == "false":
                continue
            if name.startswith("use_"):
                if value is True or value == "true":
                    modes.append(mode)
                else:
                    uncertain = True
                continue
            if not isinstance(value, str) or len(value) > 4096:
                uncertain = True
                continue
            match = _REFERENCE.fullmatch(value)
            if match:
                references[name] = "secret-reference" if match[1] == "secrets" else "input-reference"
            elif "${{" in value:
                uncertain = True
                continue
            else:
                references[name] = "literal-redacted"
            modes.append(mode)
        # Record only the declaration, never provider identifiers or credentials.
        if "federation" in modes:
            organization = inputs.get("anthropic_organization_id")
            if (
                not isinstance(organization, str)
                or not organization
                or len(organization) > 4096
                or ("${{" in organization and not _REFERENCE.fullmatch(organization))
            ):
                uncertain = True
    state = "unknown" if uncertain else "confirmed" if modes else "absent-in-complete-scope"
    return [
        _fact("auth-declaration", state, provenance, {"modes": modes, "references": references}),
        _fact("secret-availability", "unknown", provenance),
        _fact("auth-readiness", "unknown", provenance),
    ]


class _Discovery:
    def __init__(self, definitions: Sequence[Definition], bounds: Bounds):
        self.bounds = bounds
        self.facts: list[dict[str, Any]] = []
        self.found = False
        self.partial = len(definitions) > bounds.files
        self.nodes = 0
        self.definitions: dict[Scope, Definition] = {}
        duplicates: set[Scope] = set()
        for item in definitions[: bounds.files]:
            if item.provenance.scope in self.definitions:
                duplicates.add(item.provenance.scope)
                self.partial = True
            self.definitions[item.provenance.scope] = item
        for scope in duplicates:
            del self.definitions[scope]

    def take(self) -> bool:
        self.nodes += 1
        if self.nodes > self.bounds.nodes:
            self.partial = True
            return False
        return True

    def reference(self, uses: object, item: Definition, depth: int, stack: frozenset[Scope], *, reusable: bool) -> None:
        if not isinstance(uses, str) or len(uses) > 512:
            self.partial = True
            return
        scope = item.provenance.scope
        target: Scope | None = None
        if uses.startswith("./"):
            path = uses[2:]
            if not reusable:
                path += "/action.yml"
            if _path(path) and (not reusable or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path)):
                target = Scope(scope.repository, path, scope.revision)
                if target not in self.definitions and not reusable:
                    target = Scope(scope.repository, path[:-4] + ".yaml", scope.revision)
        elif "@" in uses:
            address, revision = uses.rsplit("@", 1)
            parts = address.split("/", 2)
            # Other known standalone actions are not Claude, names have no role.
            if not reusable and address == "actions/checkout" and _REF.fullmatch(revision):
                return
            if len(parts) >= 2 and _sha(revision):
                repo = _repository("/".join(parts[:2]))
                path = parts[2] if len(parts) == 3 else ""
                if not reusable:
                    path = (path + "/" if path else "") + "action.yml"
                if (
                    repo
                    and _path(path)
                    and (not reusable or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path))
                ):
                    target = Scope(repo, path, revision)
                    if target not in self.definitions and not reusable:
                        target = Scope(repo, path[:-4] + ".yaml", revision)
        child = self.definitions.get(target) if target else None
        if child is None or child.kind != ("workflow" if reusable else "composite"):
            self.partial = True
            self.facts.append(_fact("definition-reference", "unknown", item.provenance))
            return
        if reusable:
            triggers = child.body.get("on") if isinstance(child.body, Mapping) else None
            declared_call = triggers == "workflow_call" or (
                isinstance(triggers, (Mapping, list)) and len(triggers) <= 40 and "workflow_call" in triggers
            )
            if not declared_call:
                self.partial = True
                self.facts.append(_fact("definition-reference", "unknown", item.provenance))
                return
        self.visit(child, depth + 1, stack)

    def steps(self, value: object, item: Definition, depth: int, stack: frozenset[Scope]) -> None:
        if not isinstance(value, list) or len(value) > 64:
            self.partial = True
            return
        for step in value:
            if not self.take():
                return
            if not isinstance(step, Mapping):
                self.partial = True
                continue
            uses = step.get("uses")
            if uses is not None and "run" in step:
                self.partial = True
                continue
            if uses is None:
                if not isinstance(step.get("run"), str):
                    self.partial = True
                continue
            if isinstance(uses, str) and len(uses) <= 512 and uses.startswith("anthropics/claude-code-action@"):
                ref = uses[len("anthropics/claude-code-action@") :]
                if _REF.fullmatch(ref):
                    self.found = True
                    self.facts.append(_fact("action-use", "confirmed", item.provenance, {"ref": ref}))
                    self.facts.extend(_auth(step.get("with", {}), item.provenance))
                    if "if" in step:
                        self.facts.append(_fact("execution-condition", "unknown", item.provenance))
                    continue
            self.reference(uses, item, depth, stack, reusable=False)

    def visit(self, item: Definition, depth: int, stack: frozenset[Scope]) -> None:
        scope = item.provenance.scope
        if depth > self.bounds.depth or scope in stack or not self.take():
            self.partial = True
            return
        if item.http_status not in {None, 200} or not isinstance(item.body, Mapping) or item.complete is not True:
            self.partial = True
            state = "access-denied" if item.http_status == 403 else "unknown"
            self.facts.append(
                _fact(
                    "definition-access",
                    state,
                    item.provenance,
                    item.http_status if item.http_status in {403, 404, 429} else None,
                )
            )
            return
        body = item.body
        stack = stack | {scope}
        if item.kind == "composite":
            runs = body.get("runs")
            if not isinstance(runs, Mapping) or runs.get("using") != "composite":
                self.partial = True
                return
            self.steps(runs.get("steps"), item, depth, stack)
            return
        triggers = body.get("on")
        events = list(triggers) if isinstance(triggers, (Mapping, list)) and len(triggers) <= 40 else [triggers]
        valid_events = all(isinstance(event, str) and event in _EVENTS for event in events)
        self.facts.append(
            _fact(
                "workflow-triggers",
                "confirmed" if valid_events else "unknown",
                item.provenance,
                events if valid_events else None,
            )
        )
        permissions = _permissions(body.get("permissions"))
        self.facts.append(
            _fact(
                "workflow-permissions",
                "confirmed" if permissions is not None else "unknown",
                item.provenance,
                permissions,
            )
        )
        jobs = body.get("jobs")
        if not isinstance(jobs, Mapping) or not jobs or len(jobs) > 32:
            self.partial = True
            return
        for job in jobs.values():
            if not self.take():
                return
            if not isinstance(job, Mapping):
                self.partial = True
                continue
            if "if" in job:
                self.facts.append(_fact("execution-condition", "unknown", item.provenance))
            if "permissions" in job:
                permission = _permissions(job["permissions"])
                self.facts.append(
                    _fact(
                        "job-permissions",
                        "confirmed" if permission is not None else "unknown",
                        item.provenance,
                        permission,
                    )
                )
            if "uses" in job:
                if "steps" in job:
                    self.partial = True
                    continue
                self.reference(job["uses"], item, depth, stack, reusable=True)
            else:
                self.steps(job.get("steps"), item, depth, stack)


def diagnose(
    inventory: Inventory,
    *,
    observations: Sequence[Observation] = (),
    runs: Sequence[ActionRunEvidence] = (),
    default_revision: str | None = None,
    bounds: Bounds | None = None,
) -> dict[str, Any]:
    """JSON-compatible facts, limited to supplied scope and independent evidence.

    Completeness is the caller's explicit inventory attestation. Never infer it
    from an empty result, status code, successful execution or metadata read.
    ``default_revision`` is separately supplied, never inferred from a branch.
    """
    provenance = inventory.provenance
    scope = provenance.scope
    discovery = _Discovery(inventory.definitions, bounds or Bounds())
    roots = [
        item
        for item in discovery.definitions.values()
        if item.kind == "workflow"
        and item.provenance.scope.repository == scope.repository
        and item.provenance.scope.revision == scope.revision
        and re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", item.provenance.scope.path)
    ]
    for item in roots:
        discovery.visit(item, 1, frozenset())
    # Out-of-scope rows cannot stand in for a complete inventory of this revision.
    if inventory.definitions and not roots:
        discovery.partial = True
    state = (
        "confirmed"
        if discovery.found
        else ("absent-in-complete-scope" if inventory.complete is True and not discovery.partial else "unknown")
    )
    facts = [_fact("workflow-discovery", state, provenance)] + discovery.facts
    supplied_default = _sha(default_revision)
    relation = (
        "unknown"
        if supplied_default is None
        else ("default-revision" if supplied_default == scope.revision else "non-default-revision")
    )
    facts.append(_fact("revision-relation", "unknown" if relation == "unknown" else "confirmed", provenance, relation))
    facts.append(_fact("default-configuration", state if relation == "default-revision" else "unknown", provenance))
    included: set[str] = set()
    for observation in observations[:32]:
        observed_scope = observation.provenance.scope
        if observed_scope.repository != scope.repository or observed_scope.revision != scope.revision:
            continue
        if observation.facet == "workflow-enabled" and not any(
            item.provenance.scope == observed_scope for item in roots
        ):
            continue
        facts.append(_fact(observation.facet, observation.state, observation.provenance, observation.value))
        included.add(observation.facet)
    for facet in _OBSERVATIONS:
        if facet not in included:
            facts.append(_fact(facet, "unknown", provenance))
    for facet in ("auth-declaration", "secret-availability", "auth-readiness"):
        if not any(fact["facet"] == facet for fact in facts):
            facts.append(_fact(facet, "unknown", provenance))
    executions = 0
    for run in runs[:20]:
        if (
            not isinstance(run, ActionRunEvidence)
            or run.workflow.repository != scope.repository
            or run.workflow.revision != scope.revision
        ):
            continue
        source = Provenance(
            "normalized-actions-run",
            Scope(scope.repository, scope.path, run.workflow.revision),
            f"https://github.com/{scope.repository}/actions/runs/{run.run_id}",
            run.observed_at,
        )
        facts.append(
            _fact(
                "execution",
                "confirmed",
                source,
                {
                    "workflow_id": run.workflow.workflow_id,
                    "run_id": run.run_id,
                    "run_attempt": run.run_attempt,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "head_relation": run.head_relation,
                },
            )
        )
        executions += 1
    if not executions:
        facts.append(_fact("execution", "unknown", provenance))
    facts.append(_fact("cloud-lifecycle", "unsupported", provenance))
    recommendations = []
    if state == "absent-in-complete-scope":
        recommendations.append("Supply a reviewed Claude Action workflow definition for this revision.")
    if any(fact["facet"] == "auth-declaration" and fact["state"] == "absent-in-complete-scope" for fact in facts):
        recommendations.append("Declare supported authentication in the workflow and verify availability separately.")
    return {
        "facts": facts,
        "partial": discovery.partial or len(observations) > 32 or len(runs) > 20,
        "recommendations": recommendations,
        "findings_state": "unknown",
        "merge_readiness": "unknown",
    }
