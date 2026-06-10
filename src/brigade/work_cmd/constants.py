"""Shared constants for the work command family."""

from __future__ import annotations

import re


OK = "ok"


WARN = "warn"


FAIL = "fail"


IMPORT_KINDS = ("task", "finding", "decision", "preference", "incident", "link", "command", "context")


CONTEXT_KINDS = ("link", "transcript", "error", "issue", "note")


TASK_TYPES = ("task", "feature", "bug", "docs", "security", "workflow", "research", "chore")


TASK_PRIORITIES = ("low", "normal", "high", "urgent")


TASK_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "vertical-slice": {
        "acceptance": (
            "One user-visible path is implemented end to end.",
            "Focused tests cover the new path.",
            "Documentation or help text is updated when user behavior changes.",
        ),
        "guidance": (
            "Define the smallest end-to-end path before editing.",
            "Add or update a focused test around that path.",
            "Implement only the supporting code needed for the slice.",
        ),
    },
    "bugfix": {
        "acceptance": (
            "The bug is reproduced by a focused failing test or equivalent fixture.",
            "The fix addresses the root cause.",
            "The regression test passes with the fix.",
        ),
        "guidance": (
            "Reproduce the failing behavior first.",
            "Patch the narrow root cause.",
            "Keep the regression test close to the bug.",
        ),
    },
    "red-green-refactor": {
        "acceptance": (
            "A failing test describes the desired behavior.",
            "The test passes after the implementation.",
            "The final code is refactored without changing the tested behavior.",
        ),
        "guidance": (
            "Write the smallest meaningful failing test.",
            "Make it pass with the simplest implementation.",
            "Refactor only after the test is green.",
        ),
    },
    "docs": {
        "acceptance": (
            "The documented command or workflow matches current behavior.",
            "Examples are concise and runnable or clearly illustrative.",
            "Related index, changelog, or roadmap entries are updated when appropriate.",
        ),
        "guidance": (
            "Verify the current behavior before writing docs.",
            "Prefer concise examples over broad explanation.",
            "Check formatting and public-safe wording.",
        ),
    },
    "security-follow-up": {
        "acceptance": (
            "The finding or risk is clearly described without exposing sensitive material.",
            "The mitigation is implemented or a bounded follow-up is documented.",
            "Verification evidence is captured with secrets redacted.",
        ),
        "guidance": (
            "Preserve redaction and avoid copying sensitive evidence.",
            "Prefer the narrowest mitigation that removes the risk.",
            "Document any remaining manual validation or follow-up.",
        ),
    },
}


WORKFLOW_RULE_TEMPLATES = (
    "rules/issue-tdd-loop.md",
    "rules/acceptance-driven-work.md",
)


ACTIVE_SESSION_STALE_HOURS = 24


IMPORT_STALE_HOURS = 72


DISMISSED_SOURCE_WARN_THRESHOLD = 5


PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


BACKUP_CONFIG_REL_PATH = ".brigade/backups.toml"


BACKUP_UNSAFE_FIELDS = {
    "backup_password",
    "channel_id",
    "host",
    "hostname",
    "mount",
    "mount_path",
    "password",
    "remote",
    "remote_name",
    "repo",
    "repo_path",
    "repository",
    "repository_url",
    "secret",
    "token",
    "url",
    "webhook",
    "webhook_url",
}


BACKUP_DEFAULTS = (
    {
        "id": "nas",
        "kind": "nas",
        "command_label": "local backup summary producer",
        "summary_path": ".brigade/backups/nas-summary.json",
        "snapshot_stale_hours": 36,
        "check_stale_hours": 168,
        "prune_stale_hours": 168,
        "restore_rehearsal_stale_days": 90,
        "enabled": True,
    },
    {
        "id": "cloud",
        "kind": "cloud",
        "command_label": "cloud backup summary producer",
        "summary_path": ".brigade/backups/cloud-summary.json",
        # Off-site cloud copies usually run on a slower cadence than the local
        # NAS (a weekly off-site backup is common). Default thresholds are
        # widened so a once-a-week cloud repo does not report stale every day.
        # Tighten these per destination if your cloud backup runs more often.
        "snapshot_stale_hours": 192,
        "check_stale_hours": 336,
        "prune_stale_hours": 336,
        "restore_rehearsal_stale_days": 90,
        "enabled": True,
    },
)


BACKUP_SUMMARY_REQUIRED_FIELDS = (
    "destination_label",
    "latest_snapshot_at",
    "latest_check_at",
    "latest_check_result",
    "latest_prune_at",
    "latest_prune_result",
    "latest_restore_rehearsal_at",
    "latest_restore_rehearsal_result",
    "summary",
    "evidence_path",
)


