"""Sample PR rules for tests and the disposable preview. Sample data only, never a real policy."""

from __future__ import annotations

import copy
from typing import Any

SAMPLE_RULES: list[dict[str, Any]] = [
    {
        "id": "docs-only-skip-review",
        "title": "Doc-only PRs skip CodeRabbit and CI",
        "when": {"condition": "docs_only"},
        "then": ["skip_label:coderabbit-review", "skip_ci"],
        "why": "Documentation changes do not need a review quota or a CI run.",
        "enabled": True,
    },
    {
        "id": "coderabbit-dry-use-greptile",
        "title": "When CodeRabbit OSS is out of reviews, use Greptile sparingly",
        "when": {"condition": "review_quota_exhausted", "reviewer": "coderabbit"},
        "then": ["use_reviewer:greptile", "limit_reviewer:greptile:3_per_day", "no_paid_credits:greptile"],
        "scope": {"repos": ["sample-org/sample-app", "sample-org/sample-lib"]},
        "why": "Greptile is the fallback, so keep its free allowance for PRs that need it.",
        "enabled": True,
    },
    {
        "id": "rate-limited-is-not-approval",
        "title": "A rate-limited or skipped review is not an approval",
        "when": {"condition": "always"},
        "then": ["note:Check the reviewed commit before relying on an approval."],
        "enabled": True,
    },
]


def sample_rules() -> list[dict[str, Any]]:
    return copy.deepcopy(SAMPLE_RULES)


def sample_document(base: dict[str, Any] | None = None) -> dict[str, Any]:
    from brigade import fleet_policy

    document = copy.deepcopy(base) if base is not None else fleet_policy.empty_document()
    document["pr_rules"] = sample_rules()
    return document
