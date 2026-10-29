"""Synthetic skills reports for three fake machines. No real names, paths, or content."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

H = {name: (name[0] * 64) for name in "abcdef0123456789"}


def _copy(skill, harness, status="current", installed="a", registry="a", rollback=False, selector=True):
    return {
        "skill_id": skill,
        "harness": harness,
        "status": status,
        "installed_sha256": H[installed] if installed else None,
        "registry_sha256": H[registry] if registry else None,
        "rollback_available": rollback,
        "update_selector": f"registry:{skill}" if selector else None,
    }


def _score(
    skill,
    helped,
    hurt,
    neutral=0,
    wilson=0.5,
    recency=0.5,
    status="promoted",
    suggestion="hold",
    reason="no change",
    last=None,
):
    return {
        "artifact_id": skill,
        "helped": helped,
        "hurt": hurt,
        "neutral": neutral,
        "total": helped + hurt,
        "wilson_lower_bound": wilson,
        "recency_wilson": recency,
        "status": status,
        "suggestion": suggestion,
        "suggestion_reason": reason,
        "last_signal_at": last,
    }


def reports(now: datetime | None = None, *, hub_version: str = "0.31.0") -> dict[str, dict]:
    """Keys are the machine slot: linux1, linux2, windows1."""
    now = now or datetime.now(timezone.utc)
    stamp = now.replace(microsecond=0).isoformat()
    ago = lambda days: (now - timedelta(days=days)).replace(microsecond=0).isoformat()  # noqa: E731
    base = {"schema_version": 1, "registry_revision": None, "truncated": False, "observed_at": stamp}
    linux1 = {
        **base,
        "brigade_version": hub_version,
        "usage_tracked": True,
        "copies": [
            _copy("sample-review", "claude", rollback=True),
            _copy("sample-review", "codex"),
            _copy("sample-deploy", "claude", installed="b"),
            _copy("sample-deploy", "codex", installed="c"),
            _copy("sample-lint", "claude", status="stale", installed="d", registry="e", rollback=True),
            _copy("sample-format", "claude"),
            _copy("sample-notes", "claude"),
        ],
        "conflicts": [{"skill_id": "sample-deploy", "kind": "divergent-copies", "locations": ["claude", "codex"]}],
        "scores": [
            _score(
                "sample-review", 14, 2, 1, 0.64, 0.58, "promoted", "rollback", "verified regression measured", ago(2)
            ),
            _score("sample-lint", 5, 0, 0, 0.57, 0.5, "promoted", "bump", "sustained verified helped", ago(6)),
            _score("sample-format", 9, 0, 2, 0.70, 0.66, "promoted", "bump", "sustained verified helped", ago(1)),
            _score("sample-notes", 3, 0, 0, 0.44, 0.2, "candidate", "hold", "insufficient verified evidence", ago(75)),
        ],
    }
    linux2 = {
        **base,
        "brigade_version": "0.29.0",
        "usage_tracked": True,
        "copies": [
            _copy("sample-review", "claude"),
            _copy("sample-lint", "claude", status="stale", installed="d", registry="e"),
            _copy("sample-docs", "claude", status="missing", installed=None),
        ],
        "conflicts": [],
        "scores": [_score("sample-lint", 3, 0, 0, 0.44, 0.4, "promoted", "hold", "no change", ago(3))],
    }
    windows1 = {
        **base,
        "brigade_version": hub_version,
        "usage_tracked": False,
        "copies": [_copy("sample-format", "claude"), _copy("sample-search", "claude")],
        "conflicts": [],
        "scores": [],
    }
    return {"linux1": linux1, "linux2": linux2, "windows1": windows1}