BACKUP_SUMMARY_ACCEPTED_SUCCESS_RESULTS = ("ok", "success", "passed", "pass")


SCANNER_CONFIG_REL_PATH = ".brigade/scanners.toml"


SCANNER_OUTPUT_STALE_HOURS = 48


SCANNER_RUN_STALE_HOURS = 48


SCANNER_SWEEP_STALE_HOURS = 36


SCANNER_SWEEP_REVIEW_STALE_HOURS = 24


IMPORT_ARCHIVE_STALE_HOURS = 168


SCANNER_REQUIRED_IDS = ("chat-memory-sweep", "memory-refresh", "handoff-ingest")


SCANNER_HIGH_RISK_COMMANDS = {"bash", "sh", "zsh", "fish", "powershell", "pwsh", "ssh", "scp", "rsync"}


SCANNER_SHELL_META_RE = re.compile(r"[;&|`<>]|\$\(")


SCANNER_DEFAULTS = (
    {
        "id": "chat-memory-sweep",
        "source": "chat-memory-sweep",
        "command": "brigade work import chat-sweep --json",
        "cadence": "daily@02:15",
        "enabled": True,
        "timeout": 300,
        "output_path": ".brigade/chat-memory-sweeps/latest.json",
        "conflict_window": "02:00-02:30",
    },
    {
        "id": "chat-surfaces",
        "source": "chat-memory-sweep",
        "command": "brigade chat sweep import-issues discord-export --json",
        "cadence": "daily@02:20",
        "enabled": False,
        "timeout": 300,
        "output_path": ".brigade/chat-memory-sweeps/discord-export-latest.json",
        "conflict_window": "02:15-02:35",
    },
    {
        "id": "memory-refresh",
        "source": "memory-refresh",
        "command": "brigade work import memory-refresh --json",
        "cadence": "daily@02:45",
        "enabled": True,
        "timeout": 300,
        "output_path": "memory/cards/decay/refresh-queue.json",
        "conflict_window": "02:30-03:00",
    },
    {
        "id": "memory-care",
        "source": "memory-care",
        "command": "brigade memory care import-issues --json",
        "cadence": "daily@03:00",
        "enabled": False,
        "timeout": 180,
        "output_path": "memory/cards/decay/refresh-queue.json",
        "conflict_window": "02:55-03:15",
    },
    {
        "id": "handoff-ingest",
        "source": "handoff-ingest",
        "command": "brigade handoff sync-issues --json",
        "cadence": "hourly@15",
        "enabled": True,
        "timeout": 180,
        "output_path": ".brigade/handoff-sources.json",
        "conflict_window": "00:10-00:25",
    },
    {
        "id": "backup-health",
        "source": "backup-health",
        "command": "brigade work backup import-issues --json",
        "cadence": "daily@04:00",
        "enabled": False,
        "timeout": 180,
        "output_path": ".brigade/backups",
        "conflict_window": "04:00-04:20",
    },
    {
        "id": "security-scan",
        "source": "security-scan",
        "command": "brigade security scan --import-findings",
        "cadence": "daily@03:30",
        "enabled": False,
        "timeout": 600,
        "output_path": ".brigade/security/latest/security-report.json",
        "conflict_window": "03:20-03:50",
    },
    {
        "id": "tool-catalog",
        "source": "tool-catalog",
        "command": "brigade tools import-issues --json",
        "cadence": "daily@04:30",
        "enabled": False,
        "timeout": 180,
        "output_path": ".brigade/tools.toml",
        "conflict_window": "04:20-04:40",
    },
)


PROVENANCE_AUDIT_SOURCES = {
    "backup-health",
    "chat-memory-sweep",
    "code-review",
    "context-pack",
    "handoff-ingest",
    "learning-loop",
    "memory-care",
    "memory-refresh",
    "project-consolidation",
    "repo-fleet",
    "repo-fleet-release",
    "roadmap-audit",
    "scanner-health",
    "security-scan",
    "tool-catalog",
}


PROVENANCE_SAFE_SUMMARY_KEYS = {
    "detail",
    "evidence_summary",
    "rationale",
    "safe_description",
    "safe_detail",
    "safe_summary",
    "summary",
}


PROVENANCE_EVIDENCE_KEYS = {
    "evidence_path",
    "evidence_references",
    "local_evidence_path",
    "log_path",
    "queue_path",
    "receipt_path",
    "report_path",
    "review_run_id",
    "scanner_receipt_path",
    "scanner_run_id",
    "source_path",
}


REVIEW_CONFIG_REL_PATH = ".brigade/reviews.toml"


