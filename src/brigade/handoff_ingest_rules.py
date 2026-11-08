"""Ingest acceptance rules shared by `brigade ingest`, `handoff lint` and `handoff draft`.

One place defines what an ingester accepts, so the writer (`brigade handoff
draft`) and the checker (`brigade handoff lint`) cannot drift from the reader.
Every function returns plain one-line problem strings and never raises on bad
handoff content.

Two layers:

* Static rules need only the handoff text: card name shape, card frontmatter
  (`title` or `topic`, `category`, `tags`), document target allow-list, and the
  `Expected card SHA256` section format for `update-card`.
* Workspace rules read the canonical workspace (`<workspace>/memory/cards`,
  bootstrap documents): the expected SHA must match the live card, `create-card`
  must not collide with an existing card, and an append must fit the bootstrap
  budget. They are skipped when the workspace is not given or not reachable.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from . import budgets

SAFE_CARD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
SAFE_RULE_PATH_RE = re.compile(r"^rules/[A-Za-z0-9._-]+\.md$")
SAFE_SPECIAL_TARGETS = frozenset(
    {
        "TOOLS.md",
        "USER.md",
        ".learnings/LEARNINGS.md",
        ".learnings/ERRORS.md",
        ".learnings/FEATURE_REQUESTS.md",
    }
)

EXPECTED_SHA_SECTION = "Expected card SHA256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_KEYS = ("created", "updated", "last_verified", "last_reviewed", "review_after", "expires", "fresh_until")
_FLOW_LIST_RE = re.compile(r"^\[(?P<items>.*)\]$")
CARDS_RELATIVE = Path("memory") / "cards"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_card_name(name: str) -> bool:
    return bool(SAFE_CARD_NAME_RE.fullmatch(name))


def safe_document_target(document: str) -> bool:
    return document in SAFE_SPECIAL_TARGETS or bool(SAFE_RULE_PATH_RE.fullmatch(document))


def cards_dir(workspace: Path | None) -> Path | None:
    """The workspace card directory, or None when the workspace has no readable one."""
    if workspace is None:
        return None
    path = workspace / CARDS_RELATIVE
    return path if path.is_dir() and not path.is_symlink() else None


def read_card_bytes(workspace: Path | None, name: str) -> bytes | None:
    """Current bytes of `<workspace>/memory/cards/<name>`, or None when absent or unreadable."""
    directory = cards_dir(workspace)
    if directory is None or not safe_card_name(name):
        return None
    path = directory / name
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _parse_frontmatter_block(lines: list[str]) -> tuple[dict[str, object], str | None]:
    fields: dict[str, object] = {}
    current_list: str | None = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            stripped = line.strip()
            if current_list is not None and stripped.startswith("- "):
                items = fields[current_list]
                assert isinstance(items, list)
                items.append(_strip_quotes(stripped[2:]))
            continue
        current_list = None
        if ":" not in line:
            return fields, "card frontmatter has a line that is not `key: value`"
        key, value = line.split(":", 1)
        key = _strip_quotes(key)
        if key in fields:
            return fields, f"card frontmatter repeats the key {key!r}"
        value = value.strip()
        flow = _FLOW_LIST_RE.fullmatch(value)
        if flow:
            fields[key] = [_strip_quotes(item) for item in flow.group("items").split(",") if item.strip()]
        elif value == "":
            fields[key] = []
            current_list = key
        else:
            fields[key] = _strip_quotes(value)
    return fields, None


def card_frontmatter_problems(content: str) -> list[str]:
    """Problems that make the ingester refuse a card: frontmatter, body, title, category, tags."""
    lines = content.replace("\r\n", "\n").lstrip().split("\n")
    if not lines or lines[0].strip() != "---":
        return ["card content missing YAML frontmatter (it must start with `---`)"]
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return ["card frontmatter has no closing `---`"]
    fields, parse_problem = _parse_frontmatter_block(lines[1:end])
    if parse_problem:
        return [parse_problem]
    problems: list[str] = []
    if not "\n".join(lines[end + 1 :]).strip():
        problems.append("card body is empty after the frontmatter")
    if not any(isinstance(fields.get(key), str) and str(fields[key]).strip() for key in ("title", "topic")):
        problems.append("card frontmatter needs a nonempty `title` or `topic`")
    category = fields.get("category")
    if not isinstance(category, str) or not category.strip():
        problems.append("card frontmatter needs a nonempty `category` (for example `category: foundation`)")
    tags = fields.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
        problems.append("card frontmatter needs `tags` as a list (for example `tags: [memory, handoff]`)")
    for key in _DATE_KEYS:
        value = fields.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
                raise ValueError(key)
            date.fromisoformat(value)
        except ValueError:
            problems.append(f"card frontmatter `{key}` must be an ISO date like 2026-11-08")
    return problems


def expected_sha_format_problem(value: str) -> str | None:
    if not value:
        return f"update-card requires an `## {EXPECTED_SHA_SECTION}` section holding the current card's SHA-256"
    if not SHA256_RE.fullmatch(value.strip().lower()):
        return f"`{EXPECTED_SHA_SECTION}` must be 64 lowercase hex characters"
    return None


def card_state_problems(
    *,
    action: str,
    target_card: str,
    expected_sha: str,
    workspace: Path | None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the expected SHA and the live card state.

    The SHA format rule always runs for update-card. The state rules run only when
    `<workspace>/memory/cards` exists. An unverifiable update-card is a warning, not
    an error, so linting outside a memory workspace stays usable.
    """
    errors: list[str] = []
    warnings: list[str] = []
    sha = expected_sha.strip().lower()
    if action == "update-card":
        sha_problem = expected_sha_format_problem(sha)
        if sha_problem:
            errors.append(sha_problem)
    if not safe_card_name(target_card):
        return errors, warnings
    reachable = cards_dir(workspace) is not None
    existing = read_card_bytes(workspace, target_card)
    if action == "update-card":
        if not reachable:
            warnings.append(
                "expected SHA not checked against the live card: no memory/cards directory under the "
                "workspace (pass --workspace <memory workspace> to check it)"
            )
        elif existing is None:
            errors.append(f"update-card target {target_card} does not exist in the workspace cards (use create-card)")
        elif SHA256_RE.fullmatch(sha) and not sha_matches(existing, sha):
            errors.append(
                f"update-card {EXPECTED_SHA_SECTION} does not match the current {target_card} "
                "(the card changed after the SHA was read, re-read it)"
            )
    elif reachable and existing is not None:
        errors.append(f"create-card target {target_card} already exists in the workspace cards (use update-card)")
    return errors, warnings


def sha_matches(existing: bytes, expected: str) -> bool:
    return sha256_hex(existing) == expected.strip().lower()


def document_handoff_problems(*, document: str, content: str, workspace: Path | None) -> list[str]:
    """Problems for a no-card handoff: allow-listed target and bootstrap budget."""
    if not safe_document_target(document):
        return [
            f"unsafe target document {document!r}: use one of "
            + ", ".join(sorted(SAFE_SPECIAL_TARGETS))
            + ", or rules/<name>.md"
        ]
    problem = bootstrap_budget_problem(document, content, workspace)
    return [problem] if problem else []


def bootstrap_budget_problem(document: str, content: str, workspace: Path | None) -> str | None:
    """Reason an append to a bootstrap document would exceed its budget, or None."""
    if workspace is None:
        return None
    dest = workspace / document
    if dest.is_symlink():
        return None
    would_exceed, budget = budgets.route_would_exceed_budget(dest, "\n\n" + content.strip() + "\n")
    if not would_exceed:
        return None
    return (
        f"bootstrap budget guard: appending to {document} would exceed "
        f"its {budget}B budget; trim the file or promote to a memory card"
    )