REVIEW_RUN_STALE_HOURS = 72


REVIEW_REQUIRED_FIELDS = ("id", "name", "command", "output_path", "findings_path", "privacy_mode")


REVIEW_PRIVACY_MODES = ("safe-summary", "local-only")


REVIEW_DEFAULTS = (
    {
        "id": "codex-review",
        "name": "Codex local code review",
        "command": "brigade dogfood --json",
        "cwd": ".",
        "enabled": False,
        "timeout": 600,
        "target_paths": ["."],
        "base_ref": "HEAD",
        "output_path": ".brigade/reviews/codex-review-output.json",
        "findings_path": ".brigade/reviews/codex-review-findings.json",
        "supported_modes": ["diff", "workspace"],
        "privacy_mode": "safe-summary",
    },
    {
        "id": "claude-opus-review",
        "name": "Claude Opus subagent code review",
        "command": "claude /review",
        "cwd": ".",
        "enabled": False,
        "timeout": 900,
        "target_paths": ["."],
        "base_ref": "HEAD",
        "output_path": ".brigade/reviews/claude-opus-review-output.json",
        "findings_path": ".brigade/reviews/claude-opus-review-findings.json",
        "supported_modes": ["diff", "workspace", "subagents"],
        "privacy_mode": "safe-summary",
    },
    {
        "id": "custom",
        "name": "Custom local code review",
        "command": "brigade dogfood --json",
        "cwd": ".",
        "enabled": False,
        "timeout": 600,
        "target_paths": ["."],
        "base_ref": "HEAD",
        "output_path": ".brigade/reviews/custom-output.json",
        "findings_path": ".brigade/reviews/custom-findings.json",
        "supported_modes": ["diff", "workspace"],
        "privacy_mode": "safe-summary",
    },
)


REVIEW_SEVERITIES = ("low", "medium", "high", "critical")


REVIEW_CATEGORIES = ("bug", "test", "docs", "security", "design", "maintainability", "performance", "workflow")


REVIEW_UNSAFE_FIELD_NAMES = {
    "body",
    "channel_id",
    "host",
    "hostname",
    "message",
    "password",
    "private_text",
    "raw",
    "raw_output",
    "secret",
    "stderr",
    "stdout",
    "token",
    "transcript",
    "url",
    "user_id",
    "webhook",
}


REVIEW_UNSAFE_VALUE_RE = re.compile(
    r"(?:https?://[^\s]+|/home/[^\s]+|/Users/[^\s]+|[A-Za-z]:\\[^\s]+|xox[baprs]-[A-Za-z0-9-]+|[A-Za-z0-9_]*(?:token|secret|password|api_key)[A-Za-z0-9_]*\s*[:=]\s*[A-Za-z0-9_./+=:-]{8,})",
    re.IGNORECASE,
)


CONFIDENCE_RANK = {"high": 0, "medium": 1, "normal": 1, "low": 2}


RAW_CHAT_FIELDS = {
    "body",
    "bodies",
    "message",
    "message_body",
    "message_bodies",
    "message_text",
    "messages",
    "private_text",
    "quote",
    "quotes",
    "raw",
    "raw_message",
    "raw_messages",
    "raw_text",
    "text",
    "transcript",
    "transcripts",
}


HANDOFF_READY_KINDS = ("finding", "decision", "preference", "incident", "link", "command")


HANDOFF_UNSAFE_FIELD_NAMES = {
    "channel_id",
    "dm_id",
    "host",
    "hostname",
    "message_id",
    "password",
    "private_url",
    "remote",
    "secret",
    "token",
    "url",
    "user_id",
    "webhook",
    "webhook_url",
}


HANDOFF_UNSAFE_VALUE_RE = re.compile(
    r"(?:https?://[^\s]+|/home/[^\s]+|/Users/[^\s]+|[A-Za-z]:\\[^\s]+|xox[baprs]-[A-Za-z0-9-]+|[A-Za-z0-9_]*(?:token|secret|password|api_key)[A-Za-z0-9_]*\s*[:=]\s*[A-Za-z0-9_./+=:-]{8,})",
    re.IGNORECASE,
)


HANDOFF_TARGETS = {
    "preference": "USER.md",
    "command": "TOOLS.md",
    "incident": ".learnings/ERRORS.md",
    "link": ".learnings/LEARNINGS.md",
}


ISSUE_ACCEPTANCE_HEADINGS = {
    "acceptance",
    "acceptance criteria",
    "definition of done",
    "done when",
}


ISSUE_TEST_HEADINGS = {
    "test",
    "tests",
    "testing",
    "test plan",
    "verification",
}


_PROPOSAL_KINDS = ("template", "rule", "skill")
